"""
Telegram бот v2.0 — с кнопками и диалогом
"""
import asyncio, os, re, json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from dotenv import load_dotenv
from analyzer import analyze_lot, detect_type
from database import (get_portfolio, get_global_stats,
                      add_to_portfolio, init_db, get_price_trend)

load_dotenv()

TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_KEY   = os.getenv("GROQ_API_KEY")
GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
MODEL      = "llama-3.1-8b-instant"

# Хранилище текущих анализов (в памяти)
lot_cache = {}


async def ask_expert(question: str) -> str:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [{
                        "role": "user",
                        "content": f"""Ты эксперт-консультант по банкротным торгам России с 10-летним опытом.
Отвечай кратко, конкретно, с эмодзи. Как профессионал инвестору.

Вопрос: {question}"""
                    }],
                    "max_tokens": 500,
                    "temperature": 0.3,
                }
            )
            data = resp.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠️ Ошибка: {e}"
    return "Не удалось получить ответ"


async def analyze_lot_url(url: str) -> tuple:
    """Возвращает (lot, analysis)"""
    from playwright.async_api import async_playwright
    from analyzer import get_lot_details
    import pdfplumber, io

    lot_id_m = re.search(r'id=(\d+)', url)
    lot_id = lot_id_m.group(1) if lot_id_m else "unknown"

    lot = {
        "id": lot_id, "title": "", "url": url,
        "region": "moskva", "pdf_text": "",
        "description": "", "price": 0,
        "step_current": 0, "step_total": 0,
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()
        details = await get_lot_details(url, page)
        lot.update({
            "price":        details.get("price", 0),
            "description":  details.get("description", ""),
            "step_current": details.get("step_current", 0),
            "step_total":   details.get("step_total", 0),
        })
        if details.get("title_full"):
            lot["title"] = details["title_full"]
        lot["category"] = detect_type(
            f"{lot['title']} {lot['description'][:300]}"
        )
        try:
            resp = await ctx.request.get(
                f"https://files.tbankrot.ru/egrn_files/{lot_id}.pdf"
            )
            if resp.status == 200:
                with pdfplumber.open(io.BytesIO(await resp.body())) as pdf:
                    lot["pdf_text"] = "\n".join(
                        p.extract_text() or "" for p in pdf.pages[:4]
                    )[:3000]
        except:
            pass
        await browser.close()

    analysis = await analyze_lot(lot)
    return lot, analysis


def build_lot_message(lot: dict, an: dict, trend: dict = None) -> str:
    disc  = f" | -{an.get('discount_pct','?')}%" if an.get('discount_pct','?') not in ('?','0') else ""
    step  = f"\n📊 {an['step']}" if an.get('step') else ""
    check = f"\n🔎 _{an.get('what_to_check','')}_" if an.get('what_to_check') else ""

    trend_str = ""
    if trend and trend.get("drop_pct", 0) > 0:
        trend_str = f"\n📉 История: -{trend['drop_pct']}% за {trend['days_tracked']} дн."

    invest_icons = {"высокий":"🔥","средний":"📈","низкий":"📉"}
    risk_icons   = {"низкий":"🟢","средний":"🟡","высокий":"🟠","критический":"🔴"}

    return (
        f"🏷 *Анализ лота #{lot.get('id','')}*\n"
        f"{lot.get('title','')[:70]}\n\n"
        f"*Балл: {an.get('total_score','?')}/10*"
        f" | {invest_icons.get(an.get('invest_potential','средний'),'📈')} {an.get('invest_potential','?')}"
        f" | {risk_icons.get(an.get('risk_level','средний'),'🟡')} риск: {an.get('risk_level','?')}\n\n"
        f"💰 {an.get('price','—')} → рынок {an.get('market_price','—')}{disc}"
        f"{step}{trend_str}\n"
        f"💧 {an.get('liquidity_text','—')}\n"
        f"📈 {an.get('roi_text','—')}\n"
        f"⚖️ {an.get('legal_text','—')}\n\n"
        f"{an.get('action_emoji','⚠️')} *{an.get('action','?')}*\n"
        f"💡 _{an.get('strategy','')}_"
        f"{check}"
    )


def build_lot_keyboard(lot_id: str, url: str) -> InlineKeyboardMarkup:
    """Кнопки под каждым лотом"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Войти в лот",        callback_data=f"enter_{lot_id}"),
            InlineKeyboardButton("⏳ Наблюдать",           callback_data=f"watch_{lot_id}"),
        ],
        [
            InlineKeyboardButton("📋 Инструкция по подаче", callback_data=f"how_{lot_id}"),
            InlineKeyboardButton("❌ Пропустить",           callback_data=f"skip_{lot_id}"),
        ],
        [
            InlineKeyboardButton("🔗 Открыть на сайте",    url=url),
        ]
    ])


# ─── Обработчик кнопок ──────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    action, lot_id = data.rsplit("_", 1)
    cached = lot_cache.get(lot_id, {})
    lot = cached.get("lot", {})
    an  = cached.get("analysis", {})

    if action == "enter":
        # Подтверждение входа
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Да, подаю заявку", callback_data=f"confirm_{lot_id}"),
                InlineKeyboardButton("↩️ Назад",            callback_data=f"back_{lot_id}"),
            ]
        ])
        price = an.get("price", "уточните на сайте")
        step  = an.get("step", "")
        await query.edit_message_text(
            f"⚠️ *Подтвердите решение*\n\n"
            f"Лот: {lot.get('title','')[:60]}\n"
            f"Цена: {price}"
            f"{f' | {step}' if step else ''}\n\n"
            f"Вы уверены что хотите войти в этот лот?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    elif action == "confirm":
        # Инструкция по подаче заявки
        url = lot.get("url", "")
        await query.edit_message_text(
            f"📋 *Инструкция по подаче заявки*\n\n"
            f"*Лот:* {lot.get('title','')[:60]}\n\n"
            f"*Шаги:*\n"
            f"1️⃣ Перейдите на сайт торгов по ссылке ниже\n"
            f"2️⃣ Нажмите *«Подать заявку»*\n"
            f"3️⃣ Приложите документы: паспорт + ИНН\n"
            f"4️⃣ Внесите задаток на указанный счёт\n"
            f"5️⃣ Дождитесь подтверждения заявки\n\n"
            f"⚖️ *Юридика:* {an.get('legal_text','')}\n\n"
            f"🔗 {url}\n\n"
            f"_Лот добавлен в ваш портфель_ ✅",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        # Добавляем в портфель
        if lot and an:
            add_to_portfolio(lot, an, notes="Решил войти через бот")

    elif action == "watch":
        if lot and an:
            add_to_portfolio(lot, an, notes="Наблюдение")
        await query.edit_message_text(
            f"👁 *Добавлено в наблюдение*\n\n"
            f"{lot.get('title','')[:60]}\n\n"
            f"Агент будет следить за этим лотом и уведомит если цена снизится или торги заканчиваются.",
            parse_mode="Markdown"
        )

    elif action == "how":
        await query.edit_message_text(
            f"📖 *Как участвовать в банкротных торгах*\n\n"
            f"*1. Регистрация на ЭТП*\n"
            f"Получите ЭЦП (электронную подпись) — от 3000 руб в УЦ\n\n"
            f"*2. Аккредитация*\n"
            f"Зарегистрируйтесь на площадке (Т-Банкрот, Сбербанк-АСТ и др.)\n\n"
            f"*3. Задаток*\n"
            f"Обычно 5-20% от начальной цены. Возвращается если не выиграли.\n\n"
            f"*4. Заявка*\n"
            f"Подайте заявку до окончания срока приёма\n\n"
            f"*5. Торги*\n"
            f"Аукцион (цена растёт) или Публичное предложение (цена падает)\n\n"
            f"*6. Оплата*\n"
            f"После победы — 30 дней на полную оплату\n\n"
            f"💡 _Начните с небольших лотов для опыта_",
            parse_mode="Markdown"
        )

    elif action == "skip":
        await query.edit_message_text(
            f"❌ Лот пропущен\n\n_{lot.get('title','')[:60]}_",
            parse_mode="Markdown"
        )

    elif action == "back":
        # Возврат к анализу
        msg = build_lot_message(lot, an)
        keyboard = build_lot_keyboard(lot_id, lot.get("url",""))
        await query.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=True
        )


# ─── Команды ────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика",  callback_data="cmd_stats")],
        [InlineKeyboardButton("📋 Мой портфель", callback_data="cmd_portfolio")],
        [InlineKeyboardButton("❓ Как работают торги", callback_data="how_help")],
    ])
    await update.message.reply_text(
        "👋 *Привет! Я ваш эксперт по банкротным торгам.*\n\n"
        "Что умею:\n"
        "🔗 Скинь ссылку на лот → полный анализ + кнопки\n"
        "❓ Задай вопрос → отвечу как эксперт\n"
        "📊 /stats — статистика агента\n"
        "📋 /portfolio — ваш портфель\n\n"
        "_Попробуй скинуть ссылку с tbankrot.ru_ 👇",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stats = get_global_stats()
    cats  = "\n".join(f"  {cat}: {cnt}" for cat, cnt in stats.get("top_cats",[])[:5])
    await update.message.reply_text(
        f"📊 *Статистика агента*\n\n"
        f"🔍 Изучено лотов: *{stats['total_lots']}*\n"
        f"🔄 Запусков: *{stats['total_runs']}*\n"
        f"🟢 «Входить» за 7 дней: *{stats['recent_go']}*\n\n"
        f"*Топ категорий:*\n{cats}",
        parse_mode="Markdown"
    )


async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = get_portfolio()
    if not items:
        await update.message.reply_text(
            "📋 *Портфель пуст*\n\n"
            "Нажмите «Наблюдать» под любым лотом — он появится здесь.",
            parse_mode="Markdown"
        )
        return
    text = "📋 *Ваш портфель:*\n\n"
    status_icons = {"watching":"👀","interested":"🔥","applied":"📝","won":"🏆","lost":"❌"}
    for item in items[:8]:
        icon = status_icons.get(item["status"], "📌")
        text += (
            f"{icon} {item['title'][:50]}\n"
            f"💰 {item['buy_price']/1e6:.1f} млн → рынок {item['market_price']/1e6:.1f} млн\n"
            f"🔗 {item['url']}\n\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown",
                                    disable_web_page_preview=True)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    # Ссылка на лот
    if "tbankrot.ru/item" in text or re.search(r'id=\d+', text):
        url_m = re.search(r'https?://\S+', text)
        if url_m:
            url = url_m.group()
        else:
            id_m = re.search(r'id=(\d+)', text)
            url = f"https://tbankrot.ru/item?id={id_m.group(1)}" if id_m else text

        msg = await update.message.reply_text("⏳ Анализирую лот... (~1 минута)")
        try:
            lot, analysis = await analyze_lot_url(url)
            lot_id = lot.get("id", "0")

            # Сохраняем в кэш для кнопок
            lot_cache[lot_id] = {"lot": lot, "analysis": analysis}

            # Получаем историю цены
            from database import get_price_trend, save_lot
            save_lot(lot, analysis)
            trend = get_price_trend(lot_id)

            result = build_lot_message(lot, analysis, trend)
            keyboard = build_lot_keyboard(lot_id, url)

            await msg.delete()
            await update.message.reply_text(
                result, parse_mode="Markdown",
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception as e:
            await msg.edit_text(f"⚠️ Ошибка анализа: {e}")
        return

    # Вопрос эксперту
    if len(text) > 3:
        msg = await update.message.reply_text("💭 Думаю...")
        answer = await ask_expert(text)
        await msg.edit_text(answer)


async def handle_inline_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок из /start"""
    query = update.callback_query
    await query.answer()
    if query.data == "cmd_stats":
        stats = get_global_stats()
        cats  = "\n".join(f"  {cat}: {cnt}" for cat, cnt in stats.get("top_cats",[])[:5])
        await query.edit_message_text(
            f"📊 *Статистика*\n\nИзучено: {stats['total_lots']} | Запусков: {stats['total_runs']}\n\n{cats}",
            parse_mode="Markdown"
        )
    elif query.data == "cmd_portfolio":
        await cmd_portfolio(update, ctx)
    elif query.data == "how_help":
        await query.edit_message_text(
            "📖 *Как участвовать в торгах*\n\n"
            "1. Получите ЭЦП (~3000 руб)\n"
            "2. Зарегистрируйтесь на ЭТП\n"
            "3. Внесите задаток (5-20% от цены)\n"
            "4. Подайте заявку до дедлайна\n"
            "5. При победе — оплата в течение 30 дней\n\n"
            "_Скиньте ссылку на лот для анализа_ 👇",
            parse_mode="Markdown"
        )


def run_bot():
    init_db()
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CallbackQueryHandler(handle_inline_cmd,
                    pattern="^cmd_|^how_help$"))
    app.add_handler(CallbackQueryHandler(handle_callback,
                    pattern="^(enter|confirm|watch|how|skip|back)_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Бот v2.0 запущен! Отправьте /start в Telegram.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()

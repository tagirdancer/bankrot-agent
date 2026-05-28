"""
Telegram бот v4.0 — удобное меню + запрос в любое время
"""
import asyncio, os, re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from dotenv import load_dotenv
from analyzer import analyze_lot, detect_type, get_lot_details
from database import (get_portfolio, get_global_stats,
                      add_to_portfolio, init_db, get_price_trend, save_lot)

load_dotenv()

TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_KEY   = os.getenv("GROQ_API_KEY")
GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
MODEL      = "llama-3.1-8b-instant"

lot_cache = {}

# Настройки пользователя
settings = {
    "budget_max":  0,
    "min_score":   7.0,
    "regions":     "all",  # all / main / extra
}


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 ЗАПУСТИТЬ АНАЛИЗ СЕЙЧАС",
                                 callback_data="menu_run"),
        ],
        [
            InlineKeyboardButton("🏠 Квартиры",    callback_data="run_квартира"),
            InlineKeyboardButton("🏡 Дома",         callback_data="run_дом"),
        ],
        [
            InlineKeyboardButton("🏢 Коммерция",   callback_data="run_коммерция"),
            InlineKeyboardButton("🌱 Земля",         callback_data="run_земля"),
        ],
        [
            InlineKeyboardButton("🚗 Авто",          callback_data="run_авто"),
            InlineKeyboardButton("🅿️ Гаражи",       callback_data="run_гараж"),
        ],
        [
            InlineKeyboardButton("💼 Бизнес",        callback_data="run_бизнес"),
            InlineKeyboardButton("⚡ Горячие 9+",    callback_data="run_hot"),
        ],
        [
            InlineKeyboardButton("📋 Мой портфель",  callback_data="portfolio"),
            InlineKeyboardButton("📊 Статистика",    callback_data="stats"),
        ],
        [
            InlineKeyboardButton("⚙️ Настройки",    callback_data="settings"),
            InlineKeyboardButton("❓ Помощь",         callback_data="help"),
        ],
    ])


def run_submenu(category: str) -> InlineKeyboardMarkup:
    """Подменю после выбора категории"""
    cat_names = {
        "квартира":"🏠 Квартиры","дом":"🏡 Дома","коммерция":"🏢 Коммерция",
        "земля":"🌱 Земля","авто":"🚗 Авто","гараж":"🅿️ Гаражи","бизнес":"💼 Бизнес",
    }
    label = cat_names.get(category, category)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🔍 {label} — Москва и МО",
                                 callback_data=f"exec_{category}_main"),
        ],
        [
            InlineKeyboardButton(f"🌍 {label} — Все регионы (9+)",
                                 callback_data=f"exec_{category}_all"),
        ],
        [
            InlineKeyboardButton("↩️ Назад", callback_data="back_menu"),
        ],
    ])


def lot_keyboard(lot_id: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Войти в лот",
                                 callback_data=f"enter_{lot_id}"),
            InlineKeyboardButton("👁 Наблюдать",
                                 callback_data=f"watch_{lot_id}"),
        ],
        [
            InlineKeyboardButton("📋 Как подать заявку",
                                 callback_data=f"how_{lot_id}"),
            InlineKeyboardButton("❌ Пропустить",
                                 callback_data=f"skip_{lot_id}"),
        ],
        [InlineKeyboardButton("🔗 Открыть на сайте", url=url)],
    ])


async def ask_expert(q: str) -> str:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [{"role":"user","content":
                        f"Ты эксперт по банкротным торгам России. "
                        f"Отвечай кратко, конкретно, с эмодзи.\n\n{q}"}],
                    "max_tokens": 500,
                    "temperature": 0.3,
                }
            )
            data = resp.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠️ {e}"
    return "Нет ответа"


def build_lot_msg(lot: dict, an: dict, trend: dict = None) -> str:
    disc  = f" | -{an.get('discount_pct','?')}%" if an.get('discount_pct','?') not in ('?','0') else ""
    step  = f" | {an['step']}" if an.get('step') else ""
    comp  = f"\n👥 {an['competition']}" if an.get('competition') else ""
    extra = f"\n{an['extra_checks']}" if an.get('extra_checks') else ""
    check = f"\n🔎 _{an.get('what_to_check','')}_" if an.get('what_to_check') else ""
    trend_str = ""
    if trend and trend.get("drop_pct",0) > 0:
        trend_str = f"\n📉 -{trend['drop_pct']}% за {trend['days_tracked']} дн."
    invest = {"высокий":"🔥","средний":"📈","низкий":"📉"}
    risk   = {"низкий":"🟢","средний":"🟡","высокий":"🟠","критический":"🔴"}
    region_note = f" 🌍 {lot.get('region','')}" if lot.get("is_extra_region") else ""
    return (
        f"*Балл: {an.get('total_score','?')}/10*"
        f" {invest.get(an.get('invest_potential','средний'),'📈')}"
        f" {risk.get(an.get('risk_level','средний'),'🟡')}"
        f"{region_note}\n"
        f"{lot.get('title','')[:65]}\n"
        f"💰 {an.get('price','—')} → {an.get('market_price','—')}{disc}{step}"
        f"{trend_str}{comp}\n"
        f"💧 {an.get('liquidity_text','—')}\n"
        f"📈 {an.get('roi_text','—')}\n"
        f"⚖️ {an.get('legal_text','—')}"
        f"{extra}{check}\n"
        f"{an.get('action_emoji','⚠️')} *{an.get('action','?')}*\n"
        f"💡 _{an.get('strategy','')}_\n"
    )


async def do_run(chat_id: str, bot, category: str = "все",
                 extra_regions: bool = False, hot_only: bool = False):
    """Реальный запуск анализа"""
    import pdfplumber, io
    from playwright.async_api import async_playwright
    from platforms import get_all_platform_lots
    from agent import login_tbankrot, collect_lots, enrich, REGIONS_MAIN, REGIONS_EXTRA, ALERT_SCORE, CATEGORIES

    cat_icons = {
        "квартира":"🏠","дом":"🏡","коммерция":"🏢","земля":"🌱",
        "авто":"🚗","гараж":"🅿️","бизнес":"💼","все":"📦","горячие":"⚡"
    }
    icon = cat_icons.get(category,"📦")

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"{icon} *Запускаю анализ: {category}*\n\n"
            f"{'🌍 Все регионы' if extra_regions else '📍 Москва и МО'} | "
            f"{'⚡ Только 9+' if hot_only else f'Балл {settings[\"min_score\"]}+'}\n\n"
            f"_Займёт 15-25 минут. Горячие лоты пришлю сразу._"
        ),
        parse_mode="Markdown"
    )

    results = {}
    cats_to_analyze = (
        {category} if category not in ("все","горячие")
        else set(CATEGORIES.keys())
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx     = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()

        await login_tbankrot(page)

        regions = list(REGIONS_MAIN)
        if extra_regions:
            regions += REGIONS_EXTRA

        lots = await collect_lots(page, regions, max_pages=8)
        platform_lots = await get_all_platform_lots()
        all_lots = lots + platform_lots

        await bot.send_message(
            chat_id=chat_id,
            text=f"✅ Найдено *{len(all_lots)}* лотов — анализирую...",
            parse_mode="Markdown"
        )

        for lot in all_lots:
            try:
                await enrich(lot, page, ctx)
                cat   = lot.get("category","прочее")
                price = lot.get("price", 0)

                if cat not in cats_to_analyze:
                    continue
                if settings["budget_max"] > 0 and price > settings["budget_max"]:
                    continue

                an    = await analyze_lot(lot)
                score = float(an.get("total_score", 0))

                min_s = ALERT_SCORE if hot_only else settings["min_score"]
                if score < min_s:
                    continue

                # Доп. регионы — только 9+
                if lot.get("is_extra_region") and score < ALERT_SCORE:
                    continue

                save_lot(lot, an)
                trend = get_price_trend(lot["id"])

                if cat not in results:
                    results[cat] = []
                results[cat].append((lot, an, trend))

                # Горячие — отправляем сразу
                if score >= ALERT_SCORE:
                    lot_id = lot["id"]
                    lot_cache[lot_id] = {"lot": lot, "analysis": an}
                    msg = f"🔔 *ГОРЯЧИЙ ЛОТ {score}/10*\n\n" + build_lot_msg(lot, an, trend)
                    kbd = lot_keyboard(lot_id, lot.get("url",""))
                    await bot.send_message(
                        chat_id=chat_id, text=msg,
                        parse_mode="Markdown",
                        reply_markup=kbd,
                        disable_web_page_preview=True
                    )

            except:
                continue
            await asyncio.sleep(0.3)

        await browser.close()

    # Итог
    total = sum(len(v) for v in results.values())
    if total == 0:
        await bot.send_message(
            chat_id=chat_id,
            text="😔 Подходящих лотов не найдено.\nПопробуйте снизить балл в ⚙️ Настройках.",
            reply_markup=main_menu()
        )
        return

    # Отправляем топ-5 по каждой найденной категории
    for cat, cat_results in results.items():
        if not cat_results:
            continue
        cat_results.sort(key=lambda x: float(x[1].get("total_score",0)), reverse=True)
        cat_info = CATEGORIES.get(cat, {"icon":"📦","label":cat})
        icon_c   = cat_info["icon"]
        label_c  = cat_info["label"]
        text     = f"{icon_c} *{label_c} — топ {min(5,len(cat_results))}*\n\n"

        for i, (lot, an, trend) in enumerate(cat_results[:5]):
            lot_id = lot["id"]
            lot_cache[lot_id] = {"lot": lot, "analysis": an}
            block  = build_lot_msg(lot, an, trend)
            kbd    = lot_keyboard(lot_id, lot.get("url",""))

            if len(text) + len(block) > 3500:
                await bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode="Markdown", disable_web_page_preview=True
                )
                text = block
            else:
                text += block

        if text.strip():
            await bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode="Markdown",
                reply_markup=lot_keyboard(
                    cat_results[0][0]["id"],
                    cat_results[0][0].get("url","")
                ),
                disable_web_page_preview=True
            )
        await asyncio.sleep(1)

    await bot.send_message(
        chat_id=chat_id,
        text=f"✅ *Анализ завершён!*\nНайдено: {total} лотов\n\nВыберите следующее действие:",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )


# ─── Команды ────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Добро пожаловать!*\n\n"
        "Я ваш эксперт по банкротным торгам 🏆\n\n"
        "• Нажмите кнопку — получите анализ\n"
        "• Скиньте ссылку на лот — разберу\n"
        "• Задайте вопрос — отвечу как эксперт\n\n"
        "_Ежедневный дайджест приходит в 08:00_",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📱 Главное меню:",
        reply_markup=main_menu()
    )


# ─── Кнопки ─────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    await q.answer()
    data    = q.data
    chat_id = str(q.message.chat_id)
    bot     = ctx.bot

    # Главное меню — выбор категории
    if data == "menu_run":
        await q.edit_message_text(
            "📊 *Выберите что искать:*\n\n"
            "_Или нажмите «ЗАПУСТИТЬ АНАЛИЗ СЕЙЧАС» для полного дайджеста_",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )

    elif data == "back_menu":
        await q.edit_message_text(
            "📱 Главное меню:", reply_markup=main_menu()
        )

    # Выбор категории — показываем подменю
    elif data.startswith("run_") and not data.startswith("run_hot"):
        cat = data.replace("run_", "")
        cat_names = {
            "квартира":"🏠 Квартиры","дом":"🏡 Дома","коммерция":"🏢 Коммерция",
            "земля":"🌱 Земля","авто":"🚗 Авто","гараж":"🅿️ Гаражи","бизнес":"💼 Бизнес",
        }
        await q.edit_message_text(
            f"*{cat_names.get(cat,cat)}*\n\nВыберите регион:",
            parse_mode="Markdown",
            reply_markup=run_submenu(cat)
        )

    elif data == "run_hot":
        await q.edit_message_text("⚡ Ищу только горячие лоты (балл 9+)...")
        asyncio.create_task(do_run(chat_id, bot, "горячие", True, True))

    # Запуск анализа
    elif data.startswith("exec_"):
        parts  = data.split("_")  # exec_квартира_main
        cat    = parts[1]
        region = parts[2] if len(parts) > 2 else "main"
        extra  = region == "all"
        await q.edit_message_text(
            f"🔍 Запускаю анализ: *{cat}*...",
            parse_mode="Markdown"
        )
        asyncio.create_task(do_run(chat_id, bot, cat, extra, False))

    # Лот — войти
    elif data.startswith("enter_"):
        lot_id = data[6:]
        cached = lot_cache.get(lot_id, {})
        lot    = cached.get("lot", {})
        an     = cached.get("analysis", {})
        await q.edit_message_text(
            f"⚠️ *Подтвердите*\n\n"
            f"{lot.get('title','')[:60]}\n"
            f"💰 {an.get('price','—')}\n\n"
            f"Войти в этот лот?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, подаю заявку",
                                     callback_data=f"confirm_{lot_id}"),
                InlineKeyboardButton("↩️ Назад",
                                     callback_data=f"back_{lot_id}"),
            ]])
        )

    elif data.startswith("confirm_"):
        lot_id = data[8:]
        cached = lot_cache.get(lot_id, {})
        lot    = cached.get("lot", {})
        an     = cached.get("analysis", {})
        if lot and an:
            add_to_portfolio(lot, an, "Решил войти")
        await q.edit_message_text(
            f"📋 *Инструкция по подаче заявки*\n\n"
            f"1️⃣ Перейдите по ссылке ниже\n"
            f"2️⃣ Нажмите «Подать заявку»\n"
            f"3️⃣ Документы: паспорт + ИНН\n"
            f"4️⃣ Внесите задаток на указанный счёт\n"
            f"5️⃣ Дождитесь подтверждения заявки\n\n"
            f"⚖️ {an.get('legal_text','')}\n\n"
            f"🔗 {lot.get('url','')}\n"
            f"✅ _Добавлено в портфель_",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    elif data.startswith("watch_"):
        lot_id = data[6:]
        cached = lot_cache.get(lot_id, {})
        if cached.get("lot") and cached.get("analysis"):
            add_to_portfolio(cached["lot"], cached["analysis"], "Наблюдение")
        await q.edit_message_text(
            "👁 *Добавлено в наблюдение*\n\n"
            "Агент уведомит при изменении цены.",
            parse_mode="Markdown"
        )

    elif data.startswith("how_"):
        await q.edit_message_text(
            "📖 *Как участвовать в торгах*\n\n"
            "1️⃣ Получите ЭЦП (~3000 руб в УЦ)\n"
            "2️⃣ Зарегистрируйтесь на ЭТП\n"
            "3️⃣ Задаток: обычно 5-20% от цены\n"
            "4️⃣ Подайте заявку до дедлайна\n"
            "5️⃣ При победе — оплата в 30 дней\n\n"
            "💡 _Начните с небольших лотов_",
            parse_mode="Markdown"
        )

    elif data.startswith("skip_"):
        lot_id = data[5:]
        cached = lot_cache.get(lot_id, {})
        lot    = cached.get("lot", {})
        await q.edit_message_text(
            f"❌ Пропущен\n_{lot.get('title','')[:60]}_",
            parse_mode="Markdown"
        )

    elif data.startswith("back_"):
        lot_id = data[5:]
        cached = lot_cache.get(lot_id, {})
        lot    = cached.get("lot", {})
        an     = cached.get("analysis", {})
        trend  = get_price_trend(lot_id) if lot_id else {}
        msg    = build_lot_msg(lot, an, trend)
        await q.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=lot_keyboard(lot_id, lot.get("url","")),
            disable_web_page_preview=True
        )

    # Статистика
    elif data == "stats":
        s    = get_global_stats()
        cats = "\n".join(f"  {c}: {n}" for c,n in s.get("top_cats",[])[:5])
        await q.edit_message_text(
            f"📊 *Статистика*\n\n"
            f"🔍 Изучено лотов: *{s['total_lots']}*\n"
            f"🔄 Запусков: *{s['total_runs']}*\n"
            f"🟢 «Входить» за 7 дней: *{s['recent_go']}*\n\n"
            f"Топ категорий:\n{cats}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Меню", callback_data="back_menu")
            ]])
        )

    # Портфель
    elif data == "portfolio":
        items = get_portfolio()
        if not items:
            text = "📋 *Портфель пуст*\n\nНажмите «Наблюдать» под любым лотом."
        else:
            text = "📋 *Ваш портфель:*\n\n"
            icons = {"watching":"👀","interested":"🔥","applied":"📝","won":"🏆","lost":"❌"}
            for item in items[:8]:
                icon = icons.get(item["status"],"📌")
                bp   = item["buy_price"]/1e6 if item["buy_price"] else 0
                mp   = item["market_price"]/1e6 if item["market_price"] else 0
                text += f"{icon} {item['title'][:45]}\n💰 {bp:.1f}→{mp:.1f} млн | {item['url']}\n\n"
        await q.edit_message_text(
            text, parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Меню", callback_data="back_menu")
            ]])
        )

    # Настройки
    elif data == "settings":
        budget = f"до {settings['budget_max']//1_000_000} млн" \
                 if settings["budget_max"] > 0 else "без ограничений"
        await q.edit_message_text(
            f"⚙️ *Настройки*\n\n"
            f"💰 Бюджет: {budget}\n"
            f"⭐ Мин. балл: {settings['min_score']}\n\n"
            f"Напишите: *бюджет 10* или *балл 8*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 До 5 млн",  callback_data="sb_5"),
                 InlineKeyboardButton("💰 До 10 млн", callback_data="sb_10"),
                 InlineKeyboardButton("💰 До 30 млн", callback_data="sb_30"),
                 InlineKeyboardButton("💰 Без лимита",callback_data="sb_0")],
                [InlineKeyboardButton("⭐ Балл 7+",   callback_data="ss_7"),
                 InlineKeyboardButton("⭐ Балл 8+",   callback_data="ss_8"),
                 InlineKeyboardButton("⭐ Балл 9+",   callback_data="ss_9")],
                [InlineKeyboardButton("↩️ Меню",      callback_data="back_menu")],
            ])
        )

    elif data.startswith("sb_"):
        mlns = int(data[3:])
        settings["budget_max"] = mlns * 1_000_000
        note = f"до {mlns} млн ₽" if mlns > 0 else "без ограничений"
        await q.answer(f"✅ Бюджет: {note}")

    elif data.startswith("ss_"):
        score = float(data[3:])
        settings["min_score"] = score
        await q.answer(f"✅ Минимальный балл: {score}+")

    # Помощь
    elif data == "help":
        await q.edit_message_text(
            "❓ *Как пользоваться*\n\n"
            "🚀 *Запустить анализ* — полный дайджест всех категорий\n\n"
            "🏠 *Категория* → выберите регион → получите топ-5 лотов\n\n"
            "⚡ *Горячие 9+* — только исключительные лоты из всех регионов\n\n"
            "🔗 *Скиньте ссылку* на любой лот — разберу детально\n\n"
            "❓ *Задайте вопрос* — отвечу как эксперт по инвестициям\n\n"
            "⚙️ *Настройки* — установите бюджет и минимальный балл\n\n"
            "📋 *Портфель* — лоты которые наблюдаете\n\n"
            "_Ежедневный дайджест недвижимости приходит в 08:00_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Меню", callback_data="back_menu")
            ]])
        )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text or ""
    chat_id = str(update.message.chat_id)
    bot     = ctx.bot

    # Бюджет
    m = re.match(r'бюджет\s+(\d+)', text, re.IGNORECASE)
    if m:
        mlns = int(m.group(1))
        settings["budget_max"] = mlns * 1_000_000
        await update.message.reply_text(
            f"✅ Бюджет: до {mlns} млн ₽",
            reply_markup=main_menu()
        )
        return

    # Балл
    m = re.match(r'балл\s+([\d.]+)', text, re.IGNORECASE)
    if m:
        settings["min_score"] = float(m.group(1))
        await update.message.reply_text(
            f"✅ Минимальный балл: {settings['min_score']}+",
            reply_markup=main_menu()
        )
        return

    # Ссылка на лот
    if "tbankrot.ru/item" in text or re.search(r'id=\d+', text):
        url_m = re.search(r'https?://\S+', text)
        url   = url_m.group() if url_m else ""
        if not url:
            id_m = re.search(r'id=(\d+)', text)
            url  = f"https://tbankrot.ru/item?id={id_m.group(1)}" if id_m else ""
        if url:
            msg = await update.message.reply_text("⏳ Анализирую лот (~1 мин)...")
            try:
                import pdfplumber, io
                from playwright.async_api import async_playwright

                lot_id = (re.search(r'id=(\d+)', url) or re.search(r'','')).group(1) \
                         if re.search(r'id=(\d+)', url) else "0"
                lot    = {
                    "id": lot_id, "title": "", "url": url,
                    "region": "moskva", "pdf_text": "",
                    "description": "", "price": 0,
                    "step_current":0,"step_total":0,
                    "participants":0,"vin":"","cadastral":"",
                    "is_extra_region": False, "source": "Т-Банкрот",
                }

                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    ctx2    = await browser.new_context()
                    page    = await ctx2.new_page()
                    details = await get_lot_details(url, page)
                    lot.update({
                        "price":        details.get("price",0),
                        "description":  details.get("description",""),
                        "step_current": details.get("step_current",0),
                        "step_total":   details.get("step_total",0),
                        "participants": details.get("participants",0),
                        "vin":          details.get("vin",""),
                        "cadastral":    details.get("cadastral",""),
                    })
                    if details.get("title_full"):
                        lot["title"] = details["title_full"]
                    lot["category"] = detect_type(
                        f"{lot['title']} {lot['description'][:300]}"
                    )
                    try:
                        resp = await ctx2.request.get(
                            f"https://files.tbankrot.ru/egrn_files/{lot_id}.pdf"
                        )
                        if resp.status == 200:
                            with pdfplumber.open(io.BytesIO(await resp.body())) as pdf:
                                lot["pdf_text"] = "\n".join(
                                    p.extract_text() or ""
                                    for p in pdf.pages[:4]
                                )[:3000]
                    except:
                        pass
                    await browser.close()

                an    = await analyze_lot(lot)
                trend = get_price_trend(lot_id)
                save_lot(lot, an)
                lot_cache[lot_id] = {"lot": lot, "analysis": an}

                result = build_lot_msg(lot, an, trend)
                kbd    = lot_keyboard(lot_id, url)
                await msg.delete()
                await update.message.reply_text(
                    result, parse_mode="Markdown",
                    reply_markup=kbd,
                    disable_web_page_preview=True
                )
            except Exception as e:
                await msg.edit_text(f"⚠️ Ошибка: {e}")
        return

    # Вопрос эксперту
    if len(text) > 3:
        m2 = await update.message.reply_text("💭 Думаю...")
        answer = await ask_expert(text)
        await m2.edit_text(
            answer,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📱 Меню", callback_data="back_menu")
            ]])
        )


def run_bot():
    init_db()
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CommandHandler("m",     cmd_menu))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_message
    ))
    print("🤖 Бот v4.0 запущен! Напишите /start в Telegram.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()

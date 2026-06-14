# v2.0 — Bankrot Bot with regions
import asyncio, os, re, logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot_main")

TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")
GROQ_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GH_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Кэш спарсенных лотов (кадастр, адрес, дата) для «Полного анализа»
lot_cache = {}

# Все регионы Т-Банкрот
REGIONS = {
    "🏛 Москва":          "moskva",
    "🌆 Московская обл.": "moskovskaya-oblast",
    "🌊 Санкт-Петербург": "sankt-peterburg",
    "🌿 Ленинградская":   "leningradskaya-oblast",
    "☀️ Краснодар":       "krasnodar",
    "🏔 Екатеринбург":    "ekaterinburg",
    "❄️ Новосибирск":     "novosibirsk",
    "🌲 Татарстан":       "tatarstan",
    "💎 Башкортостан":    "bashkortostan",
    "🏙 Ростов-на-Дону":  "rostov-na-donu",
    "🌸 Самара":          "samara",
    "🎯 Нижний Новгород": "nizhegorodskaya-oblast",
    "🌊 Волгоград":       "volgogradskaya-oblast",
    "🏔 Красноярск":      "krasnoyarskiy-kray",
    "🌏 Все регионы":     "all",
}

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Запустить анализ", callback_data="menu_run")],
        [InlineKeyboardButton("🗺 Выбрать регион",   callback_data="menu_regions")],
        [
            InlineKeyboardButton("🏠 Квартиры",  callback_data="run_квартира"),
            InlineKeyboardButton("🏢 Коммерция", callback_data="run_коммерция"),
        ],
        [
            InlineKeyboardButton("🏡 Дома",  callback_data="run_дом"),
            InlineKeyboardButton("🌱 Земля", callback_data="run_земля"),
        ],
        [
            InlineKeyboardButton("🚗 Авто",        callback_data="run_авто"),
            InlineKeyboardButton("⚡ Горячие 9+",  callback_data="run_hot"),
        ],
        [InlineKeyboardButton("📊 Статус", callback_data="status")],
    ])

def regions_menu():
    rows = []
    items = list(REGIONS.items())
    for i in range(0, len(items), 2):
        row = []
        for label, code in items[i:i+2]:
            row.append(InlineKeyboardButton(label, callback_data=f"region_{code}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("↩️ Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(rows)

async def trigger_workflow(category: str = "все", region: str = "moskva") -> bool:
    import httpx
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN not found in environment")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.github.com/repos/tagirdancer/bankrot-agent/actions/workflows/agent.yml/dispatches",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"ref": "main", "inputs": {"category": category}}
            )
            print(f"GitHub API status: {resp.status_code}")
            return resp.status_code == 204
    except Exception as e:
        print(f"GitHub API error: {e}")
        return False

async def ask_expert(question: str) -> str:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"token {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content":
                        f"Ты эксперт по банкротным торгам России. Отвечай кратко.\n\n{question}"}],
                    "max_tokens": 400, "temperature": 0.5,
                }
            )
            data = resp.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Ошибка: {e}"
    return "Нет ответа"


def extract_lot_id(text: str):
    """Распознаёт id лота из ссылки tbankrot.ru или просто номера."""
    text = (text or "").strip()
    m = re.search(r"tbankrot\.ru/item[^\d]*id=(\d+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\bid=(\d+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{5,10}", text):
        return text
    m = re.search(r"(?:лот|торг|id|№)\s*[:#]?\s*(\d{5,10})", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


async def fetch_and_analyze_lot(lot_id: str):
    """Парсит и анализирует лот той же логикой, что дайджест (agent.enrich + analyze_lot)."""
    from playwright.async_api import async_playwright
    from agent import enrich
    from analyzer import analyze_lot

    url = f"https://tbankrot.ru/item?id={lot_id}"
    lot = {
        "id": lot_id, "title": "", "url": url,
        "region": "moskva", "pdf_text": "", "description": "",
        "price": 0, "step_current": 0, "step_total": 0,
        "participants": 0, "vin": "", "cadastral": "", "address": "",
        "is_extra": False, "source": "Т-Банкрот",
    }
    parsed_at = datetime.now()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()
        await enrich(lot, page, ctx)
        await browser.close()
    from database import record_digest_lot
    dedup = record_digest_lot(lot_id, lot.get("price", 0))
    if dedup.get("note"):
        lot["dedup_note"] = dedup["note"]
    an = await analyze_lot(lot)
    lot["parsed_at"] = parsed_at.isoformat()
    return lot, an, parsed_at


async def deep_analysis(lot_id: str, facts: dict = None) -> str:
    """Полный анализ — детерминированный вердикт без LLM (анти-галлюцинации)."""
    import json
    from analyzer import build_verification_links
    from verdict import run_verdict_pipeline

    facts = facts or {}
    url = f"https://tbankrot.ru/item?id={lot_id}"
    cached = lot_cache.get(lot_id, {})
    lot = cached.get("lot") or {"id": lot_id, "url": url}
    an = cached.get("an")

    if an and an.get("verdict_card"):
        card = an["verdict_card"]
        facts_json = an.get("facts_json", {})
    else:
        partial = {
            "lot_price_raw": facts.get("price_raw") or lot.get("price"),
            "market_price_raw": facts.get("market_raw"),
            "discount_pct": facts.get("disc", "0"),
            "land_manual_market": lot.get("category") == "земля",
        }
        vr = run_verdict_pipeline(lot, partial)
        card = vr["verdict_card"]
        facts_json = vr["facts_json"]

    parsed_hdr = ""
    pts = facts.get("parsed_at") or cached.get("parsed_at")
    if pts:
        try:
            if isinstance(pts, int):
                parsed_hdr = datetime.fromtimestamp(pts).strftime("%d.%m.%Y %H:%M")
            else:
                parsed_hdr = datetime.fromisoformat(str(pts)).strftime("%d.%m.%Y %H:%M")
        except Exception:
            pass

    cadastral = facts.get("cadastral") or cached.get("cadastral", "")
    address = facts.get("address") or cached.get("address", "")
    lot_type = lot.get("category") or (an or {}).get("lot_type", "")
    vin = lot.get("vin") or ""
    verify_links = build_verification_links(cadastral, address, vin, lot_type)

    clean_facts = {k: v for k, v in facts_json.items() if not str(k).startswith("_")}
    json_block = json.dumps(clean_facts, ensure_ascii=False, indent=2)

    parts = []
    if parsed_hdr:
        parts.append(f"📅 Данные спарсены: {parsed_hdr}\n")
    parts.append(card)
    parts.append(f"\n*Извлечённые факты (шаг 1):*\n```\n{json_block}\n```")
    parts.append(f"\n*Ссылки для ручной проверки:*\n{verify_links}")
    return "\n".join(parts)


# Хранилище выбранного региона
user_region = {}

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Банкротный агент*\n\n"
        "Анализирует лоты с Т-Банкрот и отправляет дайджест\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Меню:", reply_markup=main_menu())


async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from database import get_saved_lots
    items = get_saved_lots(update.message.chat_id)
    if not items:
        await update.message.reply_text(
            "⭐ *Сохранённые лоты пусты*\n\nНажмите «Сохранить» под любым лотом.",
            parse_mode="Markdown", reply_markup=main_menu(),
        )
        return
    text = "⭐ *Сохранённые лоты:*\n\n"
    for item in items[:10]:
        dl = f" | заявки до {item['deadline']}" if item.get("deadline") else ""
        text += f"• {item['title'][:50]}\n  {item['url']}{dl}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)


async def check_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    from database import get_due_reminders, mark_reminded
    for item in get_due_reminders():
        try:
            await ctx.bot.send_message(
                chat_id=item["chat_id"],
                text=(
                    f"⏰ *Напоминание*\n\n"
                    f"Через 1–2 дня дедлайн заявок по лоту:\n"
                    f"{item['title'][:60]}\n"
                    f"📅 до {item['deadline']}\n"
                    f"🔗 {item['url']}"
                ),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            mark_reminded(item["lot_id"], item["chat_id"])
        except Exception:
            log.exception("reminder failed for lot %s", item.get("lot_id"))


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data

    if data.startswith("save_"):
        lot_id = data[5:]
        cached = lot_cache.get(lot_id, {})
        lot, an = cached.get("lot", {}), cached.get("an", {})
        if not (lot and an):
            await q.answer("Загружаю лот...")
            try:
                lot, an, parsed_at = await fetch_and_analyze_lot(lot_id)
                lot_cache[lot_id] = {
                    "lot": lot, "an": an,
                    "cadastral": lot.get("cadastral", ""),
                    "address": lot.get("address", ""),
                    "parsed_at": int(parsed_at.timestamp()),
                }
            except Exception:
                log.exception("save failed for lot %s", lot_id)
                await q.answer("Не удалось загрузить лот", show_alert=True)
                return
        from database import save_lot_for_user
        save_lot_for_user(str(q.message.chat_id), lot, an)
        await q.answer("⭐ Лот сохранён")
        return

    if data.startswith("deep_"):
        payload = data[5:]
        bits = payload.split("_")
        lot_id = bits[0]
        facts = {}
        if len(bits) >= 5:
            facts = {"price_raw": bits[1], "market_raw": bits[2],
                     "disc": bits[3], "parts": bits[4]}
        if len(bits) >= 6:
            try:
                facts["parsed_at"] = int(bits[5])
            except ValueError:
                pass
        cached = lot_cache.get(lot_id, {})
        if not cached.get("an"):
            await q.answer("Загружаю лот...")
            try:
                lot, an, parsed_at = await fetch_and_analyze_lot(lot_id)
                lot_cache[lot_id] = {
                    "lot": lot, "an": an,
                    "cadastral": lot.get("cadastral", ""),
                    "address": lot.get("address", ""),
                    "parsed_at": int(parsed_at.timestamp()),
                }
            except Exception:
                log.exception("deep fetch failed for lot %s", lot_id)
        cached = lot_cache.get(lot_id, {})
        if cached.get("cadastral"):
            facts["cadastral"] = cached["cadastral"]
        if cached.get("address"):
            facts["address"] = cached["address"]
        if cached.get("parsed_at") and "parsed_at" not in facts:
            facts["parsed_at"] = cached["parsed_at"]
        try:
            await q.answer("Анализирую...")
            await q.message.reply_text("🔍 Готовлю вердикт по лоту (~1 мин)...")
            analysis = await deep_analysis(lot_id, facts)
            try:
                await q.message.reply_text(analysis, parse_mode="Markdown")
            except Exception:
                log.exception("full_analysis failed: ошибка отправки с Markdown, шлю без форматирования")
                await q.message.reply_text(analysis)
        except Exception:
            log.exception("full_analysis failed")
            await q.message.reply_text("\u26a0\ufe0f Не удалось получить анализ (внутренняя ошибка). Подробности в логах Railway.")
        return

    await q.answer()
    chat = str(q.message.chat_id)

    if data == "back_menu":
        await q.edit_message_text("📱 Меню:", reply_markup=main_menu())

    elif data == "menu_regions":
        cur = user_region.get(chat, "moskva")
        cur_name = next((k for k,v in REGIONS.items() if v == cur), cur)
        await q.edit_message_text(
            f"🗺 *Выбор региона*\n\nТекущий: *{cur_name}*\n\nВыберите регион для следующего анализа:",
            parse_mode="Markdown",
            reply_markup=regions_menu()
        )

    elif data.startswith("region_"):
        code = data[7:]
        user_region[chat] = code
        name = next((k for k,v in REGIONS.items() if v == code), code)
        await q.edit_message_text(
            f"✅ Регион выбран: *{name}*\n\nТеперь запускайте анализ — он будет по этому региону.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Запустить анализ", callback_data="menu_run"),
                InlineKeyboardButton("↩️ Меню", callback_data="back_menu"),
            ]])
        )

    elif data == "menu_run" or data == "status":
        region = user_region.get(chat, "moskva")
        region_name = next((k for k,v in REGIONS.items() if v == region), region)
        if data == "status":
            await q.edit_message_text(
                f"📊 *Статус*\n\nРегион: *{region_name}*\nДайджест: каждый день в 08:00\nАгент: работает в GitHub Actions",
                parse_mode="Markdown",
                reply_markup=main_menu()
            )
        else:
            await q.edit_message_text(
                f"🚀 *Запустить анализ*\n\nРегион: *{region_name}*\n\nВыберите категорию:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 Все категории", callback_data="run_full")],
                    [
                        InlineKeyboardButton("🏠 Квартиры",  callback_data="run_квартира"),
                        InlineKeyboardButton("🏢 Коммерция", callback_data="run_коммерция"),
                    ],
                    [
                        InlineKeyboardButton("🏡 Дома",  callback_data="run_дом"),
                        InlineKeyboardButton("🌱 Земля", callback_data="run_земля"),
                    ],
                    [
                        InlineKeyboardButton("🚗 Авто",       callback_data="run_авто"),
                        InlineKeyboardButton("⚡ Горячие 9+", callback_data="run_hot"),
                    ],
                    [InlineKeyboardButton("↩️ Назад", callback_data="back_menu")],
                ])
            )

    elif data.startswith("run_"):
        cat = data[4:]
        region = user_region.get(chat, "moskva")
        region_name = next((k for k,v in REGIONS.items() if v == region), region)
        cat_names = {
            "full":"все категории","квартира":"квартиры","коммерция":"коммерция",
            "дом":"дома","земля":"земля","авто":"авто","hot":"горячие 9+",
        }
        label = cat_names.get(cat, cat)
        github_cat = "все" if cat in ("full","hot") else cat

        ok = await trigger_workflow(github_cat, region)
        if ok:
            await q.edit_message_text(
                f"🚀 *Анализ запущен!*\n\n"
                f"📂 Категория: *{label}*\n"
                f"📍 Регион: *{region_name}*\n\n"
                f"⏳ ~30 минут — результаты придут в этот чат\n"
                f"_Агент работает в GitHub Actions_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Меню", callback_data="back_menu")
                ]])
            )
        else:
            await q.edit_message_text(
                f"⚠️ *GITHUB TOKEN не настроен*\n\n"
                f"Зайдите вручную:\n"
                f"github.com/tagirdancer/bankrot-agent/actions\n"
                f"→ Run workflow",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Меню", callback_data="back_menu")
                ]])
            )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    lot_id = extract_lot_id(text)
    if lot_id:
        from analyzer import format_short_lot_message, lot_action_keyboard
        msg = await update.message.reply_text("⏳ Парсю и анализирую лот (~1 мин)...")
        try:
            lot, an, parsed_at = await fetch_and_analyze_lot(lot_id)
            lot_cache[lot_id] = {
                "lot": lot, "an": an,
                "cadastral": lot.get("cadastral", ""),
                "address": lot.get("address", ""),
                "parsed_at": int(parsed_at.timestamp()),
            }
            kb = lot_action_keyboard(lot_id, an, lot, parsed_at)
            await msg.edit_text(
                format_short_lot_message(lot, an),
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("link_analysis failed for lot %s", lot_id)
            await msg.edit_text(
                f"⚠️ Не удалось спарсить лот *{lot_id}*.\n\n"
                f"Проверьте ссылку или попробуйте позже.\n"
                f"Подробности записаны в логи Railway.",
                parse_mode="Markdown",
            )
        return

    if re.search(r"tbankrot|банкрот|лот|id\s*=", text, re.IGNORECASE):
        await update.message.reply_text(
            "Не распознал лот. Пришлите:\n"
            "• ссылку: `https://tbankrot.ru/item?id=7629977`\n"
            "• или номер лота: `7629977`",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )
        return

    if len(text) > 3:
        msg = await update.message.reply_text("💭 Думаю...")
        answer = await ask_expert(text)
        await msg.edit_text(answer, reply_markup=main_menu())

def run():
    from database import init_db
    init_db()
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CommandHandler("m",     cmd_menu))
    app.add_handler(CommandHandler("saved", cmd_saved))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.job_queue.run_repeating(check_reminders, interval=3600, first=120)
    print("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    run()

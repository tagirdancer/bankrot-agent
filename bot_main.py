# v2.1 — расписание + /latest + стриминг при ручном запуске
import asyncio, os, re, logging
from datetime import datetime, time
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
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
_run_lock = asyncio.Lock()
MSK = ZoneInfo("Europe/Moscow")

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

# Постоянное нижнее меню (reply-кнопки)
REPLY_LATEST = "📋 Последние результаты"
REPLY_HOT    = "🔥 Горячие лоты"
REPLY_RUN    = "🚀 Запустить анализ"
REPLY_SAVED  = "⭐ Сохранённые"
REPLY_BUTTONS = {REPLY_LATEST, REPLY_HOT, REPLY_RUN, REPLY_SAVED}


def reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(REPLY_LATEST), KeyboardButton(REPLY_HOT)],
            [KeyboardButton(REPLY_RUN), KeyboardButton(REPLY_SAVED)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )


def _user_region_code(chat_id) -> str:
    return user_region.get(str(chat_id), "moskva")


def _user_region_name(chat_id) -> str:
    code = _user_region_code(chat_id)
    return next((k for k, v in REGIONS.items() if v == code), code)


def _region_filter_for_agent(chat_id):
    """None = все регионы агента; иначе список slug для collect()."""
    code = _user_region_code(chat_id)
    if code == "all":
        return None
    return [code]


def run_category_menu():
    """Inline-выбор категории — как callback menu_run."""
    return InlineKeyboardMarkup([
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
        [InlineKeyboardButton("↩️ Меню", callback_data="back_menu")],
    ])


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Последние результаты", callback_data="latest")],
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

async def show_latest(update: Update, *, edit_message=None):
    """Показать снимок последнего прогона из БД — без нового запуска."""
    from database import get_latest_run, format_latest_run_messages
    run = get_latest_run()
    chat_id = update.effective_chat.id
    bot = update.get_bot()

    if not run:
        text = (
            "📋 *Последних результатов пока нет.*\n\n"
            "Автопрогоны: *08:00* и *19:00* (МСК).\n"
            "Или нажмите «Запустить анализ» — горячие лоты придут по ходу."
        )
        if edit_message:
            await edit_message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu())
        else:
            await bot.send_message(
                chat_id=chat_id, text=text, parse_mode="Markdown",
                reply_markup=reply_keyboard(),
            )
        return

    parts = format_latest_run_messages(run)
    if edit_message:
        await edit_message.edit_text(parts[0], parse_mode="Markdown", disable_web_page_preview=True)
        for part in parts[1:]:
            await bot.send_message(chat_id=chat_id, text=part, parse_mode="Markdown", disable_web_page_preview=True)
    else:
        for part in parts:
            await bot.send_message(
                chat_id=chat_id, text=part, parse_mode="Markdown",
                disable_web_page_preview=True, reply_markup=reply_keyboard(),
            )
    await bot.send_message(chat_id=chat_id, text="📱 Меню:", reply_markup=main_menu())


async def cmd_latest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_latest(update)


async def _run_agent_background(chat_id: str, cats, hot_only: bool, bot, label: str, region_filter=None):
    from agent import run as agent_run
    stream_min = 9.0 if hot_only else 8.0
    try:
        async with _run_lock:
            await agent_run(
                cats=cats,
                include_extra=True,
                daily=True,
                save_to_db=True,
                run_type="manual",
                stream_chat_id=str(chat_id),
                stream_bot=bot,
                stream_min_score=stream_min,
                hot_only=hot_only,
                region_filter=region_filter,
            )
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ *Прогон завершён* — {label}\n\n"
                f"📋 /latest — полный снимок без ожидания\n"
                f"⏰ Следующий автопрогон: 08:00 или 19:00 МСК"
            ),
            parse_mode="Markdown",
            reply_markup=reply_keyboard(),
        )
    except Exception:
        log.exception("manual agent run failed")
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Прогон прерван с ошибкой. Попробуйте позже или /latest для прошлого снимка.",
            reply_markup=reply_keyboard(),
        )


def _cats_for_run(cat: str):
    from agent import DEFAULT_CATS
    if cat in ("full", "hot", "все"):
        return DEFAULT_CATS, cat == "hot"
    return {cat}, False


async def _launch_agent_run(chat_id: str, cat: str, bot):
    """Запуск прогона — общая логика для inline и reply-кнопок."""
    region_name = _user_region_name(chat_id)
    region_filter = _region_filter_for_agent(chat_id)
    cat_names = {
        "full": "все категории", "квартира": "квартиры", "коммерция": "коммерция",
        "дом": "дома", "земля": "земля", "авто": "авто", "hot": "горячие 9+",
    }
    label = cat_names.get(cat, cat)
    cats, hot_only = _cats_for_run(cat)

    if _run_lock.locked():
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ *Уже идёт прогон*\n\n"
                "Горячие лоты приходят по мере нахождения.\n"
                "📋 /latest — прошлый готовый снимок"
            ),
            parse_mode="Markdown",
            reply_markup=reply_keyboard(),
        )
        return

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🚀 *Анализ запущен!*\n\n"
            f"📂 Категория: *{label}*\n"
            f"📍 Регион: *{region_name}*\n\n"
            f"⚡ Горячие лоты (дисконт ≥30%) — по мере тяжёлого анализа\n"
            f"📦 Полный дайджест — в конце (~30–45 мин)\n"
            f"📋 /latest — не ждать, открыть прошлый снимок"
        ),
        parse_mode="Markdown",
        reply_markup=reply_keyboard(),
    )
    asyncio.create_task(
        _run_agent_background(str(chat_id), cats, hot_only, bot, label, region_filter)
    )


async def scheduled_agent_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Автопрогон по расписанию (08:00 / 19:00 МСК)."""
    if _run_lock.locked():
        log.warning("scheduled agent skipped — already running")
        return
    slot = (ctx.job.data or {}).get("slot", "?")
    log.info("scheduled agent start %s", slot)
    try:
        from agent import run as agent_run, DEFAULT_CATS
        async with _run_lock:
            await agent_run(
                cats=DEFAULT_CATS,
                include_extra=True,
                daily=True,
                save_to_db=True,
                run_type="scheduled",
            )
        log.info("scheduled agent done %s", slot)
    except Exception:
        log.exception("scheduled agent failed %s", slot)

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
    """Парсит и анализирует лот. EGRN/рынок/Groq не должны ронять карточку."""
    from playwright.async_api import async_playwright
    from agent import enrich, login, launch_browser
    from analyzer import analyze_lot, minimal_lot_analysis

    url = f"https://tbankrot.ru/item?id={lot_id}"
    lot = {
        "id": lot_id, "title": "", "url": url,
        "region": "moskva", "pdf_text": "", "description": "",
        "price": 0, "step_current": 0, "step_total": 0,
        "participants": 0, "vin": "", "cadastral": "", "address": "",
        "is_extra": False, "source": "Т-Банкрот",
    }
    parsed_at = datetime.now()
    login_ok = False

    log.info("fetch_and_analyze_lot start id=%s url=%s", lot_id, url)
    async with async_playwright() as p:
        browser = await launch_browser(p)
        try:
            ctx = await browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            ))
            page = await ctx.new_page()
            login_ok = await login(page)
            log.info("lot %s login_ok=%s", lot_id, login_ok)
            try:
                await enrich(lot, page, ctx, heavy=True)
            except Exception:
                log.exception("lot %s enrich failed (continuing with card data)", lot_id)
        finally:
            await browser.close()

    if not lot.get("title") and not lot.get("description"):
        raise RuntimeError(
            f"Карточка лота {lot_id} пуста — страница не загрузилась или изменилась вёрстка"
        )

    lot["login_ok"] = login_ok
    try:
        from database import record_digest_lot
        dedup = record_digest_lot(lot_id, lot.get("price", 0))
        if dedup.get("note"):
            lot["dedup_note"] = dedup["note"]
    except Exception:
        log.exception("lot %s record_digest_lot failed", lot_id)

    try:
        an = await analyze_lot(lot)
    except Exception:
        log.exception("lot %s analyze_lot failed, using minimal card", lot_id)
        an = minimal_lot_analysis(lot)

    lot["parsed_at"] = parsed_at.isoformat()
    log.info(
        "fetch_and_analyze_lot done id=%s title=%r price=%s cadastral=%s login_ok=%s",
        lot_id, (lot.get("title") or "")[:50], lot.get("price"),
        lot.get("cadastral"), login_ok,
    )
    return lot, an, parsed_at


async def deep_analysis(lot_id: str, facts: dict = None) -> str:
    """Полный анализ — детерминированный вердикт; не падает целиком при ошибке шага."""
    import json
    from analyzer import build_verification_links

    facts = facts or {}
    url = f"https://tbankrot.ru/item?id={lot_id}"
    cached = lot_cache.get(lot_id, {})
    lot = cached.get("lot") or {"id": lot_id, "url": url}
    an = cached.get("an") or {}

    card = ""
    facts_json: dict = {}

    try:
        if an.get("verdict_card"):
            card = an["verdict_card"]
            facts_json = an.get("facts_json") or {}
        else:
            from verdict import run_verdict_pipeline
            partial = {
                "lot_price_raw": facts.get("price_raw") or lot.get("price"),
                "market_price_raw": facts.get("market_raw"),
                "discount_pct": facts.get("disc", "0"),
                "land_manual_market": lot.get("category") == "земля",
                "market_known": bool(facts.get("market_raw")),
            }
            vr = run_verdict_pipeline(lot, partial)
            card = vr.get("verdict_card") or ""
            facts_json = vr.get("facts_json") or {}
    except Exception:
        log.exception("deep_analysis verdict failed lot=%s", lot_id)
        card = card or (
            "*Вердикт по документам*\n\n"
            "Частичный результат: вердикт не собран полностью. "
            "Смотрите короткую карточку и документы на сайте."
        )

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
    lot_type = lot.get("category") or an.get("lot_type", "")
    vin = lot.get("vin") or ""

    try:
        verify_links = build_verification_links(cadastral, address, vin, lot_type)
    except Exception:
        log.exception("deep_analysis verify links failed lot=%s", lot_id)
        verify_links = "Ссылки для проверки не сформированы."

    try:
        clean_facts = {
            k: v for k, v in (facts_json or {}).items()
            if not str(k).startswith("_")
        }
        log.debug(
            "deep_analysis facts lot=%s: %s",
            lot_id,
            json.dumps(clean_facts, ensure_ascii=False, default=str)[:4000],
        )
    except Exception:
        log.exception("deep_analysis facts log failed lot=%s", lot_id)

    parts = []
    if parsed_hdr:
        parts.append(f"📅 Данные спарсены: {parsed_hdr}\n")
    parts.append(card or "Вердикт не сформирован — данных недостаточно.")
    parts.append(f"\n*Ссылки для ручной проверки:*\n{verify_links}")
    return "\n".join(parts)


def _split_telegram_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


# Хранилище выбранного региона
user_region = {}

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Банкротный агент*\n\n"
        "Автопрогоны: *08:00* и *19:00* (МСК)\n"
        "📋 /latest — готовые результаты без ожидания\n\n"
        "Используйте меню внизу или inline-кнопки:",
        parse_mode="Markdown",
        reply_markup=reply_keyboard(),
    )
    await update.message.reply_text("📱 Расширенное меню:", reply_markup=main_menu())


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📱 Меню:",
        reply_markup=main_menu(),
    )
    await update.message.reply_text(
        "⌨️ Быстрые кнопки внизу — всегда под полем ввода.",
        reply_markup=reply_keyboard(),
    )


async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from database import get_saved_lots
    items = get_saved_lots(update.message.chat_id)
    if not items:
        await update.message.reply_text(
            "⭐ *Сохранённые лоты пусты*\n\nНажмите «Сохранить» под любым лотом.",
            parse_mode="Markdown",
            reply_markup=reply_keyboard(),
        )
        return
    text = "⭐ *Сохранённые лоты:*\n\n"
    for item in items[:10]:
        dl = f" | заявки до {item['deadline']}" if item.get("deadline") else ""
        text += f"• {item['title'][:50]}\n  {item['url']}{dl}\n\n"
    await update.message.reply_text(
        text, parse_mode="Markdown",
        disable_web_page_preview=True, reply_markup=reply_keyboard(),
    )


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
            for chunk in _split_telegram_message(analysis):
                try:
                    await q.message.reply_text(chunk, parse_mode="Markdown")
                except Exception:
                    log.exception("full_analysis markdown send failed lot=%s", lot_id)
                    await q.message.reply_text(chunk)
        except Exception:
            log.exception("full_analysis failed lot=%s", lot_id)
            try:
                partial = await deep_analysis(lot_id, facts)
                for chunk in _split_telegram_message(partial):
                    await q.message.reply_text(chunk)
            except Exception:
                log.exception("full_analysis fallback failed lot=%s", lot_id)
                await q.message.reply_text(
                    "⚠️ Полный анализ частично недоступен. "
                    "Смотрите короткую карточку выше и документы на сайте."
                )
        return

    if data == "latest":
        await q.answer()
        await show_latest(update, edit_message=q.message)
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
                f"📊 *Статус*\n\n"
                f"Регион: *{region_name}*\n"
                f"Автопрогоны: *08:00* и *19:00* (МСК)\n"
                f"📋 /latest — последний снимок из базы\n"
                f"🚀 Ручной запуск — горячие лоты по мере нахождения",
                parse_mode="Markdown",
                reply_markup=main_menu()
            )
        else:
            await q.edit_message_text(
                f"🚀 *Запустить анализ*\n\nРегион: *{region_name}*\n\nВыберите категорию:",
                parse_mode="Markdown",
                reply_markup=run_category_menu(),
            )

    elif data.startswith("run_"):
        cat = data[4:]
        if _run_lock.locked():
            await q.edit_message_text(
                "⏳ *Уже идёт прогон*\n\n"
                "Горячие лоты приходят по мере нахождения.\n"
                "📋 /latest — прошлый готовый снимок",
                parse_mode="Markdown",
                reply_markup=main_menu(),
            )
            return

        region_name = _user_region_name(chat)
        region_filter = _region_filter_for_agent(chat)
        cat_names = {
            "full": "все категории", "квартира": "квартиры", "коммерция": "коммерция",
            "дом": "дома", "земля": "земля", "авто": "авто", "hot": "горячие 9+",
        }
        label = cat_names.get(cat, cat)
        await q.edit_message_text(
            f"🚀 *Анализ запущен!*\n\n"
            f"📂 Категория: *{label}*\n"
            f"📍 Регион: *{region_name}*\n\n"
            f"⚡ Горячие лоты (дисконт ≥30%) — по мере тяжёлого анализа\n"
            f"📦 Полный дайджест — в конце (~30–45 мин)\n"
            f"📋 /latest — не ждать, открыть прошлый снимок",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Последние результаты", callback_data="latest"),
                InlineKeyboardButton("↩️ Меню", callback_data="back_menu"),
            ]]),
        )
        cats, hot_only = _cats_for_run(cat)
        asyncio.create_task(
            _run_agent_background(chat, cats, hot_only, ctx.bot, label, region_filter)
        )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text in REPLY_BUTTONS:
        if text == REPLY_LATEST:
            await show_latest(update)
        elif text == REPLY_SAVED:
            await cmd_saved(update, ctx)
        elif text == REPLY_HOT:
            await _launch_agent_run(update.effective_chat.id, "hot", ctx.bot)
        elif text == REPLY_RUN:
            region_name = _user_region_name(update.effective_chat.id)
            await update.message.reply_text(
                f"🚀 *Запустить анализ*\n\nРегион: *{region_name}*\n\nВыберите категорию:",
                parse_mode="Markdown",
                reply_markup=run_category_menu(),
            )
        return

    lot_id = extract_lot_id(text)
    if lot_id:
        from analyzer import format_short_lot_message, lot_action_keyboard
        from telegram.error import BadRequest
        msg = await update.message.reply_text(
            "⏳ Парсю и анализирую лот (~1 мин)...",
            reply_markup=reply_keyboard(),
        )
        try:
            lot, an, parsed_at = await fetch_and_analyze_lot(lot_id)
        except Exception as exc:
            log.exception("link_analysis parse failed for lot %s: %r", lot_id, exc)
            await msg.edit_text(
                f"⚠️ Не удалось спарсить лот {lot_id}.\n\n"
                f"Проверьте ссылку или попробуйте позже.\n"
                f"Подробности — в логах Railway (link_analysis parse failed).",
            )
            return
        lot_cache[lot_id] = {
            "lot": lot, "an": an,
            "cadastral": lot.get("cadastral", ""),
            "address": lot.get("address", ""),
            "parsed_at": int(parsed_at.timestamp()),
        }
        kb = lot_action_keyboard(lot_id, an, lot, parsed_at)
        card_text = format_short_lot_message(lot, an)
        try:
            await msg.edit_text(
                card_text,
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except BadRequest:
            log.exception("link_analysis markdown failed for lot %s, plain text", lot_id)
            plain = card_text.replace("*", "").replace("_", "")
            await msg.edit_text(plain, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            log.exception("link_analysis send failed for lot %s", lot_id)
            await msg.edit_text(
                f"✅ Лот {lot_id} спарсен, но не удалось отправить карточку.\n"
                f"{lot.get('title', '')[:80]}\n🔗 {lot.get('url', '')}",
            )
        return

    if re.search(r"tbankrot|банкрот|лот|id\s*=", text, re.IGNORECASE):
        await update.message.reply_text(
            "Не распознал лот. Пришлите:\n"
            "• ссылку: `https://tbankrot.ru/item?id=7629977`\n"
            "• или номер лота: `7629977`",
            parse_mode="Markdown",
            reply_markup=reply_keyboard(),
        )
        return

    if len(text) > 3:
        msg = await update.message.reply_text("💭 Думаю...", reply_markup=reply_keyboard())
        answer = await ask_expert(text)
        await msg.edit_text(answer)

def run():
    from agent import ensure_playwright_env
    from database import init_db
    ensure_playwright_env()
    init_db()
    try:
        from egrn_pdf import ocr_diagnostics
        d = ocr_diagnostics()
        if d["tesseract"] == "NOT FOUND":
            log.info("OCR check: tesseract NOT FOUND (%s)", d.get("reason", ""))
        else:
            log.info(
                "OCR check: tesseract version = %s (path=%s)",
                d.get("tesseract_version") or "?",
                d["tesseract"],
            )
        log.info(
            "OCR check: pymupdf=%s version=%s pytesseract=%s pillow=%s",
            d["pymupdf"], d.get("pymupdf_version", "?"),
            d["pytesseract"], d["pillow"],
        )
        log.info("OCR startup: available=%s reason=%s", d["available"], d["reason"])
    except Exception as e:
        log.warning("OCR startup check failed: %s", e)
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CommandHandler("m",     cmd_menu))
    app.add_handler(CommandHandler("saved", cmd_saved))
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    if app.job_queue:
        app.job_queue.run_daily(
            scheduled_agent_job,
            time=time(hour=8, minute=0, tzinfo=MSK),
            data={"slot": "08:00"},
            name="agent_morning",
        )
        app.job_queue.run_daily(
            scheduled_agent_job,
            time=time(hour=19, minute=0, tzinfo=MSK),
            data={"slot": "19:00"},
            name="agent_evening",
        )
        app.job_queue.run_repeating(check_reminders, interval=3600, first=120)
        log.info("Scheduled agent jobs: 08:00 and 19:00 MSK")
    else:
        log.warning("JobQueue недоступен — напоминания /saved отключены (нужен python-telegram-bot[job-queue])")
    print("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    run()

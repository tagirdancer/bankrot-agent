"""
Агент v10.0 — двухфaseный прогон под лимит GitHub Actions (60 мин)
- Фаза 1: лёгкий проход (карточка, балл) по всем лотам
- Фаза 2: PDF ЕГРН + Groq только для топ-кандидатов
- Таймауты на операции; частичный дайджест при нехватке времени
"""
import os, asyncio, schedule, time, pdfplumber, io, re
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from analyzer import (analyze_lot, detect_type, get_lot_details, MIN_SCORE,
                      format_short_lot_message, lot_action_keyboard, format_price_line,
                      get_rosreestr_data, is_real_estate)
from database import init_db, record_digest_lot

load_dotenv()

LOGIN    = os.getenv("TBANKROT_LOGIN")
PASSWORD = os.getenv("TBANKROT_PASSWORD")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

REGIONS_MAIN  = ["moskva", "moskovskaya-oblast"]
REGIONS_EXTRA = ["sankt-peterburg","krasnodar","ekaterinburg","novosibirsk"]

MAX_PAGES       = int(os.getenv("AGENT_MAX_PAGES", "12"))
EXTRA_MAX_PAGES = int(os.getenv("AGENT_EXTRA_PAGES", "3"))
TOP_N           = 15

# Бюджет прогона (сек): по умолчанию 55 мин — оставляем запас на отправку в TG
RUN_BUDGET_SEC    = int(os.getenv("AGENT_BUDGET_SEC", "3300"))
FLUSH_BEFORE_SEC  = int(os.getenv("AGENT_FLUSH_SEC", "300"))
LOT_TIMEOUT_LIGHT = int(os.getenv("LOT_TIMEOUT_LIGHT", "12"))
LOT_TIMEOUT_HEAVY = int(os.getenv("LOT_TIMEOUT_HEAVY", "45"))
MAX_HEAVY_LOTS    = int(os.getenv("AGENT_MAX_HEAVY", "45"))
PDF_TIMEOUT       = int(os.getenv("PDF_TIMEOUT", "12"))
HEAVY_MIN_SCORE   = float(os.getenv("AGENT_HEAVY_MIN_SCORE", str(max(MIN_SCORE, 6.5))))

CATEGORIES = {
    "квартира":    {"icon":"🏠","label":"Квартиры",                 "default":True},
    "апартаменты": {"icon":"🏙️","label":"Апартаменты",              "default":True},
    "дом":       {"icon":"🏡","label":"Дома и дачи",              "default":True},
    "коммерция": {"icon":"🏢","label":"Коммерческая недвижимость","default":True},
    "земля":     {"icon":"🌱","label":"Земельные участки",        "default":True},
    "авто":      {"icon":"🚗","label":"Транспорт",                "default":False},
    "гараж":     {"icon":"🅿️","label":"Гаражи",                  "default":False},
    "бизнес":    {"icon":"💼","label":"Бизнес",                   "default":False},
    "прочее":    {"icon":"📦","label":"Прочее",                   "default":False},
}

DEFAULT_CATS = {k for k,v in CATEGORIES.items() if v["default"]}


def _budget_left(start_ts: float) -> float:
    return RUN_BUDGET_SEC - (time.time() - start_ts)


def _should_flush(start_ts: float) -> bool:
    return _budget_left(start_ts) < FLUSH_BEFORE_SEC


def _apply_details(lot, details):
    lot.update({
        "price":        details.get("price", 0),
        "description":  details.get("description", ""),
        "step_current": details.get("step_current", 0),
        "step_total":   details.get("step_total", 0),
        "participants": details.get("participants", 0),
        "vin":          details.get("vin", ""),
        "cadastral":    details.get("cadastral", ""),
        "address":      details.get("address", ""),
        "analytics_text": details.get("analytics_text", ""),
        "rosreestr_data": details.get("rosreestr_data", ""),
        "pdf_from_egrn": details.get("pdf_from_egrn", False),
        "pdf_download_failed": details.get("pdf_download_failed", False),
        "egrn_parsed":  details.get("egrn_parsed", {}),
    })
    if details.get("pdf_from_egrn") and details.get("pdf_text"):
        lot["egrn_pdf_text"] = details["pdf_text"]
    for key in ("auction_format", "application_deadline", "deposit",
                "next_reduction_date", "next_reduction_price", "area_sqm", "area_sotka"):
        if key in details:
            lot[key] = details[key]
    lot["parsed_at"] = datetime.now().isoformat()
    if details.get("title_full"):
        lot["title"] = details["title_full"]
    lot["category"] = detect_type(
        f"{lot['title']} {lot.get('description', '')[:500]}"
    )
    if lot["category"] == "авто":
        from analyzer import parse_auto_meta
        lot.update(parse_auto_meta(f"{lot['title']} {lot.get('description', '')}"))


async def login(page) -> bool:
    if not LOGIN or not PASSWORD:
        return False
    try:
        await page.goto("https://tbankrot.ru/", timeout=30000)
        await page.wait_for_timeout(2000)
        if await page.locator("text=Выйти").count():
            print("[login] OK already")
            return True
        await page.click("text=Войти", timeout=8000)
        await page.wait_for_timeout(1500)
        for tab in ("Email", "E-mail", "Почта", "email"):
            try:
                t = page.locator(f"text={tab}").first
                if await t.count() and await t.is_visible():
                    await t.click()
                    await page.wait_for_timeout(400)
                    break
            except Exception:
                pass
        email_sel = "input[type='email'], input[name*='mail'], input[placeholder*='mail' i]"
        await page.wait_for_selector(email_sel, timeout=8000)
        await page.fill(email_sel, LOGIN)
        await page.wait_for_timeout(400)
        try:
            await page.locator("input[type='password']:visible").first.fill(PASSWORD, timeout=5000)
        except Exception:
            await page.locator("input[type='password']").first.fill(PASSWORD, force=True)
        await page.wait_for_timeout(400)
        for btn in await page.query_selector_all("button"):
            if (await btn.inner_text()).strip() == "Войти":
                await btn.click()
                break
        await page.wait_for_timeout(3500)
        if await page.locator("text=Выйти").count():
            print("[login] OK")
            return True
        await page.goto("https://tbankrot.ru/", timeout=20000)
        await page.wait_for_timeout(1500)
        ok = await page.locator("text=Выйти").count() > 0
        if not ok:
            ok = await page.locator("text=Войти").count() == 0
        if not ok:
            cookies = await page.context.cookies()
            ok = any(
                c.get("name", "").lower() in ("session", "sessionid", "auth", "token", "jwt")
                or "session" in c.get("name", "").lower()
                for c in cookies
            )
        print("[login] OK" if ok else "[login] NOT CONFIRMED")
        return ok
    except Exception as e:
        print(f"[login] FAIL: {e}")
        return False


async def _fetch_pdf_bytes(page, url: str) -> bytes:
    """Скачивает PDF с cookies сессии браузера (ctx.request без login → 403)."""
    try:
        data = await page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: 'include'});
                if (!r.ok) return {ok: false, status: r.status};
                const buf = await r.arrayBuffer();
                return {ok: true, bytes: Array.from(new Uint8Array(buf))};
            }""",
            url,
        )
        if data and data.get("ok") and data.get("bytes"):
            return bytes(data["bytes"])
        if data and data.get("status") in (403, 404):
            return b""
    except Exception:
        pass
    return b""


async def _try_egrn_pdf(lot, page, ctx):
    from analyzer import apply_egrn_to_lot
    for pdf_url in (
        f"https://files.tbankrot.ru/egrn_files/{lot['id']}.pdf",
        f"https://tbankrot.ru/files/egrn/{lot['id']}.pdf",
    ):
        try:
            raw = await _fetch_pdf_bytes(page, pdf_url)
            if not raw and ctx:
                resp = await ctx.request.get(pdf_url)
                raw = await resp.body() if resp.status == 200 else b""
            if raw and b"%PDF" in raw[:10]:
                with pdfplumber.open(io.BytesIO(raw)) as pdf:
                    text = "\n".join(p.extract_text() or "" for p in pdf.pages[:8])[:6000]
                if text and len(text) > 100:
                    apply_egrn_to_lot(lot, text, True)
                    print(f"    📄 ЕГРН скачан ({len(text)} симв.)")
                    return
            if not raw:
                lot["pdf_download_failed"] = True
        except Exception:
            continue


async def collect(page, regions, max_pages=MAX_PAGES) -> list:
    lots, seen = [], set()
    for region in regions:
        for pg in range(1, max_pages+1):
            try:
                await page.goto(
                    f"https://tbankrot.ru/torgi/r/{region}?page={pg}",
                    timeout=30000
                )
                await page.wait_for_timeout(1500)
                links = await page.query_selector_all("a[href*='/item?id=']")
                added = 0
                for link in links:
                    try:
                        href = await link.get_attribute("href") or ""
                        if not href.startswith("http"):
                            href = "https://tbankrot.ru" + href
                        m = re.search(r'id=(\d+)', href)
                        if not m or m.group(1) in seen: continue
                        seen.add(m.group(1))
                        title = (await link.inner_text()).strip()[:200]
                        lots.append({
                            "id": m.group(1), "title": title,
                            "url": href, "region": region,
                            "is_extra": region not in REGIONS_MAIN,
                            "source": "Т-Банкрот",
                            "pdf_text":"","description":"",
                            "price":0,"step_current":0,"step_total":0,
                            "participants":0,"vin":"","cadastral":"",
                        })
                        added += 1
                    except: continue
                print(f"  {region} стр.{pg}: +{added} (итого {len(lots)})")
                if len(lots) >= max_pages * 20: break
            except: break
    return lots


async def enrich_light(lot, page):
    """Лёгкий проход: только карточка лота, без analytics/Росреестра/PDF."""
    details = await get_lot_details(lot["url"], page, light=True)
    _apply_details(lot, details)


async def enrich_heavy(lot, page, ctx):
    """Тяжёлый проход: analytics, Росреестр, PDF ЕГРН."""
    cat = lot.get("category", "прочее")
    if cat != "авто":
        if not lot.get("analytics_text"):
            try:
                m = re.search(r'id=(\d+)', lot.get("url", ""))
                if m:
                    await page.goto(f"https://tbankrot.ru/analytics/{m.group(1)}", timeout=12000)
                    await page.wait_for_timeout(800)
                    analytics = await page.inner_text("body")
                    if len(analytics) > 200:
                        lot["analytics_text"] = analytics[:2000]
            except Exception:
                pass
        if is_real_estate(cat) and lot.get("cadastral") and not lot.get("rosreestr_data"):
            try:
                lot["rosreestr_data"] = await asyncio.wait_for(
                    get_rosreestr_data(lot["cadastral"]), timeout=8,
                )
            except asyncio.TimeoutError:
                lot["rosreestr_timeout"] = True
            except Exception:
                pass
        if not lot.get("egrn_pdf_text"):
            try:
                await asyncio.wait_for(_try_egrn_pdf(lot, page, ctx), timeout=PDF_TIMEOUT)
            except asyncio.TimeoutError:
                lot["pdf_timeout"] = True


async def enrich(lot, page, ctx, heavy: bool = True):
    """Полный enrich для одиночного анализа (бот). Login — снаружи, один раз."""
    details = await get_lot_details(lot["url"], page, light=not heavy)
    _apply_details(lot, details)
    if heavy and lot.get("category") != "авто" and not lot.get("egrn_pdf_text"):
        try:
            await asyncio.wait_for(_try_egrn_pdf(lot, page, ctx), timeout=PDF_TIMEOUT)
        except asyncio.TimeoutError:
            lot["pdf_timeout"] = True


def fmt_block(lot, an, i=0) -> str:
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
              "1️⃣1️⃣","1️⃣2️⃣","1️⃣3️⃣","1️⃣4️⃣","1️⃣5️⃣"]
    medal  = medals[i] if i < len(medals) else f"#{i+1}"
    step    = f"\n📊 {an['step']}" if an.get('step') else ""
    urgency = f"\n{an['urgency']}" if an.get('urgency') else ""
    mkt    = f"\n_📊 {an['market_comment']}_" if an.get('market_comment') and not an.get('market_known') else ""
    extra  = f"\n{an['extra_checks']}" if an.get('extra_checks') else ""
    check  = f"\n🔎 _{an['what_to_check']}_" if an.get('what_to_check') else ""
    encumb = f"\n🔒 {an['encumbrances']}" if an.get('encumbrances') else ""
    exit_s = f"\n🚪 Выход: {an['exit_strategy']}" if an.get('exit_strategy') else ""
    doc_st = f"\n📄 _{an['document_status']}_" if an.get('document_status') else ""
    legal  = f"\n📋 {an['legal_text']}" if an.get('legal_text') else ""
    auto_s = f"\n🚗 {an['auto_summary']}" if an.get('auto_summary') else ""
    simple = f"\n\n🎯 *{an['verdict_simple']}*" if an.get('verdict_simple') else ""
    region_note = " 🌍" if lot.get("is_extra") else ""
    price_line = an.get("price_line") or format_price_line(an)
    return (
        f"{medal} *{an.get('score_label','5/10')}*"
        f" | {an.get('invest_text','📈 потенциал: средний')}"
        f" | {an.get('risk_text','🟡 риск: средний')}"
        f"{region_note}\n"
        f"{lot.get('title','')[:65]}\n"
        f"{price_line}"
        f"{mkt}{step}{urgency}\n"
        f"💧 Ликвидность: {an.get('liquidity_text','—')}\n"
        f"📈 {an.get('roi_text','нет данных')}"
        f"{doc_st}{legal}{auto_s}{encumb}{exit_s}"
        f"{extra}\n"
        f"{an.get('action_emoji','⚠️')} *{an.get('verdict_label') or an.get('action','?')}*\n"
        f"💡 _{an.get('strategy','')}_"
        f"{simple}"
        f"{check}\n"
        f"🔗 {lot.get('url','')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

def build_msgs(cat_key, results) -> list:
    cat  = CATEGORIES[cat_key]
    now  = datetime.now().strftime("%d.%m.%Y")
    go   = sum(1 for _,a in results if a.get("action")=="ВХОДИТЬ СЕЙЧАС")
    wait = sum(1 for _,a in results if a.get("action")=="ЖДАТЬ СНИЖЕНИЯ")
    header = (
        f"{cat['icon']} *{cat['label']} — {now}*\n"
        f"Лотов: {len(results)} | 🟢 {go} войти | ⏳ {wait} ждать\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    # Топ-3 лота кратко для быстрого обзора
    top3 = results[:3]
    quick_view = "📌 *Топ лоты:*\n"
    for j,(l,a) in enumerate(top3):
        disc = a.get('discount_pct','0')
        disc_s = f"-{disc}%" if disc not in ('0','?') else ""
        quick_view += f"{'🥇🥈🥉'[j]} {l.get('title','')[:40]} | {a.get('price','—')} {disc_s} | {a.get('action_emoji','⚠️')} {a.get('action','?')}\n"
    quick_view += "\n"
    header = header + quick_view
    parts, current = [], header
    for i,(lot,an) in enumerate(results[:TOP_N]):
        block = fmt_block(lot, an, i)
        if len(current)+len(block) > 3800:
            parts.append(current); current = block
        else:
            current += block
    parts.append(current)
    return parts


async def send(msgs, reply_markup=None):
    bot = telegram.Bot(token=TG_TOKEN)
    for msg in msgs:
        try:
            await bot.send_message(
                chat_id=TG_CHAT, text=msg,
                parse_mode="Markdown", disable_web_page_preview=True,
                reply_markup=reply_markup
            )
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  TG: {e}")


async def send_daily_digest(results, all_lots_count, alerts, skipped,
                            partial=False, stats=None, phase_note=""):
    total = sum(len(v) for v in results.values())
    go = sum(sum(1 for _, a in v if a.get("action") == "ВХОДИТЬ СЕЙЧАС")
             for v in results.values())
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    partial_note = (
        "\n\n⚠️ _Частичный дайджест: не все лоты успели пройти тяжёлый анализ до лимита времени._"
        if partial else ""
    )
    stats_line = ""
    if stats:
        stats_line = (
            f"\n⏱ Сбор: {stats.get('collect_sec', 0):.0f}с | "
            f"лёгкий: {stats.get('light_sec', 0):.0f}с ({stats.get('light_n', 0)} лот.) | "
            f"тяжёлый: {stats.get('heavy_sec', 0):.0f}с ({stats.get('heavy_n', 0)} лот.)"
        )
    await send([
        f"🌅 *Доброе утро! Дайджест {now}*{phase_note}{partial_note}\n\n"
        f"🔍 Изучено: *{all_lots_count}* лотов\n"
        f"⭐ В дайджесте: *{total}* лотов\n"
        f"🟢 Входить сейчас: *{go}*\n"
        f"🔔 Горячих алертов: *{alerts}*\n"
        f"⏭ Отсеяно: *{skipped}*{stats_line}\n\n"
        + "\n".join(
            f"{CATEGORIES[k]['icon']} {CATEGORIES[k]['label']}: {len(v)} лотов"
            for k, v in results.items() if v
        )
        + "\n\n_Детальный разбор по категориям ниже ↓_"
    ])
    await asyncio.sleep(2)

    for cat_key in ["квартира", "коммерция", "дом", "земля", "авто", "гараж", "бизнес", "прочее"]:
        v = results.get(cat_key, [])
        if not v:
            continue
        v.sort(key=lambda x: float(x[1].get("total_score", 0)), reverse=True)
        cat = CATEGORIES[cat_key]
        print(f"\n{cat['icon']} Отправляем {cat['label']}: {len(v)}")
        await send(build_msgs(cat_key, v))
        await asyncio.sleep(2)


def _build_results(scored, heavy_map):
    """Собирает results из лёгкого прохода + тяжёлых замен."""
    out = {k: [] for k in CATEGORIES}
    for lot, light_an, _score, cat in scored:
        if lot["id"] in heavy_map:
            lot, an = heavy_map[lot["id"]]
        else:
            an = light_an
        key = cat if cat in out else "прочее"
        out[key].append((lot, an))
    return out


async def run(cats=None, include_extra=True, daily=True):
    init_db()
    if cats is None:
        cats = DEFAULT_CATS
    start_ts = time.time()
    stats = {
        "collect_sec": 0, "light_sec": 0, "heavy_sec": 0,
        "light_n": 0, "heavy_n": 0, "light_timeouts": 0, "heavy_timeouts": 0,
    }
    print(f"\n{'='*55}")
    print(f"🤖 Агент v10.0: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print(f"Категории: {', '.join(cats)} | Мин.балл: {MIN_SCORE}")
    print(f"Бюджет: {RUN_BUDGET_SEC}s | Тяжёлых лотов макс: {MAX_HEAVY_LOTS} | ≥{HEAVY_MIN_SCORE} балл")
    print(f"{'='*55}\n")

    results = {k: [] for k in CATEGORIES}
    skipped = alerts = 0
    digest_sent = False
    partial = False
    heavy_map = {}

    async def _flush_if_needed(scored_list, reason=""):
        nonlocal digest_sent, partial
        if digest_sent or not daily or not _should_flush(start_ts):
            return False
        partial = True
        res = _build_results(scored_list, heavy_map)
        print(f"\n📤 Ранняя отправка дайджеста ({reason}), осталось {_budget_left(start_ts):.0f}с")
        await send_daily_digest(
            res, len(all_lots), alerts, skipped,
            partial=True, stats=stats,
            phase_note=f"\n_{reason}_",
        )
        digest_sent = True
        return True

    regions = list(REGIONS_MAIN)
    if include_extra:
        regions += REGIONS_EXTRA

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()
        await login(page)

        print("\n📡 Собираем лоты...")
        t0 = time.time()
        main_lots = await collect(page, REGIONS_MAIN, MAX_PAGES)
        extra_lots = await collect(page, REGIONS_EXTRA, EXTRA_MAX_PAGES) if include_extra else []
        all_lots = main_lots + extra_lots
        stats["collect_sec"] = time.time() - t0
        print(f"✅ Собрано: {len(all_lots)} лотов за {stats['collect_sec']:.0f}с")
        print(f"   (main: {len(main_lots)} × до {MAX_PAGES} стр., extra: {len(extra_lots)} × {EXTRA_MAX_PAGES} стр.)\n")

        # ── Фаза 1: лёгкий проход ──
        print("🔍 Фаза 1 — лёгкий проход (карточка + балл, без PDF/Groq)...")
        scored = []  # (lot, analysis, score)
        light_t0 = time.time()

        for i, lot in enumerate(all_lots):
            if await _flush_if_needed(scored, "лимит времени в фазе 1"):
                break
            print(f"  [L {i+1}/{len(all_lots)}] ", end="", flush=True)
            try:
                async def _light_one():
                    await enrich_light(lot, page)
                    cat = lot.get("category", "прочее")
                    if cat not in cats:
                        return None
                    dedup = record_digest_lot(lot["id"], lot.get("price", 0))
                    if dedup.get("note"):
                        lot["dedup_note"] = dedup["note"]
                    an = await analyze_lot(lot, light=True)
                    score = float(an.get("total_score", 0))
                    if lot.get("is_extra") and score < 7.0:
                        return None
                    return cat, an, score

                out = await asyncio.wait_for(_light_one(), timeout=LOT_TIMEOUT_LIGHT)
                if out is None:
                    print(f"skip ({lot.get('category', '?')})")
                    skipped += 1
                    continue
                cat, an, score = out
                scored.append((lot, an, score, cat))
                stats["light_n"] += 1
                extra_note = "🌍" if lot.get("is_extra") else ""
                print(f"{cat:12} | ⭐{score:.1f} | {an.get('action', '?')} {extra_note}")
            except asyncio.TimeoutError:
                stats["light_timeouts"] += 1
                print("timeout — пропуск")
            except Exception as e:
                print(f"ошибка: {e}")
            await asyncio.sleep(0.15)

        stats["light_sec"] = time.time() - light_t0
        avg_light = stats["light_sec"] / max(stats["light_n"], 1)
        print(f"\n📊 Фаза 1: {stats['light_n']} лотов за {stats['light_sec']:.0f}с "
              f"(~{avg_light:.1f}с/лот, таймаутов: {stats['light_timeouts']})")

        # Отбор топ-кандидатов для тяжёлого анализа
        scored.sort(key=lambda x: x[2], reverse=True)
        # Приоритет: все 9+ баллов, затем остальные ≥ HEAVY_MIN_SCORE
        must_heavy = [(lot, an, score, cat) for lot, an, score, cat in scored if score >= 9.0]
        must_ids = {lot["id"] for lot, _, _, _ in must_heavy}
        rest_heavy = [
            (lot, an, score, cat) for lot, an, score, cat in scored
            if score >= HEAVY_MIN_SCORE and lot["id"] not in must_ids
        ]
        heavy_queue = (must_heavy + rest_heavy)[:MAX_HEAVY_LOTS]
        print(f"🎯 В тяжёлый анализ: {len(heavy_queue)} лотов (балл ≥ {HEAVY_MIN_SCORE})\n")

        heavy_map = {}  # lot id -> (lot, an)
        heavy_t0 = time.time()

        # ── Фаза 2: тяжёлый проход ──
        if heavy_queue and not digest_sent:
            print("🔬 Фаза 2 — PDF ЕГРН + Groq для топ-кандидатов...")
            for j, (lot, light_an, score, cat) in enumerate(heavy_queue):
                if await _flush_if_needed(scored, "лимит времени в фазе 2"):
                    break
                print(f"  [H {j+1}/{len(heavy_queue)}] id={lot['id']} ⭐{score:.1f} ", end="", flush=True)
                try:
                    async def _heavy_one():
                        await enrich_heavy(lot, page, ctx)
                        return await analyze_lot(lot, light=False)

                    an = await asyncio.wait_for(_heavy_one(), timeout=LOT_TIMEOUT_HEAVY)
                    heavy_map[lot["id"]] = (lot, an)
                    stats["heavy_n"] += 1
                    print(f"→ ⭐{float(an.get('total_score', score)):.1f} | {an.get('action', '?')}")

                    new_score = float(an.get("total_score", 0))
                    if new_score >= 9.0:
                        lot_id = lot.get("id", "")
                        kb = lot_action_keyboard(lot_id, an, lot, lot.get("parsed_at"))
                        await send([format_short_lot_message(lot, an, "ГОРЯЧИЙ ЛОТ")], reply_markup=kb)
                        alerts += 1
                except asyncio.TimeoutError:
                    stats["heavy_timeouts"] += 1
                    heavy_map[lot["id"]] = (lot, light_an)
                    print("timeout — оставляем лёгкий анализ")
                except Exception as e:
                    heavy_map[lot["id"]] = (lot, light_an)
                    print(f"ошибка: {e}")
                await asyncio.sleep(0.2)

        stats["heavy_sec"] = time.time() - heavy_t0

        results = _build_results(scored, heavy_map)

        await browser.close()

    elapsed = time.time() - start_ts
    print(f"\n⏱ Итого: {elapsed:.0f}s / бюджет {RUN_BUDGET_SEC}s")
    print(f"   Сбор {stats['collect_sec']:.0f}s | Лёгкий {stats['light_sec']:.0f}s | "
          f"Тяжёлый {stats['heavy_sec']:.0f}s")

    if daily and not digest_sent:
        await send_daily_digest(
            results, len(all_lots), alerts, skipped,
            partial=partial, stats=stats,
        )
        digest_sent = True

    print(f"\n✅ Готово! Алертов: {alerts} | Отсеяно: {skipped} | Частичный: {partial}")


def daily_job():
    asyncio.run(run(cats=DEFAULT_CATS, include_extra=True, daily=True))


if __name__ == "__main__":
    import sys
    if "--now" in sys.argv:
        asyncio.run(run(cats=DEFAULT_CATS, include_extra=True, daily=True))
    elif "--bot" in sys.argv:
        from bot_handler import run_bot
        run_bot()
    elif "--cat" in sys.argv:
        idx = sys.argv.index("--cat")
        cat = sys.argv[idx+1] if idx+1 < len(sys.argv) else "квартира"
        asyncio.run(run(cats={cat}, include_extra=True, daily=True))
    else:
        print("⏰ Запуск в 08:00 ежедневно")
        schedule.every().day.at("08:00").do(daily_job)
        while True:
            schedule.run_pending()

"""
Агент v9.0 — финальный
- Только лоты 8+ баллов
- Недвижимость + земля по умолчанию
- Другие регионы если 9+
- Скачивает ЕГРН отчёты
- Сравнивает с рынком
"""
import os, asyncio, schedule, time, pdfplumber, io, re
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import telegram
from analyzer import analyze_lot, detect_type, get_lot_details, MIN_SCORE

load_dotenv()

LOGIN    = os.getenv("TBANKROT_LOGIN")
PASSWORD = os.getenv("TBANKROT_PASSWORD")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

REGIONS_MAIN  = ["moskva", "moskovskaya-oblast"]
REGIONS_EXTRA = ["sankt-peterburg","krasnodar","ekaterinburg","novosibirsk"]

MAX_PAGES = 12
TOP_N     = 15

CATEGORIES = {
    "квартира":  {"icon":"🏠","label":"Квартиры",                 "default":True},
    "дом":       {"icon":"🏡","label":"Дома и дачи",              "default":True},
    "коммерция": {"icon":"🏢","label":"Коммерческая недвижимость","default":True},
    "земля":     {"icon":"🌱","label":"Земельные участки",        "default":True},
    "авто":      {"icon":"🚗","label":"Транспорт",                "default":False},
    "гараж":     {"icon":"🅿️","label":"Гаражи",                  "default":False},
    "бизнес":    {"icon":"💼","label":"Бизнес",                   "default":False},
    "прочее":    {"icon":"📦","label":"Прочее",                   "default":False},
}

DEFAULT_CATS = {k for k,v in CATEGORIES.items() if v["default"]}


async def login(page) -> bool:
    try:
        await page.goto("https://tbankrot.ru/", timeout=30000)
        await page.wait_for_timeout(2000)
        await page.click("text=Войти", timeout=8000)
        await page.wait_for_timeout(2000)
        await page.wait_for_selector(
            "input[type='email'], input[placeholder*='mail']",
            timeout=8000
        )
        await page.fill("input[type='email'], input[placeholder*='mail']", LOGIN)
        await page.wait_for_timeout(500)
        await page.fill("input[type='password']", PASSWORD)
        await page.wait_for_timeout(500)
        for btn in await page.query_selector_all("button"):
            if (await btn.inner_text()).strip() == "Войти":
                await btn.click(); break
        await page.wait_for_timeout(3000)
        print("✅ Авторизован")
        return True
    except Exception as e:
        print(f"⚠️ Авторизация: {e}")
        return False


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


async def enrich(lot, page, ctx):
    """Заходит на страницу лота и скачивает ЕГРН PDF"""
    details = await get_lot_details(lot["url"], page)
    lot.update({
        "price":        details.get("price", 0),
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
        f"{lot['title']} {lot.get('description','')[:300]}"
    )
    # Скачиваем ЕГРН PDF
    try:
        resp = await ctx.request.get(
            f"https://files.tbankrot.ru/egrn_files/{lot['id']}.pdf"
        )
        if resp.status == 200:
            with pdfplumber.open(io.BytesIO(await resp.body())) as pdf:
                lot["pdf_text"] = "\n".join(
                    p.extract_text() or "" for p in pdf.pages[:5]
                )[:4000]
            print(f"    📄 ЕГРН скачан")
    except: pass
    # Если PDF не скачался — берём данные со страницы лота
    if not lot.get("pdf_text") and lot.get("description"):
        lot["pdf_text"] = lot["description"][:2000]
        print(f"    📝 Используем описание страницы вместо PDF")


def fmt_block(lot, an, i=0) -> str:
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
              "1️⃣1️⃣","1️⃣2️⃣","1️⃣3️⃣","1️⃣4️⃣","1️⃣5️⃣"]
    medal  = medals[i] if i < len(medals) else f"#{i+1}"
    disc   = an.get('discount_pct','0')
    disc_s = f" | -{disc}%" if disc not in ('0','?','') else ""
    step   = f"\n📊 {an['step']}" if an.get('step') else ""
    mkt    = f"\n_📊 {an['market_comment']}_" if an.get('market_comment') else ""
    extra  = f"\n{an['extra_checks']}" if an.get('extra_checks') else ""
    check  = f"\n🔎 _{an['what_to_check']}_" if an.get('what_to_check') else ""
    encumb = f"\n🔒 {an['encumbrances']}" if an.get('encumbrances') else ""
    exit_s = f"\n🚪 Выход: {an['exit_strategy']}" if an.get('exit_strategy') else ""
    region_note = " 🌍" if lot.get("is_extra") else ""
    return (
        f"{medal} *{an.get('score_label','5/10')}*"
        f" | {an.get('invest_text','📈 потенциал: средний')}"
        f" | {an.get('risk_text','🟡 риск: средний')}"
        f"{region_note}\n"
        f"{lot.get('title','')[:65]}\n"
        f"💰 {an.get('price','—')} → рынок {an.get('market_price','—')}{disc_s}"
        f"{mkt}{step}\n"
        f"💧 Ликвидность: {an.get('liquidity_text','—')}\n"
        f"📈 {an.get('roi_text','нет данных')}\n"
        f"⚖️ {an.get('legal_text','—')}"
        f"{encumb}"
        f"{exit_s}"
        f"{extra}\n"
        f"{an.get('action_emoji','⚠️')} *{an.get('action','?')}*\n"
        f"💡 _{an.get('strategy','')}_"
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
    parts, current = [], header
    for i,(lot,an) in enumerate(results[:TOP_N]):
        block = fmt_block(lot, an, i)
        if len(current)+len(block) > 3800:
            parts.append(current); current = block
        else:
            current += block
    parts.append(current)
    return parts


async def send(msgs):
    bot = telegram.Bot(token=TG_TOKEN)
    for msg in msgs:
        try:
            await bot.send_message(
                chat_id=TG_CHAT, text=msg,
                parse_mode="Markdown", disable_web_page_preview=True
            )
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  TG: {e}")


async def run(cats=None, include_extra=True, daily=True):
    if cats is None: cats = DEFAULT_CATS
    print(f"\n{'='*55}")
    print(f"🤖 Агент v9.0: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print(f"Категории: {', '.join(cats)} | Мин.балл: {MIN_SCORE}")
    print(f"{'='*55}\n")

    results = {k: [] for k in CATEGORIES}
    skipped = alerts = 0

    regions = list(REGIONS_MAIN)
    if include_extra: regions += REGIONS_EXTRA

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()
        await login(page)

        print("\n📡 Собираем лоты...")
        main_lots  = await collect(page, REGIONS_MAIN)
        extra_lots = await collect(page, REGIONS_EXTRA, 3) if include_extra else []
        all_lots   = main_lots + extra_lots
        print(f"✅ Собрано: {len(all_lots)} лотов\n")

        print("🔍 Анализируем...")
        for i, lot in enumerate(all_lots):
            print(f"  [{i+1}/{len(all_lots)}] ", end="", flush=True)
            try:
                await enrich(lot, page, ctx)
                cat = lot.get("category","прочее")

                if cat not in cats:
                    print(f"не та категория ({cat})")
                    skipped += 1
                    continue

                an    = await analyze_lot(lot)
                score = float(an.get("total_score",0))

                if lot.get("is_extra") and score < 7.0:
                    skipped += 1
                    continue

                if cat not in results: cat = "прочее"
                results[cat].append((lot, an))
                extra_note = "🌍" if lot.get("is_extra") else ""
                print(f"{cat:12} | ⭐{score:.1f} | {an.get('action','?')} {extra_note}")

                if score >= 9.0:
                    disc = an.get("discount_pct","?")
                    rn   = f"\n🌍 Регион: {lot.get('region','')}" if lot.get("is_extra") else ""
                    await send([
                        f"🔔 *ГОРЯЧИЙ ЛОТ! Балл {score}/10*{rn}\n\n"
                        f"{lot.get('title','')[:70]}\n"
                        f"💰 {an.get('price','—')} → рынок {an.get('market_price','—')}"
                        f"{f' (-{disc}%)' if disc not in ('?','0') else ''}\n"
                        f"{an.get('action_emoji','⚠️')} *{an.get('action','?')}*\n"
                        f"💡 _{an.get('strategy','')}_ \n\n"
                        f"🔗 {lot.get('url','')}"
                    ])
                    alerts += 1

            except Exception as e:
                print(f"ошибка: {e}")
            await asyncio.sleep(0.5)

        await browser.close()

    if daily:
        total = sum(len(v) for v in results.values())
        go    = sum(sum(1 for _,a in v if a.get("action")=="ВХОДИТЬ СЕЙЧАС")
                    for v in results.values())
        now   = datetime.now().strftime("%d.%m.%Y %H:%M")

        await send([
            f"🌅 *Доброе утро! Дайджест {now}*\n\n"
            f"🔍 Изучено: *{len(all_lots)}* лотов\n"
            f"⭐ Балл {MIN_SCORE}+: *{total}* лотов\n"
            f"🟢 Входить сейчас: *{go}*\n"
            f"🔔 Горячих алертов: *{alerts}*\n\n"
            + "\n".join(
                f"{CATEGORIES[k]['icon']} {CATEGORIES[k]['label']}: {len(v)} лотов"
                for k,v in results.items() if v
            ) +
            "\n\n_Детальный разбор по категориям ниже ↓_"
        ])
        await asyncio.sleep(2)

        for cat_key in ["квартира","коммерция","дом","земля","авто","гараж","бизнес","прочее"]:
            v = results.get(cat_key,[])
            if not v: continue
            v.sort(key=lambda x: float(x[1].get("total_score",0)), reverse=True)
            cat = CATEGORIES[cat_key]
            print(f"\n{cat['icon']} Отправляем {cat['label']}: {len(v)}")
            await send(build_msgs(cat_key, v))
            await asyncio.sleep(2)

    print(f"\n✅ Готово! Алертов: {alerts} | Отсеяно: {skipped}")


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

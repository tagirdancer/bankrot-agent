"""
Агент v8.0 — финальная конфигурация
- Дайджест только в 8:00
- По умолчанию: недвижимость + земля
- Другие регионы если балл 9+
- Кнопка запроса в любое время
"""
import os, asyncio, schedule, time, pdfplumber, io, re
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import telegram
from analyzer import analyze_lot, detect_type, get_lot_details
from platforms import get_all_platform_lots
from database import (init_db, save_lot, get_price_trend,
                      was_notified_recently, mark_notified,
                      save_stats, add_to_portfolio)

load_dotenv()

TBANKROT_LOGIN    = os.getenv("TBANKROT_LOGIN")
TBANKROT_PASSWORD = os.getenv("TBANKROT_PASSWORD")
TG_TOKEN          = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID")

# Основные регионы
REGIONS_MAIN  = ["moskva", "moskovskaya-oblast"]
# Дополнительные регионы — только если балл 9+
REGIONS_EXTRA = ["sankt-peterburg", "leningradskaya-oblast",
                 "krasnodar", "ekaterinburg", "novosibirsk"]

MAX_PAGES   = 10
MAX_LOTS    = 200
TOP_N       = 15
ALERT_SCORE = 9.0  # Только реально горячие

# Категории по умолчанию (ежедневный дайджест)
DEFAULT_CATEGORIES = {"квартира", "дом", "коммерция", "земля"}

CATEGORIES = {
    "квартира":  {"icon":"🏠","label":"Квартиры",                 "min_score":0,   "default":True},
    "дом":       {"icon":"🏡","label":"Дома и дачи",              "min_score":0,   "default":True},
    "коммерция": {"icon":"🏢","label":"Коммерческая недвижимость","min_score":0,   "default":True},
    "земля":     {"icon":"🌱","label":"Земельные участки",        "min_score":7.0, "default":True},
    "авто":      {"icon":"🚗","label":"Транспорт",                "min_score":8.5, "default":False},
    "гараж":     {"icon":"🅿️","label":"Гаражи",                  "min_score":8.5, "default":False},
    "бизнес":    {"icon":"💼","label":"Бизнес/оборудование",      "min_score":8.5, "default":False},
    "прочее":    {"icon":"📦","label":"Прочее",                   "min_score":9.0, "default":False},
}


async def login_tbankrot(page) -> bool:
    try:
        print("🔐 Авторизуемся...")
        await page.goto("https://tbankrot.ru/", timeout=30000)
        await page.wait_for_timeout(2000)
        await page.click("text=Войти", timeout=8000)
        await page.wait_for_timeout(2000)
        await page.wait_for_selector(
            "input[type='email'], input[placeholder*='mail'], input[placeholder*='огин']",
            timeout=8000
        )
        await page.fill(
            "input[type='email'], input[placeholder*='mail'], input[placeholder*='огин']",
            TBANKROT_LOGIN
        )
        await page.wait_for_timeout(500)
        await page.fill("input[type='password']", TBANKROT_PASSWORD)
        await page.wait_for_timeout(500)
        btns = await page.query_selector_all("button")
        for btn in btns:
            if (await btn.inner_text()).strip() == "Войти":
                await btn.click()
                break
        await page.wait_for_timeout(3000)
        print("✅ Авторизован")
        return True
    except Exception as e:
        print(f"⚠️ Авторизация: {e}")
        return False


async def collect_lots(page, regions: list, max_pages: int = MAX_PAGES) -> list:
    lots, seen = [], set()
    for region in regions:
        for pg in range(1, max_pages + 1):
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
                        if not m or m.group(1) in seen:
                            continue
                        seen.add(m.group(1))
                        title = (await link.inner_text()).strip()[:200]
                        lots.append({
                            "id": m.group(1), "title": title,
                            "url": href, "region": region,
                            "source": "Т-Банкрот",
                            "is_extra_region": region not in REGIONS_MAIN,
                            "pdf_text": "", "description": "",
                            "price": 0, "step_current": 0, "step_total": 0,
                            "participants": 0, "vin": "", "cadastral": "",
                        })
                        added += 1
                    except:
                        continue
                print(f"  {region} стр.{pg}: +{added} (всего {len(lots)})")
                if len(lots) >= max_pages * 15:
                    break
            except:
                break
    return lots


async def enrich(lot: dict, page, ctx):
    details = await get_lot_details(lot["url"], page)
    lot.update({
        "price":        details.get("price", lot.get("price", 0)),
        "description":  details.get("description", ""),
        "step_current": details.get("step_current", 0),
        "step_total":   details.get("step_total", 0),
        "participants": details.get("participants", 0),
        "vin":          details.get("vin", ""),
        "cadastral":    details.get("cadastral", ""),
    })
    if details.get("title_full"):
        lot["title"] = details["title_full"]
    lot["category"] = detect_type(
        f"{lot['title']} {lot.get('description','')[:300]}"
    )
    try:
        resp = await ctx.request.get(
            f"https://files.tbankrot.ru/egrn_files/{lot['id']}.pdf"
        )
        if resp.status == 200:
            with pdfplumber.open(io.BytesIO(await resp.body())) as pdf:
                lot["pdf_text"] = "\n".join(
                    p.extract_text() or "" for p in pdf.pages[:4]
                )[:3000]
    except:
        pass

def build_block(lot: dict, an: dict, trend: dict = None, index: int = 0) -> str:
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
              "1️⃣1️⃣","1️⃣2️⃣","1️⃣3️⃣","1️⃣4️⃣","1️⃣5️⃣"]
    medal = medals[index] if index < len(medals) else f"#{index+1}"
    disc  = f" | -{an.get('discount_pct','?')}%" if an.get('discount_pct','?') not in ('?','0') else ""
    step  = f"\n📊 {an['step']}" if an.get('step') else ""
    comp  = f"\n👥 {an['competition']}" if an.get('competition') else ""
    extra = f"\n{an['extra_checks']}" if an.get('extra_checks') else ""
    check = f"\n🔎 _{an['what_to_check']}_" if an.get('what_to_check') else ""
    trend_str = ""
    if trend and trend.get("drop_pct", 0) > 0:
        trend_str = f"\n📉 История: -{trend['drop_pct']}% за {trend['days_tracked']} дн."
    extra_region = " 🌍 Другой регион" if lot.get("is_extra_region") else ""
    src   = f" [{lot.get('source','Т-Банкрот')}{extra_region}]"

    invest_icons = {"высокий":"🔥","средний":"📈","низкий":"📉"}
    risk_icons   = {"низкий":"🟢","средний":"🟡","высокий":"🟠","критический":"🔴"}

    return (
        f"{medal} *Балл: {an.get('total_score','?')}/10*"
        f" | {invest_icons.get(an.get('invest_potential','средний'),'📈')} {an.get('invest_potential','?')}"
        f" | {risk_icons.get(an.get('risk_level','средний'),'🟡')} риск: {an.get('risk_level','?')}"
        f"{src}\n"
        f"{lot.get('title','')[:65]}\n"
        f"💰 {an.get('price','—')} → рынок {an.get('market_price','—')}{disc}"
        f"{step}{trend_str}{comp}\n"
        f"💧 {an.get('liquidity_text','—')}\n"
        f"📈 {an.get('roi_text','—')}\n"
        f"⚖️ {an.get('legal_text','—')}"
        f"{extra}{check}\n"
        f"{an.get('action_emoji','⚠️')} *{an.get('action','?')}*\n"
        f"💡 _{an.get('strategy','')}_\n"
        f"🔗 {lot.get('url','')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )


def format_cat_msgs(cat_key: str, results: list) -> list:
    cat  = CATEGORIES[cat_key]
    now  = datetime.now().strftime("%d.%m.%Y")
    go   = sum(1 for _,a,_ in results if a.get("action") == "ВХОДИТЬ СЕЙЧАС")
    wait = sum(1 for _,a,_ in results if a.get("action") == "ЖДАТЬ СНИЖЕНИЯ")
    min_s = cat.get("min_score", 0)
    filt = f" | Балл {min_s}+" if min_s >= 7 else ""
    header = (
        f"{cat['icon']} *{cat['label']} — {now}*{filt}\n"
        f"Лотов: {len(results)} | 🟢 {go} войти | ⏳ {wait} ждать\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    parts, current = [], header
    for i, (lot, an, trend) in enumerate(results[:TOP_N]):
        block = build_block(lot, an, trend, i)
        if len(current) + len(block) > 3800:
            parts.append(current)
            current = block
        else:
            current += block
    parts.append(current)
    return parts


async def send_tg(messages: list):
    bot = telegram.Bot(token=TG_TOKEN)
    for msg in messages:
        try:
            await bot.send_message(
                chat_id=TG_CHAT_ID, text=msg,
                parse_mode="Markdown", disable_web_page_preview=True
            )
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  ⚠️ TG: {e}")


async def run_agent(
    categories: set = None,
    daily_report: bool = True,
    include_extra_regions: bool = True,
    min_score_override: float = None,
):
    """
    Основной запуск агента.
    categories — какие категории анализировать (None = дефолт)
    daily_report — отправлять полный отчёт или только алерты
    include_extra_regions — включать другие регионы (только балл 9+)
    """
    if categories is None:
        categories = DEFAULT_CATEGORIES

    print(f"\n{'='*55}")
    print(f"🤖 Агент v8.0: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print(f"Категории: {', '.join(categories)}")
    print(f"{'='*55}\n")

    init_db()
    results_by_cat = {k: [] for k in CATEGORIES}
    skipped = alerts_sent = 0

    # Регионы для сбора
    regions_to_scan = list(REGIONS_MAIN)
    if include_extra_regions:
        regions_to_scan += REGIONS_EXTRA

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()

        await login_tbankrot(page)

        print("\n📡 Собираем лоты...")
        # Основные регионы — полный сбор
        main_lots  = await collect_lots(page, REGIONS_MAIN, MAX_PAGES)
        # Доп. регионы — меньше страниц
        extra_lots = await collect_lots(page, REGIONS_EXTRA, 3) if include_extra_regions else []
        # Доп. площадки
        platform_lots = await get_all_platform_lots()

        all_lots = main_lots + extra_lots + platform_lots
        print(f"✅ Собрано: {len(all_lots)} лотов\n")

        print(f"🔍 Анализируем...")
        for i, lot in enumerate(all_lots):
            print(f"  [{i+1}/{len(all_lots)}] ", end="", flush=True)
            try:
                await enrich(lot, page, ctx)
                cat = lot.get("category", "прочее")

                # Для доп. регионов — анализируем только если категория подходит
                if lot.get("is_extra_region") and cat not in categories:
                    print(f"пропущен (доп. регион, не та категория)")
                    continue

                # Фильтр по нужным категориям
                if cat not in categories and cat not in CATEGORIES:
                    print(f"пропущен (категория {cat} не выбрана)")
                    skipped += 1
                    continue

                analysis = await analyze_lot(lot)
                score    = float(analysis.get("total_score", 0))

                # Для доп. регионов — только балл 9+
                if lot.get("is_extra_region") and score < ALERT_SCORE:
                    print(f"доп.регион {score:.1f} < {ALERT_SCORE} — пропущен")
                    skipped += 1
                    continue

                # Минимальный балл категории
                min_s = min_score_override or CATEGORIES.get(cat, {}).get("min_score", 0)
                if score < min_s:
                    print(f"{cat:12} | {score:.1f} < {min_s} — отсеян")
                    skipped += 1
                    continue

                save_lot(lot, analysis)
                trend = get_price_trend(lot["id"])

                if cat not in results_by_cat:
                    cat = "прочее"
                results_by_cat[cat].append((lot, analysis, trend))

                status = "🌍" if lot.get("is_extra_region") else ""
                print(f"{cat:12} | Балл:{score:.1f} | {analysis.get('action','?')} {status}")

                # Горячий алерт 9+
                if score >= ALERT_SCORE and not was_notified_recently(lot["id"], hours=12):
                    region_note = f"\n🌍 Регион: {lot.get('region','')}" if lot.get("is_extra_region") else ""
                    await send_tg([
                        f"🔔 *ГОРЯЧИЙ ЛОТ! Балл {score}/10*{region_note}\n\n"
                        f"{lot.get('title','')[:70]}\n"
                        f"💰 {analysis.get('price','—')} → рынок {analysis.get('market_price','—')}\n"
                        f"📍 {lot.get('source','Т-Банкрот')}\n"
                        f"{analysis.get('action_emoji','⚠️')} *{analysis.get('action','?')}*\n"
                        f"💡 _{analysis.get('strategy','')}_\n\n"
                        f"🔗 {lot.get('url','')}"
                    ])
                    mark_notified(lot["id"], score, analysis.get("action",""))
                    alerts_sent += 1

                if score >= 9.0:
                    add_to_portfolio(lot, analysis, "Автодобавлен — балл 9+")

            except Exception as e:
                print(f"ошибка: {e}")
            await asyncio.sleep(0.3)

        await browser.close()

    # Полный отчёт
    if daily_report:
        total = sum(len(v) for v in results_by_cat.values())
        now   = datetime.now().strftime("%d.%m.%Y %H:%M")

        summary = [
            f"🌅 *Доброе утро! Дайджест {now}*\n",
            f"🔍 Изучено: *{len(all_lots)}* лотов",
            f"✅ Отобрано: *{total}* | 🚫 Отсеяно: *{skipped}*",
            f"🔔 Горячих алертов: *{alerts_sent}*\n",
            f"📊 По категориям:"
        ]
        for cat_key, cat_results in results_by_cat.items():
            if cat_results:
                cat = CATEGORIES[cat_key]
                go  = sum(1 for _,a,_ in cat_results if a.get("action")=="ВХОДИТЬ СЕЙЧАС")
                summary.append(
                    f"{cat['icon']} {cat['label']}: {len(cat_results)} лотов | 🟢 {go} войти"
                )
        summary.append(
            "\n💬 _Нажмите кнопку в боте для запроса по любой категории_"
        )
        await send_tg(["\n".join(summary)])
        await asyncio.sleep(2)

        # Отправляем по категориям (приоритет — недвижимость)
        priority = ["квартира","коммерция","дом","земля","авто","гараж","бизнес","прочее"]
        for cat_key in priority:
            cat_results = results_by_cat.get(cat_key, [])
            if not cat_results:
                continue
            cat_results.sort(
                key=lambda x: float(x[1].get("total_score",0)), reverse=True
            )
            cat = CATEGORIES[cat_key]
            print(f"\n{cat['icon']} Отправляем {cat['label']}: {len(cat_results)}")
            await send_tg(format_cat_msgs(cat_key, cat_results))
            await asyncio.sleep(2)

    print(f"\n✅ Готово! Алертов: {alerts_sent}")
    return results_by_cat


def daily_job():
    """08:00 — полный дайджест недвижимость + земля"""
    asyncio.run(run_agent(
        categories=DEFAULT_CATEGORIES,
        daily_report=True,
        include_extra_regions=True,
    ))


if __name__ == "__main__":
    import sys
    init_db()
    print("🤖 Агент по банкротным торгам v8.0")
    print("=" * 45)

    if "--now" in sys.argv:
        # Полный запуск
        asyncio.run(run_agent(
            categories=DEFAULT_CATEGORIES,
            daily_report=True,
            include_extra_regions=True,
        ))
    elif "--bot" in sys.argv:
        from bot_handler import run_bot
        run_bot()
    elif "--cat" in sys.argv:
        # Запуск по конкретной категории
        idx = sys.argv.index("--cat")
        cat = sys.argv[idx+1] if idx+1 < len(sys.argv) else "квартира"
        asyncio.run(run_agent(
            categories={cat},
            daily_report=True,
            include_extra_regions=True,
            min_score_override=0,
        ))
    else:
        print("⏰ Расписание: только 08:00 — полный дайджест")
        print("   Доп. запросы — через Telegram бот\n")
        schedule.every().day.at("08:00").do(daily_job)
        while True:
            schedule.run_pending()
            time.sleep(60)

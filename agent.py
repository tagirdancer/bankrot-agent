"""
ИИ-агент v6.1 — исправлена авторизация Т-Банкрот
Форма входа — модальное окно на главной странице
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

REGIONS    = ["moskva", "moskovskaya-oblast"]
MAX_PAGES  = 8
MAX_LOTS   = 150
TOP_N      = 15
ALERT_SCORE = 8.5

CATEGORIES = {
    "квартира":  {"icon":"🏠","label":"Квартиры",                 "min_score":0},
    "дом":       {"icon":"🏡","label":"Дома и дачи",              "min_score":0},
    "коммерция": {"icon":"🏢","label":"Коммерческая недвижимость","min_score":0},
    "земля":     {"icon":"🌱","label":"Земельные участки",        "min_score":7.0},
    "авто":      {"icon":"🚗","label":"Транспорт",                "min_score":8.0},
    "гараж":     {"icon":"🅿️","label":"Гаражи",                  "min_score":8.0},
    "бизнес":    {"icon":"💼","label":"Бизнес/оборудование",      "min_score":8.0},
    "прочее":    {"icon":"📦","label":"Прочее",                   "min_score":8.5},
}


async def login_tbankrot(page) -> bool:
    """Авторизация через модальное окно на главной"""
    try:
        print("🔐 Открываем главную страницу...")
        await page.goto("https://tbankrot.ru/", timeout=30000)
        await page.wait_for_timeout(2000)

        # Нажимаем кнопку Войти в шапке
        print("  Нажимаем кнопку Войти...")
        await page.click("text=Войти", timeout=8000)
        await page.wait_for_timeout(2000)

        # Ждём модальное окно
        print("  Ждём форму входа...")
        await page.wait_for_selector(
            "input[type='email'], input[type='text'][placeholder*='mail'], input[placeholder*='огин']",
            timeout=8000
        )

        # Email
        print("  Вводим email...")
        await page.fill(
            "input[type='email'], input[placeholder*='mail'], input[placeholder*='огин']",
            TBANKROT_LOGIN
        )
        await page.wait_for_timeout(500)

        # Пароль
        print("  Вводим пароль...")
        await page.fill("input[type='password']", TBANKROT_PASSWORD)
        await page.wait_for_timeout(500)

        # Кнопка Войти в модальном окне
        print("  Нажимаем Войти...")
        # Ищем зелёную кнопку Войти внутри модального окна
        btns = await page.query_selector_all("button")
        for btn in btns:
            txt = (await btn.inner_text()).strip()
            if txt == "Войти":
                await btn.click()
                break
        else:
            await page.click("button:has-text('Войти')")

        await page.wait_for_timeout(3000)

        # Проверяем успех — кнопка Войти должна исчезнуть
        still_login = await page.query_selector("button:has-text('Войти')")
        if not still_login:
            print("✅ Авторизация успешна!")
            return True

        # Проверяем по наличию аватара/профиля
        profile = await page.query_selector(
            ".user-menu, .user-avatar, [class*='avatar'], [class*='profile'], .logout"
        )
        if profile:
            print("✅ Авторизация успешна!")
            return True

        print("⚠️ Авторизация — возможно неверный логин/пароль")
        return False

    except Exception as e:
        print(f"⚠️ Ошибка авторизации: {e}")
        return False


async def collect_tbankrot(page) -> list:
    lots, seen = [], set()
    for region in REGIONS:
        for pg in range(1, MAX_PAGES + 1):
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
                            "id":       m.group(1),
                            "title":    title,
                            "url":      href,
                            "region":   region,
                            "source":   "Т-Банкрот",
                            "pdf_text": "",
                            "description": "",
                            "price":    0,
                            "step_current": 0,
                            "step_total":   0,
                        })
                        added += 1
                    except:
                        continue
                print(f"  {region} стр.{pg}: +{added} (итого {len(lots)})")
                if len(lots) >= MAX_LOTS:
                    break
            except Exception as e:
                print(f"  ⚠️ стр.{pg}: {e}")
                break
        if len(lots) >= MAX_LOTS:
            break
    return lots


async def enrich(lot: dict, page, ctx):
    if lot.get("source") == "Т-Банкрот":
        details = await get_lot_details(lot["url"], page)
        lot.update({
            "price":        details.get("price", lot.get("price", 0)),
            "description":  details.get("description", ""),
            "step_current": details.get("step_current", 0),
            "step_total":   details.get("step_total", 0),
        })
        if details.get("title_full"):
            lot["title"] = details["title_full"]
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
    lot["category"] = detect_type(
        f"{lot['title']} {lot.get('description','')[:300]}"
    )


def build_block(i: int, lot: dict, an: dict, trend: dict) -> str:
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
              "1️⃣1️⃣","1️⃣2️⃣","1️⃣3️⃣","1️⃣4️⃣","1️⃣5️⃣"]
    medal = medals[i] if i < len(medals) else f"#{i+1}"
    disc  = f" | -{an.get('discount_pct','?')}%" if an.get('discount_pct','?') not in ('?','0') else ""
    step  = f"\n📊 {an['step']}" if an.get('step') else ""
    src   = f" [{lot.get('source','Т-Банкрот')}]"
    trend_str = ""
    if trend and trend.get("drop_pct", 0) > 0:
        trend_str = f"\n📉 История: -{trend['drop_pct']}% за {trend['days_tracked']} дн."
    invest_icons = {"высокий":"🔥","средний":"📈","низкий":"📉"}
    risk_icons   = {"низкий":"🟢","средний":"🟡","высокий":"🟠","критический":"🔴"}
    return (
        f"{medal} *Балл: {an.get('total_score','?')}/10*"
        f" | {invest_icons.get(an.get('invest_potential','средний'),'📈')} {an.get('invest_potential','?')}"
        f" | {risk_icons.get(an.get('risk_level','средний'),'🟡')} риск: {an.get('risk_level','?')}"
        f"{src}\n"
        f"{lot.get('title','')[:65]}\n"
        f"💰 {an.get('price','—')} → рынок {an.get('market_price','—')}{disc}"
        f"{step}{trend_str}\n"
        f"💧 {an.get('liquidity_text','—')}\n"
        f"📈 {an.get('roi_text','—')}\n"
        f"⚖️ {an.get('legal_text','—')}\n"
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
        block = build_block(i, lot, an, trend)
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


async def run_agent(daily_report: bool = True):
    print(f"\n{'='*55}")
    print(f"🤖 Агент v6.1: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print(f"Режим: {'ПОЛНЫЙ ОТЧЁТ' if daily_report else 'МОНИТОРИНГ'}")
    print(f"{'='*55}\n")

    init_db()
    results_by_cat = {k: [] for k in CATEGORIES}
    skipped = alerts_sent = 0
    stats = {"analyzed":0,"recommended":0,"go":0,"wait":0,"categories":{}}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()

        # Авторизация
        await login_tbankrot(page)

        # Сбор с Т-Банкрот
        print("\n📡 Собираем лоты с Т-Банкрот...")
        tbankrot_lots = await collect_tbankrot(page)
        print(f"✅ Т-Банкрот: {len(tbankrot_lots)} лотов")

        # Доп. площадки
        extra_lots = await get_all_platform_lots()
        all_lots = tbankrot_lots + extra_lots
        print(f"\n📦 Всего лотов: {len(all_lots)}")
        stats["analyzed"] = len(all_lots)

        # Анализ
        print(f"\n🔍 Анализируем {len(all_lots)} лотов...")
        for i, lot in enumerate(all_lots):
            print(f"  [{i+1}/{len(all_lots)}] ", end="", flush=True)
            try:
                await enrich(lot, page, ctx)
                analysis = await analyze_lot(lot)
                score = float(analysis.get("total_score", 0))
                cat   = lot.get("category", "прочее")
                if cat not in results_by_cat:
                    cat = "прочее"

                save_lot(lot, analysis)
                trend = get_price_trend(lot["id"])
                min_s = CATEGORIES.get(cat, {}).get("min_score", 0)

                if score < min_s:
                    skipped += 1
                    print(f"{cat:12} | {score:.1f} < {min_s} — отсеян")
                else:
                    results_by_cat[cat].append((lot, analysis, trend))
                    stats["recommended"] += 1
                    if analysis.get("action") == "ВХОДИТЬ СЕЙЧАС":
                        stats["go"] += 1
                    elif analysis.get("action") == "ЖДАТЬ СНИЖЕНИЯ":
                        stats["wait"] += 1
                    print(f"{cat:12} | Балл:{score:.1f} | {analysis.get('action','?')}")

                # Алерт
                if score >= ALERT_SCORE and not was_notified_recently(lot["id"], hours=12):
                    disc = analysis.get("discount_pct","?")
                    await send_tg([
                        f"🔔 *ГОРЯЧИЙ ЛОТ! Балл {score}/10*\n\n"
                        f"{lot.get('title','')[:70]}\n"
                        f"💰 {analysis.get('price','—')} → рынок {analysis.get('market_price','—')}"
                        f"{f' (-{disc}%)' if disc not in ('?','0') else ''}\n"
                        f"📍 {lot.get('source','Т-Банкрот')}\n"
                        f"{analysis.get('action_emoji','⚠️')} *{analysis.get('action','?')}*\n"
                        f"💡 _{analysis.get('strategy','')}_\n\n"
                        f"🔗 {lot.get('url','')}"
                    ])
                    mark_notified(lot["id"], score, analysis.get("action",""))
                    alerts_sent += 1

                if score >= 9.0:
                    add_to_portfolio(lot, analysis, "Автодобавлен — балл 9+")

                stats["categories"][cat] = stats["categories"].get(cat,0) + 1

            except Exception as e:
                print(f"ошибка: {e}")
            await asyncio.sleep(0.3)

        await browser.close()

    save_stats(stats)

    if daily_report:
        total = sum(len(v) for v in results_by_cat.values())
        now   = datetime.now().strftime("%d.%m.%Y %H:%M")
        summary = [
            f"📊 *Инвестиционный дайджест v6.1*\n_{now}_\n",
            f"🔍 Изучено: *{stats['analyzed']}* лотов",
            f"✅ Прошли отбор: *{total}* | 🚫 Отсеяно: *{skipped}*\n",
            f"🟢 Входить сейчас: *{stats['go']}*",
            f"⏳ Ждать снижения: *{stats['wait']}*",
            f"🔔 Алертов: *{alerts_sent}*\n",
        ]
        for cat_key, cat_results in results_by_cat.items():
            if cat_results:
                cat = CATEGORIES[cat_key]
                go = sum(1 for _,a,_ in cat_results if a.get("action")=="ВХОДИТЬ СЕЙЧАС")
                summary.append(f"{cat['icon']} {cat['label']}: {len(cat_results)} | 🟢{go}")
        summary.append("\n⬇️ _Детальный разбор по категориям ниже_")
        await send_tg(["\n".join(summary)])
        await asyncio.sleep(2)

        for cat_key in ["квартира","коммерция","дом","земля","авто","гараж","бизнес","прочее"]:
            cat_results = results_by_cat.get(cat_key, [])
            if not cat_results:
                continue
            cat_results.sort(key=lambda x: float(x[1].get("total_score",0)), reverse=True)
            cat = CATEGORIES[cat_key]
            print(f"\n{cat['icon']} Отправляем {cat['label']}: {len(cat_results)}")
            await send_tg(format_cat_msgs(cat_key, cat_results))
            await asyncio.sleep(2)

    print(f"\n✅ Готово! Алертов: {alerts_sent}")


def job():     asyncio.run(run_agent(daily_report=True))
def monitor(): asyncio.run(run_agent(daily_report=False))


if __name__ == "__main__":
    import sys
    init_db()
    print("🤖 Агент по банкротным торгам v6.1")
    print("=" * 45)
    if   "--now"     in sys.argv: asyncio.run(run_agent(daily_report=True))
    elif "--monitor" in sys.argv: asyncio.run(run_agent(daily_report=False))
    elif "--bot"     in sys.argv:
        from bot_handler import run_bot
        run_bot()
    else:
        print("⏰ 08:00 — дайджест | каждые 2ч — мониторинг\n")
        for t in ["08:00"]:
            schedule.every().day.at(t).do(job)
        for t in ["10:00","12:00","14:00","16:00","18:00","20:00"]:
            schedule.every().day.at(t).do(monitor)
        while True:
            schedule.run_pending()
            time.sleep(60)

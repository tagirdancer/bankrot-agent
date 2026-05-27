"""
Парсеры дополнительных площадок:
- ЕФРСБ (fedresurs.ru) — официальный реестр
- Сбербанк-АСТ
- РТС-Тендер
"""
import httpx, re, asyncio
from playwright.async_api import async_playwright


async def get_efrsb_lots(region_codes: list = ["77", "50"]) -> list:
    """Парсит лоты с ЕФРСБ — открытый реестр"""
    lots = []
    regions = ",".join(region_codes)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

            # Поиск через API ЕФРСБ
            resp = await client.get(
                f"https://bankrupt.ru/bankrupt/search/?region={regions}&status=active&type=property",
                headers=headers
            )
            text = resp.text

            # Ищем лоты в HTML
            lot_pattern = re.finditer(
                r'href="(/bankrupt/lot/\d+)[^"]*"[^>]*>([^<]+)</a>.*?'
                r'(\d[\d\s]+)\s*(?:руб|₽)',
                text, re.DOTALL
            )
            for m in lot_pattern:
                try:
                    url = "https://bankrupt.ru" + m.group(1)
                    title = m.group(2).strip()[:200]
                    price_str = re.sub(r'\s', '', m.group(3))
                    price = float(price_str)
                    if 10_000 < price < 5_000_000_000:
                        lots.append({
                            "id": f"efrsb_{m.group(1).split('/')[-1]}",
                            "title": title,
                            "url": url,
                            "region": "moskva" if "77" in regions else "moskovskaya-oblast",
                            "pdf_text": "",
                            "description": "",
                            "price": price,
                            "step_current": 0,
                            "step_total": 0,
                            "source": "ЕФРСБ",
                        })
                except:
                    continue

            print(f"  ЕФРСБ: найдено {len(lots)} лотов")
    except Exception as e:
        print(f"  ЕФРСБ: ошибка — {e}")
    return lots[:30]


async def get_sberast_lots() -> list:
    """Парсит банкротные лоты Сбербанк-АСТ"""
    lots = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(
                "https://www.sberbank-ast.ru/Trade/SaleBankrupt.aspx?filter=region%3A77%2C50",
                timeout=30000
            )
            await page.wait_for_timeout(3000)

            # Ищем строки таблицы с лотами
            rows = await page.query_selector_all("tr.lot-row, .lot-item, [class*='trade-row']")
            for row in rows[:30]:
                try:
                    text = await row.inner_text()
                    link = await row.query_selector("a[href*='Trade']")
                    if not link:
                        continue
                    href = await link.get_attribute("href")
                    if href and not href.startswith("http"):
                        href = "https://www.sberbank-ast.ru" + href

                    # Цена
                    price_m = re.search(r'(\d[\d\s]+)\s*(?:руб|₽)', text)
                    price = 0
                    if price_m:
                        price = float(re.sub(r'\s', '', price_m.group(1)))

                    lots.append({
                        "id": f"sber_{re.search(r'id=(\w+)', href or '').group(1) if href and re.search(r'id=(\w+)', href) else len(lots)}",
                        "title": text[:150].strip(),
                        "url": href or "",
                        "region": "moskva",
                        "pdf_text": "",
                        "description": text[:500],
                        "price": price,
                        "step_current": 0,
                        "step_total": 0,
                        "source": "Сбербанк-АСТ",
                    })
                except:
                    continue

            await browser.close()
            print(f"  Сбербанк-АСТ: найдено {len(lots)} лотов")
    except Exception as e:
        print(f"  Сбербанк-АСТ: ошибка — {e}")
    return lots


async def get_rts_lots() -> list:
    """Парсит банкротные лоты РТС-Тендер"""
    lots = []
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = await client.get(
                "https://www.rts-tender.ru/sale/bankrupt?region=77,50",
                headers=headers
            )
            text = resp.text

            # Ищем лоты
            items = re.finditer(
                r'href="(/sale/bankrupt/\d+)[^"]*"[^>]*>([^<]{10,200})</a>.*?'
                r'(\d[\d\s]{3,})\s*(?:руб|₽)',
                text, re.DOTALL
            )
            for m in items:
                try:
                    url = "https://www.rts-tender.ru" + m.group(1)
                    title = m.group(2).strip()[:200]
                    price = float(re.sub(r'\s', '', m.group(3)))
                    if 10_000 < price < 5_000_000_000:
                        lots.append({
                            "id": f"rts_{m.group(1).split('/')[-1]}",
                            "title": title,
                            "url": url,
                            "region": "moskva",
                            "pdf_text": "",
                            "description": title,
                            "price": price,
                            "step_current": 0,
                            "step_total": 0,
                            "source": "РТС-Тендер",
                        })
                except:
                    continue

            print(f"  РТС-Тендер: найдено {len(lots)} лотов")
    except Exception as e:
        print(f"  РТС-Тендер: ошибка — {e}")
    return lots[:20]


async def get_all_platform_lots() -> list:
    """Собирает лоты со всех дополнительных площадок параллельно"""
    print("\n🌐 Собираем с дополнительных площадок...")
    results = await asyncio.gather(
        get_efrsb_lots(),
        get_sberast_lots(),
        get_rts_lots(),
        return_exceptions=True
    )
    all_lots = []
    for r in results:
        if isinstance(r, list):
            all_lots.extend(r)
    print(f"✅ Всего с доп. площадок: {len(all_lots)} лотов")
    return all_lots

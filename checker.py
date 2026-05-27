"""
Проверка должника через kad.arbitr.ru
"""
import httpx, re, asyncio
from playwright.async_api import async_playwright


async def check_arbitr(debtor_name: str) -> dict:
    """Ищет дело о банкротстве на kad.arbitr.ru"""
    result = {
        "found": False,
        "case_number": "",
        "stage": "",
        "creditors_count": 0,
        "manager": "",
        "disputed_deals": False,
        "summary": "не найдено",
        "risk": "неизвестно"
    }
    if not debtor_name or len(debtor_name) < 3:
        return result

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(
                f"https://kad.arbitr.ru/Search?&name={debtor_name}&category=bankrupt",
                timeout=20000
            )
            await page.wait_for_timeout(3000)
            text = await page.inner_text("body")

            # Номер дела
            case_m = re.search(r'А\d+[-–]\d+/\d+', text)
            if case_m:
                result["case_number"] = case_m.group()
                result["found"] = True

            # Стадия
            if "реализац" in text.lower():
                result["stage"] = "реализация имущества"
            elif "наблюден" in text.lower():
                result["stage"] = "наблюдение"
            elif "конкурсн" in text.lower():
                result["stage"] = "конкурсное производство"
            elif "реструктуриз" in text.lower():
                result["stage"] = "реструктуризация долгов"

            # Оспариваемые сделки
            if any(w in text.lower() for w in ["оспариван", "недействительн", "61.2", "61.3"]):
                result["disputed_deals"] = True

            # Итог
            if result["found"]:
                stage = result["stage"] or "банкротство"
                disputed = " ⚠️ Есть оспариваемые сделки!" if result["disputed_deals"] else ""
                result["summary"] = f"Дело {result['case_number']} | {stage}{disputed}"
                result["risk"] = "высокий" if result["disputed_deals"] else "средний"
            else:
                result["summary"] = "Дел о банкротстве не найдено в открытых источниках"
                result["risk"] = "низкий"

            await browser.close()
    except Exception as e:
        result["summary"] = f"Не удалось проверить: {e}"

    return result


async def check_debtor_from_egr(pdf_text: str) -> dict:
    """Извлекает имя должника из ЕГРН и проверяет его"""
    # Ищем имя должника в тексте
    patterns = [
        r'должник[:\s]+([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)',
        r'([А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.[А-ЯЁ]\.)',
        r'собственник[:\s]+([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)',
    ]
    name = ""
    for pattern in patterns:
        m = re.search(pattern, pdf_text, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            break

    if name:
        return await check_arbitr(name)
    return {
        "found": False,
        "summary": "Имя должника не найдено в документах",
        "risk": "неизвестно"
    }

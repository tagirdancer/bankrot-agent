"""
Парсинг отчёта об оценке (второй PDF на карточке лота tbankrot).
"""
from __future__ import annotations

import re

MIN_TEXT = 80


def classify_pdf_type(text: str, url: str = "") -> str:
    """egrn | appraisal | unknown"""
    tl = (text or "").lower()
    ul = (url or "").lower()
    if "egrn" in ul or "/egrn" in ul:
        return "egrn"
    if any(x in ul for x in ("оцен", "ocen", "appraisal", "report")):
        return "appraisal"

    egrn_hits = sum(1 for k in (
        "правообладат", "кадастровый номер", "обременен", "росреестр",
        "егрн", "выписк", "кадастров",
    ) if k in tl)
    appr_hits = sum(1 for k in (
        "оценк", "ликвидацион", "рыночн", "оценщик", "отчет об оценке",
        "отчёт об оценке", "стоимость объекта оценки",
    ) if k in tl)

    if egrn_hits >= 2 and egrn_hits >= appr_hits:
        return "egrn"
    if appr_hits >= 2:
        return "appraisal"
    return "unknown"


def _parse_money_near(text: str, label_pat: str) -> int:
    m = re.search(label_pat, text, re.I | re.S)
    if not m:
        return 0
    chunk = m.group(0)[:400]
    for pm in re.finditer(r"(\d[\d\s]{4,11})(?:[.,](\d{2}))?\s*(?:₽|руб)", chunk, re.I):
        v = int(re.sub(r"\s", "", pm.group(1)))
        if 100_000 <= v <= 2_000_000_000:
            return v
    for pm in re.finditer(r"(\d+[.,]\d+)\s*(?:млн|mln)\s*(?:₽|руб)?", chunk, re.I):
        v = int(float(pm.group(1).replace(",", ".")) * 1_000_000)
        if 100_000 <= v <= 2_000_000_000:
            return v
    return 0


def parse_appraisal_pdf(text: str) -> dict:
    """Извлекает поля из отчёта об оценке. Нет данных → 'не указано'."""
    result = {
        "market_price": 0,
        "liquidation_price": 0,
        "appraisal_price": 0,
        "comparables_range": "не указано",
        "auction_avg_reduction_pct": "не указано",
        "auction_participants_hint": "не указано",
        "court_cases": "не указано",
        "restriction_flags": [],
        "parsed_ok": False,
        "summary": "",
    }
    if not text or len(text) < MIN_TEXT:
        return result

    norm = re.sub(r"[ \t]+", " ", text)
    norm = re.sub(r"\n{3,}", "\n\n", norm)
    tl = norm.lower()

    result["market_price"] = _parse_money_near(
        norm,
        r"рыночн[\s\S]{0,120}?стоим",
    )
    result["liquidation_price"] = _parse_money_near(
        norm,
        r"ликвидацион[\s\S]{0,120}?стоим",
    )
    result["appraisal_price"] = _parse_money_near(
        norm,
        r"(?:оценочн|итогов)[\s\S]{0,120}?стоим",
    )
    if not result["appraisal_price"]:
        result["appraisal_price"] = _parse_money_near(norm, r"стоимость объекта оценки")

    # Объявления / сопоставимые
    comp = re.search(
        r"(?:объявлени[яе]|сопоставим|аналог)[^\n]{0,80}\n([\s\S]{30,800}?)"
        r"(?:\n\s*\d+\.\s|\nАналитик|\nСудебн|\Z)",
        norm, re.I,
    )
    if comp:
        prices = []
        for pm in re.finditer(r"(\d[\d\s]{5,11})\s*(?:₽|руб)", comp.group(1), re.I):
            v = int(re.sub(r"\s", "", pm.group(1)))
            if 100_000 <= v <= 2_000_000_000:
                prices.append(v)
        if len(prices) >= 2:
            result["comparables_range"] = (
                f"{min(prices)/1e6:.1f}–{max(prices)/1e6:.1f} млн ₽ "
                f"({len(prices)} цен в тексте)"
            )
        elif len(prices) == 1:
            result["comparables_range"] = f"~{prices[0]/1e6:.1f} млн ₽ (1 цена в тексте)"

    # Аналитика торгов
    red = re.search(
        r"(?:аналитик[^\n]*торг|снижен[^\n]{0,60})[\s\S]{0,600}",
        norm, re.I,
    )
    if red:
        chunk = red.group(0)
        rm = re.search(r"(\d+[.,]?\d*)\s*%", chunk)
        if rm:
            result["auction_avg_reduction_pct"] = f"~{rm.group(1).replace(',', '.')}%"
        pm = re.search(r"(\d+)\s*(?:заявок|участник)", chunk, re.I)
        if pm:
            result["auction_participants_hint"] = pm.group(1)

    # Судебные дела
    if re.search(r"судебн[^\n]{0,40}дел[^\n]{0,40}(?:не\s+обнаруж|отсутств)", tl):
        result["court_cases"] = "не обнаружены"
    elif re.search(r"судебн[^\n]{0,40}дел[^\n]{0,40}(?:обнаруж|имеют|найден)", tl):
        result["court_cases"] = "обнаружены"
    elif re.search(r"судебн[^\n]{0,60}дел", tl):
        result["court_cases"] = "упоминаются — проверить вручную"

    # Зоны ограничений
    flags = []
    zones = [
        ("садоводств", "зона садоводства"),
        ("приаэродром", "приаэродромная зона"),
        ("охранн.*зон", "охранная зона"),
        ("затоплен", "зона затопления"),
        ("санитарн.*зон", "санитарная зона"),
        ("культурн.*наслед", "ОКН / культурное наследие"),
        ("заповедн", "заповедная/ООПТ зона"),
    ]
    for pat, label in zones:
        if re.search(pat, tl):
            flags.append(label)
    result["restriction_flags"] = flags

    best_price = (
        result["market_price"]
        or result["appraisal_price"]
        or result["liquidation_price"]
    )
    result["parsed_ok"] = best_price > 0 or any(
        x not in ("", "не указано") for x in (
            result["comparables_range"],
            result["court_cases"],
            result["auction_avg_reduction_pct"],
        )
    ) or bool(flags)

    parts = []
    if result["market_price"]:
        parts.append(f"рыночная {result['market_price']/1e6:.1f} млн")
    elif result["appraisal_price"]:
        parts.append(f"оценочная {result['appraisal_price']/1e6:.1f} млн")
    if result["liquidation_price"]:
        parts.append(f"ликвидационная {result['liquidation_price']/1e6:.1f} млн")
    if result["comparables_range"] != "не указано":
        parts.append(f"аналоги: {result['comparables_range']}")
    if flags:
        parts.append("зоны: " + ", ".join(flags[:3]))
    result["summary"] = "; ".join(parts)[:300]
    return result

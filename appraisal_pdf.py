"""
Парсинг отчёта об оценке (второй PDF на карточке лота tbankrot).
"""
from __future__ import annotations

import re

MIN_TEXT = 60


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
    chunk = m.group(0)[:500]
    for pm in re.finditer(r"(\d[\d\s]{4,11})(?:[.,](\d{2}))?\s*(?:₽|руб)", chunk, re.I):
        v = int(re.sub(r"\s", "", pm.group(1)))
        if 100_000 <= v <= 2_000_000_000:
            return v
    for pm in re.finditer(r"(\d+[.,]\d+)\s*(?:млн|mln)\s*(?:₽|руб)?", chunk, re.I):
        v = int(float(pm.group(1).replace(",", ".")) * 1_000_000)
        if 100_000 <= v <= 2_000_000_000:
            return v
    return 0


def _extract_comparables(norm: str) -> tuple[str, list[int]]:
    prices: list[int] = []
    for pat in (
        r"(?:объявлени[яе]|сопоставим|аналог)[^\n]{0,100}\n([\s\S]{50,2500}?)"
        r"(?:\n\s*(?:\d+\.|Аналитик|Судебн|Заключ|\d+\s+Вывод)|\Z)",
        r"(?:рынок[^\n]{0,40}объявлен)[^\n]{0,80}\n([\s\S]{50,2000}?)(?:\nАналитик|\Z)",
    ):
        comp = re.search(pat, norm, re.I)
        if comp:
            for pm in re.finditer(r"(\d[\d\s]{5,11})\s*(?:₽|руб)", comp.group(1), re.I):
                v = int(re.sub(r"\s", "", pm.group(1)))
                if 100_000 <= v <= 2_000_000_000:
                    prices.append(v)
            if prices:
                break
    if len(prices) >= 2:
        return (
            f"{min(prices)/1e6:.1f}–{max(prices)/1e6:.1f} млн ₽ "
            f"({len(prices)} объявлений в отчёте)",
            prices,
        )
    if len(prices) == 1:
        return f"~{prices[0]/1e6:.1f} млн ₽ (1 объявление)", prices
    return "не указано", prices


def _extract_auction_analytics(norm: str) -> dict:
    out = {
        "avg_reduction_pct": "не указано",
        "participants_hint": "не указано",
        "raw_snippet": "",
    }
    for pat in (
        r"(?:аналитик[^\n]*торг)[^\n]{0,80}\n([\s\S]{30,1200}?)(?:\n\s*\d+\.\s|\nСудебн|\Z)",
        r"(?:снижен[^\n]{0,60}на\s+торг)[^\n]{0,80}\n([\s\S]{30,800}?)(?:\n|\Z)",
    ):
        red = re.search(pat, norm, re.I)
        if red:
            chunk = red.group(0)[:600]
            out["raw_snippet"] = chunk[:200]
            rm = re.search(r"(?:средн[^\n]{0,30})?(\d+[.,]?\d*)\s*%", chunk, re.I)
            if rm:
                out["avg_reduction_pct"] = f"~{rm.group(1).replace(',', '.')}% (отчёт об оценке)"
            pm = re.search(r"(\d+)\s*(?:заявок|участник|заявител)", chunk, re.I)
            if pm:
                out["participants_hint"] = pm.group(1)
            break
    return out


def parse_appraisal_pdf(text: str) -> dict:
    """Извлекает поля из отчёта об оценке. Нет данных → 'не указано'."""
    result = {
        "market_price": 0,
        "liquidation_price": 0,
        "appraisal_price": 0,
        "comparables_range": "не указано",
        "comparables_prices": [],
        "auction_avg_reduction_pct": "не указано",
        "auction_participants_hint": "не указано",
        "auction_analytics_snippet": "",
        "court_cases": "не указано",
        "restriction_flags": [],
        "market_price_label": "",
        "parsed_ok": False,
        "summary": "",
        "source": "отчёт об оценке",
    }
    if not text or len(text) < MIN_TEXT:
        return result

    norm = re.sub(r"[ \t]+", " ", text)
    norm = re.sub(r"\n{3,}", "\n\n", norm)
    tl = norm.lower()

    for label, pat in (
        ("рыночная стоимость", r"рыночн[\s\S]{0,150}?стоим"),
        ("рыночная стоимость", r"рыночн[\s\S]{0,80}?составляет"),
        ("оценочная стоимость", r"(?:оценочн|итогов)[\s\S]{0,120}?стоим"),
        ("стоимость объекта оценки", r"стоимость объекта оценки"),
    ):
        v = _parse_money_near(norm, pat)
        if v and not result["market_price"]:
            result["market_price"] = v
            result["market_price_label"] = label
        if v and not result["appraisal_price"] and "оценоч" in label:
            result["appraisal_price"] = v

    result["liquidation_price"] = _parse_money_near(
        norm, r"ликвидацион[\s\S]{0,120}?стоим",
    )
    if not result["appraisal_price"]:
        result["appraisal_price"] = result["market_price"] or _parse_money_near(
            norm, r"стоимость объекта оценки",
        )

    comp_range, comp_prices = _extract_comparables(norm)
    result["comparables_range"] = comp_range
    result["comparables_prices"] = comp_prices

    analytics = _extract_auction_analytics(norm)
    result["auction_avg_reduction_pct"] = analytics["avg_reduction_pct"]
    result["auction_participants_hint"] = analytics["participants_hint"]
    result["auction_analytics_snippet"] = analytics["raw_snippet"]

    if re.search(r"судебн[^\n]{0,40}дел[^\n]{0,40}(?:не\s+обнаруж|отсутств)", tl):
        result["court_cases"] = "не обнаружены"
    elif re.search(r"судебн[^\n]{0,40}дел[^\n]{0,40}(?:обнаруж|имеют|найден)", tl):
        result["court_cases"] = "обнаружены"
    elif re.search(r"судебн[^\n]{0,60}дел", tl):
        result["court_cases"] = "упоминаются — проверить вручную"

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
            result["auction_participants_hint"],
        )
    ) or bool(flags)

    parts = []
    if result["market_price"]:
        parts.append(
            f"{result.get('market_price_label') or 'рыночная'} "
            f"{result['market_price']/1e6:.2f} млн ₽"
        )
    elif result["appraisal_price"]:
        parts.append(f"оценочная {result['appraisal_price']/1e6:.2f} млн ₽")
    if result["liquidation_price"]:
        parts.append(f"ликвидационная {result['liquidation_price']/1e6:.2f} млн ₽")
    if result["comparables_range"] != "не указано":
        parts.append(f"аналоги: {result['comparables_range']}")
    if result["auction_avg_reduction_pct"] != "не указано":
        parts.append(f"снижение на торгах: {result['auction_avg_reduction_pct']}")
    if flags:
        parts.append("зоны: " + ", ".join(flags[:3]))
    result["summary"] = "; ".join(parts)[:400]
    return result

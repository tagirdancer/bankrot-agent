"""
Парсинг отчёта об оценке (второй PDF на карточке лота tbankrot).
"""
from __future__ import annotations

import re
import statistics

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
    chunk = text[m.start(): m.start() + 500]
    for pm in re.finditer(r"(\d[\d\s]{4,11})(?:[.,](\d{2}))?\s*(?:₽|руб|p\.|P\.)?", chunk, re.I):
        v = int(re.sub(r"\s", "", pm.group(1)))
        if 100_000 <= v <= 2_000_000_000:
            return v
    for pm in re.finditer(r"(\d+[.,]\d+)\s*(?:млн|mln)\s*(?:₽|руб)?", chunk, re.I):
        v = int(float(pm.group(1).replace(",", ".")) * 1_000_000)
        if 100_000 <= v <= 2_000_000_000:
            return v
    return 0


def _scan_price_numbers(blob: str) -> list[int]:
    """Цены из таблицы объявлений — допускает OCR без символа ₽."""
    prices: list[int] = []
    seen: set[int] = set()
    for pm in re.finditer(
        r"(?<!\d)(\d[\d\s]{5,10})(?:[.,](\d{2}))?(?:\s*(?:₽|руб|p\.|P\.))?",
        blob,
        re.I,
    ):
        v = int(re.sub(r"\s", "", pm.group(1)))
        if 200_000 <= v <= 500_000_000 and v not in seen:
            seen.add(v)
            prices.append(v)
    return prices


def _fmt_mln(v: int) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн ₽"
    return f"{v:,} ₽".replace(",", " ")


def _extract_comparables(norm: str) -> tuple[str, list[int], int]:
    """
    Раздел «Объявления» — цены аналогов.
    Возвращает (диапазон-текст, список цен, медиана).
    """
    prices: list[int] = []
    section = ""
    for pat in (
        r"(?:об[ъo]явлен[^\n]{0,60})\n([\s\S]{80,4500}?)"
        r"(?:\n\s*(?:\d+\.|аналитик|судебн|заключ|вывод|\d+\s+вывод)|\Z)",
        r"(?:рынок[^\n]{0,50}об[ъo]явлен)[^\n]{0,80}\n([\s\S]{80,3000}?)(?:\nаналитик|\Z)",
        r"(?:сопоставим[^\n]{0,40}об[ъo]ект)[^\n]{0,80}\n([\s\S]{80,3000}?)(?:\nаналитик|\Z)",
    ):
        comp = re.search(pat, norm, re.I)
        if comp:
            section = comp.group(1)
            prices = _scan_price_numbers(section)
            if len(prices) >= 2:
                break

    if len(prices) < 2:
        idx = norm.lower().find("объявлен")
        if idx < 0:
            idx = norm.lower().find("объявлени")
        if idx >= 0:
            blob = norm[idx: idx + 5000]
            extra = _scan_price_numbers(blob)
            if len(extra) > len(prices):
                prices = extra

    if not prices:
        return "не указано", [], 0

    med = int(statistics.median(prices))
    if len(prices) >= 2:
        rng = f"{_fmt_mln(min(prices))}–{_fmt_mln(max(prices))} ({len(prices)} объявл.)"
    else:
        rng = f"~{_fmt_mln(prices[0])} (1 объявл.)"
    return rng, prices, med


def _extract_cadastral_value(norm: str) -> int:
    for pat in (
        r"кадастров[\s\S]{0,80}?стоим",
        r"кадастров[\s\S]{0,40}оцен",
    ):
        v = _parse_money_near(norm, pat)
        if v:
            return v
    return 0


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


def resolve_market_orientir(parsed: dict, lot_cadastral_value: int = 0) -> dict:
    """
    Единый рыночный ориентир из распознанного отчёта.
    Приоритет: рыночная оценка отчёта → медиана объявлений → кадастровая стоимость.
    """
    out = {
        "price": 0,
        "source": "",
        "label": "",
        "disclaimer": "",
        "comparables_range": parsed.get("comparables_range", "не указано"),
        "comparables_median": parsed.get("comparables_median", 0),
    }
    mp = int(parsed.get("market_price") or parsed.get("appraisal_price") or 0)
    if mp > 0:
        out.update(
            price=mp,
            source="appraisal_valuation",
            label="рыночная оценка отчёта",
            disclaimer="из отчёта об оценке",
        )
        return out

    med = int(parsed.get("comparables_median") or 0)
    if med > 0:
        out.update(
            price=med,
            source="appraisal_comparables",
            label=f"аналоги {parsed.get('comparables_range', '')}",
            disclaimer="ориентир по объявлениям из отчёта, проверить",
        )
        return out

    cv = int(parsed.get("cadastral_value") or lot_cadastral_value or 0)
    if cv > 0:
        out.update(
            price=cv,
            source="cadastral_coarse",
            label="кадастровая стоимость",
            disclaimer="грубо, по кадастру",
        )
        return out
    return out


def parse_appraisal_pdf(text: str) -> dict:
    """Извлекает поля из отчёта об оценке. Нет данных → 'не указано'."""
    result = {
        "market_price": 0,
        "liquidation_price": 0,
        "appraisal_price": 0,
        "cadastral_value": 0,
        "comparables_range": "не указано",
        "comparables_prices": [],
        "comparables_median": 0,
        "comparables_min": 0,
        "comparables_max": 0,
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

    result["cadastral_value"] = _extract_cadastral_value(norm)

    comp_range, comp_prices, comp_med = _extract_comparables(norm)
    result["comparables_range"] = comp_range
    result["comparables_prices"] = comp_prices
    result["comparables_median"] = comp_med
    if comp_prices:
        result["comparables_min"] = min(comp_prices)
        result["comparables_max"] = max(comp_prices)

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

    orient = resolve_market_orientir(result)
    best_price = orient.get("price") or 0
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
    elif result["comparables_median"]:
        parts.append(
            f"аналоги (мед. {_fmt_mln(result['comparables_median'])}): {result['comparables_range']}"
        )
    elif result["appraisal_price"]:
        parts.append(f"оценочная {result['appraisal_price']/1e6:.2f} млн ₽")
    if result["liquidation_price"]:
        parts.append(f"ликвидационная {result['liquidation_price']/1e6:.2f} млн ₽")
    if result["cadastral_value"] and not result["market_price"]:
        parts.append(f"кадастр {_fmt_mln(result['cadastral_value'])}")
    if result["comparables_range"] != "не указано" and result["comparables_median"]:
        parts.append(f"объявления: {result['comparables_range']}")
    if result["auction_avg_reduction_pct"] != "не указано":
        parts.append(f"снижение на торгах: {result['auction_avg_reduction_pct']}")
    if flags:
        parts.append("зоны: " + ", ".join(flags[:3]))
    result["summary"] = "; ".join(parts)[:400]
    return result

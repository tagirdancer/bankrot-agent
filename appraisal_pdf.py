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


def _clean_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def _lot_type_from_line(line: str) -> str:
    ll = line.lower()
    if "гараж" in ll or "машиномест" in ll:
        return "гараж"
    if re.search(r"зем\.?\s*уч|земельн[^\n]{0,20}участ|(?:^|\s)участок", ll):
        return "земля"
    if "квартира" in ll or re.search(r"\d[\s-]*комнат", ll):
        if "нежил" not in ll:
            return "квартира"
    if "нежил" in ll or "помещен" in ll:
        return "коммерция"
    if "жилой дом" in ll or "домовлад" in ll:
        return "дом"
    return ""


def _types_compatible(subject: str, candidate: str) -> bool:
    """Тот же класс объекта для сравнения."""
    if not candidate:
        return False
    s = subject if subject in ("квартира", "апартаменты", "дом", "коммерция", "земля", "гараж") else "прочее"
    groups = {
        "квартира": {"квартира", "апартаменты"},
        "апартаменты": {"квартира", "апартаменты"},
        "дом": {"дом"},
        "коммерция": {"коммерция"},
        "земля": {"земля"},
        "гараж": {"гараж"},
    }
    allowed = groups.get(s, {s})
    return candidate in allowed


def _extract_trading_table_section(norm: str) -> str:
    tl = norm.lower()
    for marker in (
        "состав объектов выставленных на торги",
        "объект текущая цена",
        "завершенные торги на карте",
    ):
        i = tl.find(marker)
        if i >= 0:
            return norm[i: i + 9000]
    i = tl.find("аналитика торгов")
    if i >= 0:
        return norm[i: i + 9000]
    return ""


def _parse_trading_table_row(line: str) -> dict | None:
    line = re.sub(r"\s+", " ", _clean_html(line)).strip()
    if len(line) < 12:
        return None
    if line.lower().startswith(("объект ", "текущая цена", "тип ", "статус")):
        return None
    area_m = re.search(r"(\d+[.,]?\d*)\s*(?:м²|м2|кв\.?\s*м|кв\.м)", line, re.I)
    if not area_m:
        return None
    try:
        area = float(area_m.group(1).replace(",", "."))
    except ValueError:
        return None
    if area < 5 or area > 500_000:
        return None
    prices: list[int] = []
    for pm in re.finditer(r"(?<!\d)(\d[\d\s]{5,10})(?:[.,](\d{2}))?", line):
        v = int(re.sub(r"\s", "", pm.group(1)))
        if 100_000 <= v <= 500_000_000:
            prices.append(v)
    if not prices:
        return None
    price = max(prices)
    obj_type = _lot_type_from_line(line)
    ll = line.lower()
    status = "не указано"
    if "завершен" in ll:
        status = "завершённые"
    elif "открыт" in ll:
        status = "открыт приём"
    return {
        "price": price,
        "area_sqm": area,
        "type": obj_type,
        "status": status,
        "raw": line[:140],
    }


def _parse_trading_table(norm: str) -> list[dict]:
    section = _extract_trading_table_section(norm)
    if not section:
        return []
    rows: list[dict] = []
    seen: set[tuple] = set()
    for line in section.split("\n"):
        row = _parse_trading_table_row(line)
        if not row:
            continue
        key = (row["price"], round(row["area_sqm"], 1), row["type"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def _extract_auction_analytics(norm: str) -> dict:
    out = {
        "avg_reduction_pct": "не указано",
        "participants_hint": "не указано",
        "raw_snippet": "",
    }
    chunk = norm
    idx = norm.lower().find("аналитик")
    if idx >= 0:
        chunk = norm[idx: idx + 3500]
    out["raw_snippet"] = chunk[:220]

    rm = re.search(
        r"Среднее\s+снижение\s+цены\s+на\s+публичном\s+предложении\s+(-?\d+[.,]\d+)\s*%",
        norm,
        re.I,
    )
    if rm:
        out["avg_reduction_pct"] = f"{rm.group(1).replace(',', '.')}% (аналитика торгов)"

    pm = re.search(
        r"Среднее\s+количество\s+заявок\s+на\s+лот\s+(\d+[.,]\d+)",
        norm,
        re.I,
    )
    if pm:
        out["participants_hint"] = pm.group(1).replace(",", ".")
    return out


def parse_trading_analytics(text: str) -> dict:
    """Аналитика торгов: таблица лотов + сводные метрики."""
    result = {
        "table_rows": [],
        "avg_reduction_pct": "не указано",
        "participants_hint": "не указано",
        "parsed_ok": False,
    }
    if not text or len(text) < MIN_TEXT:
        return result
    norm = re.sub(r"[ \t]+", " ", _clean_html(text))
    norm = re.sub(r"\n{3,}", "\n\n", norm)
    if "аналитик" not in norm.lower() or "торг" not in norm.lower():
        return result
    result["table_rows"] = _parse_trading_table(norm)
    stats = _extract_auction_analytics(norm)
    result["avg_reduction_pct"] = stats["avg_reduction_pct"]
    result["participants_hint"] = stats["participants_hint"]
    result["parsed_ok"] = bool(result["table_rows"]) or stats["avg_reduction_pct"] != "не указано"
    return result


def compute_trading_market(lot_type: str, area_sqm: float, text: str) -> dict:
    """
    Рыночный ориентир из таблицы соседних лотов (аналитика торгов).
    Медиана ₽/м² по аналогам того же типа и площади ±30%.
    """
    empty = {
        "market_price": 0,
        "median_ppm": 0,
        "n_analogs": 0,
        "analogs": [],
        "coarse": False,
        "comment": "рынок не определён",
        "manual_market": True,
        "market_source": "",
    }
    if not text or area_sqm <= 0:
        return empty

    ta = parse_trading_analytics(text)
    rows = ta.get("table_rows") or []
    if not rows:
        return empty

    lo, hi = area_sqm * 0.7, area_sqm * 1.3
    analogs = [
        r for r in rows
        if _types_compatible(lot_type, r.get("type") or "")
        and lo <= r["area_sqm"] <= hi
    ]
    open_only = [r for r in analogs if "заверш" not in (r.get("status") or "").lower()]
    if open_only:
        analogs = open_only
    if not analogs:
        return empty

    tol = max(1.0, area_sqm * 0.02)
    exact = [r for r in analogs if abs(r["area_sqm"] - area_sqm) <= tol]
    if exact:
        analogs = exact

    ppms = [r["price"] / r["area_sqm"] for r in analogs]
    med_ppm = statistics.median(ppms)
    orientir = int(med_ppm * area_sqm)
    coarse = len(analogs) < 3

    if coarse and len(analogs) < 1:
        return empty

    comment = "по аналогичным лотам рядом (аналитика торгов)"
    if coarse:
        comment = f"мало аналогов ({len(analogs)}), ориентир грубый; {comment}"

    return {
        "market_price": orientir,
        "median_ppm": int(med_ppm),
        "n_analogs": len(analogs),
        "analogs": analogs[:10],
        "coarse": coarse,
        "comment": comment,
        "manual_market": False,
        "market_source": "trading_analytics",
        "trading_analytics": ta,
        "market_disclaimer": "по аналогичным лотам рядом (аналитика торгов)",
    }


def resolve_market_orientir(parsed: dict, lot_cadastral_value: int = 0) -> dict:
    """
    Рыночный ориентир из распознанного отчёта.
    Кадастровая стоимость НЕ используется как рынок.
    """
    out = {
        "price": 0,
        "source": "",
        "label": "",
        "disclaimer": "",
        "comparables_range": parsed.get("comparables_range", "не указано"),
        "comparables_median": parsed.get("comparables_median", 0),
    }
    ta_orient = int(parsed.get("trading_market_price") or 0)
    if ta_orient > 0:
        out.update(
            price=ta_orient,
            source="trading_analytics",
            label=f"медиана {parsed.get('trading_median_ppm', 0):,} ₽/м²".replace(",", " "),
            disclaimer="по аналогичным лотам рядом (аналитика торгов)",
        )
        return out

    mp = int(parsed.get("market_price") or parsed.get("appraisal_price") or 0)
    if mp > 0:
        out.update(
            price=mp,
            source="appraisal_valuation",
            label="рыночная оценка отчёта",
            disclaimer="из отчёта об оценке",
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
    ta = parse_trading_analytics(norm)
    result["trading_table_rows"] = ta.get("table_rows") or []
    if ta.get("avg_reduction_pct", "не указано") != "не указано":
        result["auction_avg_reduction_pct"] = ta["avg_reduction_pct"]
    if ta.get("participants_hint", "не указано") != "не указано":
        result["auction_participants_hint"] = ta["participants_hint"]

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
    result["parsed_ok"] = best_price > 0 or bool(result["trading_table_rows"]) or any(
        x not in ("", "не указано") for x in (
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
    if result["trading_table_rows"]:
        parts.append(f"лотов в аналитике: {len(result['trading_table_rows'])}")
    if result["auction_avg_reduction_pct"] != "не указано":
        parts.append(f"снижение на публичке: {result['auction_avg_reduction_pct']}")
    if result["auction_participants_hint"] != "не указано":
        parts.append(f"заявок (сред.): {result['auction_participants_hint']}")
    if flags:
        parts.append("зоны: " + ", ".join(flags[:3]))
    result["summary"] = "; ".join(parts)[:400]
    return result

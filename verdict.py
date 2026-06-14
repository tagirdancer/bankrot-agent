"""
Структурированный вердикт по лоту — без выдумок.
Шаг 1: извлечение фактов в JSON (только из источников карточки/ЕГРН).
Шаг 2–4: скоринг риска и инвест-привлекательности строго по JSON.
"""
import re
from typing import Optional

LEGAL_FOOTER = (
    "Проверка юриста по конкретному лоту обязательна. "
    "Это не юридическое заключение."
)

OBJECT_TYPE_MAP = {
    "квартира": "квартира",
    "апартаменты": "квартира",
    "дом": "иное",
    "коммерция": "коммерция",
    "земля": "земля",
    "авто": "авто",
    "гараж": "иное",
    "бизнес": "иное",
    "прочее": "иное",
}

LIQUIDITY = {
    "квартира": 75,
    "коммерция": 55,
    "земля": 40,
    "авто": 60,
    "иное": 45,
}


def _sources(lot: dict) -> list[tuple[str, str]]:
    out = []
    if lot.get("description"):
        out.append((lot["description"], "карточка tbankrot"))
    if lot.get("pdf_text"):
        out.append((lot["pdf_text"], "ЕГРН/PDF"))
    if lot.get("analytics_text"):
        out.append((lot["analytics_text"], "аналитика tbankrot"))
    title = lot.get("title_full") or lot.get("title", "")
    if title:
        out.append((title, "заголовок карточки"))
    return out


def _scan(sources: list[tuple[str, str]], patterns: list[str]) -> tuple[bool, str]:
    for text, src in sources:
        tl = text.lower()
        for p in patterns:
            if p in tl:
                return True, f"{src}: «{p}»"
    return False, ""


def _yes_no_unknown(
    sources: list[tuple[str, str]],
    yes_p: list[str],
    no_p: list[str],
) -> tuple[str, str]:
    found, src = _scan(sources, yes_p)
    if found:
        return "да", src
    found, src = _scan(sources, no_p)
    if found:
        return "нет", src
    return "не указано", ""


def _fmt_money(v) -> str:
    try:
        v = float(v)
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f} млн ₽"
        if v > 0:
            return f"{int(v):,} ₽".replace(",", " ")
    except (TypeError, ValueError):
        pass
    return "не указано"


def extract_lot_facts(lot: dict, an: dict) -> dict:
    """ШАГ 1 — факты только из доступных полей источника."""
    sources = _sources(lot)
    src_map: dict[str, str] = {}

    lot_type = lot.get("category", "прочее")
    object_type = OBJECT_TYPE_MAP.get(lot_type, "иное")
    if object_type != "не указано":
        src_map["object_type"] = "категория из карточки (detect_type)"

    address = (lot.get("address") or "").strip()
    egrn = lot.get("egrn_parsed") or {}
    if not address and egrn.get("address"):
        address = egrn["address"]
    if address:
        src_map["address"] = "карточка tbankrot / Росреестр"
        address_out = address[:120]
    else:
        address_out = "не указано"

    price_lot = lot.get("price") or an.get("lot_price_raw")
    if price_lot:
        src_map["price_lot"] = "карточка tbankrot"

    if an.get("land_manual_market") or not an.get("market_known", True):
        price_market = None
    else:
        price_market = an.get("market_price_raw") or None
        if price_market:
            src_map["price_market_estimate"] = "ориентир calc_market_price (площадь × таблица)"

    disc_raw = an.get("discount_pct", "?")
    try:
        discount_pct = (
            int(disc_raw)
            if disc_raw and str(disc_raw) not in ("0", "?", "")
            else None
        )
    except (TypeError, ValueError):
        discount_pct = None
    if discount_pct is not None:
        src_map["discount_pct"] = "расчёт по price_lot и price_market_estimate"

    fmt = lot.get("auction_format", "")
    if fmt == "публичное предложение":
        auction_type = "публичное предложение"
        src_map["auction_type"] = "карточка tbankrot"
    elif fmt == "аукцион":
        auction_type = "открытые торги"
        src_map["auction_type"] = "карточка tbankrot"
    else:
        auction_type = "не указано"

    deposit = lot.get("deposit") or None
    if deposit:
        src_map["deposit"] = "карточка tbankrot"

    dl_parts = []
    if lot.get("application_deadline"):
        dl_parts.append(f"заявки до {lot['application_deadline']}")
        src_map["deadlines"] = "карточка tbankrot"
    sc, st = lot.get("step_current", 0), lot.get("step_total", 0)
    if sc and st:
        dl_parts.append(f"шаг {sc}/{st}")
        src_map.setdefault("deadlines", "карточка tbankrot")
    if lot.get("next_reduction_date") and lot.get("next_reduction_price"):
        np = lot["next_reduction_price"]
        p = f"{np / 1e6:.1f} млн ₽" if np >= 1_000_000 else f"{int(np):,} ₽".replace(",", " ")
        dl_parts.append(f"след. снижение {lot['next_reduction_date']} → {p}")
        src_map.setdefault("deadlines", "карточка tbankrot")
    deadlines = " | ".join(dl_parts) if dl_parts else "не указано"

    pdf = lot.get("pdf_text", "")
    if pdf:
        tl = pdf.lower()
        if any(x in tl for x in ("нет обремен", "обременен не зарегистрир", "без обремен", "обременения отсутств")):
            egrn_clean = "да"
            src_map["egrn_clean"] = "ЕГРН/PDF"
        elif any(x in tl for x in ("обременен", "залог", "арест", "ипотек")):
            egrn_clean = "нет"
            src_map["egrn_clean"] = "ЕГРН/PDF"
        else:
            egrn_clean = "не указано"
    else:
        egrn_clean = "не указано"

    enc_found, enc_src = _scan(sources, ["обременен", "залог", "арест", "ипотек"])
    if enc_found:
        encumbrances = "упоминание в тексте"
        src_map["encumbrances"] = enc_src
    else:
        encumbrances = "не указано"

    registered_persons, reg_src = _yes_no_unknown(
        sources,
        ["прописан", "зарегистрирован", "зарегистрировано"],
        ["не прописан", "прописанных нет", "лиц не зарегистрир", "отсутствуют зарегистрир"],
    )
    if reg_src:
        src_map["registered_persons"] = reg_src

    minors_registered, min_src = _yes_no_unknown(
        sources,
        ["несовершеннолетн"],
        ["несовершеннолетн не зарегистрир", "без несовершеннолетн"],
    )
    if min_src:
        src_map["minors_registered"] = min_src

    sole_housing, sole_src = _yes_no_unknown(
        sources,
        ["единственн"],
        ["не является единствен", "не единственн"],
    )
    if sole_src:
        src_map["sole_housing"] = sole_src
    if sole_housing == "да" and not any(
        x in " ".join(t for t, _ in sources).lower()
        for x in ("жил", "жиль", "квартир", "дом")
    ):
        sole_housing = "не указано"
        src_map.pop("sole_housing", None)

    disputes, disp_src = _yes_no_unknown(
        sources,
        ["kad.arbitr", "картотек", "оспариван", "обжалован"],
        [],
    )
    active_disputes = disputes if disputes != "не указано" else "не указано"
    if disp_src:
        src_map["active_disputes_kadarbitr"] = disp_src

    direct_sale, direct_src = _scan(
        sources, ["у должника", "напрямую у", "без торгов", "вне торгов"]
    )
    rent_rights, rent_src = _scan(
        sources, ["аренд", "найм", "субаренд"]
    )

    disc_explained, _ = _scan(
        sources,
        ["согласован", "неустроен", "аварийн", "ремонт", "самовольн", "техническ"],
    )

    facts = {
        "lot_url": lot.get("url", ""),
        "object_type": object_type,
        "address": address_out,
        "price_lot": price_lot if price_lot else None,
        "price_market_estimate": price_market,
        "discount_pct": discount_pct,
        "auction_type": auction_type,
        "deposit": deposit,
        "deadlines": deadlines,
        "egrn_clean": egrn_clean,
        "encumbrances": encumbrances,
        "registered_persons": registered_persons,
        "minors_registered": minors_registered,
        "sole_housing": sole_housing,
        "active_disputes_kadarbitr": active_disputes,
        "source_per_field": src_map,
        "_direct_sale": direct_sale,
        "_rent_rights": rent_rights,
        "_extreme_discount_unexplained": (
            discount_pct is not None and discount_pct >= 50 and not disc_explained
        ),
        "_has_pdf": bool(pdf and len(pdf) > 100),
        "_is_housing": object_type == "квартира" or lot_type in ("квартира", "апартаменты", "дом"),
        "_participants": lot.get("participants", 0),
        "_public_offer_reduction": bool(lot.get("next_reduction_date")),
    }
    return facts


def score_cancellation_risk(facts: dict) -> dict:
    """ШАГ 2 — риск отмены сделки, старт 100."""
    score = 100
    red_factors: list[str] = []
    yellow_factors: list[str] = []
    manual_checks: list[str] = []

    if facts["registered_persons"] == "да":
        score -= 45
        red_factors.append("прописанные/зарегистрированные лица")

    if facts["minors_registered"] == "да":
        score -= 45
        red_factors.append("несовершеннолетние зарегистрированы")

    if facts["sole_housing"] == "да":
        score -= 45
        red_factors.append("единственное жильё должника")

    if facts.get("_direct_sale"):
        score -= 45
        red_factors.append("покупка не с открытых торгов")

    if facts["active_disputes_kadarbitr"] == "да":
        score -= 45
        red_factors.append("активный спор / оспаривание")

    if facts.get("_extreme_discount_unexplained"):
        score -= 40
        red_factors.append("экстремальный дисконт без объяснения в карточке")

    if facts["encumbrances"] != "не указано" or facts["egrn_clean"] == "нет":
        score -= 20
        yellow_factors.append("обременения / аресты в тексте")

    if facts.get("_rent_rights"):
        score -= 18
        yellow_factors.append("аренда / права третьих лиц")

    if facts["egrn_clean"] == "не указано" and facts["encumbrances"] == "не указано":
        score -= 20
        yellow_factors.append("нет данных по обременениям")
        manual_checks.append("выписка ЕГРН — обременения и аресты")

    if facts["_is_housing"]:
        if facts["registered_persons"] == "не указано":
            score -= 22
            yellow_factors.append("нет данных по прописанным")
            manual_checks.append("прописанные и зарегистрированные лица")
        if facts["minors_registered"] == "не указано" and facts["registered_persons"] != "нет":
            manual_checks.append("наличие несовершеннолетних")
        if facts["sole_housing"] == "не указано":
            manual_checks.append("статус единственного жилья должника")

    if facts["active_disputes_kadarbitr"] == "не указано":
        manual_checks.append("споры по лоту на kad.arbitr.ru")

    score = max(0, min(100, score))

    if red_factors:
        level = "🔴"
        score = min(score, 44)
    elif score >= 75:
        level = "🟢"
    elif score >= 45:
        level = "🟡"
    else:
        level = "🔴"

    if red_factors:
        level = "🔴"
    elif facts["_is_housing"] and (
        facts["registered_persons"] == "не указано"
        or facts["egrn_clean"] == "не указано"
    ):
        if level == "🟢":
            level = "🟡"
        score = min(score, 74)

    key_flags = (red_factors + yellow_factors)[:3]

    return {
        "score": score,
        "level": level,
        "red_factors": red_factors,
        "yellow_factors": yellow_factors,
        "key_flags": key_flags,
        "manual_checks": list(dict.fromkeys(manual_checks)),
    }


def score_investment_attractiveness(facts: dict, lot: dict) -> dict:
    """ШАГ 3 — инвест-привлекательность 0–100, только по JSON-фактам."""
    score = 40
    notes: list[str] = []

    obj = facts["object_type"]
    score += LIQUIDITY.get(obj, 45) * 0.25
    notes.append(f"ликвидность типа «{obj}»")

    disc = facts.get("discount_pct")
    if disc is not None and disc > 0:
        if disc >= 50:
            score += 5
            notes.append(f"дисконт {disc}% (экстремальный — учтён в риске)")
        elif disc >= 30:
            score += 18
            notes.append(f"дисконт {disc}%")
        elif disc >= 15:
            score += 12
            notes.append(f"дисконт {disc}%")
        else:
            score += 6
            notes.append(f"дисконт {disc}%")
    elif facts.get("price_market_estimate") is None and obj == "земля":
        notes.append("рыночная оценка земли не рассчитана — вручную")
    else:
        notes.append("дисконт не подтверждён данными")

    if facts["auction_type"] == "публичное предложение":
        score += 8
        notes.append("публичное предложение")
        if facts.get("_public_offer_reduction"):
            score += 10
            notes.append("есть график снижения — точка входа")

    region = lot.get("region", "")
    if region in ("moskva", "moskovskaya-oblast", "sankt-peterburg"):
        score += 8
        notes.append("крупный регион (спрос выше среднего)")
    elif region:
        score += 3

    parts = facts.get("_participants", 0)
    if parts == 0:
        score += 6
        notes.append("нет заявок")
    elif parts > 5:
        score -= 8
        notes.append(f"высокая конкуренция ({parts} заявок)")

    score = max(0, min(100, int(round(score))))
    return {"score": score, "notes": notes}


def decide_final_verdict(risk: dict, invest: dict) -> dict:
    """ШАГ 4 — итоговая логика."""
    r_level = risk["level"]
    i_score = invest["score"]
    high_invest = i_score >= 60
    manual = risk["manual_checks"]

    if r_level == "🔴":
        label = "Мимо"
        detail = "высокий риск отмены или оспаривания"
    elif r_level == "🟡" and high_invest:
        label = "Смотреть"
        missing = ", ".join(manual[:3]) if manual else "ключевые юридические данные"
        detail = f"нужна проверка: {missing}"
    elif r_level == "🟢" and high_invest:
        label = "Брать на due diligence"
        detail = "риск низкий при подтверждении данных"
    elif r_level == "🟢":
        label = "Чисто, но неинтересно по цене"
        detail = "риск низкий, привлекательность ниже порога"
    else:
        label = "Смотреть"
        missing = ", ".join(manual[:3]) if manual else "юридические данные"
        detail = f"нужна проверка: {missing}"

    return {"label": label, "detail": detail}


def format_verdict_card(facts: dict, risk: dict, invest: dict, verdict: dict) -> str:
    """Карточка вердикта для Telegram."""
    obj_line = facts["object_type"]
    if facts["address"] != "не указано":
        obj_line += f", {facts['address'][:80]}"

    price_s = _fmt_money(facts.get("price_lot"))
    mkt_s = _fmt_money(facts.get("price_market_estimate"))
    disc_s = (
        f"{facts['discount_pct']}%"
        if facts.get("discount_pct") is not None
        else "не указано"
    )

    dep_s = _fmt_money(facts.get("deposit")) if facts.get("deposit") else "не указано"

    manual_line = ""
    if risk["manual_checks"]:
        manual_line = (
            f"\nЧто проверить вручную: {', '.join(risk['manual_checks'][:4])}"
        )

    flags_line = ", ".join(risk["key_flags"]) if risk["key_flags"] else "не выявлено в тексте"

    return (
        f"*Объект:* {obj_line}\n"
        f"*Цена лота:* {price_s} | *Рыночная оценка:* {mkt_s} | *Дисконт:* {disc_s}\n"
        f"*Стадия:* {facts['auction_type']} | *Задаток:* {dep_s} | *Сроки:* {facts['deadlines']}\n"
        f"*Риск отмены:* {risk['level']} ({risk['score']}/100)\n"
        f"*Инвест-привлекательность:* {invest['score']}/100\n"
        f"*Ключевые флаги:* {flags_line}\n"
        f"*Итог:* {verdict['label']} — {verdict['detail']}"
        f"{manual_line}\n\n"
        f"_{LEGAL_FOOTER}_"
    )


def run_verdict_pipeline(lot: dict, an: dict) -> dict:
    """Полный пайплайн: факты → риск → инвест → карточка."""
    facts = extract_lot_facts(lot, an)
    risk = score_cancellation_risk(facts)
    invest = score_investment_attractiveness(facts, lot)
    verdict = decide_final_verdict(risk, invest)
    card = format_verdict_card(facts, risk, invest, verdict)
    return {
        "facts_json": facts,
        "risk_score": risk["score"],
        "risk_level": risk["level"],
        "invest_score": invest["score"],
        "verdict_label": verdict["label"],
        "verdict_detail": verdict["detail"],
        "verdict_card": card,
        "manual_checks": risk["manual_checks"],
        "key_flags": risk["key_flags"],
    }

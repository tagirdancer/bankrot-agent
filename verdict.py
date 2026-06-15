"""
Структурированный вердикт по лоту — только факты из прочитанных документов.
Без рекомендаций «бери/не бери» и без выдуманной уверенности.
"""
from __future__ import annotations

import re
from typing import Any

LEGAL_FOOTER = (
    "Это не юридическое заключение и не инвестиционная рекомендация. "
    "Решение — за вами; юрист по конкретному лоту обязателен."
)


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


def _egrn_object_label(rec: dict) -> str:
    title = (rec.get("source_title") or "").strip()
    if title:
        if "гараж" in title.lower():
            return "гараж"
        if "з.у" in title.lower() or "земл" in title.lower() or "з-у" in title.lower():
            return "земля"
        if "здан" in title.lower():
            return "здание"
        return title[:50]
    cad = rec.get("cadastral") or "объект"
    return f"кад. {cad}"


def _egrn_object_line(rec: dict, idx: int) -> dict:
    """Конкретная строка по объекту ЕГРН с источником."""
    label = _egrn_object_label(rec)
    cad = rec.get("cadastral") or "не указано"
    source = rec.get("source_title") or rec.get("source") or f"ЕГРН #{idx}"
    enc = rec.get("encumbrances") or "не указано в тексте"
    if rec.get("encumbrances_clean"):
        enc_display = enc
    else:
        enc_display = enc

    return {
        "label": label,
        "cadastral": cad,
        "owner": rec.get("owner") or "не указано",
        "area": rec.get("area") or "не указано",
        "encumbrances": enc_display,
        "encumbrances_clean": bool(rec.get("encumbrances_clean")),
        "arrests": rec.get("arrests") or "не указано",
        "source": source,
        "parsed_ok": bool(rec.get("parsed_ok")),
    }


def _build_market_comparison(lot: dict, an: dict, appr: dict) -> dict:
    """Сравнение цены лота с рынком — только если рынок из отчёта/документов."""
    price_lot = float(lot.get("price") or an.get("lot_price_raw") or 0)
    market = int(
        appr.get("market_price")
        or appr.get("appraisal_price")
        or an.get("market_price_raw")
        or 0
    )
    source = ""
    if appr.get("parsed_ok") and (appr.get("market_price") or appr.get("appraisal_price")):
        source = f"отчёт об оценке ({appr.get('extract_method') or 'текст'})"
        if appr.get("source_title"):
            source += f": {appr['source_title'][:60]}"
    elif an.get("market_source") == "appraisal":
        source = "отчёт об оценке (карточка)"
    elif an.get("market_known") and an.get("market_source"):
        source = f"ориентир {an.get('market_source')} — не из отчёта"
        market = int(an.get("market_price_raw") or 0)

    if not market or market <= 0:
        return {
            "known": False,
            "lines": ["Рынок не определён — отчёт не распознан или стоимость не извлечена"],
            "discount_pct": None,
        }

    lines = [
        f"Рыночная оценка: {_fmt_money(market)} — источник: {source or 'отчёт'}",
    ]
    if price_lot > 0:
        disc = round((market - price_lot) / market * 100) if market > price_lot else 0
        premium = round((price_lot - market) / market * 100) if price_lot > market else 0
        lines.append(f"Цена лота: {_fmt_money(price_lot)}")
        if disc > 0:
            lines.append(f"Дисконт к оценке отчёта: {disc}%")
        elif premium > 0:
            lines.append(f"Лот дороже оценки отчёта на {premium}%")
        else:
            lines.append("Цена лота равна оценке отчёта")
    else:
        disc = None
        lines.append("Цена лота на сайте не определена — дисконт не считается")

    if appr.get("comparables_range", "не указано") != "не указано":
        lines.append(f"По аналогам (раздел «Объявления», отчёт): {appr['comparables_range']}")
    if appr.get("liquidation_price"):
        lines.append(f"Ликвидационная стоимость (отчёт): {_fmt_money(appr['liquidation_price'])}")

    return {"known": True, "lines": lines, "discount_pct": disc if price_lot > 0 else None, "market": market}


def _build_trading_section(lot: dict, appr: dict) -> list[str]:
    """Торги: карточка + аналитика из отчёта (если есть)."""
    lines = []
    fmt = lot.get("auction_format") or ""
    if fmt:
        lines.append(f"Формат (карточка): {fmt}")
    sc, st = lot.get("step_current", 0), lot.get("step_total", 0)
    if sc and st:
        left = max(0, st - sc)
        lines.append(f"Стадия (карточка): шаг {sc}/{st}, осталось снижений: {left}")
    if lot.get("next_reduction_date") and lot.get("next_reduction_price"):
        np = lot["next_reduction_price"]
        lines.append(
            f"След. снижение (карточка): {lot['next_reduction_date']} → {_fmt_money(np)}"
        )
    parts = lot.get("participants")
    if parts is not None:
        lines.append(f"Заявок на момент карточки: {parts}")

    if appr.get("auction_avg_reduction_pct", "не указано") != "не указано":
        lines.append(f"Среднее снижение (отчёт, аналитика торгов): {appr['auction_avg_reduction_pct']}")
    if appr.get("auction_participants_hint", "не указано") != "не указано":
        lines.append(f"Заявок по аналитике отчёта: {appr['auction_participants_hint']}")

    return lines


def _documents_coverage(lot: dict) -> dict[str, str]:
    """Статус каждого типа документов: получен/распознан/не получен."""
    docs = lot.get("lot_documents") or []
    by_type: dict[str, list] = {}
    for d in docs:
        by_type.setdefault(d.get("type") or "other", []).append(d)

    def _status(doc_type: str) -> str:
        items = by_type.get(doc_type) or []
        if doc_type == "photo":
            if any(x.get("download_ok") for x in items):
                return "есть"
            return "не получен"
        if not items:
            if doc_type == "egrn" and (lot.get("egrn_records") or lot.get("egrn_read_ok")):
                return "распознан"
            return "не получен"
        ok = [x for x in items if x.get("download_ok")]
        parsed = [x for x in ok if (x.get("text_len") or 0) >= 40 or (x.get("extracted") or {}).get("parsed_ok")]
        if parsed:
            return "распознан"
        if ok:
            return "скачан, текст не извлечён"
        return "не скачан"

    return {
        "egrn": _status("egrn"),
        "appraisal": _status("appraisal"),
        "contract": _status("contract"),
        "application": _status("application"),
        "info_message": _status("info_message"),
        "photo": _status("photo"),
    }


def extract_document_facts(lot: dict, an: dict) -> dict[str, Any]:
    """Факты строго из прочитанных документов и полей парсера."""
    egrn_records = list(lot.get("egrn_records") or [])
    egrn_parsed = lot.get("egrn_parsed") or {}
    if not egrn_records and egrn_parsed.get("objects"):
        egrn_records = list(egrn_parsed["objects"])

    egrn_objects: list[dict] = []
    for i, rec in enumerate(egrn_records, 1):
        egrn_objects.append(_egrn_object_line(rec, i))

    coverage = _documents_coverage(lot)
    has_any_egrn = bool(egrn_objects) or coverage["egrn"] in ("распознан", "скачан, текст не извлечён")

    enc_issues = [o for o in egrn_objects if not o.get("encumbrances_clean") and o["encumbrances"] != "не указано в тексте"]
    enc_clean = [o for o in egrn_objects if o.get("encumbrances_clean")]

    appr = lot.get("appraisal_parsed") or {}
    market_cmp = _build_market_comparison(lot, an, appr)
    trading_lines = _build_trading_section(lot, appr)

    # статус отчёта
    appr_doc = next((d for d in (lot.get("lot_documents") or []) if d.get("type") == "appraisal"), None)
    appr_method = appr.get("extract_method") or (appr_doc or {}).get("method") or ""
    if appr.get("parsed_ok"):
        appr_status = f"распознан ({appr_method or 'текст'})"
    elif coverage["appraisal"] == "скачан, текст не извлечён":
        appr_status = f"скачан, OCR не дал текста ({appr_method or 'failed'})"
    else:
        appr_status = coverage["appraisal"]

    contract = lot.get("contract_parsed") or {}
    application = lot.get("application_parsed") or {}
    info_msg = lot.get("info_message_parsed") or {}

    doc_types_found = []
    for d in lot.get("lot_documents") or []:
        if d.get("download_ok"):
            doc_types_found.append(d.get("type") or "other")
    doc_types_found = list(dict.fromkeys(doc_types_found))

    return {
        "egrn_count": len(egrn_objects),
        "egrn_objects": egrn_objects,
        "egrn_has_data": has_any_egrn,
        "egrn_encumbrance_issues": enc_issues,
        "egrn_encumbrance_clean": enc_clean,
        "coverage": coverage,
        "appraisal_status": appr_status,
        "appraisal_parsed": appr,
        "market_comparison": market_cmp,
        "trading_lines": trading_lines,
        "appraisal_flags": appr.get("restriction_flags") or lot.get("appraisal_flags") or [],
        "appraisal_court": appr.get("court_cases") or "не указано",
        "contract_summary": contract.get("summary") if contract.get("parsed_ok") else "",
        "application_summary": application.get("summary") if application.get("parsed_ok") else "",
        "info_message_summary": info_msg.get("summary") if info_msg.get("parsed_ok") else "",
        "documents_downloaded": lot.get("documents_downloaded_count") or 0,
        "doc_types_found": doc_types_found,
        "price_lot": lot.get("price") or an.get("lot_price_raw"),
        "discount_pct": market_cmp.get("discount_pct") if market_cmp.get("known") else an.get("discount_pct"),
        "document_status": an.get("document_status") or "",
    }


def build_verdict_sections(facts: dict) -> dict[str, Any]:
    """Плюсы, минусы, ручные проверки, уровень риска — только из facts."""
    pluses: list[str] = []
    minuses: list[str] = []
    manual: list[str] = []
    known_lines: list[str] = []
    unknown_lines: list[str] = []

    cov = facts["coverage"]

    # --- Известное из документов ---
    if facts["egrn_count"]:
        known_lines.append(f"ЕГРН: {facts['egrn_count']} выписк(и/а) распознана(ы)")
        for i, obj in enumerate(facts["egrn_objects"], 1):
            line = (
                f"  • Объект {i} ({obj['label']}), кад. {obj['cadastral']}: "
                f"{obj['encumbrances']} [источник: {obj['source'][:55]}]"
            )
            if obj["owner"] != "не указано":
                line += f"; собственник: {obj['owner'][:60]}"
            if obj["area"] != "не указано":
                line += f"; {obj['area']}"
            known_lines.append(line)
    elif cov["egrn"] == "скачан, текст не извлечён":
        unknown_lines.append("ЕГРН: файл скачан, текст не извлечён")
    elif cov["egrn"] == "не получен":
        unknown_lines.append("ЕГРН: не получена")

    mc = facts.get("market_comparison") or {}
    if mc.get("known"):
        known_lines.append("*Сравнение с рынком (отчёт):*")
        known_lines.extend(f"  {ln}" for ln in mc.get("lines", []))
    else:
        for ln in mc.get("lines", ["Рынок не определён"]):
            unknown_lines.append(ln)

    if facts.get("appraisal_status") and "распознан" in facts["appraisal_status"]:
        appr = facts.get("appraisal_parsed") or {}
        if appr.get("summary"):
            known_lines.append(f"Отчёт об оценке [{facts['appraisal_status']}]: {appr['summary'][:200]}")
    elif cov["appraisal"] == "скачан, текст не извлечён":
        unknown_lines.append(
            f"Отчёт об оценке: {facts.get('appraisal_status', 'скачан, текст не извлечён')}"
        )

    if facts.get("trading_lines"):
        known_lines.append("*Торги:*")
        known_lines.extend(f"  • {tl}" for tl in facts["trading_lines"])

    if cov["contract"] == "распознан" and facts["contract_summary"]:
        known_lines.append(f"Договор: {facts['contract_summary'][:120]}")
    elif cov["contract"] == "скачан, текст не извлечён":
        unknown_lines.append("Договор: скачан, текст не извлечён")
    if cov["application"] == "распознан" and facts["application_summary"]:
        known_lines.append(f"Заявка: {facts['application_summary'][:100]}")
    elif cov["application"] == "распознан":
        unknown_lines.append("Заявка: шаблон без заполненных полей")
    if cov["info_message"] == "распознан" and facts["info_message_summary"]:
        known_lines.append(f"Инф.сообщение: {facts['info_message_summary'][:100]}")
    elif cov["info_message"] == "скачан, текст не извлечён":
        unknown_lines.append("Инф.сообщение: скачан, текст не извлечён")
    if cov["photo"] == "есть":
        known_lines.append("Фото объекта: есть на сайте")

    # --- Плюсы ---
    for obj in facts["egrn_encumbrance_clean"]:
        pluses.append(
            f"ЕГРН «{obj['source'][:45]}» (кад. {obj['cadastral']}): {obj['encumbrances']}"
        )
    if facts["egrn_count"] >= 2:
        pluses.append(f"Комплект выписок: {facts['egrn_count']} объекта")
    if mc.get("known") and mc.get("discount_pct") and mc["discount_pct"] > 0:
        pluses.append(
            f"Дисконт {mc['discount_pct']}% к рыночной оценке отчёта (факт из документов)"
        )
    if mc.get("known"):
        pluses.append("Рыночный ориентир из отчёта об оценке")
    if facts["contract_summary"]:
        pluses.append("Проект договора прочитан — виден предмет сделки")
    if cov["photo"] == "есть":
        pluses.append("Есть фото объекта")

    # --- Минусы / внимание ---
    for obj in facts["egrn_encumbrance_issues"]:
        minuses.append(
            f"ЕГРН «{obj['source'][:45]}» (кад. {obj['cadastral']}): {obj['encumbrances']}"
        )
    for obj in facts["egrn_objects"]:
        if obj["arrests"] and obj["arrests"] != "не указано":
            minuses.append(
                f"ЕГРН «{obj['source'][:40]}»: аресты — {obj['arrests'][:90]}"
            )
    for flag in facts["appraisal_flags"]:
        minuses.append(f"Отчёт об оценке: {flag}")
    if facts["appraisal_court"] not in ("не указано", "не обнаружены", ""):
        if "обнаруж" in str(facts["appraisal_court"]).lower():
            minuses.append(f"Отчёт об оценке: судебные дела — {facts['appraisal_court']}")
    if not facts["egrn_has_data"]:
        minuses.append("Нет распознанной выписки ЕГРН — юр. статус не проверен")
    if cov["appraisal"] == "скачан, текст не извлечён" or not (facts.get("appraisal_parsed") or {}).get("parsed_ok"):
        if cov["appraisal"] != "не получен":
            minuses.append("Отчёт об оценке не распознан — сравнение с рынком недоступно")

    # --- Ручные проверки ---
    if not facts["egrn_has_data"]:
        manual.append("Актуальная выписка ЕГРН на каждый объект (здание, земля, гараж)")
    elif facts["egrn_encumbrance_issues"]:
        manual.append("Расшифровка обременений/ограничений с юристом по каждой выписке")
    if cov["appraisal"] != "распознан":
        manual.append("Отчёт об оценке — прочитать вручную или заказать OCR")
    if cov["info_message"] == "скачан, текст не извлечён":
        manual.append("Информационное сообщение — условия торгов и сроки")
    manual.append("Прописанные лица, единственное жильё, споры на kad.arbitr.ru")
    if facts["egrn_count"]:
        manual.append("Соответствие кадастровых номеров в договоре и выписках")

    manual = list(dict.fromkeys(m for m in manual if m))

    risk_level = _calc_risk_level(facts, cov)
    risk_reasons = _risk_reasons(facts, cov)
    assessment_score, assessment_logic = _unified_assessment(facts, risk_level)

    return {
        "known_lines": known_lines,
        "unknown_lines": unknown_lines,
        "pluses": pluses[:8],
        "minuses": minuses[:10],
        "manual_checks": manual[:6],
        "document_risk_level": risk_level,
        "document_risk_reasons": risk_reasons,
        "assessment_score": assessment_score,
        "assessment_logic": assessment_logic,
    }


def _calc_risk_level(facts: dict, cov: dict) -> str:
    risk_score = 0
    if not facts["egrn_has_data"]:
        risk_score += 3
    if facts["egrn_encumbrance_issues"]:
        risk_score += 2 * len(facts["egrn_encumbrance_issues"])
    if any("арест" in (o.get("arrests") or "").lower() for o in facts["egrn_objects"]):
        risk_score += 2
    if not (facts.get("market_comparison") or {}).get("known"):
        risk_score += 1
    if facts["appraisal_flags"]:
        risk_score += 1
    if risk_score >= 4:
        return "высокий"
    if risk_score >= 2:
        return "средний"
    if facts["egrn_has_data"] and not facts["egrn_encumbrance_issues"]:
        return "низкий"
    return "высокий" if not facts["egrn_has_data"] else "средний"


def _risk_reasons(facts: dict, cov: dict) -> list[str]:
    reasons: list[str] = []
    if not facts["egrn_has_data"]:
        reasons.append("нет проверенной выписки ЕГРН")
    for obj in facts["egrn_encumbrance_issues"]:
        reasons.append(f"{obj['label']} (кад. {obj['cadastral']}): {obj['encumbrances'][:60]}")
    if any("арест" in (o.get("arrests") or "").lower() for o in facts["egrn_objects"]):
        reasons.append("упоминание арестов в выписке")
    if not (facts.get("market_comparison") or {}).get("known"):
        reasons.append("рыночный ориентир из отчёта отсутствует")
    if facts["appraisal_flags"]:
        reasons.append("зоны ограничений в отчёте")
    if not reasons:
        reasons.append("критичных обременений в прочитанных выписках не найдено")
    return reasons[:5]


def _unified_assessment(facts: dict, risk_level: str) -> tuple[int, str]:
    """
    Единый балл 0–100: выше = больше ясности по документам и ниже юр. риск.
    Не инвест-привлекательность — информированность для решения.
    """
    base = {"низкий": 72, "средний": 48, "высокий": 22}[risk_level]
    logic = [f"риск по документам: {risk_level}"]
    mc = facts.get("market_comparison") or {}
    if mc.get("known"):
        base += 12
        logic.append("рыночная оценка из отчёта прочитана")
    else:
        base -= 8
        logic.append("рынок не определён — ценовая привлекательность не оценивается")
    if facts["egrn_encumbrance_issues"]:
        base -= 6 * min(len(facts["egrn_encumbrance_issues"]), 3)
        logic.append(
            f"обременения в {len(facts['egrn_encumbrance_issues'])} выписках снижают балл"
        )
    if facts["egrn_count"] >= 2:
        base += 4
        logic.append(f"прочитано {facts['egrn_count']} выписок ЕГРН")
    return max(0, min(100, base)), "; ".join(logic)


def format_verdict_card(facts: dict, sections: dict) -> str:
    lines = ["*Вердикт по документам*", ""]

    if sections["known_lines"]:
        lines.append("*Что известно из документов:*")
        lines.extend(sections["known_lines"])
        lines.append("")

    if sections["unknown_lines"]:
        lines.append("*Что не определено:*")
        for u in sections["unknown_lines"]:
            lines.append(f"• {u}")
        lines.append("")

    if sections["pluses"]:
        lines.append("*В плюс (по фактам):*")
        for p in sections["pluses"]:
            lines.append(f"• {p}")
        lines.append("")

    if sections["minuses"]:
        lines.append("*Минусы / на что обратить внимание:*")
        for m in sections["minuses"]:
            lines.append(f"• {m}")
        lines.append("")

    if sections["manual_checks"]:
        lines.append("*Проверить вручную (сверх документов):*")
        for c in sections["manual_checks"]:
            lines.append(f"• {c}")
        lines.append("")

    rl = sections["document_risk_level"]
    rr = "; ".join(sections["document_risk_reasons"][:3])
    lines.append(f"*Риск по документам:* {rl} — {rr}")

    asc = sections.get("assessment_score")
    if asc is not None:
        lines.append(f"*Оценка по документам:* {asc}/100")
        if sections.get("assessment_logic"):
            lines.append(f"  _{sections['assessment_logic']}_")

    price_s = _fmt_money(facts.get("price_lot"))
    mc = facts.get("market_comparison") or {}
    if mc.get("known") and mc.get("discount_pct"):
        disc_s = f"{mc['discount_pct']}% к оценке отчёта"
    else:
        disc = facts.get("discount_pct")
        disc_s = f"{disc}%" if disc and str(disc) not in ("?", "0", "") else "не определён"
    lines.append(f"*Цена лота:* {price_s} | *Дисконт:* {disc_s}")

    lines.append("")
    lines.append(f"_{LEGAL_FOOTER}_")
    return "\n".join(lines)


def run_verdict_pipeline(lot: dict, an: dict) -> dict:
    """Полный пайплайн вердикта по документам."""
    facts = extract_document_facts(lot, an)
    sections = build_verdict_sections(facts)
    card = format_verdict_card(facts, sections)

    rl = sections["document_risk_level"]
    asc = sections.get("assessment_score", 50)
    headline = f"Риск: {rl} | оценка {asc}/100"
    detail = sections.get("assessment_logic") or "; ".join(sections["document_risk_reasons"][:2])

    risk_emoji = {"низкий": "🟢", "средний": "🟡", "высокий": "🔴"}.get(rl, "🟡")

    return {
        "facts_json": facts,
        "document_risk_level": rl,
        "document_risk_reasons": sections["document_risk_reasons"],
        "verdict_pluses": sections["pluses"],
        "verdict_minuses": sections["minuses"],
        "manual_checks": sections["manual_checks"],
        "known_lines": sections["known_lines"],
        "unknown_lines": sections["unknown_lines"],
        "assessment_score": asc,
        "assessment_logic": sections.get("assessment_logic", ""),
        "risk_score": 100 - asc,
        "risk_level": risk_emoji,
        "invest_score": asc,
        "verdict_label": headline,
        "verdict_detail": detail,
        "verdict_card": card,
        "key_flags": sections["minuses"][:3],
    }


# --- совместимость со старыми вызовами ---
def extract_lot_facts(lot: dict, an: dict) -> dict:
    return extract_document_facts(lot, an)


def score_cancellation_risk(facts: dict) -> dict:
    sections = build_verdict_sections(facts)
    rl = sections["document_risk_level"]
    return {
        "score": {"низкий": 75, "средний": 50, "высокий": 25}.get(rl, 50),
        "level": {"низкий": "🟢", "средний": "🟡", "высокий": "🔴"}.get(rl, "🟡"),
        "red_factors": sections["minuses"][:3],
        "yellow_factors": sections["unknown_lines"][:2],
        "key_flags": sections["minuses"][:3],
        "manual_checks": sections["manual_checks"],
    }


def score_investment_attractiveness(facts: dict, lot: dict) -> dict:
    return {"score": 0, "notes": ["не рассчитывается — вердикт только по документам"]}


def decide_final_verdict(risk: dict, invest: dict) -> dict:
    return {"label": "см. verdict_label", "detail": ""}

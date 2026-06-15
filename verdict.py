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


def _encumbrance_status(text: str) -> str:
    if not text or not str(text).strip():
        return "не указано"
    tl = str(text).lower()
    has_negative = any(x in tl for x in (
        "ограничен", "ипотек", "залог", "арест", "сервитут", "рента",
        "статьей 56", "ст. 56",
    ))
    has_clean = any(x in tl for x in (
        "не зарегистрир", "отсутств", "нет (по тексту", "без обремен",
        "обременени",  # часто «обременения: не зарегистрировано»
    ))
    if has_negative and ("ограничен" in tl or "статьей" in tl or "ипотек" in tl or "арест" in tl):
        return "есть ограничения/обременения"
    if has_negative:
        return "есть упоминания — проверить вручную"
    if has_clean and "не зарегистрир" in tl:
        return "нет (по тексту ЕГРН)"
    if has_clean:
        return "явных обременений в тексте нет"
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
    for rec in egrn_records:
        enc_st = _encumbrance_status(rec.get("encumbrances", ""))
        egrn_objects.append({
            "label": _egrn_object_label(rec),
            "cadastral": rec.get("cadastral") or "не указано",
            "owner": rec.get("owner") or "не указано",
            "area": rec.get("area") or "не указано",
            "encumbrances": enc_st,
            "encumbrances_raw": (rec.get("encumbrances") or "")[:160],
            "arrests": rec.get("arrests") or "не указано",
            "parsed_ok": bool(rec.get("parsed_ok")),
            "source": rec.get("source_title") or "ЕГРН",
        })

    coverage = _documents_coverage(lot)
    has_any_egrn = bool(egrn_objects) or coverage["egrn"] in ("распознан", "скачан, текст не извлечён")

    # Сводка по обременениям
    enc_issues = [o for o in egrn_objects if "есть" in o["encumbrances"]]
    enc_clean = [o for o in egrn_objects if o["encumbrances"].startswith("нет")]

    appr = lot.get("appraisal_parsed") or {}
    market_known = bool(an.get("market_known"))
    market_source = an.get("market_source") or ""
    if appr.get("parsed_ok"):
        market_line = appr.get("summary") or "ориентир из отчёта об оценке"
        market_status = "определена из отчёта"
    elif coverage["appraisal"] == "скачан, текст не извлечён":
        market_line = "отчёт скачан, текст не распознан (вероятно скан)"
        market_status = "не определена"
    elif coverage["appraisal"] == "распознан":
        market_line = appr.get("summary") or "данные отчёта частично извлечены"
        market_status = "частично"
    elif market_known and market_source == "appraisal":
        market_line = an.get("market_comment") or "из отчёта"
        market_status = "из отчёта (карточка)"
    elif market_known:
        market_line = f"ориентир {an.get('market_price', '—')} ({market_source or 'расчёт'})"
        market_status = "ориентир без отчёта"
    else:
        market_line = "не определена — отчёт не распознан или не получен"
        market_status = "не определена"

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
        "market_status": market_status,
        "market_line": market_line,
        "appraisal_flags": appr.get("restriction_flags") or lot.get("appraisal_flags") or [],
        "appraisal_court": appr.get("court_cases") or "не указано",
        "contract_summary": contract.get("summary") if contract.get("parsed_ok") else "",
        "application_summary": application.get("summary") if application.get("parsed_ok") else "",
        "info_message_summary": info_msg.get("summary") if info_msg.get("parsed_ok") else "",
        "documents_downloaded": lot.get("documents_downloaded_count") or 0,
        "doc_types_found": doc_types_found,
        "price_lot": lot.get("price") or an.get("lot_price_raw"),
        "discount_pct": an.get("discount_pct"),
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
        for obj in facts["egrn_objects"]:
            line = (
                f"  • {obj['label']}: кад. {obj['cadastral']}, "
                f"обременения — {obj['encumbrances']}"
            )
            if obj["owner"] != "не указано":
                line += f", собственник: {obj['owner'][:70]}"
            known_lines.append(line)
    elif cov["egrn"] == "скачан, текст не извлечён":
        unknown_lines.append("ЕГРН: файл скачан, текст не извлечён")
    elif cov["egrn"] == "не получен":
        unknown_lines.append("ЕГРН: не получена")

    if facts["market_status"] == "определена из отчёта":
        known_lines.append(f"Рыночная оценка: {facts['market_line']}")
    else:
        unknown_lines.append(f"Рыночная цена: {facts['market_line']}")

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
            f"ЕГРН ({obj['label']}): обременений по тексту выписки нет"
        )
    if facts["egrn_count"] >= 2:
        pluses.append(f"Полный комплект выписок: {facts['egrn_count']} объекта в документах")
    if facts["market_status"] == "определена из отчёта":
        pluses.append("Есть ориентир рыночной стоимости из отчёта об оценке")
    if facts["contract_summary"]:
        pluses.append("Проект договора прочитан — виден предмет сделки")
    if cov["photo"] == "есть":
        pluses.append("Есть фото объекта")

    # --- Минусы / внимание ---
    for obj in facts["egrn_encumbrance_issues"]:
        minuses.append(
            f"ЕГРН ({obj['label']}): {obj['encumbrances']}"
            + (f" — {obj['encumbrances_raw'][:80]}" if obj.get("encumbrances_raw") else "")
        )
    for obj in facts["egrn_objects"]:
        if obj["arrests"] and obj["arrests"] != "не указано":
            minuses.append(f"ЕГРН ({obj['label']}): аресты — {obj['arrests'][:80]}")
    for flag in facts["appraisal_flags"]:
        minuses.append(f"Отчёт об оценке: зона/ограничение — {flag}")
    if facts["appraisal_court"] not in ("не указано", "не обнаружены", ""):
        if "обнаруж" in str(facts["appraisal_court"]).lower():
            minuses.append(f"Отчёт об оценке: судебные дела — {facts['appraisal_court']}")
    if not facts["egrn_has_data"]:
        minuses.append("Нет распознанной выписки ЕГРН — юр. статус не проверен")
    if cov["appraisal"] == "скачан, текст не извлечён":
        minuses.append("Отчёт об оценке не распознан — рыночный ориентир недоступен")

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

    # --- Уровень риска по документам ---
    risk_score = 0
    risk_reasons: list[str] = []

    if not facts["egrn_has_data"]:
        risk_score += 3
        risk_reasons.append("нет проверенной выписки ЕГРН")
    if facts["egrn_encumbrance_issues"]:
        risk_score += 2 * len(facts["egrn_encumbrance_issues"])
        risk_reasons.append("обременения/ограничения в выписках")
    if any("арест" in (o.get("arrests") or "").lower() for o in facts["egrn_objects"]):
        risk_score += 2
        risk_reasons.append("упоминание арестов")
    if cov["appraisal"] == "скачан, текст не извлечён":
        risk_score += 1
        risk_reasons.append("отчёт об оценке не распознан")
    if facts["appraisal_flags"]:
        risk_score += 1
        risk_reasons.append("зоны ограничений в отчёте")

    if risk_score >= 4:
        risk_level = "высокий"
    elif risk_score >= 2:
        risk_level = "средний"
    elif facts["egrn_has_data"] and not facts["egrn_encumbrance_issues"]:
        risk_level = "низкий"
    elif not facts["egrn_has_data"]:
        risk_level = "высокий"
    else:
        risk_level = "средний"

    if not risk_reasons:
        if risk_level == "низкий":
            risk_reasons.append("выписки прочитаны, критичных обременений в тексте не найдено")
        else:
            risk_reasons.append("данных недостаточно для уверенной оценки")

    return {
        "known_lines": known_lines,
        "unknown_lines": unknown_lines,
        "pluses": pluses[:6],
        "minuses": minuses[:8],
        "manual_checks": manual[:6],
        "document_risk_level": risk_level,
        "document_risk_reasons": risk_reasons[:4],
    }


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
    rr = "; ".join(sections["document_risk_reasons"])
    lines.append(f"*Риск по документам:* {rl} — {rr}")

    price_s = _fmt_money(facts.get("price_lot"))
    disc = facts.get("discount_pct")
    disc_s = f"{disc}%" if disc and str(disc) not in ("?", "0", "") else "не указано"
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
    headline = f"Риск по документам: {rl}"
    detail = "; ".join(sections["document_risk_reasons"][:2]) or "см. карточку"

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
        "risk_score": {"низкий": 25, "средний": 55, "высокий": 85}.get(rl, 55),
        "risk_level": risk_emoji,
        "invest_score": None,
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

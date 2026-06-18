"""
Агент v10.0 — двухфaseный прогон под лимит GitHub Actions (60 мин)
- Фаза 1: лёгкий проход (карточка, балл) по всем лотам
- Фаза 2: PDF ЕГРН + Groq только для топ-кандидатов
- Таймауты на операции; частичный дайджест при нехватке времени
"""
import os, asyncio, schedule, time, pdfplumber, io, re, logging, json
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from analyzer import (analyze_lot, detect_type, get_lot_details, MIN_SCORE, MIN_DISCOUNT_PCT,
                      HOT_LABEL_PCT, DIGEST_TOP_N, DIGEST_FORMULA, TG_MSG_LIMIT,
                      format_short_lot_message, format_short_lot_message_plain,
                      format_minimal_lot_card, lot_action_keyboard, format_price_line,
                      enrich_digest_metrics, clamp_telegram_message,
                      get_rosreestr_data, is_real_estate)
from database import init_db, record_digest_lot, save_agent_run

load_dotenv()
log = logging.getLogger("agent")

from platform_config import is_tbankrot_enabled, is_mass_scraping_enabled

LOGIN    = os.getenv("TBANKROT_LOGIN")
PASSWORD = os.getenv("TBANKROT_PASSWORD")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]


def ensure_playwright_env() -> None:
    os.environ.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "1")


async def launch_browser(playwright):
    """Chromium для Railway/Docker: без sandbox + пропуск проверки host deps."""
    ensure_playwright_env()
    try:
        browser = await playwright.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        log.info("Chromium launched")
        return browser
    except Exception:
        log.exception("Chromium launch failed")
        raise

REGIONS_MAIN  = ["moskva", "moskovskaya-oblast"]
REGIONS_EXTRA = ["sankt-peterburg","krasnodar","ekaterinburg","novosibirsk"]

MAX_PAGES       = int(os.getenv("AGENT_MAX_PAGES", "12"))
EXTRA_MAX_PAGES = int(os.getenv("AGENT_EXTRA_PAGES", "3"))
TOP_N           = 15
HEAVY_TOP_N     = int(os.getenv("AGENT_HEAVY_TOP", str(TOP_N)))

# Бюджет прогона (сек): по умолчанию 55 мин — оставляем запас на отправку в TG
RUN_BUDGET_SEC    = int(os.getenv("AGENT_BUDGET_SEC", "3300"))
FLUSH_BEFORE_SEC  = int(os.getenv("AGENT_FLUSH_SEC", "300"))
LOT_TIMEOUT_LIGHT = int(os.getenv("LOT_TIMEOUT_LIGHT", "12"))
LOT_TIMEOUT_HEAVY = int(os.getenv("LOT_TIMEOUT_HEAVY", "45"))
MAX_HEAVY_LOTS    = int(os.getenv("AGENT_MAX_HEAVY", "45"))
PDF_TIMEOUT       = int(os.getenv("PDF_TIMEOUT", "90"))
HEAVY_MIN_SCORE   = float(os.getenv("AGENT_HEAVY_MIN_SCORE", "0"))

CATEGORIES = {
    "квартира":    {"icon":"🏠","label":"Квартиры",                 "default":True},
    "апартаменты": {"icon":"🏙️","label":"Апартаменты",              "default":True},
    "дом":       {"icon":"🏡","label":"Дома и дачи",              "default":True},
    "коммерция": {"icon":"🏢","label":"Коммерческая недвижимость","default":True},
    "земля":     {"icon":"🌱","label":"Земельные участки",        "default":True},
    "авто":      {"icon":"🚗","label":"Транспорт",                "default":False},
    "гараж":     {"icon":"🅿️","label":"Гаражи",                  "default":False},
    "бизнес":    {"icon":"💼","label":"Бизнес",                   "default":False},
    "прочее":    {"icon":"📦","label":"Прочее",                   "default":False},
}

DEFAULT_CATS = {k for k,v in CATEGORIES.items() if v["default"]}


def _budget_left(start_ts: float) -> float:
    return RUN_BUDGET_SEC - (time.time() - start_ts)


def _should_flush(start_ts: float) -> bool:
    return _budget_left(start_ts) < FLUSH_BEFORE_SEC


def _apply_details(lot, details):
    lot.update({
        "price":        details.get("price", 0),
        "description":  details.get("description", ""),
        "step_current": details.get("step_current", 0),
        "step_total":   details.get("step_total", 0),
        "participants": details.get("participants", 0),
        "vin":          details.get("vin", ""),
        "cadastral":    details.get("cadastral", ""),
        "address":      details.get("address", ""),
        "analytics_text": details.get("analytics_text", ""),
        "rosreestr_data": details.get("rosreestr_data", ""),
        "pdf_from_egrn": details.get("pdf_from_egrn", False),
        "pdf_download_failed": details.get("pdf_download_failed", False),
        "egrn_parsed":  details.get("egrn_parsed", {}),
        "has_egrn_on_site": details.get("has_egrn_on_site", False),
    })
    if details.get("pdf_from_egrn") and details.get("pdf_text"):
        lot["egrn_pdf_text"] = details["pdf_text"]
    for key in ("auction_format", "application_deadline", "deposit",
                "next_reduction_date", "next_reduction_price", "area_sqm", "area_sotka"):
        if key in details:
            lot[key] = details[key]
    lot["parsed_at"] = datetime.now().isoformat()
    if details.get("title_full"):
        lot["title"] = details["title_full"]
    lot["category"] = detect_type(
        f"{lot['title']} {lot.get('description', '')[:500]}"
    )
    if lot["category"] == "авто":
        from analyzer import parse_auto_meta
        lot.update(parse_auto_meta(f"{lot['title']} {lot.get('description', '')}"))


async def _confirm_login(page, check_lot_wall: bool = True) -> tuple[bool, str]:
    """Проверка успешного входа."""
    if await page.locator("text=Выйти").count() > 0:
        return True, "logout_link_visible"
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        body = ""
    if "личный кабинет" in body or "мой профиль" in body:
        return True, "profile_in_body"
    if await page.locator("a.button.stroke:text('Войти')").count() > 0:
        if not check_lot_wall:
            return False, "login_button_visible"
    if not check_lot_wall:
        return False, "not_logged_in"
    await page.goto("https://tbankrot.ru/item?id=7618864", timeout=35000, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    body = (await page.inner_text("body")).lower()
    if "войдите или зарегистрируйтесь" not in body and "для просмотра полной информации" not in body:
        if await page.locator("text=Выйти").count() > 0:
            return True, "lot_page_full_access"
        if re.search(r"\.pdf|егрн|выписк", body):
            return True, "lot_page_has_docs_hint"
    if await page.locator("text=Выйти").count() > 0:
        return True, "logout_on_lot_page"
    return False, "login_wall_still_present"


async def _read_login_hints(page) -> list[str]:
    try:
        return [
            h.strip() for h in await page.locator(".login_form__hint").all_inner_texts()
            if h and h.strip()
        ]
    except Exception:
        return []


async def _do_login_submit(page) -> tuple[bool, str]:
    """Отправка login через #login-btn и ожидание ответа /script/ajax.php."""
    if await page.locator("#lg-captcha").count() > 0:
        try:
            if await page.locator("#lg-captcha").is_visible():
                return False, "captcha_required"
        except Exception:
            pass

    try:
        async with page.expect_response(
            lambda r: "/script/ajax.php" in r.url and r.request.method == "POST",
            timeout=20000,
        ) as resp_info:
            clicked = await _submit_login_form(page)
            if not clicked:
                await page.locator("#login-btn").click(timeout=5000)
        resp = await resp_info.value
        body = (await resp.text()).strip()
        log.info("[login] ajax response: %r", body[:120])
        if body == "success":
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            return True, "ajax_success"
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("auth") in (1, "1", True):
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
                return True, "ajax_auth_ok"
        except (json.JSONDecodeError, TypeError):
            pass
        hints = {
            "badMail": "invalid_email",
            "badPas": "invalid_password",
            "badAuth": "bad_credentials",
            "tooMany": "rate_limited",
            "badCaptcha": "bad_captcha",
            "notCaptcha": "captcha_reload_needed",
        }
        ui_hints = await _read_login_hints(page)
        if ui_hints:
            log.warning("[login] form hints: %s", ui_hints)
        return False, hints.get(body, f"ajax:{body}")
    except Exception as e:
        log.warning("[login] ajax wait failed: %s — trying fetch fallback", e)

    try:
        result = await page.evaluate(
            """async (cred) => {
                const fd = new URLSearchParams();
                fd.set('key', 'login');
                fd.set('mail', cred.mail);
                fd.set('pas', cred.pas);
                fd.set('captcha', document.getElementById('lg-captcha')?.value || '');
                fd.set('captcha_token', document.querySelector('input[name=captcha_token]')?.value || '');
                const r = await fetch('/script/ajax.php', {method:'POST', body: fd, credentials:'include'});
                return {body: (await r.text()).trim(), status: r.status};
            }""",
            {"mail": LOGIN, "pas": PASSWORD},
        )
        body = (result or {}).get("body", "")
        log.info("[login] fetch fallback response: %r", body[:120])
        if body == "success":
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            return True, "fetch_success"
        return False, f"fetch:{body or 'empty'}"
    except Exception:
        log.exception("[login] fetch fallback failed")
        return False, "fetch_failed"


async def _submit_login_form(page) -> str:
    """Отправка формы входа. Возвращает способ отправки или пустую строку."""
    for sel in ("#login-btn", "div#login-btn.button", ".modal.visible #login-btn"):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=5000)
                return f"click:{sel}"
        except Exception as e:
            log.debug("[login] submit %s: %s", sel, e)
    for sel in ("button[type='submit']", "input[type='submit']"):
        try:
            loc = page.locator(f"#login_form {sel}, form#login_form {sel}").first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=3000)
                return f"click:{sel}"
        except Exception:
            pass
    for text in ("Войти", "Вход", "Log in"):
        try:
            loc = page.locator(
                f"#login_pop >> text={text}, .modal.visible >> text={text}"
            ).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=3000)
                return f"text:{text}"
        except Exception:
            pass
    try:
        await page.locator("#lg-pas").press("Enter")
        return "enter_on_password"
    except Exception:
        pass
    try:
        await page.evaluate("""() => {
            const b = document.getElementById('login-btn');
            if (b) { b.click(); return true; }
            const f = document.getElementById('login_form');
            if (f && f.requestSubmit) { f.requestSubmit(); return true; }
            return false;
        }""")
        return "js_login_btn"
    except Exception:
        return ""


LAST_LOGIN_REASON = ""


async def page_access_blocked(page) -> bool:
    """Cloudflare / Access denied / антибот на странице."""
    try:
        from platform_config import page_body_blocked
        body = await page.content()
        return page_body_blocked(body)
    except Exception:
        return False


async def login(page) -> bool:
    global LAST_LOGIN_REASON
    LAST_LOGIN_REASON = ""
    if not is_tbankrot_enabled():
        LAST_LOGIN_REASON = "disabled"
        log.info("[login] skipped — tbankrot access disabled")
        return False
    login_set = bool(LOGIN and LOGIN.strip())
    pwd_set = bool(PASSWORD and PASSWORD.strip())
    log.info("[login] TBANKROT_LOGIN set=%s TBANKROT_PASSWORD set=%s", login_set, pwd_set)
    if not login_set or not pwd_set:
        log.warning("[login] skip: credentials missing in env")
        return False
    try:
        await page.goto("https://tbankrot.ru/", timeout=45000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        ok, reason = await _confirm_login(page, check_lot_wall=False)
        if ok:
            log.info("[login] OK — already logged in (%s)", reason)
            return True

        log.info("[login] opening login modal")
        opened = False
        for sel in ("a.button.stroke:text('Войти')", "header a.button.stroke", "text=Войти"):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=5000)
                    opened = True
                    log.info("[login] modal opened via %s", sel)
                    break
            except Exception as e:
                log.debug("[login] open %s: %s", sel, e)
        if not opened:
            log.warning("[login] could not open login modal")
            return False

        await page.wait_for_selector("#login_form, #lg-mail", timeout=10000)
        await page.wait_for_timeout(500)

        email_filled = False
        for sel in ("#lg-mail", "input[name='mail']", "input[type='email']"):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.fill(LOGIN, timeout=5000)
                    email_filled = True
                    log.info("[login] email filled via %s", sel)
                    break
            except Exception:
                pass
        if not email_filled:
            log.warning("[login] email field not found")
            return False

        pwd_filled = False
        for sel in ("#lg-pas", "input[name='pas']", "#login_form input[type='password']"):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.fill(PASSWORD, timeout=5000)
                    pwd_filled = True
                    log.info("[login] password filled via %s", sel)
                    break
            except Exception:
                pass
        if not pwd_filled:
            log.warning("[login] password field not found")
            return False

        submit_ok, submit_reason = await _do_login_submit(page)
        if not submit_ok:
            log.warning("[login] submit failed: %s", submit_reason)
        else:
            log.info("[login] form submitted OK (%s)", submit_reason)

        ok, reason = await _confirm_login(page, check_lot_wall=True)
        if ok:
            log.info("[login] OK — %s (submit=%s)", reason, submit_reason)
            LAST_LOGIN_REASON = reason
        else:
            log.warning("[login] NOT CONFIRMED — %s (submit=%s)", reason, submit_reason)
            LAST_LOGIN_REASON = submit_reason if submit_reason not in ("ajax_success", "fetch_success") else reason
        return ok
    except Exception:
        log.exception("[login] FAIL")
        return False


async def _fetch_file_bytes(page, ctx, url: str, referer: str = "") -> tuple[bytes, int]:
    """Скачивает файл с cookies сессии. Возвращает (bytes, http_status)."""
    referer = referer or getattr(page, "url", None) or "https://tbankrot.ru/"
    if ctx:
        try:
            resp = await ctx.request.get(
                url,
                headers={
                    "Referer": referer,
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            if resp.status == 200:
                return await resp.body(), resp.status
            if resp.status in (403, 404):
                return b"", resp.status
        except Exception as e:
            log.debug("ctx.request %s: %s", url, e)

    try:
        data = await page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: 'include'});
                if (!r.ok) return {ok: false, status: r.status};
                const buf = await r.arrayBuffer();
                return {ok: true, status: r.status, bytes: Array.from(new Uint8Array(buf))};
            }""",
            url,
        )
        if data and data.get("ok") and data.get("bytes"):
            return bytes(data["bytes"]), int(data.get("status") or 200)
        return b"", int((data or {}).get("status") or 0)
    except Exception:
        return b"", 0


async def _fetch_pdf_bytes(page, url: str) -> bytes:
    """Legacy: PDF через fetch в странице."""
    raw, _ = await _fetch_file_bytes(page, None, url)
    return raw


def _legacy_discover_pdf_urls(lot_id: str) -> list:
    return [
        f"https://files.tbankrot.ru/egrn_files/{lot_id}.pdf",
        f"https://tbankrot.ru/files/egrn/{lot_id}.pdf",
        f"https://tbankrot.ru/item/egrn?id={lot_id}",
    ]


def _legacy_extract_pdf_text(raw: bytes) -> tuple[str, str]:
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            parts = [(p.extract_text() or "") for p in pdf.pages[:8]]
        text = "\n".join(parts).strip()
        if len(text) >= 80:
            return text[:12000], "text"
    except Exception as e:
        print(f"    PDF text layer: {e}")
    return "", "failed"


async def _try_lot_pdfs(lot, page, ctx):
    """Скачивает и разбирает все документы лота (PDF, DOCX, фото)."""
    from analyzer import apply_egrn_to_lot, apply_appraisal_to_lot, apply_lot_document
    extract_pdf_text = None
    try:
        from egrn_pdf import extract_pdf_text, extract_appraisal_pdf_text
        from lot_documents import (
            discover_lot_documents, classify_document, extract_docx_text,
            parse_document_content, IMAGE_EXTS, READABLE_EXTS,
        )
    except ImportError as e:
        log.warning("Document modules unavailable: %s", e)
        return await _try_lot_pdfs_legacy(lot, page, ctx)

    lot_url = lot.get("url") or f"https://tbankrot.ru/item?id={lot['id']}"
    html = ""
    try:
        await page.goto(lot_url, timeout=25000, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        html = await page.content()
    except Exception as e:
        log.warning("lot page reload for documents failed: %s", e)
        try:
            html = await page.content()
        except Exception:
            pass

    doc_refs = discover_lot_documents(html, lot["id"])
    lot["document_urls_found"] = [d["url"] for d in doc_refs]
    lot["has_documents_on_site"] = bool(doc_refs)
    lot["has_egrn_on_site"] = lot["has_documents_on_site"] or any(
        x in (html or "").lower() for x in ("егрн", "egrn", "выписк", "оценк", "договор", "заявк")
    )

    downloaded: list[dict] = []
    got_readable = False

    for ref in doc_refs:
        url = ref["url"]
        title = ref.get("title") or ""
        ext = ref.get("ext") or ""
        entry: dict = {
            "url": url, "title": title, "ext": ext,
            "bytes": 0, "download_ok": False, "type": "other",
            "method": "", "text_len": 0, "extracted": {},
        }
        try:
            if ext in IMAGE_EXTS or (not ext and "etpphoto" in url.lower()):
                entry["type"] = "photo"
                entry["ext"] = ext or "jpg"
                raw, status = await _fetch_file_bytes(page, ctx, url, lot_url)
                entry["bytes"] = len(raw)
                entry["download_ok"] = len(raw) > 1000
                entry["extracted"] = {"summary": "есть фото" if entry["download_ok"] else "не скачано"}
                lot["has_photos"] = lot.get("has_photos") or entry["download_ok"]
                downloaded.append(entry)
                continue

            raw, status = await _fetch_file_bytes(page, ctx, url, lot_url)
            entry["bytes"] = len(raw)
            entry["download_ok"] = len(raw) > 200
            if not entry["download_ok"]:
                log.debug("doc skip %s status=%s bytes=%d", url, status, len(raw))
                downloaded.append(entry)
                continue

            text, method = "", "failed"
            if ext in ("docx", "doc") or raw[:2] == b"PK":
                text, method = extract_docx_text(raw)
                entry["ext"] = "docx"
            elif ext == "pdf" or raw[:4] == b"%PDF":
                doc_type_pre = classify_document(title, url, "", "pdf")
                is_appr = doc_type_pre == "appraisal" or bool(
                    re.search(r"отч[её]t|оцен", title, re.I)
                    and "егрн" not in title.lower()
                )
                if is_appr:
                    log.info(
                        "appraisal step: downloaded bytes=%d title=%r",
                        len(raw), title[:60],
                    )
                    try:
                        text, method = extract_appraisal_pdf_text(raw)
                        log.info(
                            "appraisal OCR: %d chars, method=%s title=%r",
                            len(text or ""), method, title[:60],
                        )
                    except Exception:
                        log.exception("appraisal OCR failed title=%r", title[:60])
                        text, method = "", "failed"
                    if len(text or "") < 80:
                        text2, method2 = extract_appraisal_pdf_text(raw)
                        if len(text2 or "") > len(text or ""):
                            text, method = text2, method2
                            log.info("appraisal OCR retry: %d chars method=%s", len(text), method)
                elif extract_pdf_text:
                    if len(raw) > 400_000:
                        text, method = extract_appraisal_pdf_text(raw)
                    else:
                        text, method = extract_pdf_text(raw)
                else:
                    text, method = _legacy_extract_pdf_text(raw)
                entry["ext"] = "pdf"
                if is_appr and len(text or "") < 80:
                    try:
                        from egrn_pdf import ocr_diagnostics
                        diag = ocr_diagnostics()
                        entry["ocr_diagnostics"] = {
                            k: diag.get(k) for k in (
                                "available", "reason", "tesseract",
                                "tesseract_version", "pymupdf", "pymupdf_version",
                            )
                        }
                        log.warning(
                            "appraisal OCR weak: %d chars method=%s; diag=%s",
                            len(text or ""), method, entry["ocr_diagnostics"],
                        )
                    except ImportError:
                        entry["ocr_available"] = False
            elif raw[:4] == b"%PDF":
                text, method = extract_pdf_text(raw) if extract_pdf_text else _legacy_extract_pdf_text(raw)
            else:
                # HTML-ответ get_doc.php и прочее
                if raw.lstrip()[:15].lower().startswith(b"<"):
                    entry["type"] = "other"
                    entry["extracted"] = {"summary": "HTML-шаблон, не файл"}
                    downloaded.append(entry)
                    continue

            entry["method"] = method
            entry["text_len"] = len(text or "")
            doc_type = classify_document(title, url, text or "", entry["ext"])
            entry["type"] = doc_type

            if doc_type == "photo":
                entry["extracted"] = {"summary": "есть фото"}
                lot["has_photos"] = True
            elif text and len(text) >= 40:
                got_readable = True
                parsed = parse_document_content(doc_type, text, title)
                entry["extracted"] = parsed
                apply_lot_document(lot, doc_type, text, parsed, title=title, method=method)
                log.info(
                    "DOC %s type=%s bytes=%d text=%d method=%s title=%r",
                    url, doc_type, len(raw), len(text), method, title[:60],
                )
            else:
                entry["extracted"] = {"summary": "скачан, текст не извлечён"}
                if doc_type == "egrn":
                    lot["egrn_ocr_failed"] = True

            downloaded.append(entry)
        except Exception as e:
            log.warning("DOC err %s: %s", url, e)
            entry["extracted"] = {"summary": f"ошибка: {e}"}
            downloaded.append(entry)

    lot["lot_documents"] = downloaded
    lot["documents_downloaded_count"] = sum(1 for d in downloaded if d.get("download_ok"))
    lot["pdfs_downloaded"] = [d for d in downloaded if d.get("ext") == "pdf" and d.get("download_ok")]
    lot["pdfs_downloaded_count"] = len(lot["pdfs_downloaded"])

    if lot.get("has_documents_on_site") and not any(d.get("download_ok") for d in downloaded):
        lot["pdf_download_failed"] = True
    elif got_readable and not lot.get("egrn_read_ok") and not lot.get("appraisal_parsed", {}).get("parsed_ok"):
        if any(d.get("type") == "egrn" and d.get("text_len", 0) < 80 for d in downloaded):
            lot["egrn_ocr_failed"] = True


async def _try_lot_pdfs_legacy(lot, page, ctx):
    """Fallback если lot_documents недоступен."""
    from analyzer import apply_egrn_to_lot, apply_appraisal_to_lot
    extract_pdf_text = discover_pdf_urls_fn = classify_pdf_type = None
    try:
        from egrn_pdf import extract_pdf_text, discover_pdf_urls as discover_pdf_urls_fn
        from appraisal_pdf import classify_pdf_type
    except ImportError as e:
        log.warning("PDF modules unavailable: %s", e)

    desc = (lot.get("description") or "").lower()
    lot["has_egrn_on_site"] = any(x in desc for x in (
        "egrn", "егрн", "выписк", "rosreestr", "росреестр", "оценк", "pdf",
    ))
    lot_url = lot.get("url") or f"https://tbankrot.ru/item?id={lot['id']}"
    html = ""
    try:
        await page.goto(lot_url, timeout=25000, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        html = await page.content()
    except Exception as e:
        log.warning("lot page reload for PDFs failed: %s", e)
        try:
            html = await page.content()
        except Exception:
            pass
    if discover_pdf_urls_fn:
        urls = discover_pdf_urls_fn(html, lot["id"])
    else:
        urls = _legacy_discover_pdf_urls(lot["id"])

    lot["pdf_urls_found"] = urls
    downloaded: list[dict] = []
    got_pdf = False

    for pdf_url in urls:
        try:
            raw, status = await _fetch_file_bytes(page, ctx, pdf_url, lot_url)
            if not raw:
                log.debug("PDF skip %s status=%s", pdf_url, status)
                continue
            if b"%PDF" not in raw[:10]:
                continue
            got_pdf = True
            lot["has_egrn_on_site"] = True
            if extract_pdf_text:
                text, method = extract_pdf_text(raw)
            else:
                text, method = _legacy_extract_pdf_text(raw)
            doc_type = classify_pdf_type(text, pdf_url) if classify_pdf_type else "unknown"
            entry = {
                "url": pdf_url, "bytes": len(raw), "method": method,
                "type": doc_type, "text_len": len(text or ""),
            }
            downloaded.append(entry)
            log.info("PDF %s type=%s bytes=%d text=%d method=%s",
                     pdf_url, doc_type, len(raw), len(text or ""), method)

            if doc_type == "egrn" and text and len(text) >= 80:
                apply_egrn_to_lot(lot, text, True)
                lot["egrn_extract_method"] = method
                entry["parsed"] = "egrn"
            elif doc_type == "appraisal" and text and len(text) >= 80:
                apply_appraisal_to_lot(lot, text)
                lot["appraisal_extract_method"] = method
                entry["parsed"] = "appraisal"
            elif text and len(text) >= 80:
                if classify_pdf_type and classify_pdf_type(text, "") == "egrn":
                    apply_egrn_to_lot(lot, text, True)
                    lot["egrn_extract_method"] = method
                    entry["parsed"] = "egrn"
                elif classify_pdf_type and classify_pdf_type(text, "") == "appraisal":
                    apply_appraisal_to_lot(lot, text)
                    lot["appraisal_extract_method"] = method
                    entry["parsed"] = "appraisal"
                else:
                    entry["parsed"] = "unclassified"
            else:
                lot["egrn_ocr_failed"] = lot.get("egrn_ocr_failed") or (doc_type == "egrn")
                entry["parsed"] = "no_text"
        except Exception as e:
            log.warning("PDF err %s: %s", pdf_url, e)
            continue

    lot["pdfs_downloaded"] = downloaded
    lot["pdfs_downloaded_count"] = len(downloaded)
    if got_pdf and not lot.get("egrn_read_ok") and not lot.get("appraisal_parsed", {}).get("parsed_ok"):
        if any(d.get("parsed") == "no_text" for d in downloaded):
            lot["egrn_ocr_failed"] = True
    elif not got_pdf and lot.get("has_egrn_on_site"):
        lot["pdf_download_failed"] = True


# alias для совместимости
_try_egrn_pdf = _try_lot_pdfs


async def collect(page, regions, max_pages=MAX_PAGES) -> list:
    lots, seen = [], set()
    for region in regions:
        for pg in range(1, max_pages+1):
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
                        if not m or m.group(1) in seen: continue
                        seen.add(m.group(1))
                        title = (await link.inner_text()).strip()[:200]
                        lots.append({
                            "id": m.group(1), "title": title,
                            "url": href, "region": region,
                            "is_extra": region not in REGIONS_MAIN,
                            "source": "Т-Банкрот",
                            "pdf_text":"","description":"",
                            "price":0,"step_current":0,"step_total":0,
                            "participants":0,"vin":"","cadastral":"",
                        })
                        added += 1
                    except: continue
                print(f"  {region} стр.{pg}: +{added} (итого {len(lots)})")
                if len(lots) >= max_pages * 20: break
            except: break
    return lots


async def enrich_light(lot, page):
    """Лёгкий проход: только карточка лота, без analytics/Росреестра/PDF."""
    details = await get_lot_details(lot["url"], page, light=True)
    _apply_details(lot, details)


async def enrich_heavy(lot, page, ctx):
    """Тяжёлый проход: analytics, Росреестр, PDF ЕГРН."""
    cat = lot.get("category", "прочее")
    if cat != "авто":
        if not lot.get("analytics_text"):
            try:
                m = re.search(r'id=(\d+)', lot.get("url", ""))
                if m:
                    await page.goto(f"https://tbankrot.ru/analytics/{m.group(1)}", timeout=12000)
                    await page.wait_for_timeout(800)
                    analytics = await page.inner_text("body")
                    if len(analytics) > 200:
                        lot["analytics_text"] = analytics[:2000]
            except Exception:
                pass
        if is_real_estate(cat) and lot.get("cadastral") and not lot.get("rosreestr_data"):
            try:
                lot["rosreestr_data"] = await asyncio.wait_for(
                    get_rosreestr_data(lot["cadastral"]), timeout=8,
                )
            except asyncio.TimeoutError:
                lot["rosreestr_timeout"] = True
            except Exception:
                pass
        if not lot.get("egrn_pdf_text"):
            try:
                await asyncio.wait_for(_try_lot_pdfs(lot, page, ctx), timeout=PDF_TIMEOUT)
            except asyncio.TimeoutError:
                lot["pdf_timeout"] = True
            except Exception as e:
                print(f"    EGRN step skipped: {e}")
                lot["pdf_download_failed"] = True


async def enrich(lot, page, ctx, heavy: bool = True):
    """Полный enrich для одиночного анализа (бот). Login — снаружи, один раз."""
    details = await get_lot_details(lot["url"], page, light=not heavy)
    _apply_details(lot, details)
    if heavy and lot.get("category") != "авто" and not lot.get("egrn_pdf_text"):
        try:
            await asyncio.wait_for(_try_egrn_pdf(lot, page, ctx), timeout=PDF_TIMEOUT)
        except asyncio.TimeoutError:
            lot["pdf_timeout"] = True
        except Exception as e:
            print(f"    EGRN step skipped: {e}")
            lot["pdf_download_failed"] = True


def fmt_block(lot, an, i=0) -> str:
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
              "1️⃣1️⃣","1️⃣2️⃣","1️⃣3️⃣","1️⃣4️⃣","1️⃣5️⃣"]
    medal  = medals[i] if i < len(medals) else f"#{i+1}"
    step    = f"\n📊 {an['step']}" if an.get('step') else ""
    urgency = f"\n{an['urgency']}" if an.get('urgency') else ""
    mkt    = f"\n_📊 {an['market_comment']}_" if an.get('market_comment') and not an.get('market_known') else ""
    extra  = f"\n{an['extra_checks']}" if an.get('extra_checks') else ""
    check  = f"\n🔎 {an['what_to_check']}" if an.get('what_to_check') else ""
    encumb = f"\n🔒 {an['encumbrances']}" if an.get('encumbrances') else ""
    exit_s = f"\n🚪 Выход: {an['exit_strategy']}" if an.get('exit_strategy') else ""
    doc_st = f"\n📄 _{an['document_status']}_" if an.get('document_status') else ""
    legal  = f"\n📋 {an['legal_text']}" if an.get('legal_text') else ""
    auto_s = f"\n🚗 {an['auto_summary']}" if an.get('auto_summary') else ""
    simple = ""
    region_note = " 🌍" if lot.get("is_extra") else ""
    price_line = an.get("price_line") or format_price_line(an)
    return (
        f"{medal} *{an.get('score_label','5/10')}*"
        f" | {an.get('risk_text','документы: —')}"
        f"{region_note}\n"
        f"{lot.get('title','')[:65]}\n"
        f"{price_line}"
        f"{mkt}{step}{urgency}\n"
        f"💧 Ликвидность: {an.get('liquidity_text','—')}\n"
        f"📈 {an.get('roi_text','нет данных')}"
        f"{doc_st}{legal}{auto_s}{encumb}{exit_s}"
        f"{extra}\n"
        f"{an.get('action_emoji','⚠️')} *{an.get('verdict_label') or an.get('action','?')}*\n"
        f"💡 _{an.get('strategy','')}_"
        f"{simple}"
        f"{check}\n"
        f"🔗 {lot.get('url','')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

def build_msgs(cat_key, results) -> list:
    cat  = CATEGORIES[cat_key]
    now  = datetime.now().strftime("%d.%m.%Y")
    go   = sum(1 for _,a in results if a.get("action")=="ВХОДИТЬ СЕЙЧАС")
    wait = sum(1 for _,a in results if a.get("action")=="ЖДАТЬ СНИЖЕНИЯ")
    header = (
        f"{cat['icon']} *{cat['label']} — {now}*\n"
        f"Лотов: {len(results)} | 🟢 {go} войти | ⏳ {wait} ждать\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    # Топ-3 лота кратко для быстрого обзора
    top3 = results[:3]
    quick_view = "📌 *Топ лоты:*\n"
    for j,(l,a) in enumerate(top3):
        disc = a.get('discount_pct','0')
        disc_s = f"-{disc}%" if disc not in ('0','?') else ""
        quick_view += f"{'🥇🥈🥉'[j]} {l.get('title','')[:40]} | {a.get('price','—')} {disc_s} | {a.get('action_emoji','⚠️')} {a.get('action','?')}\n"
    quick_view += "\n"
    header = header + quick_view
    parts, current = [], header
    for i,(lot,an) in enumerate(results[:TOP_N]):
        block = fmt_block(lot, an, i)
        if len(current)+len(block) > 3800:
            parts.append(current); current = block
        else:
            current += block
    parts.append(current)
    return parts


DIGEST_SECTIONS = (
    ("best", "🔥", "Лучшие — высокий дисконт + мало заявок"),
    ("competitive", "💰", "Выгодные, но конкурентные"),
    ("clean", "✅", "Чистые документы"),
    ("other", "📊", "Остальной топ"),
)


def build_digest_top10(heavy_map: dict, scored: list, cats: set, top_n: int = DIGEST_TOP_N) -> list:
    """Топ-N по digest_rating из тяжёлой фазы; fallback на лёгкую, если тяжёлых нет."""
    pool: list[tuple] = []
    for _lot_id, (lot, an) in heavy_map.items():
        cat = lot.get("category", "прочее")
        if cats and cat not in cats:
            continue
        pool.append((lot, enrich_digest_metrics(lot, an)))
    if not pool and scored:
        for lot, light_an, _score, cat in scored:
            if cats and cat not in cats:
                continue
            pool.append((lot, enrich_digest_metrics(lot, light_an)))
    pool.sort(key=lambda x: float(x[1].get("digest_rating", 0) or 0), reverse=True)
    return pool[:top_n]


def digest_results_dict(digest_items: list) -> dict:
    """Раскладывает топ дайджеста по категориям для сохранения в БД."""
    out = {k: [] for k in CATEGORIES}
    for lot, an in digest_items:
        key = lot.get("category", "прочее")
        if key not in out:
            key = "прочее"
        out[key].append((lot, an))
    return out


async def send(msgs, reply_markup=None, *, chat_id=None, bot=None):
    tg_bot = bot or telegram.Bot(token=TG_TOKEN)
    target = chat_id or TG_CHAT
    for msg in msgs:
        try:
            await tg_bot.send_message(
                chat_id=target, text=msg,
                parse_mode="Markdown", disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  TG: {e}")


async def _send_one_lot_card(tg_bot, target, lot, an, label) -> tuple[bool, str]:
    """Отправить одну карточку; несколько стратегий при ошибке Markdown."""
    lot_id = str(lot.get("id", "?"))
    variants = []
    try:
        variants.append(("md", format_short_lot_message(lot, an, label), True))
    except Exception as e:
        return False, f"format: {e}"
    try:
        variants.append(("plain", format_short_lot_message_plain(lot, an, label), False))
    except Exception:
        pass
    variants.append(("minimal", format_minimal_lot_card(lot, an, label), False))

    last_err = ""
    for _mode, text, use_md in variants:
        text = clamp_telegram_message(text)
        kb = None
        try:
            kb = lot_action_keyboard(lot_id, an, lot, lot.get("parsed_at"))
        except Exception as e:
            last_err = f"keyboard: {e}"
            log.exception("lot card keyboard failed id=%s", lot_id)
            continue
        try:
            await tg_bot.send_message(
                chat_id=target, text=text,
                parse_mode="Markdown" if use_md else None,
                disable_web_page_preview=True,
                reply_markup=kb,
            )
            return True, ""
        except Exception as e:
            last_err = f"{_mode}: {e}"
            log.warning("lot card send %s failed id=%s: %s", _mode, lot_id, e)
    return False, last_err or "unknown"


async def send_lot_cards(lots_with_an, *, chat_id=None, bot=None, label_prefix=""):
    """Короткая карточка + кнопки; один битый лот не роняет остальные."""
    tg_bot = bot or telegram.Bot(token=TG_TOKEN)
    target = chat_id or TG_CHAT
    stats = {"sent": 0, "failed": 0, "total": len(lots_with_an), "errors": []}
    for i, (lot, an) in enumerate(lots_with_an):
        lot_id = str(lot.get("id", "?"))
        label = f"{label_prefix} #{i + 1}" if label_prefix else f"#{i + 1}"
        try:
            ok, err = await _send_one_lot_card(tg_bot, target, lot, an, label)
        except Exception as e:
            ok, err = False, str(e)
            log.exception("lot card unexpected id=%s", lot_id)
        if ok:
            stats["sent"] += 1
        else:
            stats["failed"] += 1
            stats["errors"].append({"id": lot_id, "reason": err[:200]})
            print(f"  ⚠️ карточка {lot_id} пропущена: {err}")
        await asyncio.sleep(0.4)
    return stats


def _format_digest_send_summary(card_stats: dict, total_expected: int) -> str:
    sent = card_stats.get("sent", 0)
    failed = card_stats.get("failed", 0)
    lines = [f"📬 *Итог дайджеста:* отправлено карточек *{sent}* из *{total_expected}*"]
    errors = card_stats.get("errors") or []
    if failed:
        lines.append(f"⚠️ Пропущено: *{failed}*")
        for item in errors[:5]:
            lines.append(f"• лот `{item.get('id', '?')}`: {item.get('reason', '?')[:80]}")
        if len(errors) > 5:
            lines.append(f"_…и ещё {len(errors) - 5}_")
    return "\n".join(lines)


async def send_daily_digest(digest_items, all_lots_count, alerts, skipped,
                            partial=False, stats=None, phase_note="",
                            stream_chat_id=None, stream_bot=None):
    total = len(digest_items)
    card_stats = {"sent": 0, "failed": 0, "total": total, "errors": []}
    try:
        go = sum(1 for _, a in digest_items if a.get("action") == "ВХОДИТЬ СЕЙЧАС")
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        partial_note = (
            "\n\n⚠️ _Частичный дайджест: не все лоты успели пройти тяжёлый анализ до лимита времени._"
            if partial else ""
        )
        stats_line = ""
        if stats:
            stats_line = (
                f"\n⏱ Сбор: {stats.get('collect_sec', 0):.0f}с | "
                f"лёгкий: {stats.get('light_sec', 0):.0f}с ({stats.get('light_n', 0)} лот.) | "
                f"тяжёлый: {stats.get('heavy_sec', 0):.0f}с ({stats.get('heavy_n', 0)} лот.)"
            )
        heavy_n = stats.get("heavy_n", 0) if stats else 0
        await send([
            f"🌅 *Дайджест {now}*{phase_note}{partial_note}\n\n"
            f"🔍 Изучено: *{all_lots_count}* лотов | тяжёлый анализ: *{heavy_n}*\n"
            f"⭐ *Топ-{min(total, DIGEST_TOP_N)}* по рейтингу (не пустой — лучшие на сегодня)\n"
            f"📐 _Рейтинг = {DIGEST_FORMULA}_\n"
            f"🔥 пометка при дисконте ≥{int(HOT_LABEL_PCT)}%\n\n"
            f"🟢 Входить сейчас: *{go}* | 🔔 горячих: *{alerts}* | ⏭ отсеяно: *{skipped}*{stats_line}\n\n"
            "_Короткие карточки ниже — «Полный анализ» по кнопке ↓_"
        ], chat_id=stream_chat_id, bot=stream_bot)
        await asyncio.sleep(2)

        card_chat = stream_chat_id
        card_bot = stream_bot
        buckets = {key: [] for key, _, _ in DIGEST_SECTIONS}
        for lot, an in digest_items:
            sec = an.get("digest_section", "other")
            if sec not in buckets:
                sec = "other"
            buckets[sec].append((lot, an))

        for sec_key, icon, title in DIGEST_SECTIONS:
            items = buckets.get(sec_key, [])
            if not items:
                continue
            print(f"\n{icon} {title}: {len(items)}")
            try:
                await send(
                    [f"{icon} *{title}* — {len(items)} лот(ов)"],
                    chat_id=card_chat, bot=card_bot,
                )
            except Exception:
                log.exception("digest section header failed: %s", title)
            sec_stats = await send_lot_cards(
                items, chat_id=card_chat, bot=card_bot, label_prefix=icon,
            )
            card_stats["sent"] += sec_stats["sent"]
            card_stats["failed"] += sec_stats["failed"]
            card_stats["errors"].extend(sec_stats["errors"])
            await asyncio.sleep(1)

        await send(
            [_format_digest_send_summary(card_stats, total)],
            chat_id=stream_chat_id, bot=stream_bot,
        )
    except Exception:
        log.exception("send_daily_digest failed")
        try:
            await send(
                [_format_digest_send_summary(card_stats, total)
                 + "\n\n⚠️ _Дайджест отправлен частично из-за ошибки._"],
                chat_id=stream_chat_id, bot=stream_bot,
            )
        except Exception:
            log.exception("digest summary send failed")
    return card_stats


def _discount_value(an: dict) -> float:
    try:
        return float(an.get("discount_pct") or 0)
    except (TypeError, ValueError):
        return 0.0


def _select_heavy_queue(scored: list) -> tuple[list, str]:
    """Топ-N лёгкой фазы всегда идут в тяжёлую (для расчёта дисконта по документам)."""
    if not scored:
        return [], "нет лотов после лёгкой фазы (фильтр категории/регион)"
    ranked = sorted(scored, key=lambda x: x[2], reverse=True)
    cap = min(HEAVY_TOP_N, MAX_HEAVY_LOTS)
    queue = ranked[:cap]
    reason = f"гарантированный топ-{len(queue)} по баллу лёгкой фазы"
    if HEAVY_MIN_SCORE > 0:
        seen = {lot["id"] for lot, _, _, _ in queue}
        extra = [
            item for item in ranked[cap:]
            if item[2] >= HEAVY_MIN_SCORE and item[0]["id"] not in seen
        ]
        if extra:
            queue = (queue + extra)[:MAX_HEAVY_LOTS]
            reason += f" + {len(queue) - cap} с баллом ≥{HEAVY_MIN_SCORE}"
    return queue, reason


def _build_results(scored, heavy_map):
    """Собирает results из лёгкого прохода + тяжёлых замен."""
    out = {k: [] for k in CATEGORIES}
    for lot, light_an, _score, cat in scored:
        if lot["id"] in heavy_map:
            lot, an = heavy_map[lot["id"]]
        else:
            an = light_an
        key = cat if cat in out else "прочее"
        out[key].append((lot, an))
    return out


async def run(cats=None, include_extra=True, daily=True, *,
              save_to_db=None, run_type="scheduled",
              stream_chat_id=None, stream_bot=None,
              stream_min_score=9.0, min_result_score=None,
              hot_only=False, region_filter=None):
    init_db()
    if not is_mass_scraping_enabled():
        log.info("agent.run skipped — mass scraping disabled")
        return {k: [] for k in CATEGORIES}
    if save_to_db is None:
        save_to_db = os.getenv("AGENT_SAVE_DB", "1") != "0"
    if cats is None:
        cats = DEFAULT_CATS
    if min_result_score is None:
        min_result_score = 9.0 if hot_only else MIN_SCORE
    started_at = datetime.now().isoformat()
    start_ts = time.time()
    sent_hot_ids = set()
    stats = {
        "collect_sec": 0, "light_sec": 0, "heavy_sec": 0,
        "light_n": 0, "heavy_n": 0, "light_timeouts": 0, "heavy_timeouts": 0,
    }
    print(f"\n{'='*55}")
    print(f"🤖 Агент v10.0: {datetime.now().strftime('%d.%m.%Y %H:%M')} [{run_type}]")
    print(f"Категории: {', '.join(cats)} | Мин.балл: {min_result_score}")
    print(f"Бюджет: {RUN_BUDGET_SEC}s | Тяжёлых: топ-{HEAVY_TOP_N} + до {MAX_HEAVY_LOTS} | дисконт ≥{MIN_DISCOUNT_PCT}%")
    if stream_chat_id:
        print(f"📡 Стриминг горячих ≥{stream_min_score} → chat {stream_chat_id}")
    print(f"{'='*55}\n")

    results = {k: [] for k in CATEGORIES}
    skipped = alerts = 0
    digest_sent = False
    partial = False
    heavy_map = {}
    all_lots = []
    digest_items = []
    scored = []

    async def _emit_hot(lot, an, preliminary=False):
        nonlocal alerts
        lot_id = lot.get("id")
        if not lot_id:
            return
        score = float(an.get("total_score", 0))
        if score < stream_min_score:
            return
        if not an.get("hot_label"):
            return
        label = "⚡ 🔥 ГОРЯЧИЙ" if not preliminary else "⚡ 🔥 (предварительно)"
        kb = lot_action_keyboard(lot_id, an, lot, lot.get("parsed_at"))
        msg = format_short_lot_message(lot, an, label)
        if lot_id in sent_hot_ids and not preliminary:
            return
        if preliminary and lot_id in sent_hot_ids:
            return
        sent_hot_ids.add(lot_id)
        if stream_chat_id and stream_bot:
            try:
                await stream_bot.send_message(
                    chat_id=stream_chat_id, text=msg,
                    parse_mode="Markdown", disable_web_page_preview=True,
                    reply_markup=kb,
                )
            except Exception as e:
                print(f"  stream TG: {e}")
        elif daily and not stream_chat_id:
            await send([msg], reply_markup=kb)
        if score >= 9.0:
            alerts += 1

    async def _flush_if_needed(scored_list, reason=""):
        nonlocal partial
        if not daily or not _should_flush(start_ts):
            return False
        partial = True
        print(
            f"\n⏱ Лимит времени ({reason}), осталось {_budget_left(start_ts):.0f}с — "
            f"останавливаем текущую фазу, тяжёлый анализ будет выполнен"
        )
        return True

    regions = list(REGIONS_MAIN)
    if include_extra:
        regions += REGIONS_EXTRA

    async with async_playwright() as p:
        browser = await launch_browser(p)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()
        await login(page)

        print("\n📡 Собираем лоты...")
        t0 = time.time()
        if region_filter:
            if isinstance(region_filter, str):
                region_filter = [region_filter]
            main_lots = await collect(page, region_filter, MAX_PAGES)
            extra_lots = []
            print(f"   регион: {', '.join(region_filter)}")
        else:
            main_lots = await collect(page, REGIONS_MAIN, MAX_PAGES)
            extra_lots = await collect(page, REGIONS_EXTRA, EXTRA_MAX_PAGES) if include_extra else []
        all_lots = main_lots + extra_lots
        stats["collect_sec"] = time.time() - t0
        print(f"✅ Собрано: {len(all_lots)} лотов за {stats['collect_sec']:.0f}с")
        print(f"   (main: {len(main_lots)} × до {MAX_PAGES} стр., extra: {len(extra_lots)} × {EXTRA_MAX_PAGES} стр.)\n")

        # ── Фаза 1: лёгкий проход ──
        print("🔍 Фаза 1 — лёгкий проход (карточка + балл, без PDF/Groq)...")
        scored = []  # (lot, analysis, score)
        light_t0 = time.time()

        for i, lot in enumerate(all_lots):
            if await _flush_if_needed(scored, "лимит времени в фазе 1"):
                break
            print(f"  [L {i+1}/{len(all_lots)}] ", end="", flush=True)
            try:
                async def _light_one():
                    await enrich_light(lot, page)
                    cat = lot.get("category", "прочее")
                    if cat not in cats:
                        return None
                    dedup = record_digest_lot(lot["id"], lot.get("price", 0))
                    if dedup.get("note"):
                        lot["dedup_note"] = dedup["note"]
                    an = await analyze_lot(lot, light=True)
                    score = float(an.get("total_score", 0))
                    if lot.get("is_extra") and score < 7.0:
                        return None
                    return cat, an, score

                out = await asyncio.wait_for(_light_one(), timeout=LOT_TIMEOUT_LIGHT)
                if out is None:
                    print(f"skip ({lot.get('category', '?')})")
                    skipped += 1
                    continue
                cat, an, score = out
                scored.append((lot, an, score, cat))
                stats["light_n"] += 1
                extra_note = "🌍" if lot.get("is_extra") else ""
                print(f"{cat:12} | ⭐{score:.1f} | {an.get('action', '?')} {extra_note}")
            except asyncio.TimeoutError:
                stats["light_timeouts"] += 1
                print("timeout — пропуск")
            except Exception as e:
                print(f"ошибка: {e}")
            await asyncio.sleep(0.15)

        stats["light_sec"] = time.time() - light_t0
        avg_light = stats["light_sec"] / max(stats["light_n"], 1)
        print(f"\n📊 Лёгкая фаза: {stats['light_n']} лотов из {len(all_lots)} собранных "
              f"({stats['light_sec']:.0f}с, ~{avg_light:.1f}с/лот, таймаутов: {stats['light_timeouts']})")

        heavy_queue, sel_reason = _select_heavy_queue(scored)
        print(f"🎯 Лёгкая → тяжёлая: отобрано {len(heavy_queue)} лотов ({sel_reason})")
        if heavy_queue:
            preview = ", ".join(
                f"{lot['id']}:{score:.1f}" for lot, _, score, _ in heavy_queue[:8]
            )
            tail = "…" if len(heavy_queue) > 8 else ""
            print(f"   кандидаты: {preview}{tail}")
        elif scored:
            best = max(scored, key=lambda x: x[2])
            print(f"   ⚠️ очередь пуста при {len(scored)} лёгких; лучший балл: {best[2]:.1f}")
        print()

        heavy_map = {}  # lot id -> (lot, an)
        heavy_t0 = time.time()

        # ── Фаза 2: тяжёлый проход ──
        if heavy_queue:
            print("🔬 Фаза 2 — PDF ЕГРН + Groq для топ-кандидатов...")
            for j, (lot, light_an, score, cat) in enumerate(heavy_queue):
                if await _flush_if_needed(scored, "лимит времени в фазе 2"):
                    break
                print(f"  [H {j+1}/{len(heavy_queue)}] id={lot['id']} ⭐{score:.1f} ", end="", flush=True)
                try:
                    async def _heavy_one():
                        await enrich_heavy(lot, page, ctx)
                        return await analyze_lot(lot, light=False)

                    an = await asyncio.wait_for(_heavy_one(), timeout=LOT_TIMEOUT_HEAVY)
                    heavy_map[lot["id"]] = (lot, an)
                    stats["heavy_n"] += 1
                    print(f"→ ⭐{float(an.get('total_score', score)):.1f} | {an.get('action', '?')}")

                    new_score = float(an.get("total_score", 0))
                    if new_score >= stream_min_score and (stream_chat_id or daily):
                        await _emit_hot(lot, an, preliminary=False)
                except asyncio.TimeoutError:
                    stats["heavy_timeouts"] += 1
                    heavy_map[lot["id"]] = (lot, light_an)
                    print("timeout — оставляем лёгкий анализ")
                except Exception as e:
                    heavy_map[lot["id"]] = (lot, light_an)
                    print(f"ошибка: {e}")
                await asyncio.sleep(0.2)

        stats["heavy_sec"] = time.time() - heavy_t0
        if stats["heavy_n"]:
            disc_ok = sum(
                1 for _lot, an in heavy_map.values() if an.get("discount_ok")
            )
            mkt_known = sum(
                1 for _lot, an in heavy_map.values() if an.get("market_known")
            )
            print(
                f"\n📊 Тяжёлая фаза: {stats['heavy_n']} лотов за {stats['heavy_sec']:.0f}с | "
                f"рынок определён: {mkt_known} | дисконт ≥{MIN_DISCOUNT_PCT}%: {disc_ok}"
            )
        else:
            print(f"\n📊 Тяжёлая фаза: 0 лотов ({stats['heavy_sec']:.0f}с)")

        results = _build_results(scored, heavy_map)
        digest_items = build_digest_top10(heavy_map, scored, set(cats))
        results = digest_results_dict(digest_items)
        print(
            f"\n📊 Дайджест: топ-{len(digest_items)} по рейтингу "
            f"({DIGEST_FORMULA}) | 🔥 от {HOT_LABEL_PCT}%"
        )
        if digest_items:
            preview = ", ".join(
                f"{lot['id']}:{an.get('digest_rating', '?')}"
                for lot, an in digest_items[:5]
            )
            print(f"   {preview}{'…' if len(digest_items) > 5 else ''}")

        await browser.close()

    elapsed = time.time() - start_ts
    print(f"\n⏱ Итого: {elapsed:.0f}s / бюджет {RUN_BUDGET_SEC}s")
    print(f"   Сбор {stats['collect_sec']:.0f}s | Лёгкий {stats['light_sec']:.0f}s | "
          f"Тяжёлый {stats['heavy_sec']:.0f}s")

    if daily and not digest_sent:
        try:
            await send_daily_digest(
                digest_items, len(all_lots), alerts, skipped,
                partial=partial, stats=stats,
                stream_chat_id=stream_chat_id, stream_bot=stream_bot,
            )
        except Exception:
            log.exception("digest delivery failed — прогон продолжен")
        digest_sent = True

    if save_to_db:
        try:
            rid = save_agent_run(
                started_at, run_type, cats, results,
                len(all_lots), alerts, skipped, partial, stats,
            )
            print(f"💾 Снимок прогона #{rid} сохранён в БД")
        except Exception as e:
            print(f"💾 save_agent_run failed: {e}")

    print(f"\n✅ Готово! Алертов: {alerts} | Отсеяно: {skipped} | Частичный: {partial}")
    return results


def daily_job():
    asyncio.run(run(cats=DEFAULT_CATS, include_extra=True, daily=True))


if __name__ == "__main__":
    import sys
    if "--now" in sys.argv:
        asyncio.run(run(cats=DEFAULT_CATS, include_extra=True, daily=True))
    elif "--bot" in sys.argv:
        from bot_handler import run_bot
        run_bot()
    elif "--cat" in sys.argv:
        idx = sys.argv.index("--cat")
        cat = sys.argv[idx+1] if idx+1 < len(sys.argv) else "квартира"
        asyncio.run(run(cats={cat}, include_extra=True, daily=True))
    else:
        from platform_config import is_tbankrot_enabled
        if not is_tbankrot_enabled():
            print("TBANKROT disabled (TBANKROT_ENABLED=0). Telegram bot: python bot_main.py")
            raise SystemExit(0)
        print("⏰ Запуск в 08:00 ежедневно")
        schedule.every().day.at("08:00").do(daily_job)
        while True:
            schedule.run_pending()

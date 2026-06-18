"""
Режимы работы с tbankrot.ru.

По умолчанию: только анализ ОДНОГО лота по запросу (без массового сбора).
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _truthy(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# Анализ одного лота по ссылке/номеру — ОСНОВНОЙ режим (вкл. по умолчанию)
SINGLE_LOT_ENABLED = _truthy(os.getenv("SINGLE_LOT_ENABLED"), default=True)

# Массовый сбор, расписание, дайджест — выкл. по умолчанию
MASS_SCRAPING_ENABLED = _truthy(os.getenv("MASS_SCRAPING_ENABLED"), default=False)

# Обратная совместимость: TBANKROT_ENABLED=0 отключает всё
if os.getenv("TBANKROT_ENABLED") is not None and not _truthy(os.getenv("TBANKROT_ENABLED")):
    SINGLE_LOT_ENABLED = False
    MASS_SCRAPING_ENABLED = False


def is_single_lot_enabled() -> bool:
    return SINGLE_LOT_ENABLED


def is_mass_scraping_enabled() -> bool:
    return MASS_SCRAPING_ENABLED


def is_tbankrot_enabled() -> bool:
    """Любой доступ к tbankrot (один лот или массовый)."""
    return SINGLE_LOT_ENABLED or MASS_SCRAPING_ENABLED


def mass_scraping_disabled_message() -> str:
    return (
        "⏸ *Массовый сбор отключён*\n\n"
        "Автопрогоны, дайджест и сбор по категориям не запускаются.\n\n"
        "📊 Пришлите *ссылку* или *номер* одного лота:\n"
        "`https://tbankrot.ru/item?id=7608654`\n"
        "или `7608654`"
    )


def tbankrot_access_limited_message() -> str:
    return (
        "⚠️ *tbankrot недоступен или доступ ограничен*\n\n"
        "Сайт не пустил бота (Access denied / логин не прошёл).\n"
        "Попробуйте позже или проверьте лот в браузере вручную."
    )


def platform_status_message() -> str:
    lines = []
    if SINGLE_LOT_ENABLED:
        lines.append("🟢 *Режим:* анализ одного лота по запросу")
    else:
        lines.append("⏸ *Анализ лота:* отключён")
    if MASS_SCRAPING_ENABLED:
        lines.append("🟡 *Массовый сбор:* включён")
    else:
        lines.append("⏸ *Массовый сбор:* отключён")
    return "\n".join(lines)


def tbankrot_disabled_message() -> str:
    return (
        "⏸ *Анализ tbankrot отключён*\n\n"
        "На сервере выключен режим одного лота (`SINGLE_LOT_ENABLED=0`).\n"
        "Обратитесь к администратору Railway."
    )


class TbankrotAccessError(Exception):
    """Сайт недоступен, Access denied или логин не прошёл."""
    pass


def page_body_blocked(body: str) -> bool:
    b = (body or "").lower()
    needles = (
        "access denied", "access is denied", "403 forbidden",
        "доступ ограничен", "доступ запрещён", "cf-error",
        "just a moment", "checking your browser",
    )
    return any(n in b for n in needles)


def analyze_lot_hint() -> str:
    return (
        "📊 *Анализ лота*\n\n"
        "Пришлите ссылку или номер лота:\n"
        "• `https://tbankrot.ru/item?id=7608654`\n"
        "• `7608654`\n\n"
        "Бот скачает документы, прочитает ЕГРН и отчёт, посчитает дисконт "
        "и пришлёт короткую карточку с кнопкой «Полный анализ»."
    )

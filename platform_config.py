"""
Флаги платформы. Код tbankrot сохранён, но по умолчанию выключен.

Включить снова: TBANKROT_ENABLED=1 в env Railway / .env
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _truthy(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# По умолчанию ВЫКЛ — не ходим на tbankrot, не крутим Playwright/прогоны
TBANKROT_ENABLED = _truthy(os.getenv("TBANKROT_ENABLED"), default=False)


def is_tbankrot_enabled() -> bool:
    return TBANKROT_ENABLED


def tbankrot_disabled_message() -> str:
    return (
        "⏸ *Парсинг tbankrot.ru отключён*\n\n"
        "Автопрогоны, логин и сбор лотов с сайта не выполняются.\n"
        "Модули анализа, PDF/OCR и Groq сохранены для других проектов.\n\n"
        "📋 /latest — последний сохранённый снимок\n"
        "⭐ /saved — ваши сохранённые лоты\n"
        "💬 Задайте вопрос эксперту текстом в чат"
    )


def platform_status_message() -> str:
    if TBANKROT_ENABLED:
        return (
            "🟢 *tbankrot.ru:* активен\n"
            "Автопрогоны: *08:00* и *19:00* (МСК)"
        )
    return (
        "⏸ *tbankrot.ru:* отключён\n"
        "Автопрогоны и парсинг не запускаются"
    )

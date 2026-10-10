"""Сообщения интерфейса и независимый выбор языка конспекта."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


LANGUAGES = ("ru", "en")


def language_for(telegram_code: str | None) -> str:
    """Выбрать начальный язык по Telegram, не подменяя сохранённый выбор."""
    return "ru" if (telegram_code or "").lower().startswith("ru") else "en"


@lru_cache(maxsize=1)
def catalogues() -> dict[str, dict[str, str]]:
    """Загрузить оба каталога и отклонить неполный перевод до обслуживания."""
    root = Path(__file__).with_name("locales")
    result = {lang: json.loads((root / f"{lang}.json").read_text(encoding="utf-8"))
              for lang in LANGUAGES}
    if result["ru"].keys() != result["en"].keys():
        raise ValueError("Каталоги переводов имеют разные ключи")
    if any(not isinstance(value, str) for entries in result.values() for value in entries.values()):
        raise ValueError("Сообщения перевода должны быть строками")
    return result


def tr(language: str, key: str, **values) -> str:
    """Отформатировать локализованное сообщение без неявного другого языка."""
    return catalogues()[language][key].format(**values)


def notes_language(user: dict) -> str:
    """Следовать интерфейсу, пока пользователь явно не переопределил язык результата."""
    return user.get("output_language_override") or user["language"]

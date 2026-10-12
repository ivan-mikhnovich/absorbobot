"""Человекочитаемые названия лекций без внутренних идентификаторов."""

import unicodedata
from datetime import datetime
from pathlib import PurePath

from .i18n import tr
from .workers.media import placeholder_platform, platform_label


def readable_text(value: str, limit: int = 160) -> str:
    """Оставить однострочный текст без управляющих и направляющих символов."""
    if not isinstance(value, str):
        return ""
    text = "".join(char for char in value if unicodedata.category(char) not in ("Cc", "Cf"))
    return " ".join(text.split())[:limit]


def work_title(store, work: dict, *, viewer: int | None = None) -> str:
    """Назвать собственную лекцию, не раскрывая имена файлов чужого владельца."""
    owner = work["owner_id"]
    viewer = owner if viewer is None else viewer
    user = store.get_user(viewer)
    language = user["language"] if user else "en"
    if owner != viewer and not store.is_admin(viewer):
        return tr(language, "work.cached_result")
    state = work.get("ui_state") or {}
    snapshot = work.get("snapshot") or {}
    title = readable_text(state.get("title") or snapshot.get("source_title"))
    if title:
        return title
    files = store.files(work["id"])
    media = next((file for file in files if file["role"] == "media" and not file["removed"]), None)
    title = readable_text(PurePath(media["original_name"]).stem) if media else ""
    platform = placeholder_platform(title)
    if title and platform is None:
        # Имя файла записи — фактический заголовок, он и есть название лекции.
        return title
    label = tr(language, platform_label(platform))
    timestamp = work.get("created_at") or work.get("accepted_at")
    if timestamp:
        try:
            return label + " · " + datetime.fromisoformat(timestamp).astimezone().strftime("%d.%m %H:%M")
        except (TypeError, ValueError):
            pass
    return label


def work_preview(work: dict) -> str | None:
    """Вернуть только относительное имя реального изображения своей лекции."""
    state = work.get("ui_state") or {}
    snapshot = work.get("snapshot") or {}
    return state.get("preview_relative_path") or snapshot.get("source_preview")

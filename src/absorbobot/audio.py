"""Выбор звуковых дорожек записи и привязка дорожки к одному человеку."""

from __future__ import annotations

import math
import unicodedata


# Пределы недоверенных метаданных: заголовок дорожки показывается в диалоге,
# а имя человека уходит в argv ядра отдельным аргументом.
MAX_TRACKS = 32
MAX_TITLE = 120
MAX_LANGUAGE = 32
MAX_SPEAKER = 80


class AudioError(ValueError):
    """Безопасный код отказа выбора дорожек."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _text(value, limit: int) -> str | None:
    """Оставить однострочный недоверенный текст без управляющих знаков."""
    if not isinstance(value, str):
        return None
    text = "".join(" " if char.isspace() else char for char in value
                   if char.isspace() or unicodedata.category(char)[:1] != "C")
    text = " ".join(text.split())[:limit].strip()
    return text or None


def _number(value, *, integer: bool, signed: bool = False) -> int | float:
    """Проверить конечное число из ffprobe или JSON."""
    if isinstance(value, bool):
        raise AudioError("invalid_audio_track")
    if integer:
        if type(value) is not int:
            raise AudioError("invalid_audio_track")
    elif type(value) not in (int, float):
        raise AudioError("invalid_audio_track")
    if not math.isfinite(value) or (not signed and value < 0):
        raise AudioError("invalid_audio_track")
    return int(value) if integer else float(value)


def normalize_tracks(raw: object) -> list[dict]:
    """Проверить список дорожек из ffprobe и закрепить индексы потоков."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_TRACKS:
        raise AudioError("invalid_audio_track")
    tracks, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise AudioError("invalid_audio_track")
        index = _number(item.get("index"), integer=True)
        if index in seen:
            raise AudioError("invalid_audio_track")
        seen.add(index)
        default = item.get("default", False)
        if type(default) is int and default in (0, 1):
            default = bool(default)
        if not isinstance(default, bool):
            raise AudioError("invalid_audio_track")
        tracks.append({
            "index": index,
            "title": _text(item.get("title"), MAX_TITLE),
            "language": _text(item.get("language"), MAX_LANGUAGE),
            "default": default,
            "channels": _number(item.get("channels"), integer=True),
            "start_time": _number(item.get("start_time"), integer=False, signed=True),
            "offset": _number(item.get("offset"), integer=False, signed=True),
        })
    return sorted(tracks, key=lambda track: track["index"])


def _normalize_selection(raw: object, indices: set[int]) -> list[int]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_TRACKS:
        raise AudioError("audio_selection_invalid")
    selection, seen = [], set()
    for value in raw:
        if type(value) is not int or value not in indices or value in seen:
            raise AudioError("audio_selection_invalid")
        seen.add(value)
        selection.append(value)
    return sorted(selection)


def _normalize_speakers(raw: object, indices: set[int]) -> dict[int, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_TRACKS:
        raise AudioError("invalid_track_speaker")
    speakers = {}
    for key, value in raw.items():
        try:
            index = int(key) if isinstance(key, (str, int)) and not isinstance(key, bool) else None
        except (TypeError, ValueError):
            index = None
        if index is None or index not in indices or index in speakers:
            raise AudioError("invalid_track_speaker")
        name = _text(value, MAX_SPEAKER + 1)
        if name is None or len(name) > MAX_SPEAKER:
            raise AudioError("invalid_track_speaker")
        speakers[index] = name
    return speakers


def normalize_document(document: object, *, require_selection: bool = True) -> dict:
    """Привести снимок выбора дорожек к каноническому виду.

    Одиночная дорожка выбирается автоматически, потому что спрашивать нечего.
    Несколько дорожек без явного подмножества отклоняются до запуска обработки.
    """
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise AudioError("invalid_audio_track")
    tracks = normalize_tracks(document.get("tracks", document.get("audio_tracks")))
    indices = {track["index"] for track in tracks}
    selection = _normalize_selection(
        document.get("selected", document.get("audio_selection")), indices)
    if len(tracks) == 1:
        selection = [tracks[0]["index"]]
    elif tracks and require_selection and not selection:
        raise AudioError("audio_selection_required")
    speakers = _normalize_speakers(
        document.get("speakers", document.get("audio_speakers")), indices)
    if require_selection and set(speakers) - set(selection):
        raise AudioError("invalid_track_speaker")
    return {"tracks": tracks, "selected": selection, "speakers": speakers}


def from_state(state: object) -> dict:
    """Прочитать выбор дорожек из ``ui_state`` черновика."""
    if not isinstance(state, dict):
        return {"tracks": [], "selected": [], "speakers": {}}
    return normalize_document({
        "tracks": state.get("audio_tracks"),
        "selected": state.get("audio_selection"),
        "speakers": state.get("audio_speakers"),
    }, require_selection=False)


def from_snapshot(snapshot: object) -> dict:
    """Прочитать замороженный выбор дорожек из снимка задания."""
    if not isinstance(snapshot, dict):
        return {"tracks": [], "selected": [], "speakers": {}}
    return normalize_document(snapshot.get("audio"), require_selection=False)


def state_fields(document: dict) -> dict:
    """Поля ``ui_state`` для сохранения выбора дорожек."""
    return {"audio_tracks": document["tracks"],
            "audio_selection": document["selected"],
            "audio_speakers": {str(index): name
                               for index, name in document["speakers"].items()}}


def signature_part(document: dict) -> dict:
    """Часть подписи входа: выбор и привязки, но не отображаемые названия."""
    value = normalize_document(document, require_selection=True)
    return {"tracks": [track["index"] for track in value["tracks"]],
            "selected": value["selected"],
            "speakers": {str(index): name
                         for index, name in value["speakers"].items()}}


def speaker_name(value) -> str:
    """Проверить имя человека, которому принадлежит вся дорожка."""
    name = _text(value, MAX_SPEAKER + 1)
    if name is None or len(name) > MAX_SPEAKER:
        raise AudioError("invalid_speaker")
    return name


def core_arguments(document: dict) -> list[str]:
    """Собрать повторяемые аргументы ядра без склейки в shell-строку."""
    value = normalize_document(document, require_selection=True)
    arguments: list[str] = []
    for index in value["selected"]:
        arguments += ["--audio-track", str(index)]
    for index in value["selected"]:
        name = value["speakers"].get(index)
        if name is not None:
            arguments += ["--track-speaker", f"{index}={name}"]
    return arguments

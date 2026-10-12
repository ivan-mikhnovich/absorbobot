"""Скачать ровно одну запись поддерживаемой площадки без пользовательской конфигурации.

Поддержаны YouTube и VK Video. Ссылку разбирает собственный строгий парсер
площадки, а в yt-dlp регистрируются только экстракторы этой площадки, поэтому
произвольные URL и чужие экстракторы процессу недоступны.
"""

from __future__ import annotations

from collections.abc import Callable
import importlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import NamedTuple
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4


class MediaError(ValueError):
    """Отказ с безопасным кодом без URL потоков и ответов сервера."""


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
VK_VIDEO_ID = re.compile(r"-?\d{1,20}_\d{1,20}")
_VIDEO_HEIGHT = 1080

# Сетевые отказы, при которых имеет смысл попробовать другой маршрут.
_ROUTE_FAILURES = frozenset({"media_failed", "timeout"})


def _url_segments(value: str, hosts: frozenset[str]) -> list[str]:
    """Разобрать простую HTTPS-ссылку площадки и вернуть сегменты её пути.

    Принимается только прямой путь без query, fragment, userinfo, порта,
    escaped-символов и двойных слэшей; завершающий слэш допускается.
    """
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise MediaError("invalid_media_url")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.hostname not in hosts or parsed.port not in (None, 443)
            or parsed.query or "%" in parsed.path or "\\" in parsed.netloc
            or "\\" in parsed.path or "//" in parsed.path):
        raise MediaError("invalid_media_url")
    return [part for part in parsed.path.split("/") if part]


def _parse_youtube(value: str) -> tuple[str, str]:
    """Разобрать только разрешённые HTTPS-формы и построить собственный URL."""
    try:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
                or parsed.port not in (None, 443)):
            raise ValueError
        # Запретить двусмысленные escaped paths и любые playlist-параметры.
        if "%" in parsed.path or "\\" in parsed.netloc or "\\" in parsed.path:
            raise ValueError
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        keys = [key for key, _ in query]
        if len(keys) != len(set(keys)) or any(key in {"list", "index", "playlist"} for key in keys):
            raise ValueError
        if any(key not in {"v", "t", "start", "si", "feature"} for key in keys):
            raise ValueError
        parts = parsed.path.split("/")
        if parsed.hostname == "youtu.be":
            if len(parts) != 2 or "v" in keys:
                raise ValueError
            video_id = parts[1]
        elif parsed.path == "/watch":
            if keys.count("v") != 1:
                raise ValueError
            video_id = dict(query)["v"]
        elif len(parts) == 3 and parts[1] in {"shorts", "embed"} and "v" not in keys:
            video_id = parts[2]
        else:
            raise ValueError
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError
        return "https://www.youtube.com/watch?v=" + video_id, video_id
    except (ValueError, TypeError, AttributeError):
        raise MediaError("invalid_media_url") from None


VK_VIDEO_ID = re.compile(r"-?\d{1,20}_\d{1,20}")
_RUTUBE_HOSTS = frozenset({"rutube.ru", "www.rutube.ru", "m.rutube.ru"})
_DZEN_HOSTS = frozenset({"dzen.ru", "www.dzen.ru", "zen.yandex.ru"})
_VK_HOSTS = frozenset({"vk.com", "m.vk.com", "new.vk.com", "vk.ru", "m.vk.ru",
                       "vkvideo.ru", "m.vkvideo.ru"})


def _parse_vk(value: str) -> tuple[str, str]:
    """Разобрать только публичные HTTPS-ссылки на одну запись VK Video."""
    segments = _url_segments(value, _VK_HOSTS)
    if len(segments) != 1 or not segments[0].startswith("video"):
        raise MediaError("invalid_media_url")
    media_id = segments[0][len("video"):]
    if not VK_VIDEO_ID.fullmatch(media_id):
        raise MediaError("invalid_media_url")
    return "https://vk.com/video" + media_id, media_id


RUTUBE_ID = re.compile(r"[0-9a-f]{32}")


def _parse_rutube(value: str) -> tuple[str, str]:
    """Разобрать ссылку на одну публичную запись Rutube.

    Принимается только ``/video/<идентификатор>``: приватные ссылки с токеном
    ``?p=``, эфиры ``/live/video/`` и встраиваемые ``/embed/`` отклоняются.
    """
    segments = _url_segments(value, _RUTUBE_HOSTS)
    if len(segments) != 2 or segments[0] != "video" or not RUTUBE_ID.fullmatch(segments[1]):
        raise MediaError("invalid_media_url")
    return "https://rutube.ru/video/" + segments[1] + "/", segments[1]


DZEN_ID = re.compile(r"[a-z0-9-]{8,64}")


def _parse_dzen(value: str) -> tuple[str, str]:
    """Разобрать ссылку на одну видеозапись Дзена."""
    segments = _url_segments(value, _DZEN_HOSTS)
    if len(segments) != 3 or segments[0] != "video" or segments[1] != "watch" \
            or not DZEN_ID.fullmatch(segments[2]):
        raise MediaError("invalid_media_url")
    return "https://dzen.ru/video/watch/" + segments[2], segments[2]


_VKPLAY_HOSTS = frozenset({"vkplay.live", "www.vkplay.live"})
_VKPLAY_USER = re.compile(r"[A-Za-z0-9._-]{1,64}")
VKPLAY_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _parse_vkplay(value: str) -> tuple[str, str]:
    """Разобрать ссылку на одну запись VK Play.

    Принимается только ``/record/<uuid>``: каналы, клипы и прямые эфиры
    обслуживает другой экстрактор, а его идентификатор не совпадает со ссылкой.
    """
    segments = _url_segments(value, _VKPLAY_HOSTS)
    if (len(segments) != 3 or segments[1] != "record"
            or not _VKPLAY_USER.fullmatch(segments[0]) or not VKPLAY_ID.fullmatch(segments[2])):
        raise MediaError("invalid_media_url")
    return "https://vkplay.live/" + segments[0] + "/record/" + segments[2], segments[2]


_SOUNDCLOUD_HOSTS = frozenset({"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"})
_SOUNDCLOUD_PART = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _parse_soundcloud(value: str) -> tuple[str, str]:
    """Разобрать ссылку на один публичный трек SoundCloud.

    Секретные ссылки ``/s-…``, ``api.soundcloud.com`` и плейлисты не
    принимаются: нужна обычная ссылка на один трек.
    """
    segments = _url_segments(value, _SOUNDCLOUD_HOSTS)
    if len(segments) != 2 or any(not _SOUNDCLOUD_PART.fullmatch(part) for part in segments):
        raise MediaError("invalid_media_url")
    track = "/".join(segments)
    return "https://soundcloud.com/" + track, track


def _soundcloud_track(info: dict, media_id: str) -> bool:
    """Сверить трек SoundCloud по пермалинку: числовой id не выводится из ссылки."""
    url = info.get("webpage_url")
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    return (parsed.hostname in _SOUNDCLOUD_HOSTS
            and parsed.path.strip("/").lower() == media_id.lower())


_TWITCH_HOSTS = frozenset({"twitch.tv", "www.twitch.tv"})
TWITCH_ID = re.compile(r"[0-9]{1,20}")


def _parse_twitch(value: str) -> tuple[str, str]:
    """Разобрать ссылку на одну запись трансляции Twitch."""
    segments = _url_segments(value, _TWITCH_HOSTS)
    if len(segments) != 2 or segments[0] != "videos" or not TWITCH_ID.fullmatch(segments[1]):
        raise MediaError("invalid_media_url")
    # Экстрактор дописывает к номеру записи префикс ``v``.
    return "https://www.twitch.tv/videos/" + segments[1], "v" + segments[1]


_ZOOM_HOST = re.compile(r"(?:[a-z0-9-]+\.)?zoom\.us")
ZOOM_ID = re.compile(r"[A-Za-z0-9._-]{8,128}")


def _parse_zoom(value: str) -> tuple[str, str]:
    """Разобрать ссылку на одну облачную запись Zoom.

    Параметры ``pwd`` и ``startTime`` отбрасываются: пароль записи бот не
    принимает, а обрезка по времени не поддерживается. Вид страницы ``play``
    или ``share`` сохраняется: у части учётных записей работает только один.
    """
    try:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or not _ZOOM_HOST.fullmatch(host) or parsed.port not in (None, 443)):
            raise ValueError
        if "%" in parsed.path or "\\" in parsed.netloc or "\\" in parsed.path or "//" in parsed.path:
            raise ValueError
        segments = [part for part in parsed.path.split("/") if part]
        if (len(segments) != 3 or segments[0] not in ("rec", "recording")
                or segments[1] not in ("play", "share") or not ZOOM_ID.fullmatch(segments[2])):
            raise ValueError
        return f"https://{host}/" + "/".join(segments), segments[2]
    except (ValueError, TypeError, AttributeError):
        raise MediaError("invalid_media_url") from None


_PANOPTO_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.panopto\.(?:com|eu)")
PANOPTO_ID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")


def _parse_panopto(value: str) -> tuple[str, str]:
    """Разобрать ссылку на одну публичную запись Panopto.

    Принимаются только страницы ``Viewer.aspx`` и ``Embed.aspx`` с полным
    идентификатором сессии: ``View.aspx?vid=`` и плейлисты с ``pid=`` экстрактор
    не обслуживает.
    """
    try:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        keys = [key for key, _ in query]
        if (parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or not _PANOPTO_HOST.fullmatch(host) or parsed.port not in (None, 443)):
            raise ValueError
        if "%" in parsed.path or "\\" in parsed.netloc or "\\" in parsed.path or "//" in parsed.path:
            raise ValueError
        if keys.count("id") != 1 or "pid" in keys:
            raise ValueError
        segments = [part for part in parsed.path.split("/") if part]
        if len(segments) != 3 or segments[:2] != ["Panopto", "Pages"] \
                or segments[2] not in ("Viewer.aspx", "Embed.aspx"):
            raise ValueError
        media_id = dict(query)["id"]
        if not PANOPTO_ID.fullmatch(media_id):
            raise ValueError
        return f"https://{host}/Panopto/Pages/{segments[2]}?id={media_id}", media_id
    except (ValueError, TypeError, AttributeError):
        raise MediaError("invalid_media_url") from None


_KALTURA_HOSTS = frozenset({"kaltura.com", "www.kaltura.com", "cdnapi.kaltura.com",
                            "cdnapisec.kaltura.com"})
KALTURA_PARTNER = re.compile(r"[0-9]{1,12}")
KALTURA_ENTRY = re.compile(r"[0-9]{1,12}_[0-9a-z]{1,32}")


def _parse_kaltura(value: str) -> tuple[str, str]:
    """Разобрать ссылку на одну публичную запись Kaltura.

    Принимается адрес встраиваемого проигрывателя: страницы учебных порталов
    yt-dlp не разбирает вовсе, поэтому форму ``kaltura:<партнёр>:<запись>``
    собираем сами.
    """
    segments = _url_segments(value, _KALTURA_HOSTS)
    if segments[:2] == ["index.php", "kwidget"]:
        rest = segments[2:]
    elif segments[:3] == ["index.php", "extwidget", "preview"]:
        rest = segments[3:]
    elif (len(segments) >= 4 and segments[:2] == ["html5", "html5lib"]
            and segments[3] == "mwEmbedFrame.php"):
        rest = segments[4:]
    else:
        raise MediaError("invalid_media_url")
    partner = entry = None
    for index, token in enumerate(rest[:-1]):
        following = rest[index + 1]
        if token == "wid" and not partner and following.startswith("_") \
                and KALTURA_PARTNER.fullmatch(following[1:]):
            partner = following[1:]
        elif token in ("p", "partner_id") and not partner and KALTURA_PARTNER.fullmatch(following):
            partner = following
        elif token in ("entry_id", "entryId") and not entry and KALTURA_ENTRY.fullmatch(following):
            entry = following
    if not partner or not entry:
        raise MediaError("invalid_media_url")
    return "kaltura:" + partner + ":" + entry, entry


def _check_youtube(info: dict) -> None:
    """Не начинать загрузку эфира, премьеры или закрытой записи."""
    if (info.get("is_live") or info.get("is_upcoming")
            or info.get("live_status") not in ("not_live", "was_live")):
        raise MediaError("media_live")
    if info.get("availability") not in (None, "public", "unlisted"):
        raise MediaError("media_unavailable")


def _check_finished(info: dict) -> None:
    """Не начинать загрузку прямого эфира, закрытой или удалённой записи.

    Площадки без поля ``live_status`` проверяются по ``is_live`` и
    ``is_upcoming``; пустой список форматов означает закрытую, удалённую или
    требующую входа запись, а незавершённый эфир без длины отклоняет общая
    проверка длительности.
    """
    if (info.get("is_live") or info.get("is_upcoming")
            or info.get("live_status") in ("is_live", "is_upcoming", "post_live")):
        raise MediaError("media_live")
    if not info.get("formats"):
        raise MediaError("media_unavailable")


class Mode(NamedTuple):
    """Режим подготовки: селектор формата yt-dlp, расширение файла и постобработка."""

    selector: str
    extension: str
    postprocessors: tuple[dict, ...] = ()
    # Предел высоты кадра в режиме видео: у экранных записей кадр выше 1080.
    max_height: int | None = _VIDEO_HEIGHT


def _same_id(info: dict, media_id: str) -> bool:
    """Тождество записи по идентификатору экстрактора."""
    return info.get("id") == media_id


class Platform(NamedTuple):
    """Правила площадки: что принимать, чем скачивать и что проверять в метаданных."""

    name: str
    # Ключи экстрактора yt-dlp, метаданным которых доверяет проверка.
    keys: frozenset[str]
    # Полные имена классов yt-dlp, подключаемых к загрузчику.
    extractors: tuple[str, ...]
    # Доступные режимы подготовки: «video» и «audio», у звуковых площадок — один.
    modes: dict[str, Mode]
    parse: Callable[[str], tuple[str, str]]
    check: Callable[[dict], None]
    # Сверка записи, когда идентификатор экстрактора не выводится из ссылки.
    identify: Callable[[dict, str], bool] = _same_id


# Видео и звук скачиваются одинаково почти у всех площадок; отличия задаёт запись.
_VIDEO = Mode(f"bv*[height<={_VIDEO_HEIGHT}]+ba/b[height<={_VIDEO_HEIGHT}]", ".mkv",
              ({"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"},))
_SOUND = Mode("bestaudio/best", ".m4a",
              ({"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},))


# Порядок площадок задаёт порядок разбора: узлы у них не пересекаются.
_PLATFORMS = (
    Platform(name="youtube", keys=frozenset({"Youtube"}),
             extractors=("yt_dlp.extractor.youtube.YoutubeIE",),
             modes={"video": Mode(f"bv[height<={_VIDEO_HEIGHT}]+ba/b[height<={_VIDEO_HEIGHT}]",
                                  ".mkv", _VIDEO.postprocessors),
                    "audio": Mode("bestaudio", ".m4a", _SOUND.postprocessors)},
             parse=_parse_youtube, check=_check_youtube),
    Platform(name="vk", keys=frozenset({"VK"}),
             extractors=("yt_dlp.extractor.vk.VKIE",),
             modes={"video": _VIDEO, "audio": _SOUND},
             parse=_parse_vk, check=_check_finished),
    Platform(name="rutube", keys=frozenset({"Rutube"}),
             extractors=("yt_dlp.extractor.rutube.RutubeIE",),
             modes={"video": _VIDEO, "audio": _SOUND},
             parse=_parse_rutube, check=_check_finished),
    Platform(name="dzen", keys=frozenset({"ZenYandex"}),
             extractors=("yt_dlp.extractor.yandexvideo.ZenYandexIE",),
             modes={"video": _VIDEO, "audio": _SOUND},
             parse=_parse_dzen, check=_check_finished),
    Platform(name="vkplay", keys=frozenset({"VKPlay"}),
             extractors=("yt_dlp.extractor.vk.VKPlayIE",),
             modes={"video": _VIDEO, "audio": _SOUND},
             parse=_parse_vkplay, check=_check_finished),
    Platform(name="twitch", keys=frozenset({"TwitchVod"}),
             extractors=("yt_dlp.extractor.twitch.TwitchVodIE",),
             modes={"video": _VIDEO, "audio": _SOUND},
             parse=_parse_twitch, check=_check_finished),
    Platform(name="soundcloud", keys=frozenset({"Soundcloud"}),
             extractors=("yt_dlp.extractor.soundcloud.SoundcloudIE",),
             modes={"audio": _SOUND},
             parse=_parse_soundcloud, check=_check_finished, identify=_soundcloud_track),
    Platform(name="zoom", keys=frozenset({"Zoom"}),
             extractors=("yt_dlp.extractor.zoom.ZoomIE",),
             # Раскладка с демонстрацией экрана выше 1080: предел не применяем.
             modes={"video": Mode("b", ".mkv", _VIDEO.postprocessors, max_height=None),
                    "audio": _SOUND},
             parse=_parse_zoom, check=_check_finished),
    Platform(name="panopto", keys=frozenset({"Panopto"}),
             extractors=("yt_dlp.extractor.panopto.PanoptoIE",),
             # Прогрессивная копия Panopto приходит без кадра, поэтому берём HLS.
             modes={"video": _VIDEO, "audio": _SOUND},
             parse=_parse_panopto, check=_check_finished),
    Platform(name="kaltura", keys=frozenset({"Kaltura"}),
             extractors=("yt_dlp.extractor.kaltura.KalturaIE",),
             modes={"video": _VIDEO, "audio": _SOUND},
             parse=_parse_kaltura, check=_check_finished),
)

# Служебная подпись площадки в имени записи и ключ её названия в интерфейсе.
LABELS: dict[str, tuple[str, str]] = {
    "youtube": ("YouTube", "work.youtube"),
    "vk": ("VK Video", "work.vk"),
    "rutube": ("Rutube", "work.rutube"),
    "dzen": ("Дзен", "work.dzen"),
    "vkplay": ("VK Play", "work.vkplay"),
    "zoom": ("Zoom", "work.zoom"),
    "panopto": ("Panopto", "work.panopto"),
    "kaltura": ("Kaltura", "work.kaltura"),
    "soundcloud": ("SoundCloud", "work.soundcloud"),
    "twitch": ("Twitch", "work.twitch"),
}


def resolve_media_url(value: str) -> tuple[Platform, str, str]:
    """Найти площадку по ссылке и вернуть её правила, канонический URL и идентификатор."""
    for platform in _PLATFORMS:
        try:
            url, media_id = platform.parse(value)
        except MediaError:
            continue
        return platform, url, media_id
    raise MediaError("invalid_media_url")


def normalize_media_url(value: str) -> str:
    """Проверить ссылку без сети и вернуть канонический URL одной записи."""
    return resolve_media_url(value)[1]


def placeholder_name(value: str) -> str:
    """Построить служебное имя записи до получения фактического заголовка."""
    platform, _, media_id = resolve_media_url(value)
    return LABELS[platform.name][0] + " " + media_id


def placeholder_platform(name: str) -> str | None:
    """Назвать площадку по служебному имени записи или вернуть ``None``."""
    for platform, (label, _) in LABELS.items():
        if name.startswith(label + " "):
            return platform
    return None


def platform_label(platform: str | None) -> str:
    """Ключ названия площадки для интерфейса, обычной лекции — по умолчанию."""
    return LABELS[platform][1] if platform in LABELS else "work.lecture"


_TITLE_LIMIT = 300


def _validate_proxy(value: object) -> str:
    """Проверить адрес прокси загрузок: http(s), узел, порт, без credentials."""
    valid = False
    if isinstance(value, str) and value and value.strip() == value \
            and not any(c in value for c in ("\n", "\r", "\t", "\x00")):
        try:
            parsed = urlsplit(value)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname is not None
                     and parsed.port is not None and parsed.username is None
                     and parsed.password is None and parsed.path in ("", "/")
                     and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
    if not valid:
        raise MediaError("invalid_media_url")
    return value


def clean_title(value) -> str | None:
    """Оставить человекочитаемый заголовок без управляющих знаков или ``None``."""
    if not isinstance(value, str):
        return None
    title = "".join(char for char in value if char >= " " and char != "\x7f").strip()
    if not title:
        return None
    return title[:_TITLE_LIMIT]


def validate_metadata(info: dict, media_id: str, max_seconds: float, platform: Platform) -> dict:
    """Не начинать загрузку эфира, плейлиста или записи неизвестной длины."""
    if (not isinstance(info, dict) or info.get("_type", "video") != "video"
            or info.get("entries") is not None or info.get("extractor_key") not in platform.keys
            or not platform.identify(info, media_id)):
        raise MediaError("invalid_media_metadata")
    platform.check(info)
    duration = info.get("duration")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise MediaError("invalid_duration")
    if duration > max_seconds:
        raise MediaError("media_too_long")
    result = {"duration": duration}
    title = clean_title(info.get("title"))
    if title is not None:
        # Заголовок берём из фактических метаданных, а не из идентификатора записи.
        result["title"] = title
    return result


def tree_bytes(root: Path) -> int:
    """Считать реальные файлы, не следуя ссылкам из недоверенного дерева."""
    total, pending = 0, [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                else:
                    raise MediaError("unsafe_file_path")
    return total


class DownloadBudget:
    """Ограничить фактический поток, включая HTTP без Content-Length."""

    def __init__(self, directory: Path, limit: int, report=None):
        self.directory, self.limit = directory.resolve(), limit
        self.report = report
        self.downloaded: dict[str, int] = {}

    def __call__(self, progress: dict) -> None:
        filename = progress.get("filename") or progress.get("tmpfilename")
        if filename:
            path = Path(filename).resolve()
            if not path.is_relative_to(self.directory):
                raise MediaError("unsafe_file_path")
            value = progress.get("downloaded_bytes")
            if type(value) is int and value >= 0:
                self.downloaded[str(path)] = max(self.downloaded.get(str(path), 0), value)
        if self.report is not None:
            # Форматы видео и аудио приходят отдельно; их общий размер заранее неизвестен.
            self.report(sum(self.downloaded.values()), None)
        if sum(self.downloaded.values()) > self.limit or tree_bytes(self.directory) > 2 * self.limit:
            raise MediaError("input_too_large")


class _QuietLogger:
    # Подписанные stream URLs и внешние ошибки не попадают в stdout/stderr бота.
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def _progress_module():
    """Загрузить модуль прогресса и в пакете, и при запуске как скрипта."""
    try:
        from . import progress as module
    except ImportError:
        import progress as module  # type: ignore[no-redef]
    return module


def _downloader(platform: Platform, options: dict, proxy: str | None = None):
    """Открыть yt-dlp с экстракторами только выбранной площадки."""
    from yt_dlp import YoutubeDL
    from yt_dlp.globals import all_plugins_loaded, plugin_dirs
    # Library API не читает CLI config; глобальная загрузка plugins отключена явно.
    plugin_dirs.value = []
    all_plugins_loaded.value = True
    parameters = {
        "quiet": True, "no_warnings": True, "logger": _QuietLogger(),
        "noplaylist": True, "cachedir": False, "cookiefile": None,
        "cookiesfrombrowser": None, "enable_file_urls": False,
        "js_runtimes": {"bun": {"path": "/usr/bin/bun"}}, "remote_components": [],
        "socket_timeout": 30, "retries": 3,
        **options,
    }
    if proxy is not None:
        parameters["proxy"] = _validate_proxy(proxy)
    downloader = YoutubeDL(parameters, auto_init=False)
    for dotted in platform.extractors:
        module, _, name = dotted.rpartition(".")
        downloader.add_info_extractor(getattr(importlib.import_module(module), name)())
    return downloader


def _download(platform: Platform, url: str, media_id: str, mode: str, byte_limit: int,
              max_seconds: float, directory: Path, proxy: str | None = None) -> dict:
    mode_spec = platform.modes[mode]
    reporter = _progress_module().ProgressWriter(directory.parent.parent, "media", "bytes",
                                                 min_interval=0.5)
    budget = DownloadBudget(directory, byte_limit, report=reporter.update)
    options = {
        "outtmpl": str(directory / "media.%(ext)s"), "restrictfilenames": True,
        "paths": {"home": str(directory), "temp": str(directory)},
        "progress_hooks": [budget], "max_filesize": byte_limit,
        "concurrent_fragment_downloads": 1, "fragment_retries": 3, "overwrites": False,
        "format": mode_spec.selector, "merge_output_format": "mkv",
        "postprocessors": list(mode_spec.postprocessors),
    }
    with _downloader(platform, options, proxy) as downloader:
        info = downloader.extract_info(url, download=False, process=False)
        metadata = validate_metadata(info, media_id, max_seconds, platform)
        # Обработка только уже проверенного результата площадки, без повторного arbitrary URL.
        result = downloader.process_ie_result(info, download=True)
        if mode == "video" and (type(result.get("height")) not in (int, float)
                                 or not 0 < result["height"]
                                 or (mode_spec.max_height is not None
                                     and result["height"] > mode_spec.max_height)):
            raise MediaError("invalid_media_metadata")
    output = directory / ("media" + mode_spec.extension)
    if output.is_symlink() or not output.is_file() or output.stat().st_size <= 0:
        raise MediaError("media_failed")
    if output.stat().st_size > byte_limit:
        raise MediaError("input_too_large")
    budget({})
    return {"relative_path": str(output.relative_to(directory.parent.parent)), **metadata}


def lookup(url: str, max_seconds: float, proxy: str | None = None) -> dict:
    """Прочитать метаданные записи без загрузки файла для восстановления подписи."""
    platform, canonical, media_id = resolve_media_url(url)
    with _downloader(platform, {}, proxy) as downloader:
        info = downloader.extract_info(canonical, download=False, process=False)
        return validate_metadata(info, media_id, max_seconds, platform)


def _stop_child(process: subprocess.Popen) -> None:
    """Завершить downloader и ffmpeg; дождаться child до возврата worker."""
    import psutil
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    _, alive = psutil.wait_procs(children, timeout=0)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=10)


def _attempt(root: Path, directory: Path, proxy: str | None, url: str, mode: str,
             byte_limit: int, max_seconds: float, max_work_bytes: int,
             min_free_bytes: int) -> dict:
    """Запустить дочернюю загрузку по одному маршруту и дождаться её результата."""
    result_path = directory / "result.json"
    command = [sys.executable, str(Path(__file__).absolute()), "--child", url,
               mode, str(byte_limit), str(max_seconds), str(directory)]
    if proxy is not None:
        command.append(proxy)
    with (directory / "downloader.stdout.log").open("xb") as stdout, (directory / "downloader.stderr.log").open("xb") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr)
    try:
        while True:
            if tree_bytes(directory) > 2 * byte_limit:
                raise MediaError("input_too_large")
            if tree_bytes(root) > max_work_bytes:
                raise MediaError("work_limit")
            usage = os.statvfs(root)
            if usage.f_bavail * usage.f_frsize < min_free_bytes:
                raise MediaError("storage_unavailable")
            if process.poll() is not None:
                break
            time.sleep(0.1)
        if not result_path.is_file() or result_path.stat().st_size > 16384:
            raise MediaError("media_failed")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if process.returncode or not result.get("ok"):
            raise MediaError(result.get("error", "media_failed"))
        output = (root / result["relative_path"]).resolve(strict=True)
        if not output.is_relative_to(directory) or output.stat().st_size > byte_limit:
            raise MediaError("input_too_large")
        return result
    except BaseException:
        # Неудачная загрузка не оставляет progress-файл стадии в дереве задания.
        _progress_module().discard_progress(root, "media")
        raise
    finally:
        # Не оставлять дочернюю extraction/merge после отказа guard или сигнала.
        _stop_child(process)


def download(url: str, mode: str, byte_limit: int, max_seconds: float,
             max_work_bytes: int, min_free_bytes: int, *, proxy: str | None = None,
             work_root: Path = Path("/work")) -> dict:
    """Скачать запись, пробуя прокси и запасное прямое подключение.

    Часть площадок доступна только из одной сети: прокси помогает одним,
    а прямое соединение — другим. Маршрут меняется один раз и только при
    сетевом отказе: проверки ссылки, метаданных и пределов от маршрута
    не зависят и повторяются бессмысленно.
    """
    platform, _, _ = resolve_media_url(url)
    if mode not in platform.modes:
        raise MediaError("invalid_media_url")
    if proxy is not None:
        _validate_proxy(proxy)
    root = work_root.resolve(strict=True)
    routes = [None] if proxy is None else [proxy, None]
    failure: MediaError | None = None
    for route in routes:
        directory = root / "tmp" / ("media-" + uuid4().hex)
        directory.mkdir(mode=0o700)
        try:
            return _attempt(root, directory, route, url, mode, byte_limit,
                            max_seconds, max_work_bytes, min_free_bytes)
        except MediaError as exc:
            failure = exc
            if route is None or str(exc) not in _ROUTE_FAILURES:
                raise
    raise failure if failure is not None else MediaError("media_failed")


def _result_error(exc: Exception) -> dict:
    code = str(exc) if isinstance(exc, MediaError) else "media_failed"
    return {"ok": False, "error": code}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--metadata":
        try:
            if len(args) not in (3, 4):
                raise MediaError("media_failed")
            proxy = args[3] if len(args) == 4 else None
            result = {"ok": True, **lookup(args[1], float(args[2]), proxy)}
        except Exception as exc:
            result = _result_error(exc)
        print(json.dumps(result, allow_nan=False))
        return 0 if result["ok"] else 2
    if args and args[0] == "--child":
        # Каталог результата задаёт родитель; прокси добавляется только при настройке.
        directory = Path(args[5]) if len(args) >= 6 else Path(args[-1])
        proxy = args[6] if len(args) == 7 else None
        try:
            if len(args) not in (6, 7):
                raise MediaError("media_failed")
            platform, canonical, media_id = resolve_media_url(args[1])
            result = {"ok": True, **_download(platform, canonical, media_id, args[2],
                                              int(args[3]), float(args[4]), directory, proxy)}
        except Exception as exc:
            detail = re.sub(r"(?<=://)[^\s/]+@", "<redacted>@", str(exc))
            print(f"{type(exc).__name__}: {detail}", file=sys.stderr)
            result = _result_error(exc)
        temporary = directory / "result.tmp"
        temporary.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
        temporary.replace(directory / "result.json")
        return 0 if result["ok"] else 2
    try:
        if len(args) not in (6, 7):
            raise MediaError("media_failed")
        proxy = args[6] if len(args) == 7 else None
        result = download(args[0], args[1], int(args[2]), float(args[3]), int(args[4]), int(args[5]),
                          proxy=proxy)
        print(json.dumps(result, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps(_result_error(exc), allow_nan=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

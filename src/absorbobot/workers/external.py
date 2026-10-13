"""Скачать ровно один файл по публичной внешней ссылке в изолированном worker.

Поддерживаются публичные файлы Яндекс.Диска, публичные файлы Google Drive и
прямые HTTP(S)-ссылки. Папки, приватные входы и архивы не распаковываются.
Транспорт берётся из :mod:`secure_http`; сам модуль никогда не ходит в сеть
напрямую и не раскрывает подписанные ссылки в ошибках или выводе.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
import math
import os
from pathlib import Path, PurePath
import re
import stat
import sys
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote_to_bytes, urlencode, urlsplit
from uuid import uuid4


class ExternalError(ValueError):
    """Отказ с безопасным кодом без подписанных ссылок и ответов сервера."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# Коды, которые разрешено показывать наружу как причину отказа.
_SAFE_CODES = frozenset({
    "invalid_url", "unsafe_url", "invalid_file_metadata", "unsupported_input",
    "input_too_large", "download_failed", "timeout", "work_limit",
    "storage_unavailable", "unsafe_file_path", "link_requires_signin",
})

_YANDEX_HOSTS = frozenset({"disk.yandex.ru", "disk.yandex.com", "yadi.sk"})
_GOOGLE_HOSTS = frozenset({"drive.google.com", "drive.usercontent.google.com"})
# Узлы, куда Google уводит анонимный запрос к закрытому файлу.
_GOOGLE_ACCOUNTS_HOSTS = frozenset({"accounts.google.com"})

_YANDEX_API = "https://cloud-api.yandex.net/v1/disk/public/resources"
_YANDEX_KEY = re.compile(r"[A-Za-z0-9_-]{3,200}")
_GOOGLE_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
_GOOGLE_CONFIRM_HOST = "drive.usercontent.google.com"
_GOOGLE_CONFIRM_PATH = "/download"
_GOOGLE_FORM_FIELDS = ("id", "export", "confirm", "uuid")

_EXTENSION = re.compile(r"\.[a-z0-9]+")

# Сетевые отказы, при которых имеет смысл попробовать другой маршрут.
_ROUTE_FAILURES = frozenset({"timeout", "download_failed"})

# Расширение по типу содержимого, когда имя файла не даёт решения.
_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.presentation": ".odp",
}

# Начало HTML-документа: страницы входа и ошибок не должны стать материалами.
_HTML_MARKER = re.compile(rb"^(?:\xef\xbb\xbf)?\s*(?:<!doctype\s+html|<html[\s>])", re.I)

_CHUNK = 64 * 1024
_SNIFF = 1024
_HTML_LIMIT = 512 * 1024
_JSON_LIMIT = 256 * 1024
_REQUEST_LIMIT = 1024 * 1024
_MAX_NAME = 160
_DEFAULT_TIMEOUT = 600.0


def normalize_external_url(value: str) -> str:
    """Проверить синтаксис ссылки без сети и вернуть её же для транспорта."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ExternalError("invalid_url")
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ExternalError("invalid_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise ExternalError("invalid_url") from None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ExternalError("invalid_url")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ExternalError("invalid_url")
    if not host:
        raise ExternalError("invalid_url")
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        raise ExternalError("invalid_url")
    return value


def _normalize_extensions(values) -> set[str]:
    """Привести список расширений к нижнему регистру с ведущей точкой."""
    if values is None:
        return set()
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ExternalError("invalid_file_metadata")
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ExternalError("invalid_file_metadata")
        item = value.strip().lower()
        if not item.startswith("."):
            item = "." + item
        if _EXTENSION.fullmatch(item):
            result.add(item)
    return result


def _provider_of(url: str) -> str:
    """Определить провайдера по точному имени узла."""
    host = urlsplit(url).hostname or ""
    if host in _YANDEX_HOSTS:
        return "yandex"
    if host in _GOOGLE_HOSTS:
        return "google"
    return "direct"


def yandex_public_key(url: str) -> tuple[str, str | None, str | None]:
    """Извлечь публичный ключ, вложенный путь и ``resourcekey`` из ссылки."""
    parsed = urlsplit(url)
    if parsed.hostname not in _YANDEX_HOSTS:
        raise ExternalError("invalid_url")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2 or segments[0] not in ("d", "i"):
        raise ExternalError("invalid_url")
    key = segments[1]
    if not _YANDEX_KEY.fullmatch(key):
        raise ExternalError("invalid_url")
    path = "/" + "/".join(segments[2:]) if len(segments) > 2 else None
    query = dict(parse_qsl(parsed.query, keep_blank_values=False))
    resourcekey = query.get("resourcekey")
    public_key = f"https://{parsed.hostname}/{segments[0]}/{key}"
    return public_key, path, resourcekey


def _yandex_url(base: str, public_key: str, path: str | None, resourcekey: str | None) -> str:
    """Собрать URL API с urlencoded публичным ключом."""
    params = [("public_key", public_key)]
    if path:
        params.append(("path", path))
    if resourcekey:
        params.append(("resourcekey", resourcekey))
    return base + "?" + urlencode(params)


def google_file_id(url: str) -> str:
    """Строго извлечь идентификатор файла Google Drive из разрешённых форм."""
    parsed = urlsplit(url)
    if parsed.hostname not in _GOOGLE_HOSTS:
        raise ExternalError("invalid_url")
    query = dict(parse_qsl(parsed.query, keep_blank_values=False))
    candidate = query.get("id")
    if not candidate:
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) >= 3 and segments[0] in ("file", "document") and segments[1] == "d":
            candidate = segments[2]
    if not isinstance(candidate, str) or not _GOOGLE_ID.fullmatch(candidate):
        raise ExternalError("invalid_url")
    return candidate


def _google_download(url: str) -> tuple[str, str]:
    """Построить собственный URL скачивания по проверенному идентификатору."""
    file_id = google_file_id(url)
    fields = {"id": file_id, "export": "download"}
    resourcekey = dict(parse_qsl(urlsplit(url).query)).get("resourcekey")
    if resourcekey:
        fields["resourcekey"] = resourcekey
    return file_id, "https://" + _GOOGLE_CONFIRM_HOST + "/download?" + urlencode(fields)


class _GoogleFormParser(HTMLParser):
    """Собрать только формы подтверждения, без скриптов и переходов."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._current: dict | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = {name.lower(): value for name, value in attrs}
        if tag == "form":
            self._current = {"action": attributes.get("action") or "",
                             "method": (attributes.get("method") or "get").lower(),
                             "inputs": {}}
        elif tag == "input" and self._current is not None:
            name = attributes.get("name")
            if name:
                self._current["inputs"][name.lower()] = attributes.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def parse_google_confirmation(html: bytes | str, expected_id: str) -> str:
    """Разобрать доверенную форму подтверждения и собрать URL скачивания."""
    if not isinstance(expected_id, str) or not _GOOGLE_ID.fullmatch(expected_id):
        raise ExternalError("invalid_url")
    text = html.decode("utf-8", "replace") if isinstance(html, (bytes, bytearray)) else str(html)
    parser = _GoogleFormParser()
    try:
        parser.feed(text)
    except Exception:
        raise ExternalError("unsupported_input") from None
    for form in parser.forms:
        if form["method"] not in ("get", ""):
            continue
        action = form["action"]
        if action.startswith("/"):
            action = "https://" + _GOOGLE_CONFIRM_HOST + action
        parsed = urlsplit(action)
        if (parsed.scheme != "https" or parsed.hostname != _GOOGLE_CONFIRM_HOST
                or parsed.path != _GOOGLE_CONFIRM_PATH):
            continue
        fields = {name: value for name, value in form["inputs"].items() if name in _GOOGLE_FORM_FIELDS}
        if fields.get("id") != expected_id:
            continue
        fields.setdefault("export", "download")
        fields.setdefault("confirm", "t")
        query = urlencode([(name, fields[name]) for name in _GOOGLE_FORM_FIELDS if name in fields])
        return "https://" + _GOOGLE_CONFIRM_HOST + _GOOGLE_CONFIRM_PATH + "?" + query
    raise ExternalError("unsupported_input")


def _signin_redirect(response) -> bool:
    """Понять по итоговому адресу, что Google увёл запрос на страницу входа."""
    url = getattr(response, "url", None)
    if url is None:
        return False
    try:
        return urlsplit(str(url)).hostname in _GOOGLE_ACCOUNTS_HOSTS
    except ValueError:
        return False


async def _read_confirmation(transport, response) -> bytes:
    """Прочитать страницу подтверждения, не путая её размер с размером входа.

    Предел ``_HTML_LIMIT`` относится к служебной странице, а не к файлу: его
    превышение означает, что Google отдал страницу входа или защиты, а не файл.
    """
    try:
        return await transport.read_small(response, _HTML_LIMIT)
    except Exception as exc:
        if getattr(exc, "code", None) == "input_too_large":
            raise ExternalError("link_requires_signin") from None
        raise


def safe_basename(value) -> str:
    """Оставить только безопасное базовое имя без путей и управляющих знаков."""
    if not isinstance(value, str):
        return ""
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char >= " " and char != "\x7f")
    name = name.strip()
    while name.startswith("."):
        name = name[1:]
    name = name.strip()
    if not name or name in (".", ".."):
        return ""
    suffix = PurePath(name).suffix
    if len(name) > _MAX_NAME:
        keep = max(1, _MAX_NAME - len(suffix))
        name = name[:keep] + suffix
    return name


def content_disposition_filename(value) -> str:
    """Извлечь имя из Content-Disposition, поддерживая RFC 5987 ``filename*``."""
    if not isinstance(value, str) or not value or len(value) > 8192:
        return ""
    match = re.search(r"filename\*\s*=\s*([A-Za-z0-9!#$&^_.+\-]+)'([^']*)'([^;\s]+)", value, re.I)
    if match:
        charset = match.group(1) or "utf-8"
        try:
            return unquote_to_bytes(match.group(3)).decode(charset, "replace")
        except (LookupError, ValueError):
            return ""
    match = re.search(r'filename\s*=\s*"([^"]*)"', value, re.I)
    if match:
        return match.group(1)
    match = re.search(r"filename\s*=\s*([^;\s]+)", value, re.I)
    if match:
        return match.group(1)
    return ""


def looks_like_html(prefix: bytes) -> bool:
    """Распознать начало HTML-документа по префиксу байт."""
    return bool(_HTML_MARKER.match(prefix[:_SNIFF]))


def _extension_for(name: str, content_type: str, media: set[str], material: set[str]) -> str:
    """Выбрать разрешённое расширение из имени или типа содержимого."""
    allowed = media | material
    suffix = PurePath(name).suffix.lower() if name else ""
    if suffix in allowed:
        return suffix
    mapped = _CONTENT_TYPES.get(content_type)
    if mapped in allowed:
        return mapped
    raise ExternalError("unsupported_input")


def _pick_role(extension: str, media: set[str], material: set[str]) -> str:
    """Выбрать роль; при пересечении списков материал имеет приоритет."""
    if extension in material:
        return "material"
    if extension in media:
        return "media"
    raise ExternalError("unsupported_input")


def _finalize_name(name: str, extension: str) -> str:
    """Привести имя к выбранному расширению, заменяя чужой суффикс."""
    suffix = PurePath(name).suffix
    if suffix.lower() == extension:
        return name
    stem = name[: -len(suffix)] if suffix else name
    stem = stem.rstrip(".").strip() or "file"
    return stem + extension


def _header(response, name: str) -> str:
    """Прочитать строковый заголовок ответа безопасно."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    value = headers.get(name)
    return value if isinstance(value, str) else ""


def _tree_bytes(root: Path) -> int:
    """Считать реальные файлы, не следуя ссылкам из недоверенного дерева."""
    total, pending = 0, [root]
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                else:
                    raise ExternalError("unsafe_file_path")
    return total


def _check_free(root: Path, min_free_bytes: int) -> None:
    """Не начинать и не продолжать запись при нехватке свободного места."""
    usage = os.statvfs(root)
    if usage.f_bavail * usage.f_frsize < min_free_bytes:
        raise ExternalError("storage_unavailable")


async def _read_json(client, transport, url: str) -> dict:
    """Прочитать небольшой JSON-ответ API, не доверяя его размеру."""
    async with client.open(url) as response:
        body = await transport.read_small(response, _JSON_LIMIT)
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        raise ExternalError("download_failed") from None
    if not isinstance(value, dict):
        raise ExternalError("download_failed")
    return value


async def _yandex_metadata(client, transport, public_key: str, path: str | None,
                           resourcekey: str | None) -> tuple[str, int]:
    """Прочитать метаданные публичного файла и отказать папке или приватному."""
    data = await _read_json(client, transport,
                            _yandex_url(_YANDEX_API, public_key, path, resourcekey))
    if data.get("type") != "file" or data.get("error"):
        raise ExternalError("unsupported_input")
    name = data.get("name")
    size = data.get("size")
    if not isinstance(name, str) or not name:
        raise ExternalError("unsupported_input")
    if type(size) is not int or size < 0:
        raise ExternalError("invalid_file_metadata")
    return name, size


async def _yandex_href(client, transport, public_key: str, path: str | None,
                       resourcekey: str | None) -> str:
    """Получить подписанную ссылку скачивания; саму ссылку наружу не выдавать."""
    data = await _read_json(client, transport,
                            _yandex_url(_YANDEX_API + "/download", public_key, path, resourcekey))
    href = data.get("href")
    if not isinstance(href, str) or not href:
        raise ExternalError("unsupported_input")
    parsed = urlsplit(href)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ExternalError("unsafe_url")
    return href


async def _write_stream(response, path: Path, limit: int, *, root: Path,
                        max_work_bytes: int, min_free_bytes: int) -> int:
    """Записать поток на диск с проверкой предела, места и HTML-подмены."""
    total = 0
    prefix = b""
    existing = _tree_bytes(root)
    _check_free(root, min_free_bytes)
    length = response.content_length
    declared = length if type(length) is int and length >= 0 else None
    reporter = _progress_module().ProgressWriter(root, "external", "bytes", min_interval=0.5)
    if length is not None and length > limit:
        raise ExternalError("input_too_large")
    if existing > max_work_bytes:
        raise ExternalError("work_limit")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb", buffering=0) as stream:
        async for chunk in response.content.iter_chunked(_CHUNK):
            if not isinstance(chunk, (bytes, bytearray)):
                raise ExternalError("download_failed")
            prefix += bytes(chunk[:max(0, _SNIFF - len(prefix))])
            if looks_like_html(prefix):
                raise ExternalError("unsupported_input")
            total += len(chunk)
            if total > limit:
                raise ExternalError("input_too_large")
            if existing + total > max_work_bytes:
                raise ExternalError("work_limit")
            usage = os.statvfs(root)
            if usage.f_bavail * usage.f_frsize - len(chunk) < min_free_bytes:
                raise ExternalError("storage_unavailable")
            stream.write(chunk)
            reporter.update(total, declared)
    _check_free(root, min_free_bytes)
    if total <= 0:
        raise ExternalError("download_failed")
    if looks_like_html(prefix):
        raise ExternalError("unsupported_input")
    return total


async def _run(url: str, tmp: Path, root: Path, media: set[str], material: set[str],
               max_media: int, max_material: int, remaining: int, max_work: int,
               min_free: int, transport, timeout: float, download_id: str,
               proxy: str | None = None) -> dict:
    """Разрешить провайдера, определить роль и скачать ровно один файл."""
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(
            transport.SecureClient(timeout=timeout, proxy=proxy))
        provider = _provider_of(url)
        google_id = ""
        declared: int | None = None
        name_hint = ""
        if provider == "yandex":
            public_key, path, resourcekey = yandex_public_key(url)
            name_hint, declared = await _yandex_metadata(client, transport, public_key, path, resourcekey)
            target = await _yandex_href(client, transport, public_key, path, resourcekey)
        elif provider == "google":
            google_id, target = _google_download(url)
        else:
            target = url
        response = await stack.enter_async_context(client.open(target))
        content_type = _header(response, "Content-Type").split(";")[0].strip().lower()
        if google_id and _signin_redirect(response):
            raise ExternalError("link_requires_signin")
        if google_id and content_type == "text/html":
            body = await _read_confirmation(transport, response)
            confirm_url = parse_google_confirmation(body, google_id)
            resourcekey = dict(parse_qsl(urlsplit(target).query)).get("resourcekey")
            if resourcekey:
                confirm_url += "&" + urlencode({"resourcekey": resourcekey})
            response = await stack.enter_async_context(client.open(confirm_url))
            content_type = _header(response, "Content-Type").split(";")[0].strip().lower()
            if _signin_redirect(response):
                raise ExternalError("link_requires_signin")
            if content_type == "text/html":
                raise ExternalError("unsupported_input")
        if content_type in ("text/html", "application/xhtml+xml"):
            raise ExternalError("unsupported_input")
        name = name_hint or content_disposition_filename(_header(response, "Content-Disposition"))
        if not name:
            name = safe_basename(urlsplit(target).path)
        name = safe_basename(name)
        extension = _extension_for(name, content_type, media, material)
        role = _pick_role(extension, media, material)
        limit = max_media if role == "media" else min(max_material, remaining)
        if declared is not None and declared > limit:
            raise ExternalError("input_too_large")
        original_name = _finalize_name(name, extension)
        # На диске файл лежит под UUID прямо в tmp: имя известно только для показа.
        path = tmp / (download_id + extension)
        try:
            written = await _write_stream(response, path, limit, root=root,
                                          max_work_bytes=max_work, min_free_bytes=min_free)
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return {"ok": True, "relative_path": str(path.relative_to(root)), "extension": extension,
                "role": role, "original_name": original_name, "bytes": written}


def _optional_proxy(value: object) -> str | None:
    """Прочитать необязательный адрес прокси: строка вида http://host:port."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ExternalError("invalid_url")
    return value


def _int_field(request: dict, key: str) -> int:
    """Прочитать неотрицательное целое поле запроса."""
    value = request.get(key)
    if type(value) is not int or value < 0:
        raise ExternalError("invalid_file_metadata")
    return value


def _load_transport():
    """Загрузить транспорт лениво: тесты и sandbox подменяют ``secure_http``."""
    try:
        from . import secure_http as module
    except ImportError:
        try:
            from .. import secure_http as module
        except ImportError:
            import secure_http as module  # type: ignore[no-redef]
    return module


def _progress_module():
    """Загрузить модуль прогресса и в пакете, и при запуске как скрипта."""
    try:
        from . import progress as module
    except ImportError:
        import progress as module  # type: ignore[no-redef]
    return module


async def fetch(request: dict, *, root: Path | None = None, transport=None) -> dict:
    """Скачать файл по описанию запроса и вернуть безопасный результат."""
    if not isinstance(request, dict):
        raise ExternalError("invalid_file_metadata")
    url = normalize_external_url(request.get("url"))
    media = _normalize_extensions(request.get("media_extensions"))
    material = _normalize_extensions(request.get("material_extensions"))
    if not media and not material:
        raise ExternalError("invalid_file_metadata")
    max_media = _int_field(request, "max_media_bytes")
    max_material = _int_field(request, "max_material_bytes")
    remaining = _int_field(request, "remaining_material_bytes")
    max_work = _int_field(request, "max_work_bytes")
    min_free = _int_field(request, "min_free_bytes")
    download_id = request.get("download_id", uuid4().hex)
    if not isinstance(download_id, str) or not re.fullmatch(r"[0-9a-f]{32}", download_id):
        raise ExternalError("unsafe_file_path")
    timeout = request.get("timeout", _DEFAULT_TIMEOUT)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ExternalError("invalid_file_metadata")
    try:
        base = (root or Path.cwd()).resolve(strict=True)
    except (OSError, ValueError):
        raise ExternalError("storage_unavailable") from None
    tmp = base / "tmp"
    try:
        tmp.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        raise ExternalError("storage_unavailable") from None
    if tmp.is_symlink() or not tmp.is_dir():
        raise ExternalError("unsafe_file_path")
    module = transport or _load_transport()
    transport_error = getattr(module, "FetchError", None)
    discard = _progress_module().discard_progress
    proxy = _optional_proxy(request.get("proxy"))
    try:
        return await asyncio.wait_for(
            _run(url, tmp, base, media, material, max_media, max_material,
                 remaining, max_work, min_free, module, float(timeout), download_id, proxy), timeout)
    except asyncio.TimeoutError:
        discard(base, "external")
        raise ExternalError("timeout") from None
    except ExternalError:
        discard(base, "external")
        raise
    except Exception as exc:
        discard(base, "external")
        if transport_error is not None and isinstance(exc, transport_error):
            code = getattr(exc, "code", None)
            raise ExternalError(code if isinstance(code, str) and code in _SAFE_CODES
                                else "download_failed") from None
        raise


async def download(request: dict, *, root: Path | None = None, transport=None) -> dict:
    """Скачать файл, пробуя настроенный прокси и запасное прямое подключение.

    Часть площадок доступна только из одной сети: прокси помогает одним,
    а прямое соединение — другим, поэтому при сетевом отказе маршрут меняется
    один раз. Ошибки проверок запроса не повторяются: они не зависят от
    маршрута, а повторный отказ остаётся отказом второго маршрута.
    """
    proxy = request.get("proxy")
    if not proxy:
        return await fetch(request, root=root, transport=transport)
    try:
        return await fetch(request, root=root, transport=transport)
    except ExternalError as exc:
        if str(exc) not in _ROUTE_FAILURES:
            raise
        return await fetch({**request, "proxy": None}, root=root, transport=transport)


def _error_result(exc: Exception) -> dict:
    """Превратить исключение в безопасный код без деталей окружения."""
    if isinstance(exc, ExternalError):
        return {"ok": False, "error": str(exc)}
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _SAFE_CODES:
        return {"ok": False, "error": code}
    return {"ok": False, "error": "download_failed"}


def _read_request(args: list[str]) -> str:
    """Прочитать запрос из переданного файла, иначе из stdin."""
    if args:
        try:
            with open(args[0], "r", encoding="utf-8") as handle:
                return handle.read(_REQUEST_LIMIT)
        except OSError:
            raise ExternalError("invalid_file_metadata") from None
    return sys.stdin.read(_REQUEST_LIMIT)


def main(argv: list[str] | None = None) -> int:
    """Прочитать запрос и напечатать безопасный JSON-ответ."""
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        request = json.loads(_read_request(args))
        result = asyncio.run(download(request))
    except json.JSONDecodeError:
        result = {"ok": False, "error": "invalid_file_metadata"}
    except Exception as exc:
        result = _error_result(exc)
    print(json.dumps(result, allow_nan=False))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())

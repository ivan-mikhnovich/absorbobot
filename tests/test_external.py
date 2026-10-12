"""Границы разбора, ролей, пределов и загрузки внешних ссылок без реальной сети."""

from __future__ import annotations

import asyncio
import io
import json
from urllib.parse import urlencode

import pytest

from absorbobot.workers import external
from absorbobot.workers.external import (
    ExternalError,
    content_disposition_filename,
    fetch,
    google_file_id,
    looks_like_html,
    normalize_external_url,
    parse_google_confirmation,
    safe_basename,
    yandex_public_key,
)


CANONICAL_DIRECT = "https://files.example.org/media/lecture.pdf"


class FakeFetchError(ValueError):
    """Безопасный отказ транспорта с кодом, как у настоящего ``FetchError``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Headers:
    """Регистронезависимый доступ к заголовкам, как у aiohttp."""

    def __init__(self, values: dict):
        self._values = {name.lower(): value for name, value in values.items()}

    def get(self, name: str, default=None):
        return self._values.get(name.lower(), default)


class _Content:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            yield chunk


class _Response:
    def __init__(self, chunks: list[bytes], headers: dict | None = None, status: int = 200):
        self.headers = _Headers(headers or {})
        self.content = _Content(chunks)
        self.status = status
        self.content_length = sum(len(chunk) for chunk in chunks)

    def close(self) -> None:
        return None


class _Open:
    def __init__(self, response: _Response):
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *exc) -> None:
        return None


class FakeTransport:
    """Подмена secure_http: раздаёт заранее заданные ответы по URL."""

    FetchError = FakeFetchError

    def __init__(self, routes: dict):
        self.routes = {url: list(responses) for url, responses in routes.items()}
        self.calls: list[str] = []

    def SecureClient(self, timeout=None):
        self.timeout = timeout
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def open(self, url: str):
        self.calls.append(url)
        pending = self.routes.get(url)
        if not pending:
            raise FakeFetchError("download_failed")
        return _Open(pending.pop(0))

    async def read_small(self, response: _Response, limit: int) -> bytes:
        body = b"".join(response.content._chunks)
        if len(body) > limit:
            raise FakeFetchError("input_too_large")
        return body


def request(**fields) -> dict:
    base = {
        "url": CANONICAL_DIRECT,
        "media_extensions": [".mp4", ".mkv"],
        "material_extensions": [".pdf", ".docx"],
        "max_media_bytes": 1000,
        "max_material_bytes": 1000,
        "remaining_material_bytes": 1000,
        "max_work_bytes": 10_000_000,
        "min_free_bytes": 0,
    }
    base.update(fields)
    return base


def run(request_value: dict, routes: dict, tmp_path, **kwargs):
    transport = FakeTransport(routes)
    return asyncio.run(fetch(request_value, root=tmp_path, transport=transport, **kwargs)), transport


@pytest.mark.parametrize("value", [
    "https://files.example.org/a.pdf",
    "http://files.example.org/a.pdf",
    "https://files.example.org:443/a.pdf",
    "https://disk.yandex.ru/d/abcdef",
    "https://drive.google.com/file/d/abcdefghijklmnop/view",
])
def test_normalize_accepts_http_https_without_userinfo(value):
    assert normalize_external_url(value) == value


@pytest.mark.parametrize("value", [
    None,
    42,
    "",
    "ftp://files.example.org/a.pdf",
    "file:///etc/passwd",
    "https://user@files.example.org/a.pdf",
    "https://user:pass@files.example.org/a.pdf",
    "https://files.example.org/a b.pdf",
    "https://files.example.org/a\nb.pdf",
    "https://files.example.org:99999/a.pdf",
    "https:///a.pdf",
    "https://",
])
def test_normalize_rejects_bad_syntax(value):
    with pytest.raises(ExternalError) as error:
        normalize_external_url(value)
    assert str(error.value) == "invalid_url"


@pytest.mark.parametrize("value,expected", [
    ("https://disk.yandex.ru/d/Abc_123", ("https://disk.yandex.ru/d/Abc_123", None, None)),
    ("https://yadi.sk/d/Abc_123", ("https://yadi.sk/d/Abc_123", None, None)),
    ("https://disk.yandex.com/i/Abc_123", ("https://disk.yandex.com/i/Abc_123", None, None)),
    ("https://disk.yandex.ru/d/Abc_123/folder/file.pdf",
     ("https://disk.yandex.ru/d/Abc_123", "/folder/file.pdf", None)),
    ("https://disk.yandex.ru/d/Abc_123?resourcekey=KEY",
     ("https://disk.yandex.ru/d/Abc_123", None, "KEY")),
])
def test_yandex_public_key_forms(value, expected):
    assert yandex_public_key(value) == expected


@pytest.mark.parametrize("value", [
    "https://disk.yandex.ru/",
    "https://disk.yandex.ru/d/",
    "https://disk.yandex.ru/x/Abc_123",
    "https://evil.example/d/Abc_123",
])
def test_yandex_public_key_rejects_unknown_shapes(value):
    with pytest.raises(ExternalError) as error:
        yandex_public_key(value)
    assert str(error.value) == "invalid_url"


@pytest.mark.parametrize("value,file_id", [
    ("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWx/view", "1AbCdEfGhIjKlMnOpQrStUvWx"),
    ("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUvWx", "1AbCdEfGhIjKlMnOpQrStUvWx"),
    ("https://drive.google.com/uc?id=1AbCdEfGhIjKlMnOpQrStUvWx&export=download", "1AbCdEfGhIjKlMnOpQrStUvWx"),
    ("https://drive.usercontent.google.com/download?id=1AbCdEfGhIjKlMnOpQrStUvWx&confirm=t", "1AbCdEfGhIjKlMnOpQrStUvWx"),
])
def test_google_file_id_forms(value, file_id):
    assert google_file_id(value) == file_id


@pytest.mark.parametrize("value", [
    "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWx",
    "https://drive.google.com/file/d/short/view",
    "https://evil.example/file/d/1AbCdEfGhIjKlMnOpQrStUvWx/view",
])
def test_google_file_id_rejects_folders_and_fake_hosts(value):
    with pytest.raises(ExternalError) as error:
        google_file_id(value)
    assert str(error.value) == "invalid_url"


@pytest.mark.parametrize("value,expected", [
    ("..%2F..%2Fetc%2Fpasswd", "%2F..%2Fetc%2Fpasswd"),
    ("/etc/passwd", "passwd"),
    ("C:\\\\Windows\\\\evil.pdf", "evil.pdf"),
    ("..", ""),
    (".hidden.pdf", "hidden.pdf"),
    ("a\x00b.pdf", "ab.pdf"),
    (None, ""),
    ("", ""),
])
def test_safe_basename_strips_paths_and_controls(value, expected):
    assert safe_basename(value) == expected


def test_safe_basename_bounds_length_keeping_extension():
    result = safe_basename("x" * 500 + ".pdf")
    assert result.endswith(".pdf")
    assert len(result) <= 160


@pytest.mark.parametrize("value,expected", [
    ('attachment; filename="report.pdf"', "report.pdf"),
    ("attachment; filename=report.docx", "report.docx"),
    ("attachment; filename*=UTF-8''%D0%BB%D0%B5%D0%BA.pdf", "лек.pdf"),
    ("attachment", ""),
    ('attachment; filename="a;b.pdf"', "a;b.pdf"),
])
def test_content_disposition_filename(value, expected):
    assert content_disposition_filename(value) == expected


@pytest.mark.parametrize("prefix,expected", [
    (b"<!DOCTYPE html><html>", True),
    (b"  <html lang='ru'>", True),
    (b"\xef\xbb\xbf<html>", True),
    (b"%PDF-1.7\n", False),
    (b"<svg xmlns='...'>", False),
    (b"", False),
])
def test_looks_like_html_sniffs_start(prefix, expected):
    assert looks_like_html(prefix) is expected


def test_fetch_direct_file_writes_under_tmp(tmp_path):
    routes = {CANONICAL_DIRECT: [_Response([b"%PDF-1.7 content"], {"Content-Type": "application/pdf"})]}
    result, transport = run(request(), routes, tmp_path)
    assert result["ok"] is True
    assert result["role"] == "material"
    assert result["extension"] == ".pdf"
    assert result["original_name"] == "lecture.pdf"
    assert result["bytes"] == 16
    assert result["relative_path"].startswith("tmp/")
    assert result["relative_path"].count("/") == 1
    written = tmp_path / result["relative_path"]
    assert written.is_file() and written.read_bytes() == b"%PDF-1.7 content"
    assert transport.calls == [CANONICAL_DIRECT]


def test_fetch_direct_uses_content_disposition_name(tmp_path):
    routes = {CANONICAL_DIRECT: [_Response([b"data"], {
        "Content-Type": "video/mp4",
        "Content-Disposition": 'attachment; filename="clip.mp4"',
    })]}
    result, _ = run(request(), routes, tmp_path)
    assert result["role"] == "media"
    assert result["extension"] == ".mp4"
    assert result["original_name"] == "clip.mp4"


def test_fetch_rejects_extension_outside_allowlist(tmp_path):
    url = "https://files.example.org/archive.bin"
    routes = {url: [_Response([b"data"], {"Content-Type": "application/octet-stream"})]}
    with pytest.raises(ExternalError) as error:
        run(request(url=url), routes, tmp_path)
    assert str(error.value) == "unsupported_input"


def test_material_overlap_prefers_material(tmp_path):
    value = request(media_extensions=[".pdf"], material_extensions=[".pdf"])
    routes = {CANONICAL_DIRECT: [_Response([b"data"], {"Content-Type": "application/pdf"})]}
    result, _ = run(value, routes, tmp_path)
    assert result["role"] == "material"


def test_fetch_stops_stream_over_media_cap(tmp_path):
    value = request(url="https://files.example.org/clip.mp4", max_media_bytes=100)
    routes = {"https://files.example.org/clip.mp4": [
        _Response([b"x" * 64, b"y" * 64], {"Content-Type": "video/mp4"})]}
    with pytest.raises(ExternalError) as error:
        run(value, routes, tmp_path)
    assert str(error.value) == "input_too_large"


def test_fetch_material_uses_smaller_remaining_budget(tmp_path):
    value = request(remaining_material_bytes=10)
    routes = {CANONICAL_DIRECT: [_Response([b"x" * 32], {"Content-Type": "application/pdf"})]}
    with pytest.raises(ExternalError) as error:
        run(value, routes, tmp_path)
    assert str(error.value) == "input_too_large"


def test_fetch_rejects_html_page_masquerading_as_pdf(tmp_path):
    routes = {CANONICAL_DIRECT: [_Response([b"<!DOCTYPE html><html>login</html>"],
                                           {"Content-Type": "application/pdf"})]}
    with pytest.raises(ExternalError) as error:
        run(request(), routes, tmp_path)
    assert str(error.value) == "unsupported_input"


def test_fetch_cleans_partial_tree_on_failure(tmp_path):
    value = request(url="https://files.example.org/clip.mp4", max_media_bytes=8)
    routes = {"https://files.example.org/clip.mp4": [
        _Response([b"x" * 64], {"Content-Type": "video/mp4"})]}
    with pytest.raises(ExternalError):
        run(value, routes, tmp_path)
    assert list((tmp_path / "tmp").iterdir()) == []


def test_fetch_maps_transport_error_without_leaking_url(tmp_path):
    secret = "https://cdn.example.org/secret?token=SUPERSECRET"
    with pytest.raises(ExternalError) as error:
        run(request(url="https://files.example.org/a.mp4"), {}, tmp_path)
    assert str(error.value) == "download_failed"
    # Неизвестный URL транспорта поднимает FakeFetchError с тем же безопасным кодом.
    with pytest.raises(ExternalError) as outside:
        run(request(url=secret), {}, tmp_path)
    assert "SUPERSECRET" not in str(outside.value)


def test_fetch_yandex_downloads_signed_href(tmp_path):
    api_meta = ("https://cloud-api.yandex.net/v1/disk/public/resources?"
                + urlencode({"public_key": "https://disk.yandex.ru/d/Abc_123"}))
    api_link = ("https://cloud-api.yandex.net/v1/disk/public/resources/download?"
                + urlencode({"public_key": "https://disk.yandex.ru/d/Abc_123"}))
    href = "https://downloader.disk.yandex.ru/secret?token=" + "s" * 40
    routes = {
        api_meta: [_Response([json.dumps({"type": "file", "name": "лекция.pdf", "size": 20}).encode()])],
        api_link: [_Response([json.dumps({"href": href}).encode()])],
        href: [_Response([b"%PDF-1.7 signed"], {"Content-Type": "application/pdf"})],
    }
    result, transport = run(request(url="https://disk.yandex.ru/d/Abc_123"), routes, tmp_path)
    assert result["ok"] is True
    assert result["role"] == "material"
    assert result["original_name"] == "лекция.pdf"
    assert href in transport.calls


def test_fetch_yandex_rejects_directory(tmp_path):
    api_meta = ("https://cloud-api.yandex.net/v1/disk/public/resources?"
                + urlencode({"public_key": "https://disk.yandex.ru/d/Dir_1"}))
    routes = {api_meta: [_Response([json.dumps({"type": "dir", "name": "folder"}).encode()])]}
    with pytest.raises(ExternalError) as error:
        run(request(url="https://disk.yandex.ru/d/Dir_1"), routes, tmp_path)
    assert str(error.value) == "unsupported_input"


def test_fetch_google_follows_confirmation_form(tmp_path):
    file_id = "1AbCdEfGhIjKlMnOpQrStUvWx"
    first = "https://drive.usercontent.google.com/download?" + urlencode({"id": file_id, "export": "download"})
    confirm = ("https://drive.usercontent.google.com/download?"
               + urlencode({"id": file_id, "export": "download", "confirm": "t", "uuid": "u-1"}))
    html = (f'<html><body><form action="https://drive.usercontent.google.com/download" method="get">'
            f'<input type="hidden" name="id" value="{file_id}">'
            f'<input type="hidden" name="export" value="download">'
            f'<input type="hidden" name="confirm" value="t">'
            f'<input type="hidden" name="uuid" value="u-1"></form></body></html>')
    routes = {
        first: [_Response([html.encode()], {"Content-Type": "text/html; charset=utf-8"})],
        confirm: [_Response([b"%PDF-1.7 drive"], {"Content-Type": "application/pdf",
                                                  "Content-Disposition": 'attachment; filename="doc.pdf"'})],
    }
    result, transport = run(request(url=f"https://drive.google.com/file/d/{file_id}/view"), routes, tmp_path)
    assert result["ok"] is True
    assert result["original_name"] == "doc.pdf"
    assert transport.calls == [first, confirm]


def test_fetch_google_rejects_untrusted_confirmation_action(tmp_path):
    file_id = "1AbCdEfGhIjKlMnOpQrStUvWx"
    first = "https://drive.usercontent.google.com/download?" + urlencode({"id": file_id, "export": "download"})
    html = (f'<html><form action="https://evil.example/steal" method="get">'
            f'<input type="hidden" name="id" value="{file_id}"></form></html>')
    routes = {first: [_Response([html.encode()], {"Content-Type": "text/html"})]}
    with pytest.raises(ExternalError) as error:
        run(request(url=f"https://drive.google.com/file/d/{file_id}/view"), routes, tmp_path)
    assert str(error.value) == "unsupported_input"


@pytest.mark.parametrize("html,expected_id,code", [
    ("<html><p>no form</p></html>", "1AbCdEfGhIjKlMnOpQrStUvWx", "unsupported_input"),
    ('<form action="https://drive.usercontent.google.com/download" method="get">'
     '<input name="id" value="WRONG_ID_ZZZZZZZZ"></form>', "1AbCdEfGhIjKlMnOpQrStUvWx", "unsupported_input"),
    ('<form action="https://drive.usercontent.google.com/download" method="post">'
     '<input name="id" value="1AbCdEfGhIjKlMnOpQrStUvWx"></form>', "1AbCdEfGhIjKlMnOpQrStUvWx", "unsupported_input"),
])
def test_parse_google_confirmation_rejects_untrusted(html, expected_id, code):
    with pytest.raises(ExternalError) as error:
        parse_google_confirmation(html, expected_id)
    assert str(error.value) == code


def test_fetch_rejects_missing_extension_lists(tmp_path):
    value = request(media_extensions=[], material_extensions=[])
    with pytest.raises(ExternalError) as error:
        run(value, {}, tmp_path)
    assert str(error.value) == "invalid_file_metadata"


def test_fetch_rejects_non_integer_budget(tmp_path):
    value = request(max_media_bytes=True)
    with pytest.raises(ExternalError) as error:
        run(value, {}, tmp_path)
    assert str(error.value) == "invalid_file_metadata"


def test_main_prints_error_for_invalid_json(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert external.main([]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "error": "invalid_file_metadata"}




def test_stream_work_limit_applies_to_small_chunked_file_and_existing_inputs(tmp_path):
    (tmp_path / "existing.bin").write_bytes(b"x" * 20)
    response = _Response([b"%PDF-", b"1234567890"], {"Content-Type": "application/pdf"})
    response.content_length = None
    with pytest.raises(ExternalError) as error:
        run(request(max_work_bytes=30), {CANONICAL_DIRECT: [response]}, tmp_path)
    assert error.value.code == "work_limit"
    assert list((tmp_path / "tmp").iterdir()) == []
    assert (tmp_path / "existing.bin").read_bytes() == b"x" * 20


def test_content_disposition_respects_charset_not_language():
    assert content_disposition_filename("attachment; filename*=ISO-8859-1'en'%E9tude.pdf") == "étude.pdf"


def test_html_sniff_spanning_small_chunks_is_rejected(tmp_path):
    response = _Response([b"<ht", b"ml>login</html>"], {"Content-Type": "application/pdf"})
    with pytest.raises(ExternalError) as error:
        run(request(), {CANONICAL_DIRECT: [response]}, tmp_path)
    assert error.value.code == "unsupported_input"
    assert list((tmp_path / "tmp").iterdir()) == []

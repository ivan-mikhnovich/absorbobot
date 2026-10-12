"""Инварианты безопасного HTTP-транспорта: SSRF, редиректы, DNS, пределы чтения."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import socket
import time

from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
import pytest

from absorbobot.workers.secure_http import (
    FetchError,
    PinnedResolver,
    SecureClient,
    _ip_is_public,
    _validate_url,
    read_small,
)


# --- политика URL и адресов ---------------------------------------------------------

@pytest.mark.parametrize("value", [
    "http://a.example/",
    "http://a.example:80/",
    "https://a.example:443/path?x=1",
    "http://[::ffff:8.8.8.8]/",  # публичный IPv4 внутри mapped-формы допустим.
])
def test_validate_url_accepts_only_http_https_default_ports(value):
    assert _validate_url(value) == value


@pytest.mark.parametrize("value,code", [
    ("ftp://a.example/", "invalid_url"),
    ("file:///etc/passwd", "invalid_url"),
    ("http://user:pw@a.example/", "invalid_url"),
    ("http://a.example:8080/", "invalid_url"),
    ("http://a.example:443/", "invalid_url"),
    ("https://a.example:80/", "invalid_url"),
    ("http://a.example/ ", "invalid_url"),
    ("http://a.example/\\x", "invalid_url"),
    ("http://a.example/\x01", "invalid_url"),
    ("http://a.example\n/", "invalid_url"),
    ("http:///nohost", "invalid_url"),
    # Legacy-формы IPv4: getaddrinfo разбирает их как адрес, но доменом они не являются.
    ("http://2130706433/", "invalid_url"),
    ("http://0x7f000001/", "invalid_url"),
    ("http://127.1/", "invalid_url"),
    ("http://" + "a" * 8200 + "/", "invalid_url"),
    ("http://127.0.0.1/", "unsafe_url"),
    ("http://169.254.169.254/latest/meta-data/", "unsafe_url"),
    ("http://10.0.0.1/", "unsafe_url"),
    ("http://224.0.0.1/", "unsafe_url"),
    ("http://[::1]/", "unsafe_url"),
    ("http://[::ffff:127.0.0.1]/", "unsafe_url"),
    ("http://[fe80::1]/", "unsafe_url"),
])
def test_validate_url_rejects_unsafe_and_malformed(value, code):
    with pytest.raises(FetchError) as error:
        _validate_url(value)
    assert error.value.code == code


@pytest.mark.parametrize("literal,expected", [
    ("8.8.8.8", True),
    ("1.1.1.1", True),
    ("127.0.0.1", False),
    ("10.0.0.1", False),
    ("192.168.0.1", False),
    ("172.16.5.5", False),
    ("169.254.169.254", False),
    ("0.0.0.0", False),
    ("224.0.0.1", False),
    ("100.64.0.1", False),
    ("198.18.0.1", False),
    ("2606:4700:4700::1111", True),
    ("2001:4860:4860::8888", True),
    ("::1", False),
    ("fe80::1", False),
    ("fc00::1", False),
    ("::ffff:127.0.0.1", False),
    ("::ffff:8.8.8.8", True),
    ("2002:0808:0808::", True),   # 6to4 с публичным 8.8.8.8.
    ("2002:7f00:0001::", False),  # 6to4 с петлёй 127.0.0.1.
    ("64:ff9b::808:808", True),   # NAT64 с публичным 8.8.8.8.
    ("64:ff9b::7f00:1", False),   # NAT64 с петлёй 127.0.0.1.
])
def test_ip_public_policy(literal, expected):
    assert _ip_is_public(ipaddress.ip_address(literal)) is expected


def _teredo(server: str, client: str) -> ipaddress.IPv6Address:
    """Собрать Teredo-адрес с заданными IPv4 сервера и клиента."""
    server_bits = int(ipaddress.IPv4Address(server))
    client_bits = int(ipaddress.IPv4Address(client)) ^ 0xFFFFFFFF
    groups = [0x2001, 0x0000, server_bits >> 16, server_bits & 0xFFFF,
              0, 0, client_bits >> 16, client_bits & 0xFFFF]
    return ipaddress.IPv6Address(":".join(f"{value:04x}" for value in groups))


def test_ip_public_policy_unwraps_teredo_both_ends():
    public = _teredo("8.8.8.8", "8.8.4.4")
    assert public.teredo == (ipaddress.IPv4Address("8.8.8.8"), ipaddress.IPv4Address("8.8.4.4"))
    assert _ip_is_public(public) is True
    assert _ip_is_public(_teredo("8.8.8.8", "192.0.2.45")) is False   # приватный клиент.
    assert _ip_is_public(_teredo("10.0.0.1", "8.8.4.4")) is False     # приватный сервер.


# --- резолвер, закрепляющий проверенные адреса --------------------------------------

def _infos(*literal: str) -> list:
    """Собрать ответ getaddrinfo из IP-литералов."""
    infos = []
    for value in literal:
        if ":" in value:
            infos.append((socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                          "", (value, 0, 0, 0)))
        else:
            infos.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                          "", (value, 0)))
    return infos


class _FakeGetaddr:
    """Подменить socket.getaddrinfo, который вызывает loop.getaddrinfo."""

    def __init__(self, answer):
        self._answer = answer

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer


def test_pinned_resolver_returns_only_verified_addresses(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _FakeGetaddr(_infos("8.8.8.8", "1.1.1.1")))

    async def scenario():
        return await PinnedResolver(timeout=1).resolve("host.test", 443)

    results = asyncio.run(scenario())
    assert {(entry["host"], entry["port"]) for entry in results} == {("8.8.8.8", 443), ("1.1.1.1", 443)}
    assert all(entry["hostname"] == "host.test" for entry in results)


@pytest.mark.parametrize("answer", [
    _infos("8.8.8.8", "10.0.0.1"),   # смешение публичного и приватного.
    _infos("192.168.1.1"),
    _infos("::ffff:127.0.0.1"),
    _infos("8.8.8.8", "::1"),
])
def test_pinned_resolver_rejects_any_private_answer(monkeypatch, answer):
    monkeypatch.setattr(socket, "getaddrinfo", _FakeGetaddr(answer))

    async def scenario():
        with pytest.raises(FetchError) as error:
            await PinnedResolver(timeout=1).resolve("host.test", 443)
        return error.value.code

    assert asyncio.run(scenario()) == "unsafe_url"


def test_pinned_resolver_maps_dns_failure_to_download_failed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _FakeGetaddr(socket.gaierror("nxdomain")))

    async def scenario():
        with pytest.raises(FetchError) as error:
            await PinnedResolver(timeout=1).resolve("host.test", 443)
        return error.value.code

    assert asyncio.run(scenario()) == "download_failed"


def test_pinned_resolver_bounds_own_lookup(monkeypatch):
    def slow(host, port, family=0, type=0, proto=0, flags=0):
        time.sleep(0.3)
        return _infos("8.8.8.8")

    monkeypatch.setattr(socket, "getaddrinfo", slow)

    async def scenario():
        with pytest.raises(FetchError) as error:
            await PinnedResolver(timeout=0.05).resolve("host.test", 443)
        return error.value.code

    assert asyncio.run(scenario()) == "timeout"


# --- сквозная проверка транспорта на локальном сервере ------------------------------

class _LoopbackResolver(AbstractResolver):
    """Тестовый резолвер: любое имя указывает на локальный сервер."""

    def __init__(self, port: int):
        self._port = port

    async def resolve(self, host: str, port: int = 0,
                      family: socket.AddressFamily = socket.AF_UNSPEC) -> list[ResolveResult]:
        return [ResolveResult(hostname=host, host="127.0.0.1", port=self._port,
                              family=socket.AF_INET, proto=socket.IPPROTO_TCP, flags=0)]

    async def close(self) -> None:
        pass


async def _chunked(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse()
    response.enable_chunked_encoding()
    await response.prepare(request)
    for _ in range(10):
        await response.write(b"x" * 100)
    await response.write_eof()
    return response


async def _slow(request: web.Request) -> web.Response:
    await asyncio.sleep(1.0)
    return web.Response(body=b"late")


def _reply(body: bytes = b"", status: int = 200, location: str | None = None,
           headers: dict | None = None):
    """Собрать асинхронный обработчик с фиксированным ответом."""
    async def handler(request: web.Request) -> web.Response:
        merged = dict(headers or {})
        if location is not None:
            merged["Location"] = location
        return web.Response(body=body, status=status, headers=merged or None)
    return handler


def _chain(prefix: str, hops: int, target: str = "/ok"):
    """Маршруты цепочки редиректов: prefix0 -> ... -> target."""
    def make(index: int):
        async def handler(request: web.Request) -> web.Response:
            following = f"{prefix}{index + 1}" if index + 1 < hops else target
            return web.Response(status=302, headers={"Location": following})
        return handler
    return [(f"{prefix}{index}", make(index)) for index in range(hops)]


def _build_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/ok", _reply(b"hello")),
        web.get("/empty", _reply(b"")),
        web.get("/redir", _reply(b"redirect body", status=302, location="/ok")),
        web.get("/err", _reply(b"boom", status=500)),
        web.get("/gz", _reply(b"\x1f\x8b", headers={"Content-Encoding": "gzip"})),
        web.get("/unsafe", _reply(status=302, location="http://169.254.169.254/")),
        web.get("/scheme", _reply(status=302, location="file:///etc/passwd")),
        web.get("/loop", _reply(status=302, location="/loop")),
        web.get("/chunk", _chunked),
        web.get("/bigcl", _reply(b"y" * 1000)),
        web.get("/slow", _slow),
    ])
    app.add_routes([web.get(path, handler) for path, handler in _chain("/deep", 5)])
    app.add_routes([web.get(path, handler) for path, handler in _chain("/toofar", 6)])
    return app


@asynccontextmanager
async def _server():
    runner = web.AppRunner(_build_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield runner.addresses[0][1]
    finally:
        await runner.cleanup()


def _client(port: int, timeout: float = 5):
    return SecureClient(timeout=timeout, resolver=_LoopbackResolver(port))


def test_open_reads_body_follows_redirects_and_accepts_empty():
    async def scenario():
        async with _server() as port:
            async with _client(port) as client:
                async with client.open("http://cdn.test/ok") as response:
                    body = await read_small(response, 100)
                async with client.open("http://cdn.test/redir") as response:
                    redirected = await read_small(response, 100)
                async with client.open("http://cdn.test/empty") as response:
                    empty = await read_small(response, 100)
        return body, redirected, empty

    assert asyncio.run(scenario()) == (b"hello", b"hello", b"")


def test_open_allows_five_redirects_and_rejects_the_sixth():
    async def scenario():
        codes = []
        async with _server() as port:
            async with _client(port) as client:
                async with client.open("http://cdn.test/deep0") as response:
                    codes.append(await read_small(response, 100))
                with pytest.raises(FetchError) as error:
                    async with client.open("http://cdn.test/toofar0") as response:
                        await read_small(response, 100)
                codes.append(error.value.code)
        return codes

    assert asyncio.run(scenario()) == [b"hello", "download_failed"]


@pytest.mark.parametrize("path,code", [
    ("/err", "download_failed"),      # не-2xx.
    ("/gz", "download_failed"),       # сжатие помимо identity.
    ("/unsafe", "unsafe_url"),        # редирект на литерал метаданных.
    ("/scheme", "unsafe_url"),        # редирект на запрещённую схему.
    ("/loop", "download_failed"),     # слишком длинная цепочка.
])
def test_open_rejects_non_2xx_compression_and_bad_redirect(path, code):
    async def scenario():
        async with _server() as port:
            async with _client(port) as client:
                with pytest.raises(FetchError) as error:
                    async with client.open("http://cdn.test" + path) as response:
                        await read_small(response, 100)
                return error.value.code

    assert asyncio.run(scenario()) == code


def test_read_small_caps_actual_stream_without_content_length():
    async def scenario():
        async with _server() as port:
            async with _client(port) as client:
                with pytest.raises(FetchError) as error:
                    async with client.open("http://cdn.test/chunk") as response:
                        await read_small(response, 50)
                return error.value.code

    assert asyncio.run(scenario()) == "input_too_large"


def test_read_small_rejects_oversized_content_length_before_reading():
    async def scenario():
        async with _server() as port:
            async with _client(port) as client:
                with pytest.raises(FetchError) as error:
                    async with client.open("http://cdn.test/bigcl") as response:
                        assert response.content_length == 1000
                        await read_small(response, 50)
                return error.value.code

    assert asyncio.run(scenario()) == "input_too_large"


def test_open_rejects_unsafe_literal_regardless_of_resolver():
    async def scenario():
        async with _server() as port:
            async with _client(port) as client:
                with pytest.raises(FetchError) as error:
                    async with client.open("http://127.0.0.1/") as response:
                        await read_small(response, 100)
                return error.value.code

    assert asyncio.run(scenario()) == "unsafe_url"


def test_open_enforces_global_deadline():
    async def scenario():
        async with _server() as port:
            async with _client(port, timeout=0.3) as client:
                with pytest.raises(FetchError) as error:
                    async with client.open("http://cdn.test/slow") as response:
                        await read_small(response, 100)
                return error.value.code

    assert asyncio.run(scenario()) == "timeout"


def test_redirect_does_not_replay_server_cookies():
    async def scenario():
        app = web.Application()

        async def start(request):
            response = web.Response(status=302, headers={"Location": "/target"})
            response.set_cookie("tracking", "secret")
            return response

        async def target(request):
            return web.Response(text=request.headers.get("Cookie", "none"))

        app.add_routes([web.get("/start", start), web.get("/target", target)])
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            async with _client(runner.addresses[0][1]) as client:
                async with client.open("http://cdn.test/start") as response:
                    return await read_small(response, 100)
        finally:
            await runner.cleanup()

    assert asyncio.run(scenario()) == b"none"

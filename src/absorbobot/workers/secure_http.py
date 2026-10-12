"""Безопасная загрузка по HTTP(S): без SSRF, подписанных URL и лишнего расхода памяти.

Модуль предоставляет ограниченный по политике транспорт для одного внешнего
ресурса. Разрешены только схемы ``http`` (порт 80) и ``https`` (порт 443), DNS
проверяется до подключения, а подключение идёт ровно на проверенные адреса.
Любая ошибка превращается в :class:`FetchError` с безопасным кодом: URL,
заголовки и ответы сервера не попадают ни в сообщение, ни в цепочку причин.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

__all__ = ["FetchError", "PinnedResolver", "SecureClient", "read_small"]


class FetchError(ValueError):
    """Отказ с безопасным кодом: ни URL, ни заголовков, ни ответов сервера."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# Разрешённые схемы и их единственно допустимые порты.
_ALLOWED_PORTS = {"http": 80, "https": 443}
# Коды перенаправлений, которые мы разбираем вручную, без implicit-редиректов.
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

_MAX_URL_LENGTH = 8192
_MAX_REDIRECTS = 5
_CONNECT_TIMEOUT = 15.0
_SOCK_READ_TIMEOUT = 30.0
_READ_CHUNK = 64 * 1024
_MAX_LINE_SIZE = 8192
_MAX_FIELD_SIZE = 8192
_MAX_HEADERS = 100
_READ_BUFSIZE = 64 * 1024

# Метка DNS-имени по RFC 1123: не пустая и без ведущего/замыкающего дефиса.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NAT64_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


def _ipv4_is_public(address: ipaddress.IPv4Address) -> bool:
    """Публичный IPv4: маршрутизируемый и не относится к особым диапазонам."""
    return (address.is_global and not address.is_private and not address.is_loopback
            and not address.is_link_local and not address.is_multicast
            and not address.is_reserved and not address.is_unspecified)


def _ipv6_is_public(address: ipaddress.IPv6Address) -> bool:
    """IPv6 с разворачиванием переходных механизмов, встраивающих IPv4."""
    if address.ipv4_mapped is not None:
        return _ipv4_is_public(address.ipv4_mapped)
    if address.sixtofour is not None:
        return _ipv4_is_public(address.sixtofour)
    if address.teredo is not None:
        server, client = address.teredo
        return _ipv4_is_public(server) and _ipv4_is_public(client)
    for prefix in _NAT64_PREFIXES:
        if address in prefix:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
            return _ipv4_is_public(embedded)
    return (address.is_global and not address.is_private and not address.is_multicast
            and not address.is_link_local and not address.is_site_local
            and not address.is_loopback and not address.is_unspecified
            and not address.is_reserved)


def _ip_is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Разрешить только глобально маршрутизируемые адреса обоих семейств."""
    if isinstance(address, ipaddress.IPv6Address):
        return _ipv6_is_public(address)
    return _ipv4_is_public(address)


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Разобрать host как IP-литерал или вернуть ``None``."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _valid_hostname(host: str) -> bool:
    """Проверить форму DNS-имени без IDN, числовых и legacy-форм адресов."""
    if len(host) > 253 or host.startswith(".") or host.endswith("."):
        return False
    labels = host.split(".")
    if any(not _LABEL.fullmatch(label) for label in labels):
        return False
    # Домен верхнего уровня начинается с буквы: legacy-формы IPv4 (2130706433,
    # 0x7f000001, 127.1) разбираются getaddrinfo как адреса, но не как домены.
    return labels[-1][0].isalpha()


def _validate_url(value: str) -> str:
    """Проверить URL до запроса и вернуть его же для передачи транспорту."""
    if not isinstance(value, str) or len(value) > _MAX_URL_LENGTH:
        raise FetchError("invalid_url")
    for char in value:
        if char.isspace() or ord(char) < 0x21 or ord(char) == 0x7F or char == "\\":
            raise FetchError("invalid_url")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise FetchError("invalid_url") from None
    if scheme not in _ALLOWED_PORTS:
        raise FetchError("invalid_url")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise FetchError("invalid_url")
    if port is not None and port != _ALLOWED_PORTS[scheme]:
        raise FetchError("invalid_url")
    if not host:
        raise FetchError("invalid_url")
    address = _parse_ip(host)
    if address is not None:
        if not _ip_is_public(address):
            raise FetchError("unsafe_url")
    elif not _valid_hostname(host):
        raise FetchError("invalid_url")
    return value


class PinnedResolver(AbstractResolver):
    """DNS-резолвер, отдающий коннектору только проверенные адреса.

    aiohttp подключается ровно к адресам из возвращённого списка, поэтому
    проверка на публичность здесь же и закрепляет соединение: второй, уже
    небезопасной резолюции не происходит. Смешение публичных и непубличных
    ответов, как и любой непубличный адрес, отклоняет весь набор.
    """

    def __init__(self, *, timeout: float = _CONNECT_TIMEOUT):
        self._timeout = timeout

    async def resolve(self, host: str, port: int = 0,
                      family: socket.AddressFamily = socket.AF_UNSPEC) -> list[ResolveResult]:
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, port or None, type=socket.SOCK_STREAM, family=family),
                self._timeout,
            )
        except asyncio.TimeoutError:
            raise FetchError("timeout") from None
        except socket.gaierror:
            raise FetchError("download_failed") from None
        results: list[ResolveResult] = []
        seen: set[tuple[str, int]] = set()
        for afamily, _, proto, _, address in infos:
            literal = address[0]
            parsed = _parse_ip(literal)
            if parsed is None or not _ip_is_public(parsed):
                raise FetchError("unsafe_url") from None
            key = (literal, afamily)
            if key in seen:
                continue
            seen.add(key)
            results.append(ResolveResult(
                hostname=host, host=literal, port=port, family=afamily,
                proto=proto, flags=0,
            ))
        if not results:
            raise FetchError("download_failed") from None
        return results

    async def close(self) -> None:
        """Резолвер не держит ресурсов, закрывать нечего."""
        pass


class SecureClient:
    """Асинхронный контекст для ограниченных HTTP(S)-запросов.

    :param timeout: общий срок одной операции :meth:`open` вместе со всеми
        перенаправлениями и чтением тела.
    :param resolver: только для тестов; по умолчанию используется
        :class:`PinnedResolver`, задающий политику безопасности.
    """

    def __init__(self, timeout: float = 600, *, resolver: AbstractResolver | None = None):
        self._timeout = float(timeout)
        self._resolver = resolver
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> SecureClient:
        if self._session is not None:
            raise RuntimeError("SecureClient уже открыт")
        resolver = self._resolver
        if resolver is None:
            resolver = PinnedResolver(timeout=min(_CONNECT_TIMEOUT, self._timeout))
        connector = aiohttp.TCPConnector(resolver=resolver)
        self._session = aiohttp.ClientSession(
            connector=connector, trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    @asynccontextmanager
    async def open(self, url: str):
        """Открыть ответ после ручного следования не более чем пяти редиректам.

        При успехе отдаёт :class:`aiohttp.ClientResponse`; читать тело следует
        через :func:`read_small`, чтобы ограничить расход памяти независимо от
        ``Content-Length``. Непустое тело редиректа не вычитывается.
        """
        session = self._session
        if session is None:
            raise RuntimeError("SecureClient используется вне своего контекста")
        response: aiohttp.ClientResponse | None = None
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._request(session, url)
                yield response
        except asyncio.TimeoutError:
            raise FetchError("timeout") from None
        finally:
            if response is not None:
                response.close()

    async def _request(self, session: aiohttp.ClientSession,
                       url: str) -> aiohttp.ClientResponse:
        """Пройти цепочку редиректов, проверяя каждый следующий адрес заново."""
        current = _validate_url(url)
        for _ in range(_MAX_REDIRECTS + 1):
            response = await self._send(session, current)
            status = response.status
            if status in _REDIRECT_STATUS:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise FetchError("download_failed")
                try:
                    following = _validate_url(urljoin(current, location))
                    if urlsplit(current).scheme == "https" and urlsplit(following).scheme != "https":
                        raise FetchError("unsafe_url")
                    current = following
                except FetchError:
                    raise FetchError("unsafe_url") from None
                continue
            if not 200 <= status < 300:
                response.close()
                raise FetchError("download_failed")
            encoding = response.headers.get("Content-Encoding")
            if encoding is not None and encoding.strip().lower() not in ("", "identity"):
                response.close()
                raise FetchError("download_failed")
            return response
        raise FetchError("download_failed")

    async def _send(self, session: aiohttp.ClientSession,
                    url: str) -> aiohttp.ClientResponse:
        """Один GET без implicit-редиректов, распаковки и cookies."""
        timeout = aiohttp.ClientTimeout(
            total=None, connect=_CONNECT_TIMEOUT,
            sock_connect=_CONNECT_TIMEOUT, sock_read=_SOCK_READ_TIMEOUT,
        )
        try:
            return await session.get(
                url, allow_redirects=False, auto_decompress=False,
                raise_for_status=False, timeout=timeout,
                headers={"Accept-Encoding": "identity"},
                max_line_size=_MAX_LINE_SIZE, max_field_size=_MAX_FIELD_SIZE,
                max_headers=_MAX_HEADERS, read_bufsize=_READ_BUFSIZE,
            )
        except FetchError:
            raise
        except asyncio.TimeoutError:
            raise FetchError("timeout") from None
        except aiohttp.ClientError:
            raise FetchError("download_failed") from None
        except OSError:
            raise FetchError("download_failed") from None


async def read_small(response: aiohttp.ClientResponse, limit: int) -> bytes:
    """Прочитать тело не более ``limit`` байт, не доверяя ``Content-Length``.

    Превышающий предел ``Content-Length`` отклоняется сразу; иначе поток
    ограничивается по фактически прочитанным байтам. Пустой ответ допустим и
    остаётся решением потребителя.
    """
    length = response.content_length
    if length is not None and length > limit:
        raise FetchError("input_too_large")
    data = bytearray()
    try:
        async for chunk in response.content.iter_chunked(_READ_CHUNK):
            data += chunk
            if len(data) > limit:
                raise FetchError("input_too_large")
    except FetchError:
        raise
    except asyncio.TimeoutError:
        raise FetchError("timeout") from None
    except (aiohttp.ClientError, OSError):
        raise FetchError("download_failed") from None
    return bytes(data)

"""Уведомления systemd о готовности и признаках жизни."""

from __future__ import annotations

import asyncio
import os
import socket


def notify(message: str) -> None:
    """Отправить datagram в NOTIFY_SOCKET; вне systemd ничего не делать."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode("utf-8"), address)
    except OSError:
        pass


class SystemdWatchdog:
    """Пинговать watchdog, пока жив event loop; зависший цикл перестаёт пинговать."""

    def __init__(self) -> None:
        try:
            self.usec = int(os.environ.get("WATCHDOG_USEC", "0"))
        except ValueError:
            self.usec = 0
        self.enabled = self.usec > 0 and bool(os.environ.get("NOTIFY_SOCKET"))

    def ready(self) -> None:
        if self.enabled:
            notify("READY=1")

    def stopping(self) -> None:
        if self.enabled:
            notify("STOPPING=1")

    async def run(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            await stop.wait()
            return
        interval = max(1.0, self.usec / 3_000_000)
        while not stop.is_set():
            notify("WATCHDOG=1")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

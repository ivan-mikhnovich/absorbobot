"""Локальный Telegram transport с транзакционными updates и outbox."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter, TelegramServerError,
)
from aiogram.types import BotCommand, FSInputFile, Update

from absorbobot.admin import Admin
from absorbobot.config import Settings, probe_core
from absorbobot.dialog import Dialog
from absorbobot.i18n import language_for
from absorbobot.store import Store, utc_now


class UpdateRouter:
    """Применить update до подтверждения offset; длительная работа только в outbox."""

    def __init__(self, store: Store, dialog: Dialog, admin: Admin):
        self.store, self.dialog, self.admin = store, dialog, admin

    def handle(self, update: Update) -> bool:
        def mutate():
            callback = update.callback_query
            message = callback.message if callback else update.message
            sender = callback.from_user if callback else (message.from_user if message else None)
            if (message is None or sender is None or message.chat.type != "private"
                    or not self.store.authorized(sender.id)):
                return
            self.store.upsert_user(
                sender.id, username=sender.username,
                full_name=sender.full_name, language=language_for(sender.language_code),
            )
            if callback:
                payload = callback.model_dump(mode="json", by_alias=True, exclude_none=True)
                self.store.add_outbox("telegram", {
                    "owner_id": sender.id, "method": "answer_callback_query",
                    "kwargs": {"callback_query_id": callback.id},
                }, dedupe_key=f"ack:{update.update_id}")
                if not self.admin.handle_callback(payload):
                    self.dialog.handle_callback(payload)
            else:
                payload = message.model_dump(mode="json", by_alias=True, exclude_none=True)
                if not self.admin.handle_message(payload):
                    self.dialog.handle_message(payload)
        return self.store.process_update(update.update_id, mutate)


class TelegramOutbox:
    """Отправить сохранённые действия без повторения оплаченной обработки."""

    METHODS = frozenset({"send_message", "send_photo", "send_document",
                         "edit_message_text", "answer_callback_query"})

    def __init__(self, bot: Bot, store: Store):
        self.bot, self.store = bot, store

    async def send(self, item: dict) -> None:
        payload = item["payload"]
        owner = payload["owner_id"]
        if not self.store.authorized(owner) and payload.get("purpose") != "cancellation":
            self.store.outbox_sent(item["id"], {"skipped": "access_revoked"})
            return
        method = payload["method"]
        if method not in self.METHODS:
            raise ValueError("Unsupported Telegram outbox method.")
        kwargs = dict(payload["kwargs"])
        if "chat_id" in kwargs and kwargs["chat_id"] != owner:
            raise ValueError("Outbox recipient does not match owner.")
        if kwargs.get("photo") == "asset:absorbent.jpg":
            kwargs["photo"] = FSInputFile(Path(__file__).with_name("assets") / "absorbent.jpg")
        if method == "send_document" and "file" in payload:
            # Только заранее подготовленный собственный файл, не произвольный путь update.
            root = (self.store.settings.storage_dir / "jobs" / payload["work_uuid"]).resolve()
            path = (root / payload["file"]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("Delivery file is outside its work tree or missing.")
            kwargs["document"] = FSInputFile(path, filename=payload.get("filename", path.name))
        try:
            response = await getattr(self.bot, method)(**kwargs)
        except TelegramRetryAfter as exc:
            self.store.outbox_retry(item["id"], utc_now() + timedelta(seconds=exc.retry_after), "telegram_rate_limit")
        except (TelegramNetworkError, TelegramServerError):
            delay = min(300, 2 ** min(item["attempts"] + 1, 8))
            self.store.outbox_retry(item["id"], utc_now() + timedelta(seconds=delay), "telegram_transport")
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            if method == "answer_callback_query":
                # Истёкший callback не должен бесконечно задерживать собственный outbox.
                self.store.outbox_sent(item["id"], {"expired": True})
            else:
                self.store.outbox_retry(item["id"], utc_now() + timedelta(seconds=300), type(exc).__name__)
        else:
            result = {"message_id": response.message_id} if hasattr(response, "message_id") else {"ok": bool(response)}
            self.store.outbox_sent(item["id"], result)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for item in self.store.pending_outbox(limit=100):
                if item["kind"] == "telegram":
                    await self.send(item)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except TimeoutError:
                pass


async def poll(bot: Bot, router: UpdateRouter, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            updates = await bot.get_updates(offset=router.store.offset(), timeout=30,
                                            allowed_updates=["message", "callback_query"])
        except TelegramRetryAfter as exc:
            try:
                await asyncio.wait_for(stop.wait(), timeout=exc.retry_after)
            except TimeoutError:
                pass
            continue
        except (TelegramNetworkError, TelegramServerError):
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass
            continue
        for update in updates:
            router.handle(update)


async def serve(settings: Settings) -> None:
    """Запустить установленный бот; сначала остановить старые деревья процессов."""
    from absorbobot.engine import Engine

    core = probe_core(settings)
    session = AiohttpSession(api=TelegramAPIServer.from_base(settings.bot_api_url, is_local=True))
    bot = Bot(os.environ["ABSORBOBOT_TOKEN"], session=session)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for value in signals:
        loop.add_signal_handler(value, stop.set)
    try:
        with Store(settings) as store:
            dialog, admin = Dialog(store, settings, core), Admin(store, settings, core)
            engine = Engine(settings, store, bot, dialog, core)
            try:
                await engine.recover()
                for language in ("ru", "en"):
                    commands = dialog.command_descriptions(language)
                    await bot.set_my_commands(
                        [BotCommand(**command) for command in commands],
                        language_code=language,
                    )
                commands = dialog.command_descriptions("en")
                await bot.set_my_commands([BotCommand(**command) for command in commands])
                async with asyncio.TaskGroup() as tasks:
                    polling = tasks.create_task(poll(bot, UpdateRouter(store, dialog, admin), stop))
                    outgoing = tasks.create_task(TelegramOutbox(bot, store).run(stop))
                    tasks.create_task(engine.run(stop))
                    await stop.wait()
                    polling.cancel()
                    outgoing.cancel()
                    await engine.shutdown()
            finally:
                stop.set()
                await engine.shutdown()
    finally:
        for value in signals:
            loop.remove_signal_handler(value)
        await bot.session.close()

"""Локальный Telegram transport с транзакционными updates и outbox."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter, TelegramServerError,
)
from aiogram.types import BotCommand, FSInputFile, InputMediaDocument, Update

from absorbobot.admin import Admin
from absorbobot.config import Settings, probe_core
from absorbobot.dialog import Dialog
from absorbobot.i18n import language_for, tr
from absorbobot.store import Store, _utc, utc_now


class UpdateRouter:
    """Применить update до подтверждения offset; длительная работа только в outbox."""

    def __init__(self, store: Store, dialog: Dialog, admin: Admin):
        self.store, self.dialog, self.admin = store, dialog, admin

    def handle(self, update: Update) -> bool:
        def mutate():
            callback = update.callback_query
            message = callback.message if callback else update.message
            sender = callback.from_user if callback else (message.from_user if message else None)
            if message is None or sender is None or message.chat.type != "private":
                return
            if not self.store.authorized(sender.id):
                # Ник из первого сообщения — единственный источник ID для отложенного доступа.
                granted = None if sender.is_bot else self.store.claim_access(sender.id, sender.username)
                if granted is None:
                    return
                self._access_notice(granted, sender.id)
            self.store.upsert_user(
                sender.id, username=sender.username,
                full_name=sender.full_name, language=language_for(sender.language_code),
            )
            # Сериализуем только поля Telegram, без служебных Default aiogram.
            if callback:
                payload = callback.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
                self.store.add_outbox("telegram", {
                    "owner_id": sender.id, "method": "answer_callback_query",
                    "kwargs": {"callback_query_id": callback.id},
                }, dedupe_key=f"ack:{update.update_id}")
                if not self.admin.handle_callback(payload):
                    self.dialog.handle_callback(payload)
            else:
                payload = message.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
                # Живой отчёт обязан оставаться последним сообщением чата: запомнить его конец.
                self.store.record_chat_tail(message.chat.id, message.message_id)
                if not self.admin.handle_message(payload):
                    self.dialog.handle_message(payload)
        return self.store.process_update(update.update_id, mutate)

    def _access_notice(self, granted: dict, user_id: int) -> None:
        """Сообщить администратору, что отложенный доступ по нику открыт."""
        actor = granted["actor_id"]
        user = self.store.get_user(actor)
        text = tr((user or {}).get("language", "en"), "admin_access_opened",
                  username="@" + granted["username"], target=user_id)
        self.store.add_outbox("telegram", {
            "owner_id": actor, "method": "send_message",
            "kwargs": {"chat_id": actor, "text": text},
        })


class TelegramOutbox:
    """Отправить сохранённые действия без повторения оплаченной обработки."""

    METHODS = frozenset({"send_message", "send_rich_message", "send_photo", "send_document", "send_media_group",
                         "edit_message_text", "delete_message", "answer_callback_query"})
    PROGRESS_INTERVAL = timedelta(seconds=5)

    def __init__(self, bot: Bot, store: Store):
        self.bot, self.store = bot, store

    def _fresh_payload(self, item_id: int) -> dict | None:
        """Прочитать payload из базы: продюсер прогресса дописывает текст до отправки."""
        row = self.store.db.execute("SELECT status,payload FROM outbox WHERE id=?", (item_id,)).fetchone()
        if row is None or row["status"] != "pending":
            return None
        return json.loads(row["payload"])

    def _progress_state(self, work_uuid: str) -> dict:
        """Действия прогресса одного задания: ожидающие, время отправки и живое сообщение."""
        pending, last_sent_at, message_id = [], None, None
        for row in self.store.progress_actions(work_uuid):
            payload = row["payload"]
            if row["status"] == "pending":
                pending.append({"id": row["id"], "payload": payload})
                continue
            result = row["result"] or {}
            if result.get("progress") and row["sent_at"]:
                last_sent_at = row["sent_at"]
            if payload.get("method") in ("send_message", "send_rich_message") and result.get("message_id"):
                message_id = result["message_id"]
        return {"pending": pending, "last_sent_at": last_sent_at, "message_id": message_id}

    def _supersede(self, pending: list[dict], representative: int) -> None:
        """Устаревшие правки одного прогресса завершить как пропущенные, не отправляя их."""
        for row in pending:
            if row["id"] != representative:
                self.store.outbox_sent(row["id"], {"skipped": "superseded", "superseded_by": representative})

    def _drop_progress_message(self, owner: int, work_uuid: str, message_id: int) -> None:
        """Убрать отставший отчёт: причина и результат приходят отдельными сообщениями."""
        self.store.forget_progress_message(work_uuid, message_id, "report_moved")
        self.store.add_outbox("telegram", {
            "owner_id": owner, "method": "delete_message",
            "kwargs": {"chat_id": owner, "message_id": message_id},
        }, dedupe_key=f"progress-drop:{work_uuid}:{message_id}")

    def _progress_target(self, owner: int, payload: dict, message_id: int | None,
                         reply_to: int | None) -> tuple[str, dict, dict | None]:
        """Выбрать отправку или правку живой строки и её текстовый запасной вид."""
        rich = payload["kwargs"].get("rich_message")
        text = payload.get("text") or ""
        if message_id is not None:
            kwargs = {"chat_id": owner, "message_id": message_id}
            if rich:
                # Отказ правки лечит пересылка строки заново, а не второй вид той же правки.
                return "edit_message_text", dict(kwargs, rich_message=rich), None
            return "edit_message_text", dict(kwargs, text=text), None
        reply = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None
        if rich:
            kwargs = {"chat_id": owner, "rich_message": rich}
            plain = {"chat_id": owner, "text": text}
            if reply:
                kwargs["reply_parameters"] = reply
                plain["reply_parameters"] = reply
            return "send_rich_message", kwargs, {"method": "send_message", "kwargs": plain}
        kwargs = {"chat_id": owner, "text": text}
        if reply:
            kwargs["reply_parameters"] = reply
        return "send_message", kwargs, None

    async def _send_progress(self, item: dict, payload: dict) -> None:
        item_id, owner = item["id"], payload["owner_id"]
        work_uuid = payload["work_uuid"]
        work = self.store.get_job(work_uuid) or self.store.get_draft(work_uuid)
        if not work or work["owner_id"] != owner:
            # Прогресс не должен уходить чужому владельцу или несуществующему заданию.
            self.store.outbox_sent(item_id, {"skipped": "owner_mismatch"})
            return
        state = self._progress_state(work_uuid)
        pending = state["pending"]
        if not pending:
            return
        if item_id != pending[-1]["id"]:
            # Действует только свежая строка: устаревшие правки одного состояния не нужны.
            self.store.outbox_sent(item_id, {"skipped": "superseded", "superseded_by": pending[-1]["id"]})
            return
        self._supersede(pending, item_id)
        payload = pending[-1]["payload"]
        message_id = state["message_id"]
        if message_id is not None and self.store.chat_tail(owner) > message_id:
            # Отчёт обязан быть последним сообщением чата: отставшее убрать, новое поставить заново.
            self._drop_progress_message(owner, work_uuid, message_id)
            message_id = None
        if state["last_sent_at"] is not None and message_id is not None:
            deadline = datetime.fromisoformat(state["last_sent_at"]) + self.PROGRESS_INTERVAL
            if utc_now() < deadline:
                # Сдвигаем только срок: попытки не тратятся, сетевые задержки не укорачиваются.
                with self.store.transaction():
                    self.store.db.execute("UPDATE outbox SET available_at=? WHERE id=? AND status='pending'",
                                          (_utc(deadline), item_id))
                return
        method, kwargs, fallback = self._progress_target(owner, payload, message_id, work["source_message_id"])
        if payload.get("method") != method:
            # Отправка вместо правки: строка обязана помнить свой вид, иначе её сообщение потеряется.
            self.store.update_outbox_payload(item_id, {**payload, "method": method})
        outcome = await self._dispatch(item, method, kwargs, extra={"progress": True},
                                       on_bad_request="fail", payload=payload, fallback=fallback)
        if outcome == "rejected" and method == "edit_message_text":
            # Сообщения отчёта больше нет: забыть его и поставить строку заново.
            self.store.forget_progress_message(work_uuid, message_id, "message_gone")
            again, kwargs, fallback = self._progress_target(owner, payload, None, work["source_message_id"])
            action = {"owner_id": owner, "method": again, "purpose": "progress",
                      "work_uuid": work_uuid, "kwargs": kwargs, "text": payload.get("text") or ""}
            if fallback is not None:
                action["fallback"] = fallback
            self.store.add_outbox("telegram", action)

    async def send(self, item: dict) -> None:
        payload = self._fresh_payload(item["id"])
        if payload is None:
            return
        owner = payload["owner_id"]
        if not self.store.authorized(owner) and payload.get("purpose") != "cancellation":
            self.store.outbox_sent(item["id"], {"skipped": "access_revoked"})
            return
        if payload.get("purpose") == "progress":
            await self._send_progress(item, payload)
            return
        method = payload["method"]
        if method not in self.METHODS:
            raise ValueError("Unsupported Telegram outbox method.")
        kwargs = dict(payload["kwargs"])
        if "chat_id" in kwargs and kwargs["chat_id"] != owner:
            raise ValueError("Outbox recipient does not match owner.")
        if kwargs.get("photo") == "asset:absorbent.jpg":
            kwargs["photo"] = FSInputFile(Path(__file__).with_name("assets") / "absorbent.jpg")
        if method == "send_photo" and "preview" in payload:
            work = self.store.get_draft(payload["work_uuid"]) or self.store.get_job(payload["work_uuid"])
            if not work or work["owner_id"] != owner or work.get("files_deleted"):
                self.store.outbox_sent(item["id"], {"skipped": "source_unavailable"})
                return
            base = (self.store.settings.storage_dir / "jobs").resolve()
            root = base / work["id"]
            source = root / payload["preview"]
            path = source.resolve()
            if (root.is_symlink() or source.is_symlink() or not root.resolve().is_relative_to(base)
                    or not path.is_relative_to(root) or path.suffix.lower() != ".jpg"
                    or not path.is_file() or path.stat().st_size > 262144):
                self.store.outbox_sent(item["id"], {"skipped": "preview_unavailable"})
                return
            kwargs["photo"] = FSInputFile(path)
        if method == "send_document" and "file" in payload:
            # Только заранее подготовленный собственный файл, не произвольный путь update.
            source = self.store.get_job(payload["work_uuid"])
            if (not source or source["status"] != "completed" or source["files_deleted"]
                    or not self.store.authorized(source["owner_id"])):
                self.store.outbox_sent(item["id"], {"skipped": "source_unavailable"})
                return
            base = (self.store.settings.storage_dir / "jobs").resolve()
            root = base / source["id"]
            path = (root / payload["file"]).resolve()
            if (root.is_symlink() or not root.resolve().is_relative_to(base)
                    or not path.is_relative_to(root) or not path.is_file()):
                self.store.outbox_sent(item["id"], {"skipped": "artifact_missing"})
                return
            kwargs["document"] = FSInputFile(path, filename=payload.get("filename", path.name))
        if method == "send_media_group":
            # Один альбом — одно сообщение; подпись Telegram показывает только у первого вложения.
            source = self.store.get_job(payload["work_uuid"])
            if (not source or source["status"] != "completed" or source["files_deleted"]
                    or not self.store.authorized(source["owner_id"])):
                self.store.outbox_sent(item["id"], {"skipped": "source_unavailable"})
                return
            base = (self.store.settings.storage_dir / "jobs").resolve()
            root = base / source["id"]
            media = []
            for position, file in enumerate(payload["files"]):
                path = (root / file["file"]).resolve()
                if (root.is_symlink() or not root.resolve().is_relative_to(base)
                        or not path.is_relative_to(root) or not path.is_file()):
                    self.store.outbox_sent(item["id"], {"skipped": "artifact_missing"})
                    return
                media.append(InputMediaDocument(
                    media=FSInputFile(path, filename=file.get("filename", path.name)),
                    caption=payload.get("caption") if position == 0 else None))
            kwargs["media"] = media
        await self._dispatch(item, method, kwargs, payload=payload)

    async def _dispatch(self, item: dict, method: str, kwargs: dict, *, extra: dict | None = None,
                        on_bad_request: str = "retry", payload: dict | None = None,
                        fallback: dict | None = None) -> str:
        """Выполнить действие Telegram и вернуть исход, решающий судьбу живого отчёта."""
        if fallback is None and payload is not None:
            fallback = payload.get("fallback")
        try:
            response = await getattr(self.bot, method)(**kwargs)
        except TelegramRetryAfter as exc:
            self.store.outbox_retry(item["id"], utc_now() + timedelta(seconds=exc.retry_after), "telegram_rate_limit")
            return "retry"
        except (TelegramNetworkError, TelegramServerError):
            delay = min(300, 2 ** min(item["attempts"] + 1, 8))
            self.store.outbox_retry(item["id"], utc_now() + timedelta(seconds=delay), "telegram_transport")
            return "retry"
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            if method == "answer_callback_query":
                # Истёкший callback не должен бесконечно задерживать собственный outbox.
                self.store.outbox_sent(item["id"], {"expired": True})
                return "rejected"
            if method == "delete_message":
                # Сообщение уже убрано владельцем: повторять удаление нечего.
                self.store.outbox_sent(item["id"], {"skipped": "already_gone"})
                return "rejected"
            fallback = fallback if isinstance(fallback, dict) else None
            if isinstance(fallback, dict) and isinstance(fallback.get("method"), str):
                # Богатая разметка не принята: та же строка уходит обычным текстом.
                self.store.outbox_sent(item["id"], {"skipped": "rich_rejected_" + type(exc).__name__})
                action = {key: payload[key] for key in ("owner_id", "purpose", "work_uuid", "text")
                          if payload.get(key) is not None}
                action.update({"method": fallback["method"], "kwargs": fallback["kwargs"]})
                self.store.add_outbox("telegram", action)
                return "rejected"
            if on_bad_request == "fail":
                self.store.outbox_sent(item["id"], {"skipped": "rejected_" + type(exc).__name__})
                return "rejected"
            self.store.outbox_retry(item["id"], utc_now() + timedelta(seconds=300), type(exc).__name__)
            return "retry"
        else:
            # Альбом отвечает списком сообщений: сохранить идентификатор первого.
            identifier = response.message_id if hasattr(response, "message_id") else None
            if identifier is None and isinstance(response, list) and response:
                identifier = getattr(response[0], "message_id", None)
            result = {"message_id": identifier} if identifier is not None else {"ok": bool(response)}
            if extra:
                result.update(extra)
            self.store.outbox_sent(item["id"], result)
            # Живой отчёт обязан оставаться последним: помнить конец чата и после отправки.
            self.store.record_chat_tail(kwargs.get("chat_id"), identifier)
            return "sent"

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


_PUBLISH_ERRORS = (TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramServerError)


async def publish_commands(bot: Bot, dialog: Dialog) -> None:
    """Опубликовать описания команд, не останавливая службу при отказе Telegram.

    Отказ `setMyCommands` (в том числе ограничение частоты) не влияет на приём
    сообщений, но при жёсткой обработке превращал перезапуск службы в цикл:
    каждая новая попытка снова упиралась в предел.
    """
    try:
        for language in ("ru", "en"):
            commands = dialog.command_descriptions(language)
            await bot.set_my_commands(
                [BotCommand(**command) for command in commands],
                language_code=language,
            )
        commands = dialog.command_descriptions("en")
        await bot.set_my_commands([BotCommand(**command) for command in commands])
    except _PUBLISH_ERRORS as exc:
        print(f"Описания команд не опубликованы: {type(exc).__name__}.", file=sys.stderr)


async def serve(settings: Settings) -> None:
    """Запустить установленный бот; сначала остановить старые деревья процессов."""
    from absorbobot.engine import Engine

    core = probe_core(settings)
    session = AiohttpSession(api=TelegramAPIServer.from_base(
        settings.bot_api_url, is_local=settings.bot_api_url != "https://api.telegram.org"))
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
                await publish_commands(bot, dialog)
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

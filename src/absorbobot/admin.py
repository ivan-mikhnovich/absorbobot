"""Административный интерфейс без сетевых вызовов в транзакциях обновлений."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import UUID

from .i18n import tr
from .store import StoreError, TERMINAL_JOBS, utc_now


PAGE_SIZE = 10
COMMANDS = {"admin", "queue", "allow", "ban", "users", "job", "stats", "storage", "purge", "pause", "resume"}
QUEUE_STATUSES = ("queued", "preparing", "running", "cancel_requested", "awaiting_reuse_choice")


def _user_id(text: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,18}", text) or int(text) > 9223372036854775807:
        raise StoreError("invalid_user_id")
    return int(text)


def _work_id(text: str) -> str:
    try:
        if str(UUID(text)) != text:
            raise ValueError
    except (ValueError, AttributeError):
        raise StoreError("invalid_uuid") from None
    return text


def _directory_bytes(root: Path) -> int | None:
    """Считать transport storage без чтения содержимого и переходов по symlink."""
    try:
        if root.is_symlink():
            return None
        if not root.exists():
            return 0
        pending = [root]
        total = 0
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        return total
    except OSError:
        return None


class Admin:
    def __init__(self, store, settings, core):
        self.store = store
        self.settings = settings
        self.core = core

    def _actor(self, update: dict, *, callback: bool = False) -> int | None:
        message = update.get("message", {}) if callback else update
        sender = update.get("from", {}).get("id")
        if message.get("chat", {}).get("type") != "private" or type(sender) is not int:
            return None
        return sender if self.store.is_admin(sender) else None

    def _lang(self, owner: int) -> str:
        return (self.store.get_user(owner) or {}).get("language", "en")

    def _t(self, recipient: int, key: str, **values) -> str:
        return tr(self._lang(recipient), "admin_" + key, **values)

    def _send(self, owner: int, text: str, keyboard=None, *, source=None, dedupe=None, purpose=None):
        # Разбивать также длинные отчёты, не передавая Telegram чрезмерный текст.
        chunks = []
        while len(text.encode("utf-16-le")) // 2 > 3500:
            end = min(len(text), 1700)
            boundary = text.rfind("\n", 0, end)
            end = boundary + 1 if boundary > 0 else end
            chunks.append(text[:end])
            text = text[end:]
        chunks.append(text)
        for index, chunk in enumerate(chunks):
            kwargs = {"chat_id": owner, "text": chunk}
            if index == len(chunks) - 1 and keyboard:
                kwargs["reply_markup"] = {"inline_keyboard": keyboard}
            if source is not None:
                kwargs["reply_parameters"] = {"message_id": source, "allow_sending_without_reply": True}
            payload = {"method": "send_message", "kwargs": kwargs, "owner_id": owner}
            if purpose is not None:
                payload["purpose"] = purpose
            self.store.add_outbox("telegram", payload, dedupe_key=f"{dedupe}:{index}" if dedupe else None)

    def _button(self, actor: int, label: str, action: str, token_target: str, **payload) -> dict:
        return {"text": self._t(actor, label), "callback_data": self.store.issue_token(actor, action, token_target, payload=payload)}

    def _home_button(self, actor: int) -> list[dict]:
        return [self._button(actor, "home", "admin_view", "overview")]

    def _pages(self, actor: int, section: str, page: int, more: bool) -> list[list[dict]]:
        navigation = []
        if page:
            navigation.append(self._button(actor, "previous", "admin_view", section, page=page - 1))
        if more:
            navigation.append(self._button(actor, "next", "admin_view", section, page=page + 1))
        return ([navigation] if navigation else []) + [self._home_button(actor)]

    def _status(self, actor: int, status: str) -> str:
        return self._t(actor, "status_" + status)

    def _value(self, actor: int, value) -> str:
        return str(value) if value is not None else self._t(actor, "unknown")

    def handle_message(self, message: dict) -> bool:
        text = message.get("text", "")
        if not isinstance(text, str) or not text.startswith("/"):
            return False
        parts = text.split(maxsplit=1)
        command = parts[0][1:].split("@", 1)[0].lower()
        if command not in COMMANDS:
            return False
        actor = self._actor(message)
        if actor is None:
            return command != "stats"
        argument = parts[1].strip() if len(parts) > 1 else ""
        with self.store.transaction():
            try:
                if command == "allow":
                    target_label = argument.split(maxsplit=1)
                    if not target_label:
                        raise StoreError("invalid_user_id")
                    target = _user_id(target_label[0])
                    label = target_label[1] if len(target_label) > 1 else None
                    if label is not None and len(label) > 200:
                        raise StoreError("invalid_arguments")
                    self.store.allow_user(actor, target, label)
                    self._send(actor, self._t(actor, "allowed", target=target))
                elif command == "ban":
                    self._confirm(actor, "ban", str(_user_id(argument)))
                elif command in ("job", "purge"):
                    target = _work_id(argument)
                    if command == "purge":
                        self._confirm(actor, "purge", target)
                    else:
                        self._job(actor, target)
                elif command == "stats":
                    self._stats(actor, _user_id(argument) if argument else None)
                else:
                    if argument:
                        raise StoreError("invalid_arguments")
                    self._view(actor, "overview" if command == "admin" else command)
            except StoreError as error:
                self._error(actor, error)
        return True

    def handle_callback(self, callback: dict) -> bool:
        data = callback.get("data", "")
        if not isinstance(data, str) or data.split(":", 1)[0] not in ("admin_menu", "admin_view", "admin_prompt", "admin_confirm", "admin_allow"):
            return False
        actor = self._actor(callback, callback=True)
        if actor is None:
            return True
        with self.store.transaction():
            try:
                token = self.store.consume_token(actor, data)
            except StoreError:
                return True
            try:
                action, target, payload = token["action"], token["target"], token["payload"]
                if action == "admin_menu":
                    self._view(actor, "overview")
                elif action == "admin_view":
                    page = payload.get("page", 0)
                    if type(page) is not int or page < 0:
                        raise StoreError("invalid_arguments")
                    self._view(actor, target, page)
                elif action == "admin_prompt":
                    self._confirm(actor, target, payload["target"])
                elif action == "admin_allow":
                    user_id = _user_id(target)
                    self.store.allow_user(actor, user_id)
                    self._send(actor, self._t(actor, "allowed", target=user_id))
                else:
                    self._execute(actor, target, payload["target"])
            except StoreError as error:
                self._error(actor, error)
        return True

    def _error(self, actor: int, error: StoreError):
        known = {"cannot_ban_admin", "not_found", "work_active", "invalid_uuid", "invalid_user_id", "invalid_arguments", "purge_failed", "safe_purge_unavailable"}
        self._send(actor, self._t(actor, "error_" + (error.code if error.code in known else "unavailable")))

    def _view(self, actor: int, section: str, page: int = 0):
        if section == "overview":
            version = self.core["version_info"]
            text = self._t(actor, "overview", version=version["version"], commit=version["commit"], state=self._t(actor, "paused" if self.store.paused() else "resumed"))
            keyboard = [[self._button(actor, name, "admin_view", name)] for name in ("queue", "users", "stats", "storage")]
            keyboard.append([self._button(actor, "resume" if self.store.paused() else "pause", "admin_view", "resume" if self.store.paused() else "pause")])
            self._send(actor, text, keyboard)
        elif section in ("pause", "resume"):
            self.store.set_paused(actor, section == "pause")
            self._send(actor, self._t(actor, "pause_notice" if section == "pause" else "resume_notice"), [self._home_button(actor)])
        elif section == "queue":
            self._queue(actor, page)
        elif section == "users":
            self._users(actor, page)
        elif section == "storage":
            self._storage(actor, page)
        elif section == "stats":
            self._stats(actor, None, page)
        elif section.startswith("job:"):
            self._job(actor, section[4:])
        elif section.startswith("stats:"):
            self._stats(actor, _user_id(section[6:]), page)
        else:
            raise StoreError("invalid_arguments")

    def _queue(self, actor: int, page: int):
        marks = ",".join("?" for _ in QUEUE_STATUSES)
        rows = self.store.db.execute(f"SELECT id FROM jobs WHERE status IN ({marks}) ORDER BY accepted_at,id LIMIT ? OFFSET ?", (*QUEUE_STATUSES, PAGE_SIZE + 1, page * PAGE_SIZE)).fetchall()
        text = [self._t(actor, "queue_title", page=page + 1)]
        keyboard = []
        for row in rows[:PAGE_SIZE]:
            job = self.store.get_job(row["id"])
            text.append(self._t(actor, "job_row", id=job["id"], owner=job["owner_id"], status=self._status(actor, job["status"]), position=self._value(actor, self.store.queue_position(job["id"]))))
            keyboard.append([self._button(actor, "details", "admin_view", "job:" + job["id"])])
        if not rows:
            text.append(self._t(actor, "empty"))
        self._send(actor, "\n\n".join(text), keyboard + self._pages(actor, "queue", page, len(rows) > PAGE_SIZE))

    def _users(self, actor: int, page: int):
        rows = self.store.db.execute("SELECT * FROM users ORDER BY id LIMIT ? OFFSET ?", (PAGE_SIZE + 1, page * PAGE_SIZE)).fetchall()
        text = [self._t(actor, "users_title", page=page + 1), self._t(actor, "allow_help")]
        keyboard = []
        for user in rows[:PAGE_SIZE]:
            status = "administrator" if self.store.is_admin(user["id"]) else ("approved" if user["approved"] else "banned")
            label = " / ".join(str(user[field])[:200] for field in ("label", "full_name", "username") if user[field])
            text.append(self._t(actor, "user_row", id=user["id"], label=label, status=self._t(actor, status)))
            keyboard.append([self._button(actor, "user_stats", "admin_view", "stats:" + str(user["id"]))])
            if not self.store.is_admin(user["id"]):
                keyboard.append([self._button(actor, "ban" if user["approved"] else "allow", "admin_prompt" if user["approved"] else "admin_allow", "ban" if user["approved"] else str(user["id"]), **({"target": str(user["id"])} if user["approved"] else {}))])
        if not rows:
            text.append(self._t(actor, "empty"))
        self._send(actor, "\n\n".join(text), keyboard + self._pages(actor, "users", page, len(rows) > PAGE_SIZE))

    def _job(self, actor: int, target: str):
        job = self.store.get_job(_work_id(target))
        if not job:
            raise StoreError("not_found")
        text = [self._t(actor, "job_detail", id=job["id"], owner=job["owner_id"], status=self._status(actor, job["status"]), accepted=job["accepted_at"], initial=job["initial_accepted_at"], version=self._value(actor, job["engine_version"]), commit=self._value(actor, job["engine_commit"]), counted=self._t(actor, "yes" if job["quota_counted"] else "no"), deleted=self._t(actor, "yes" if job["files_deleted"] else "no"))]
        for category, model in job["models"].items():
            text.append(self._t(actor, "model_row", category=category, name=model.get("name", self._t(actor, "unknown")), provider=(model.get("provider") or {}).get("name", self._t(actor, "unknown")), location=self._t(actor, "local" if model.get("local") else "cloud")))
        for field in ("language", "output_language", "fps", "hash_threshold", "min_scene_seconds", "slides_dpi", "youtube_mode"):
            if field in job["settings"]:
                text.append(self._t(actor, "parameter", label=self._t(actor, "parameter_" + field), value=job["settings"][field]))
        for field in ("cpu_user_seconds", "cpu_system_seconds", "busy_gpu_seconds"):
            text.append(self._t(actor, "job_measurement", label=self._t(actor, "metric_" + field), value=self._value(actor, job["resources"].get(field))))
        for file in self.store.files(target):
            text.append(self._t(actor, "file_row", name=file["original_name"][:200], bytes=file["bytes"], duration=self._value(actor, file["duration"]), pages=self._value(actor, file["pages"]), source=self._value(actor, file["source_message_id"])))
        if job["cancellation_actor_id"] is not None:
            text.append(self._t(actor, "cancel_actor", actor=job["cancellation_actor_id"]))
        keyboard = []
        if job["status"] not in TERMINAL_JOBS and job["status"] != "cancel_requested":
            keyboard.append([self._button(actor, "cancel", "admin_prompt", "cancel", target=target)])
        if job["status"] in TERMINAL_JOBS and not job["files_deleted"]:
            keyboard.append([self._button(actor, "purge", "admin_prompt", "purge", target=target)])
        self._send(actor, "\n\n".join(text), keyboard + [self._home_button(actor)])

    def _stats(self, actor: int, owner: int | None, page: int = 0):
        if owner is not None and self.store.get_user(owner) is None and not self.store.is_admin(owner):
            raise StoreError("not_found")
        stats = self.store.stats(owner, include_cost=True, actor_id=actor)
        text = [self._t(actor, "stats_title", owner=owner if owner is not None else self._t(actor, "everyone")), self._t(actor, "stats_counts", jobs=stats["jobs"], deliveries=stats["deliveries"], reused=stats["reused_deliveries"], attempts=stats["api_attempts"])]
        for status, count in stats["statuses"].items():
            text.append(f"{self._status(actor, status)}: {count}")
        for field in ("cpu_user_seconds", "cpu_system_seconds", "busy_gpu_seconds", "ru_maxrss_kib", "host_rss_peak_kib", "vram_peak_bytes", "queue_wait_seconds", "preparation_seconds", "processing_seconds", "render_seconds", "delivery_seconds", "prompt_tokens", "completion_tokens", "cache_hit", "cache_miss", "reasoning_tokens"):
            value = stats[field]
            measurement = value.get("known_total", value.get("known_peak"))
            text.append(self._t(actor, "measurement", label=self._t(actor, "metric_" + field), value=self._value(actor, measurement), unknown=value.get("unknown_works", value.get("unknown_calls", value.get("unknown_deliveries", 0)))))
        cost = stats["estimated_api_cost"]
        totals = ", ".join(f"{amount} {currency}" for currency, amount in cost["known_totals"].items()) or self._t(actor, "unknown")
        text.append(self._t(actor, "cost_notice", known=totals, incomplete=cost["incomplete_calls"]))
        if "quota" in stats:
            quota = stats["quota"]
            text.append(self._t(actor, "quota", used=quota["used"], remaining=self._value(actor, quota["remaining"]), next=self._value(actor, quota["next_available_at"])))
        where = " WHERE owner_id=?" if owner is not None else ""
        args = [owner] if owner is not None else []
        model_groups = self.store.db.execute("SELECT models,COUNT(*) AS jobs FROM jobs" + where + " GROUP BY models ORDER BY models LIMIT ? OFFSET ?", (*args, PAGE_SIZE + 1, page * PAGE_SIZE)).fetchall()
        text.append(self._t(actor, "models_title", page=page + 1))
        for group in model_groups[:PAGE_SIZE]:
            for category, model in json.loads(group["models"]).items():
                text.append(self._t(actor, "model_usage", category=category, name=model.get("name", self._t(actor, "unknown")), provider=(model.get("provider") or {}).get("name", self._t(actor, "unknown")), location=self._t(actor, "local" if model.get("local") else "cloud"), jobs=group["jobs"]))
        section = "stats" if owner is None else f"stats:{owner}"
        self._send(actor, "\n".join(text), self._pages(actor, section, page, len(model_groups) > PAGE_SIZE))

    def _storage(self, actor: int, page: int):
        rows = self.store.work_storage()
        visible = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        text = [self._t(actor, "storage_title", page=page + 1)]
        keyboard = []
        bound = {binding["work_uuid"] for binding in self.store.process_bindings()}
        for row in visible:
            status = row["job_status"] or row["status"]
            text.append(self._t(actor, "storage_row", id=row["id"], owner=row["owner_id"], kind=self._t(actor, "job" if row["job_status"] else "draft"), status=self._status(actor, status), bytes=self._value(actor, row["storage_bytes"]), deleted=self._t(actor, "yes" if row["files_deleted"] else "no")))
            terminal = row["job_status"] in TERMINAL_JOBS if row["job_status"] else status in ("closed", "failed", "ready", "deleted")
            if terminal and row["id"] not in bound and not row["files_deleted"]:
                keyboard.append([self._button(actor, "purge", "admin_prompt", "purge", target=row["id"])])
        if not visible:
            text.append(self._t(actor, "empty"))
        for label, path in (("native_metadata", self.settings.state_dir / "telegram-api"), ("native_files", self.settings.storage_dir / "telegram-api-files"), ("native_temp", self.settings.storage_dir / "telegram-api-tmp")):
            text.append(self._t(actor, "native_row", label=self._t(actor, label), bytes=self._value(actor, _directory_bytes(path))))
        text.append(self._t(actor, "native_notice"))
        self._send(actor, "\n\n".join(text), keyboard + self._pages(actor, "storage", page, len(rows) > (page + 1) * PAGE_SIZE))

    def _confirm(self, actor: int, action: str, target: str):
        if action == "ban":
            target_id = _user_id(target)
            if self.store.is_admin(target_id):
                raise StoreError("cannot_ban_admin")
            if not self.store.get_user(target_id):
                raise StoreError("not_found")
            text = self._t(actor, "confirm_ban", target=target_id)
        elif action == "cancel":
            job = self.store.get_job(_work_id(target))
            if not job:
                raise StoreError("not_found")
            if job["status"] in TERMINAL_JOBS or job["status"] == "cancel_requested":
                self._send(actor, self._t(actor, "nothing_cancelled"))
                return
            text = self._t(actor, "confirm_cancel", target=target)
        elif action == "purge":
            draft = self.store.get_draft(_work_id(target))
            if not draft:
                raise StoreError("not_found")
            job = self.store.get_job(target)
            if any(row["work_uuid"] == target for row in self.store.process_bindings()) or (job and job["status"] not in TERMINAL_JOBS) or (not job and draft["status"] not in ("closed", "failed", "ready", "deleted")):
                raise StoreError("work_active")
            count = self.store.db.execute("SELECT COUNT(*) FROM deliveries WHERE source_job_id=? AND owner_id<>?", (target, draft["owner_id"])).fetchone()[0]
            text = self._t(actor, "confirm_purge", target=target, count=count)
        else:
            raise StoreError("invalid_arguments")
        self._send(actor, text, [[self._button(actor, "confirm", "admin_confirm", action, target=target)], self._home_button(actor)])

    def _execute(self, actor: int, action: str, target: str):
        if action == "ban":
            target_id = _user_id(target)
            actions = self.store.ban_user(actor, target_id, utc_now())
            self._cancel_actions(actor, actions)
            self._send(actor, self._t(actor, "banned_notice", target=target_id))
        elif action == "cancel":
            job = self.store.get_job(_work_id(target))
            if not job:
                raise StoreError("not_found")
            actions = self.store.request_cancel(job["owner_id"], [target], actor_id=actor, reason="administrator", now_utc=utc_now())
            self._cancel_actions(actor, actions)
            self._send(actor, self._t(actor, "cancel_notice" if actions else "nothing_cancelled", target=target))
        elif action == "purge":
            result = self.store.purge(actor, _work_id(target))
            self._send(actor, self._t(actor, "purged", target=target, count=result["dependent_deliveries"]))
        else:
            raise StoreError("invalid_arguments")

    def _cancel_actions(self, actor: int, actions: list[dict]):
        for work in actions:
            owner, work_id = work["owner_id"], work["id"]
            stopping = work["status"] == "cancel_requested" or bool(work.get("close_requested"))
            if stopping:
                self.store.add_outbox("cancel_work", {"work_uuid": work_id, "owner_id": owner, "actor_id": actor, "reason": "administrator"}, dedupe_key=f"admin-stop:{work_id}")
            else:
                self._send(owner, self._t(owner, "cancelled", target=work_id, kind=self._t(owner, "draft" if work.get("kind") == "draft" else "job")), source=work.get("source_message_id"), dedupe=f"admin-cancellation:{work_id}", purpose="cancellation")

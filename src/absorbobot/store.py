"""Долговечное состояние, атомарные переходы и учёт заданий."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import shutil
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from .config import Settings

Job = dict[str, Any]
ACTIVE_DRAFTS = ("collecting", "preparing", "ready")
TERMINAL_JOBS = ("cancelled", "completed", "reused", "failed")
JOB_TRANSITIONS = {
    "queued": {"preparing", "cancelled", "failed"},
    "preparing": {"running", "awaiting_reuse_choice", "cancel_requested", "failed"},
    "running": {"completed", "cancel_requested", "failed"},
    "cancel_requested": {"cancelled", "failed"},
    "awaiting_reuse_choice": {"queued", "reused", "cancelled"},
}
DRAFT_TRANSITIONS = {
    "collecting": {"preparing", "ready", "closed", "failed"},
    "preparing": {"collecting", "ready", "closed", "failed"},
    "ready": {"collecting", "preparing", "submitted", "closed", "failed", "deleted"},
    "closed": {"deleted"},
    "failed": {"deleted"},
    "submitted": set(),
    "deleted": set(),
}
JSON_FIELDS = {
    "settings", "ui_state", "snapshot", "models", "source_message_ids", "metrics",
    "resources", "binding", "payload", "result", "event_start", "event_end",
    "cost_snapshot", "document",
}
_SECRET_FIELDS = {
    "api_key", "token", "password", "authorization", "secret", "secrets",
    "telegram_api_id", "telegram_api_hash", "absorbobot_token",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, label TEXT,
    approved INTEGER NOT NULL DEFAULT 0, language TEXT NOT NULL DEFAULT 'en',
    language_initialized INTEGER NOT NULL DEFAULT 0,
    settings TEXT NOT NULL DEFAULT '{}', output_language_override TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'collecting', source_message_id INTEGER,
    source_chat_id INTEGER, source_message_ids TEXT NOT NULL DEFAULT '[]',
    settings TEXT NOT NULL DEFAULT '{}', ui_state TEXT NOT NULL DEFAULT '{}',
    snapshot TEXT NOT NULL DEFAULT '{}', input_signature TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT,
    close_reason TEXT, failure_reason TEXT, close_requested INTEGER NOT NULL DEFAULT 0,
    cancellation_actor_id INTEGER, cancellation_reason TEXT,
    files_deleted INTEGER NOT NULL DEFAULT 0, files_deleted_at TEXT,
    reuse_source_job_id TEXT REFERENCES jobs(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS drafts_one_active ON drafts(owner_id)
    WHERE status IN ('collecting','preparing','ready');
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY REFERENCES drafts(id), owner_id INTEGER NOT NULL REFERENCES users(id),
    initial_accepted_at TEXT NOT NULL, accepted_at TEXT NOT NULL,
    quota_counted INTEGER NOT NULL DEFAULT 1, force_new INTEGER NOT NULL DEFAULT 0,
    reuse_source_job_id TEXT REFERENCES jobs(id), status TEXT NOT NULL,
    source_message_id INTEGER, source_chat_id INTEGER,
    source_message_ids TEXT NOT NULL DEFAULT '[]', settings TEXT NOT NULL,
    snapshot TEXT NOT NULL, models TEXT NOT NULL DEFAULT '{}', input_signature TEXT NOT NULL,
    engine_version TEXT, engine_commit TEXT, started_at TEXT, completed_at TEXT,
    cancellation_actor_id INTEGER, cancellation_reason TEXT, failure_reason TEXT,
    metrics TEXT NOT NULL DEFAULT '{}', resources TEXT NOT NULL DEFAULT '{}',
    files_deleted INTEGER NOT NULL DEFAULT 0, files_deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_quota ON jobs(owner_id,quota_counted,accepted_at);
CREATE INDEX IF NOT EXISTS jobs_fifo ON jobs(status,accepted_at,id);
CREATE INDEX IF NOT EXISTS jobs_reuse ON jobs(input_signature,status);
CREATE TABLE IF NOT EXISTS work_files (
    id INTEGER PRIMARY KEY, work_uuid TEXT NOT NULL REFERENCES drafts(id),
    role TEXT NOT NULL, position INTEGER NOT NULL, original_name TEXT NOT NULL,
    sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, duration REAL, pages INTEGER,
    relative_path TEXT NOT NULL, source_message_id INTEGER,
    removed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    UNIQUE(work_uuid,position), UNIQUE(work_uuid,relative_path)
);
CREATE UNIQUE INDEX IF NOT EXISTS work_one_media ON work_files(work_uuid)
    WHERE role='media' AND removed=0;
CREATE TABLE IF NOT EXISTS tariff_snapshots (
    digest TEXT PRIMARY KEY, document TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS tariffs_no_update BEFORE UPDATE ON tariff_snapshots
    BEGIN SELECT RAISE(ABORT,'immutable tariff'); END;
CREATE TRIGGER IF NOT EXISTS tariffs_no_delete BEFORE DELETE ON tariff_snapshots
    BEGIN SELECT RAISE(ABORT,'immutable tariff'); END;
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
    run_id TEXT NOT NULL, request_id TEXT NOT NULL, started_at TEXT NOT NULL,
    ended_at TEXT, event_start TEXT NOT NULL, event_end TEXT,
    cost_snapshot TEXT NOT NULL, tariff_digest TEXT REFERENCES tariff_snapshots(digest),
    UNIQUE(run_id,request_id)
);
CREATE INDEX IF NOT EXISTS api_calls_attempt ON api_calls(run_id,request_id);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
    source_job_id TEXT NOT NULL REFERENCES jobs(id), work_uuid TEXT REFERENCES drafts(id),
    created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    reused INTEGER NOT NULL, metrics TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id INTEGER PRIMARY KEY, processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
    dedupe_key TEXT UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
    available_at TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS durable_tokens (
    token TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL, target TEXT NOT NULL, payload TEXT NOT NULL,
    created_at TEXT NOT NULL, consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS process_bindings (
    work_uuid TEXT PRIMARY KEY REFERENCES drafts(id), binding TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS store_settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
INSERT OR IGNORE INTO store_settings(key,value) VALUES ('offset','0'),('paused','false');
PRAGMA user_version=1;
"""


class StoreError(Exception):
    def __init__(self, code: str, **details: Any):
        self.code = code
        self.details = details
        super().__init__(code)


class QuotaError(StoreError):
    def __init__(self, next_available_at: str | None):
        self.next_available_at = next_available_at
        super().__init__("quota_exceeded", next_available_at=next_available_at)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | str | None = None) -> str:
    if value is None:
        value = utc_now()
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StoreError("invalid_timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StoreError("invalid_timestamp")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise StoreError("invalid_json")
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StoreError("invalid_json")
        return value
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise StoreError("invalid_json")
            lowered = key.lower()
            if lowered in _SECRET_FIELDS or lowered.endswith("_api_key"):
                raise StoreError("secret_in_record")
            result[key] = _plain(item)
        return result
    raise StoreError("invalid_json")


def _json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in JSON_FIELDS & result.keys():
        if result[key] is not None:
            result[key] = json.loads(result[key])
    for key in ("approved", "language_initialized", "quota_counted", "force_new", "files_deleted", "close_requested", "removed", "reused"):
        if key in result:
            result[key] = bool(result[key])
    return result


def _uuid(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise StoreError("invalid_work_uuid") from exc
    return value


def _preparation(draft: dict[str, Any]) -> dict[str, Any] | None:
    """Собственные измеренные ресурсы подготовки входа из состояния черновика."""
    state = draft.get("ui_state")
    value = state.get("preparation") if isinstance(state, dict) else None
    return value if isinstance(value, dict) else None


def _preparation_resources(preparation: dict[str, Any] | None) -> dict[str, Any]:
    """Заморозить отчёты операций подготовки вместе со сводом в ресурсах задания."""
    if not preparation:
        return {}
    reports = [operation["resources"] for operation in preparation.get("operations", [])
               if isinstance(operation, dict) and isinstance(operation.get("resources"), dict)]
    values = preparation.get("resources")
    result = dict(values) if isinstance(values, dict) else {}
    result["phase_reports"] = reports
    return result


def _preparation_seconds(preparation: dict[str, Any] | None) -> float | None:
    """Известная кумулятивная длительность подготовки входа или null."""
    value = preparation.get("wall_seconds") if isinstance(preparation, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _measurement(value: Any) -> float | int | None:
    """Конечное неотрицательное измерение или None, без подмены нулём."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        state = Path(settings.state_dir).expanduser().resolve()
        storage = Path(settings.storage_dir).expanduser().resolve()
        mount = Path(settings.backing_mount).expanduser().resolve()
        if state.is_relative_to(storage) or state.is_relative_to(mount):
            raise StoreError("state_not_on_ssd")
        import psutil

        partitions = [p for p in psutil.disk_partitions(all=True) if state.is_relative_to(Path(p.mountpoint))]
        if partitions:
            partition = max(partitions, key=lambda p: len(Path(p.mountpoint).parts))
            if partition.fstype.lower() in {"ntfs", "ntfs3", "fuseblk"}:
                raise StoreError("state_not_on_ssd")
        self._lock = threading.RLock()
        self._depth = 0
        try:
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(state / "absorbobot.sqlite3", timeout=30, isolation_level=None, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            if self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise StoreError("database_wal_unavailable")
            self.db.execute("PRAGMA synchronous=FULL")
            if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise StoreError("database_schema_unknown")
            self.db.executescript(_SCHEMA)
        except (OSError, sqlite3.Error, StoreError) as exc:
            if hasattr(self, "db"):
                self.db.close()
            if isinstance(exc, StoreError):
                raise
            raise StoreError("database_unavailable") from exc

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            nested = self._depth > 0
            savepoint = f"nested_{self._depth}"
            self.db.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self.db
            except BaseException:
                if nested:
                    self.db.execute(f"ROLLBACK TO {savepoint}")
                    self.db.execute(f"RELEASE {savepoint}")
                else:
                    self.db.rollback()
                raise
            else:
                self.db.execute(f"RELEASE {savepoint}" if nested else "COMMIT")
            finally:
                self._depth -= 1

    def _one(self, query: str, args: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            return _record(self.db.execute(query, args).fetchone())

    def _many(self, query: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [_record(row) for row in self.db.execute(query, args).fetchall()]

    def is_admin(self, owner_id: int) -> bool:
        return owner_id in self.settings.admin_ids

    def authorized(self, owner_id: int) -> bool:
        if self.is_admin(owner_id):
            return True
        user = self.get_user(owner_id)
        return bool(user and user["approved"])

    def _auth(self, owner_id: int) -> None:
        if not self.authorized(owner_id):
            raise StoreError("not_authorized")

    def _admin(self, actor_id: int) -> None:
        if not self.is_admin(actor_id):
            raise StoreError("admin_required")

    def _owned(self, table: str, owner_id: int, work_uuid: str) -> dict[str, Any]:
        self._auth(owner_id)
        row = self._one(f"SELECT * FROM {table} WHERE id=?", (work_uuid,))
        if row is None:
            raise StoreError("not_found")
        if row["owner_id"] != owner_id:
            raise StoreError("not_owner")
        return row

    def get_user(self, owner_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE id=?", (owner_id,))

    def upsert_user(self, owner_id: int, username: str | None = None, full_name: str | None = None, language: str = "en") -> dict:
        if language not in ("ru", "en"):
            raise StoreError("invalid_language")
        now = _utc()
        with self.transaction():
            self._auth(owner_id)
            self.db.execute(
                "INSERT INTO users(id,username,full_name,language,language_initialized,created_at,updated_at) VALUES (?,?,?,?,1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET username=excluded.username,full_name=excluded.full_name,language=CASE WHEN users.language_initialized=0 THEN excluded.language ELSE users.language END,language_initialized=1,updated_at=excluded.updated_at",
                (owner_id, username, full_name, language, now, now),
            )
            return self.get_user(owner_id)

    def update_user(self, owner_id: int, *, language: str | None = None, settings: dict | None = None, output_language_override: Any = ...) -> dict:
        with self.transaction():
            self._auth(owner_id)
            if self.get_user(owner_id) is None:
                self.upsert_user(owner_id)
            fields: dict[str, Any] = {"updated_at": _utc()}
            if language is not None:
                if language not in ("ru", "en"):
                    raise StoreError("invalid_language")
                fields["language"] = language
                fields["language_initialized"] = 1
            if settings is not None:
                fields["settings"] = _json(settings)
            if output_language_override is not ...:
                if output_language_override not in (None, "ru", "en"):
                    raise StoreError("invalid_language")
                fields["output_language_override"] = output_language_override
            self._update("users", owner_id, fields)
            return self.get_user(owner_id)

    def allow_user(self, actor_id: int, target_id: int, label: str | None = None) -> dict:
        with self.transaction():
            self._admin(actor_id)
            now = _utc()
            self.db.execute(
                "INSERT INTO users(id,approved,label,created_at,updated_at) VALUES (?,1,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET approved=1,label=excluded.label,updated_at=excluded.updated_at",
                (target_id, label, now, now),
            )
            return self.get_user(target_id)

    def ban_user(self, actor_id: int, target_id: int, now_utc: datetime) -> list[dict]:
        with self.transaction():
            self._admin(actor_id)
            if self.is_admin(target_id):
                raise StoreError("cannot_ban_admin")
            if self.get_user(target_id) is None:
                raise StoreError("not_found")
            self._update("users", target_id, {"approved": 0, "updated_at": _utc(now_utc)})
            actions = self.request_cancel(target_id, actor_id=actor_id, reason="administrator", now_utc=now_utc)
            draft = self._one("SELECT * FROM drafts WHERE owner_id=? AND status IN ('collecting','preparing','ready')", (target_id,))
            if draft:
                action = self.close_draft(target_id, draft["id"], reason="administrator", actor_id=actor_id)
                action["kind"] = "draft"
                actions.append(action)
            return actions

    def _update(self, table: str, row_id: Any, fields: dict[str, Any], *, pk: str = "id") -> None:
        if fields:
            self.db.execute(f"UPDATE {table} SET " + ",".join(f"{key}=?" for key in fields) + f" WHERE {pk}=?", (*fields.values(), row_id))

    def create_draft(self, owner_id: int, source_message_id: int | None = None, settings: dict | None = None, now_utc: datetime | None = None) -> dict:
        with self.transaction():
            self._auth(owner_id)
            if self.get_user(owner_id) is None:
                self.upsert_user(owner_id)
            if self.active_draft(owner_id):
                raise StoreError("active_draft_exists")
            work_uuid, now = str(uuid4()), _utc(now_utc)
            chosen = settings if settings is not None else self.get_user(owner_id)["settings"]
            self.db.execute(
                "INSERT INTO drafts(id,owner_id,source_message_id,source_message_ids,settings,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (work_uuid, owner_id, source_message_id, _json([source_message_id] if source_message_id is not None else []), _json(chosen), now, now),
            )
            return self.get_draft(work_uuid)

    def active_draft(self, owner_id: int) -> dict | None:
        self._auth(owner_id)
        return self._one("SELECT * FROM drafts WHERE owner_id=? AND status IN ('collecting','preparing','ready')", (owner_id,))

    def get_draft(self, work_uuid: str, owner_id: int | None = None) -> dict | None:
        if owner_id is not None:
            self._auth(owner_id)
        draft = self._one("SELECT * FROM drafts WHERE id=?", (work_uuid,))
        if draft and owner_id is not None and draft["owner_id"] != owner_id:
            raise StoreError("not_owner")
        return draft

    def update_draft(self, owner_id: int, draft_id: str, **fields: Any) -> dict:
        allowed = {"settings", "ui_state", "status", "source_message_id", "source_chat_id", "source_message_ids", "input_signature", "snapshot", "failure_reason"}
        if fields.keys() - allowed:
            raise StoreError("invalid_field")
        with self.transaction():
            draft = self._owned("drafts", owner_id, draft_id)
            if draft["status"] not in ACTIVE_DRAFTS or draft["close_requested"]:
                raise StoreError("draft_consumed")
            status = fields.get("status", draft["status"])
            if status != draft["status"] and status not in DRAFT_TRANSITIONS[draft["status"]]:
                raise StoreError("invalid_transition")
            if status == "submitted" or status == "deleted":
                raise StoreError("invalid_transition")
            if status in ("closed", "failed") and self._bound(draft_id):
                raise StoreError("process_bound")
            if draft["status"] == "preparing" and status != "preparing" and self._bound(draft_id):
                raise StoreError("process_bound")
            if status == "preparing" and draft["status"] != "preparing":
                if self.paused():
                    raise StoreError("paused")
                if self.process_bindings() or self._one("SELECT id FROM jobs WHERE status IN ('preparing','running','cancel_requested') LIMIT 1") or self._one("SELECT id FROM drafts WHERE status='preparing' LIMIT 1"):
                    raise StoreError("execution_busy")
            if "settings" in fields and "input_signature" not in fields:
                fields.update(input_signature=None, snapshot={})
                if status == "ready":
                    fields["status"] = status = "collecting"
            updates = {key: _json(value) if key in JSON_FIELDS else value for key, value in fields.items()}
            updates["updated_at"] = _utc()
            if status in ("closed", "failed"):
                updates["closed_at"] = updates["updated_at"]
            self._update("drafts", draft_id, updates)
            return self.get_draft(draft_id)

    def close_draft(self, owner_id: int, draft_id: str, reason: str = "closed", actor_id: int | None = None) -> dict:
        actor = owner_id if actor_id is None else actor_id
        with self.transaction():
            if actor != owner_id:
                self._admin(actor)
                draft = self.get_draft(draft_id)
                if not draft or draft["owner_id"] != owner_id:
                    raise StoreError("not_owner")
            else:
                draft = self._owned("drafts", owner_id, draft_id)
            if draft["status"] not in ACTIVE_DRAFTS:
                return draft
            now = _utc()
            fields = {"updated_at": now, "cancellation_actor_id": actor, "cancellation_reason": reason}
            if self._bound(draft_id) or draft["status"] == "preparing":
                fields.update(close_requested=1, close_reason=reason)
            else:
                fields.update(status="closed", closed_at=now, close_reason=reason)
            self._update("drafts", draft_id, fields)
            return self.get_draft(draft_id)

    def add_file(self, owner_id: int, draft_id: str, *, role: str, original_name: str, relative_path: str, sha256: str, bytes: int, duration: float | None = None, pages: int | None = None, source_message_id: int | None = None) -> dict:
        if role not in ("media", "material") or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise StoreError("invalid_file")
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "input":
            raise StoreError("unsafe_path")
        if isinstance(bytes, bool) or not isinstance(bytes, int) or bytes < 0:
            raise StoreError("invalid_file")
        if duration is not None and (not math.isfinite(duration) or duration < 0):
            raise StoreError("invalid_file")
        if pages is not None and (isinstance(pages, bool) or not isinstance(pages, int) or pages < 0):
            raise StoreError("invalid_file")
        with self.transaction():
            draft = self._owned("drafts", owner_id, draft_id)
            if draft["status"] not in ("collecting", "preparing", "ready") or draft["close_requested"]:
                raise StoreError("draft_consumed")
            self._contained(draft_id, relative_path)
            if role == "media" and self._one("SELECT id FROM work_files WHERE work_uuid=? AND role='media' AND removed=0", (draft_id,)):
                raise StoreError("media_exists")
            position = self.db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM work_files WHERE work_uuid=?", (draft_id,)).fetchone()[0]
            cursor = self.db.execute(
                "INSERT INTO work_files(work_uuid,role,position,original_name,sha256,bytes,duration,pages,relative_path,source_message_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (draft_id, role, position, original_name, sha256, bytes, duration, pages, str(path), source_message_id, _utc()),
            )
            updates = {"input_signature": None, "snapshot": "{}", "updated_at": _utc()}
            if draft["status"] == "ready":
                updates["status"] = "collecting"
            self._update("drafts", draft_id, updates)
            if source_message_id is not None and source_message_id not in draft["source_message_ids"]:
                self._update("drafts", draft_id, {"source_message_ids": _json([*draft["source_message_ids"], source_message_id])})
            return self._one("SELECT * FROM work_files WHERE id=?", (cursor.lastrowid,))

    def files(self, work_uuid: str) -> list[dict]:
        return self._many("SELECT * FROM work_files WHERE work_uuid=? ORDER BY position,id", (work_uuid,))

    def remove_file(self, owner_id: int, draft_id: str, file_id: int) -> dict:
        with self.transaction():
            draft = self._owned("drafts", owner_id, draft_id)
            if draft["status"] not in ("collecting", "ready"):
                raise StoreError("draft_consumed")
            file = self._one("SELECT * FROM work_files WHERE id=? AND work_uuid=?", (file_id, draft_id))
            if not file:
                raise StoreError("not_found")
            if file["role"] == "media":
                raise StoreError("replace_requires_new_draft")
            self._update("work_files", file_id, {"removed": 1})
            self._update("drafts", draft_id, {"status": "collecting", "input_signature": None, "snapshot": "{}"})
            return self._one("SELECT * FROM work_files WHERE id=?", (file_id,))

    def quota(self, owner_id: int, now_utc: datetime, exclude_job_id: str | None = None, *, actor_id: int | None = None) -> dict:
        if actor_id is not None:
            self._admin(actor_id)
        else:
            self._auth(owner_id)
        now = datetime.fromisoformat(_utc(now_utc))
        boundary = _utc(now - timedelta(seconds=self.settings.quota_window_seconds))
        query = "SELECT accepted_at FROM jobs WHERE owner_id=? AND quota_counted=1 AND accepted_at>?"
        args: tuple = (owner_id, boundary)
        if exclude_job_id is not None:
            query += " AND id<>?"
            args += (exclude_job_id,)
        rows = self._many(query + " ORDER BY accepted_at DESC,id DESC", args)
        used = len(rows)
        next_at = None
        if used >= self.settings.quota_jobs and not self.is_admin(owner_id):
            next_at = _utc(datetime.fromisoformat(rows[self.settings.quota_jobs - 1]["accepted_at"]) + timedelta(seconds=self.settings.quota_window_seconds))
        return {"used": used, "remaining": None if self.is_admin(owner_id) else max(0, self.settings.quota_jobs - used), "next_available_at": next_at}

    def _check_quota(self, owner_id: int, now_utc: datetime, exclude_job_id: str | None = None) -> None:
        quota = self.quota(owner_id, now_utc, exclude_job_id)
        if not self.is_admin(owner_id) and quota["remaining"] == 0:
            raise QuotaError(quota["next_available_at"])

    def _ready(self, owner_id: int, draft_id: str) -> dict:
        draft = self._owned("drafts", owner_id, draft_id)
        if draft["status"] == "submitted" or self.get_job(draft_id):
            raise StoreError("draft_consumed")
        if draft["status"] != "ready" or draft["close_requested"] or draft["files_deleted"]:
            raise StoreError("draft_not_ready")
        if not draft["input_signature"] or not draft["snapshot"]:
            raise StoreError("draft_incomplete")
        media = [file for file in self.files(draft_id) if file["role"] == "media" and not file["removed"]]
        if len(media) != 1:
            raise StoreError("draft_incomplete")
        if self._bound(draft_id):
            raise StoreError("process_bound")
        return draft

    def _require_keys(self, snapshot: dict) -> None:
        from .config import ConfigError, require_key_names

        try:
            require_key_names(snapshot)
        except ConfigError as exc:
            models = snapshot.get("models", {})
            providers = [model.get("provider", {}) for model in models.values() if isinstance(model, dict)] if isinstance(models, dict) else []
            names = sorted({provider["api_key_env"] for provider in providers if isinstance(provider, dict) and isinstance(provider.get("api_key_env"), str) and provider["api_key_env"]})
            raise StoreError("model_keys_unavailable", key_names=names) from exc

    def enqueue(self, owner_id: int, draft_id: str, now_utc: datetime, *, force_new: bool) -> Job:
        with self.transaction():
            draft = self._ready(owner_id, draft_id)
            self._require_keys(draft["snapshot"])
            self._check_quota(owner_id, now_utc)
            now = _utc(now_utc)
            info = draft["snapshot"].get("version_info", {})
            # Собственная подготовка входа замораживается вместе с принятым снимком.
            preparation = _preparation(draft)
            metrics = {"preparation": preparation} if preparation is not None else {}
            seconds = _preparation_seconds(preparation)
            if seconds is not None:
                metrics["preparation_seconds"] = seconds
            self.db.execute(
                "INSERT INTO jobs(id,owner_id,initial_accepted_at,accepted_at,force_new,status,source_message_id,source_chat_id,source_message_ids,settings,snapshot,models,input_signature,engine_version,engine_commit,metrics,resources) VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?)",
                (draft_id, owner_id, now, now, int(force_new), draft["source_message_id"], draft["source_chat_id"], _json(draft["source_message_ids"]), _json(draft["settings"]), _json(draft["snapshot"]), _json(draft["snapshot"].get("models", {})), draft["input_signature"], info.get("version"), info.get("commit"), _json(metrics), _json(_preparation_resources(preparation))),
            )
            self._update("drafts", draft_id, {"status": "submitted", "updated_at": now})
            return self.get_job(draft_id)

    def get_job(self, job_id: str, owner_id: int | None = None) -> Job | None:
        if owner_id is not None:
            self._auth(owner_id)
        job = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job and owner_id is not None and job["owner_id"] != owner_id:
            raise StoreError("not_owner")
        return job

    def jobs(self, owner_id: int | None = None, offset: int = 0, limit: int = 10, statuses: list[str] | None = None) -> list[Job]:
        clauses, args = [], []
        if owner_id is not None:
            self._auth(owner_id)
            clauses.append("owner_id=?")
            args.append(owner_id)
        if statuses is not None:
            if not statuses:
                return []
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            args.extend(statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self._many("SELECT * FROM jobs" + where + " ORDER BY accepted_at DESC,id DESC LIMIT ? OFFSET ?", tuple([*args, max(0, limit), max(0, offset)]))

    def queue_position(self, job_id: str) -> int | None:
        job = self.get_job(job_id)
        if not job or job["status"] != "queued":
            return None
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND (accepted_at<? OR (accepted_at=? AND id<=?))", (job["accepted_at"], job["accepted_at"], job_id)).fetchone()[0]

    def claim_next(self, now_utc: datetime) -> Job | None:
        with self.transaction():
            if self.paused() or self.process_bindings():
                return None
            if self._one("SELECT id FROM jobs WHERE status IN ('preparing','running','cancel_requested') LIMIT 1"):
                return None
            if self._one("SELECT id FROM drafts WHERE status='preparing' LIMIT 1"):
                return None
            for job in self._many("SELECT * FROM jobs WHERE status='queued' ORDER BY accepted_at,id"):
                if not self.authorized(job["owner_id"]):
                    self._update("jobs", job["id"], {"status": "cancelled", "cancellation_reason": "authorization_revoked", "completed_at": _utc(now_utc)})
                    continue
                self._update("jobs", job["id"], {"status": "preparing", "started_at": _utc(now_utc)})
                return self.get_job(job["id"])
            return None

    def update_job(self, job_id: str, **fields: Any) -> Job:
        allowed = {"status", "failure_reason", "started_at", "completed_at", "engine_version", "engine_commit", "metrics", "resources"}
        if fields.keys() - allowed:
            raise StoreError("invalid_field")
        with self.transaction():
            job = self.get_job(job_id)
            if not job:
                raise StoreError("not_found")
            status = fields.get("status", job["status"])
            if status != job["status"] and status not in JOB_TRANSITIONS.get(job["status"], set()):
                raise StoreError("invalid_transition")
            if status in ("awaiting_reuse_choice", "reused", "queued") and status != job["status"]:
                raise StoreError("dedicated_transition_required")
            if job["status"] == "queued" and status == "preparing":
                raise StoreError("dedicated_transition_required")
            if status == "running":
                self._auth(job["owner_id"])
            if status in TERMINAL_JOBS and self._bound(job_id):
                raise StoreError("process_bound")
            if job["status"] in TERMINAL_JOBS and fields.keys() - {"status", "metrics", "resources"}:
                raise StoreError("terminal_job")
            updates = {key: _json(value) if key in JSON_FIELDS else _utc(value) if key.endswith("_at") and value is not None else value for key, value in fields.items()}
            if status in TERMINAL_JOBS and job["status"] not in TERMINAL_JOBS:
                updates.setdefault("completed_at", _utc())
            self._update("jobs", job_id, updates)
            return self.get_job(job_id)

    def _work_path(self, work_uuid: str) -> Path:
        _uuid(work_uuid)
        storage = Path(self.settings.storage_dir).resolve()
        jobs = storage / "jobs"
        tree = jobs / work_uuid
        if jobs.is_symlink() or tree.is_symlink() or not tree.resolve().is_relative_to(storage):
            raise StoreError("unsafe_path")
        return tree

    def _contained(self, work_uuid: str, relative_path: str) -> Path:
        tree = self._work_path(work_uuid)
        path = tree / relative_path
        if not path.resolve().is_relative_to(tree.resolve()):
            raise StoreError("unsafe_path")
        return path

    def _reusable(self, source_job_id: str, signature: str | None = None) -> Job | None:
        job = self.get_job(source_job_id)
        if not job or job["status"] != "completed" or job["files_deleted"] or not self.authorized(job["owner_id"]):
            return None
        if signature is not None and job["input_signature"] != signature:
            return None
        try:
            for filename in ("notes.md", "notes.html", "notes.pdf", "notes.zip"):
                path = self._contained(source_job_id, f"output/{filename}")
                if path.is_symlink() or not path.is_file():
                    return None
        except (StoreError, OSError):
            return None
        return job

    def find_reuse(self, input_signature: str, *, exclude_job_id: str | None = None) -> Job | None:
        with self.transaction():
            candidates = self._many("SELECT id FROM jobs WHERE input_signature=? AND status='completed' AND files_deleted=0 ORDER BY completed_at DESC,accepted_at DESC,id DESC", (input_signature,))
            for candidate in candidates:
                if candidate["id"] != exclude_job_id:
                    job = self._reusable(candidate["id"], input_signature)
                    if job:
                        return job
            return None

    def await_reuse(self, job_id: str, source_job_id: str, now_utc: datetime) -> Job:
        with self.transaction():
            job = self.get_job(job_id)
            if not job:
                raise StoreError("not_found")
            self._auth(job["owner_id"])
            if job["status"] != "preparing" or job["force_new"] or job_id == source_job_id:
                raise StoreError("invalid_transition")
            if self._bound(job_id):
                raise StoreError("process_bound")
            if not self._reusable(source_job_id, job["input_signature"]):
                raise StoreError("reuse_unavailable")
            self._update("jobs", job_id, {"status": "awaiting_reuse_choice", "reuse_source_job_id": source_job_id})
            return self.get_job(job_id)

    def confirm_late_reuse(self, owner_id: int, job_id: str, action: Literal["reuse", "force_new"], now_utc: datetime) -> Job:
        with self.transaction():
            job = self._owned("jobs", owner_id, job_id)
            if job["status"] != "awaiting_reuse_choice":
                raise StoreError("choice_consumed")
            if self._bound(job_id):
                raise StoreError("process_bound")
            if action == "reuse":
                source = self._reusable(job["reuse_source_job_id"], job["input_signature"])
                if not source:
                    raise StoreError("reuse_unavailable")
                self.record_delivery(owner_id, source["id"], work_uuid=job_id, now_utc=now_utc)
                self._update("jobs", job_id, {"status": "reused", "quota_counted": 0, "completed_at": _utc(now_utc)})
            elif action == "force_new":
                self._require_keys(job["snapshot"])
                self._check_quota(owner_id, now_utc, exclude_job_id=job_id)
                self._update("jobs", job_id, {"status": "queued", "quota_counted": 1, "force_new": 1, "accepted_at": _utc(now_utc)})
            else:
                raise StoreError("invalid_action")
            return self.get_job(job_id)

    def reuse_draft(self, owner_id: int, draft_id: str, source_job_id: str, now_utc: datetime) -> dict:
        with self.transaction():
            draft = self._ready(owner_id, draft_id)
            if not self._reusable(source_job_id, draft["input_signature"]):
                raise StoreError("reuse_unavailable")
            delivery = self.record_delivery(owner_id, source_job_id, work_uuid=draft_id, now_utc=now_utc)
            self._update("drafts", draft_id, {"status": "closed", "close_reason": "reused", "reuse_source_job_id": source_job_id, "closed_at": _utc(now_utc), "updated_at": _utc(now_utc)})
            return delivery

    def record_delivery(self, owner_id: int, source_job_id: str, *, work_uuid: str | None = None, now_utc: datetime | None = None) -> dict:
        with self.transaction():
            self._auth(owner_id)
            if self.get_user(owner_id) is None:
                self.upsert_user(owner_id)
            source = self._reusable(source_job_id)
            if not source:
                raise StoreError("reuse_unavailable")
            if work_uuid is not None:
                draft = self._owned("drafts", owner_id, work_uuid)
                if draft["input_signature"] != source["input_signature"]:
                    raise StoreError("reuse_unavailable")
            cursor = self.db.execute("INSERT INTO deliveries(owner_id,source_job_id,work_uuid,created_at,reused) VALUES (?,?,?,?,?)", (owner_id, source_job_id, work_uuid, _utc(now_utc), int(work_uuid != source_job_id or owner_id != source["owner_id"])))
            return self._one("SELECT * FROM deliveries WHERE id=?", (cursor.lastrowid,))

    def deliveries(self, owner_id: int, offset: int = 0, limit: int = 10) -> list[dict]:
        self._auth(owner_id)
        return self._many("SELECT deliveries.*,jobs.files_deleted, jobs.engine_version,jobs.engine_commit FROM deliveries JOIN jobs ON jobs.id=deliveries.source_job_id WHERE deliveries.owner_id=? ORDER BY deliveries.id DESC LIMIT ? OFFSET ?", (owner_id, max(0, limit), max(0, offset)))

    def request_cancel(self, owner_id: int, job_ids: list[str] | None = None, *, actor_id: int | None = None, reason: str = "user", now_utc: datetime | None = None) -> list[Job]:
        actor = owner_id if actor_id is None else actor_id
        with self.transaction():
            self._auth(actor)
            if actor != owner_id:
                self._admin(actor)
            jobs = self._many("SELECT * FROM jobs WHERE owner_id=? ORDER BY accepted_at,id", (owner_id,))
            if job_ids is not None:
                wanted = set(job_ids)
                if wanted - {job["id"] for job in jobs}:
                    raise StoreError("not_owner")
                jobs = [job for job in jobs if job["id"] in wanted]
            changed = []
            for job in jobs:
                if job["status"] in TERMINAL_JOBS or job["status"] == "cancel_requested":
                    continue
                fields = {"cancellation_actor_id": actor, "cancellation_reason": reason}
                if job["status"] in ("queued", "awaiting_reuse_choice"):
                    fields.update(status="cancelled", completed_at=_utc(now_utc))
                else:
                    fields.update(status="cancel_requested")
                self._update("jobs", job["id"], fields)
                changed.append(self.get_job(job["id"]))
            return changed

    def paused(self) -> bool:
        return self._one("SELECT value FROM store_settings WHERE key='paused'")["value"] == "true"

    def set_paused(self, actor_id: int, value: bool) -> None:
        with self.transaction():
            self._admin(actor_id)
            self.db.execute("UPDATE store_settings SET value=? WHERE key='paused'", ("true" if value else "false",))

    def _bound(self, work_uuid: str) -> bool:
        return self._one("SELECT work_uuid FROM process_bindings WHERE work_uuid=?", (work_uuid,)) is not None

    def bind_process(self, work_uuid: str, binding: dict | None) -> None:
        with self.transaction():
            if binding is None:
                self.db.execute("DELETE FROM process_bindings WHERE work_uuid=?", (work_uuid,))
                draft = self.get_draft(work_uuid)
                if draft and draft["status"] == "preparing" and draft["close_requested"]:
                    now = _utc()
                    self._update("drafts", work_uuid, {"status": "closed", "closed_at": now, "updated_at": now})
                return
            existing = self._one("SELECT * FROM process_bindings WHERE work_uuid=?", (work_uuid,))
            if existing is not None:
                previous = existing["binding"]
                identity = ("launcher_pid", "launcher_start_time", "phase_id")
                if (not binding or any(key not in previous or key not in binding for key in identity)
                        or any(binding.get(key) != value for key, value in previous.items())):
                    raise StoreError("invalid_binding")
                self.db.execute("UPDATE process_bindings SET binding=? WHERE work_uuid=?", (_json(binding), work_uuid))
                return
            draft = self.get_draft(work_uuid)
            job = self.get_job(work_uuid)
            if not draft or not self.authorized(draft["owner_id"]):
                raise StoreError("not_authorized")
            if (job and job["status"] not in ("preparing", "running")) or (not job and (draft["status"] != "preparing" or draft["close_requested"])):
                raise StoreError("invalid_transition")
            if not binding:
                raise StoreError("invalid_binding")
            if self._one("SELECT work_uuid FROM process_bindings LIMIT 1"):
                raise StoreError("execution_busy")
            if job:
                if self._one("SELECT id FROM drafts WHERE status='preparing' LIMIT 1"):
                    raise StoreError("execution_busy")
            elif self._one("SELECT id FROM jobs WHERE status IN ('preparing','running','cancel_requested') LIMIT 1"):
                raise StoreError("execution_busy")
            self.db.execute("INSERT INTO process_bindings(work_uuid,binding,created_at) VALUES (?,?,?)", (work_uuid, _json(binding), _utc()))

    def process_bindings(self) -> list[dict]:
        return self._many("SELECT * FROM process_bindings ORDER BY created_at,work_uuid")

    def recover_interrupted(self, now_utc: datetime) -> list[dict]:
        with self.transaction():
            recovered = []
            for job in self._many("SELECT * FROM jobs WHERE status IN ('preparing','running','cancel_requested') ORDER BY accepted_at,id"):
                resources = dict(job["resources"])
                resources["completeness"] = "partial"
                self._update("jobs", job["id"], {"status": "failed", "failure_reason": "interrupted", "completed_at": _utc(now_utc), "resources": _json(resources)})
                result = self.get_job(job["id"])
                result["kind"] = "job"
                recovered.append(result)
            for draft in self._many("SELECT * FROM drafts WHERE status='preparing'"):
                self._update("drafts", draft["id"], {"status": "failed", "failure_reason": "interrupted", "closed_at": _utc(now_utc), "updated_at": _utc(now_utc)})
                result = self.get_draft(draft["id"])
                result["kind"] = "draft"
                recovered.append(result)
            return recovered

    def purge(self, actor_id: int, work_uuid: str) -> dict:
        with self.transaction():
            self._admin(actor_id)
            draft = self.get_draft(_uuid(work_uuid))
            if not draft:
                raise StoreError("not_found")
            job = self.get_job(work_uuid)
            if self._bound(work_uuid) or (job and job["status"] not in TERMINAL_JOBS) or (not job and draft["status"] not in ("closed", "failed", "ready", "deleted")):
                raise StoreError("work_active")
            path = self._work_path(work_uuid)
            if not shutil.rmtree.avoids_symlink_attacks:
                raise StoreError("safe_purge_unavailable")
            dependent = self.db.execute("SELECT COUNT(*) FROM deliveries WHERE source_job_id=? AND owner_id<>?", (work_uuid, draft["owner_id"])).fetchone()[0]
            if path.exists():
                try:
                    shutil.rmtree(path)
                except OSError as exc:
                    raise StoreError("purge_failed") from exc
            now = _utc()
            self._update("drafts", work_uuid, {"files_deleted": 1, "files_deleted_at": now, "updated_at": now, **({"status": "deleted"} if not job else {})})
            if job:
                self._update("jobs", work_uuid, {"files_deleted": 1, "files_deleted_at": now})
            return {"work_uuid": work_uuid, "files_deleted": True, "dependent_deliveries": dependent}

    def work_storage(self) -> list[dict]:
        rows = self._many("SELECT drafts.*,jobs.status AS job_status FROM drafts LEFT JOIN jobs ON jobs.id=drafts.id ORDER BY drafts.created_at,drafts.id")
        for row in rows:
            try:
                tree = self._work_path(row["id"])
                size = 0
                if tree.exists():
                    for path in tree.rglob("*"):
                        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(tree.resolve()):
                            size += path.stat().st_size
                row["storage_bytes"] = size
            except (StoreError, OSError):
                row["storage_bytes"] = None
            row["recorded_input_bytes"] = self.db.execute("SELECT COALESCE(SUM(bytes),0) FROM work_files WHERE work_uuid=?", (row["id"],)).fetchone()[0]
        return rows

    def issue_token(self, owner_id: int, action: str, target: str, *, payload: dict | None = None) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", action):
            raise StoreError("invalid_action")
        with self.transaction():
            self._auth(owner_id)
            if self.get_user(owner_id) is None:
                self.upsert_user(owner_id)
            token = f"{action}:{secrets.token_urlsafe(18)}"
            self.db.execute("INSERT INTO durable_tokens(token,owner_id,action,target,payload,created_at) VALUES (?,?,?,?,?,?)", (token, owner_id, action, target, _json(payload or {}), _utc()))
            return token

    def get_token(self, owner_id: int, token: str, action: str | None = None) -> dict:
        """Прочитать собственную кнопку, включая уже выполненное действие."""
        self._auth(owner_id)
        row = self._one("SELECT * FROM durable_tokens WHERE token=?", (token,))
        if not row or row["owner_id"] != owner_id or (action is not None and row["action"] != action):
            raise StoreError("invalid_token")
        return row

    def consume_token(self, owner_id: int, token: str, action: str | None = None) -> dict:
        with self.transaction():
            row = self.get_token(owner_id, token, action)
            if row["consumed_at"] is not None:
                raise StoreError("token_consumed")
            consumed_at = _utc()
            self._update("durable_tokens", token, {"consumed_at": consumed_at}, pk="token")
            row["consumed_at"] = consumed_at
            return row

    def offset(self) -> int:
        return int(self._one("SELECT value FROM store_settings WHERE key='offset'")["value"])

    def process_update(self, update_id: int, mutator: Callable[[], Any]) -> bool:
        with self.transaction():
            if self._one("SELECT update_id FROM processed_updates WHERE update_id=?", (update_id,)):
                return False
            mutator()
            self.db.execute("INSERT INTO processed_updates(update_id,processed_at) VALUES (?,?)", (update_id, _utc()))
            self.db.execute("UPDATE store_settings SET value=? WHERE key='offset'", (str(max(self.offset(), update_id + 1)),))
            return True

    def add_outbox(self, kind: str, payload: dict, *, dedupe_key: str | None = None) -> int:
        with self.transaction():
            if dedupe_key is not None:
                existing = self._one("SELECT id FROM outbox WHERE dedupe_key=?", (dedupe_key,))
                if existing:
                    return existing["id"]
            now = _utc()
            cursor = self.db.execute("INSERT INTO outbox(kind,payload,dedupe_key,available_at,created_at) VALUES (?,?,?,?,?)", (kind, _json(payload), dedupe_key, now, now))
            return cursor.lastrowid

    def pending_outbox(self, limit: int = 20) -> list[dict]:
        return self._many("SELECT * FROM outbox WHERE status='pending' AND available_at<=? ORDER BY id LIMIT ?", (_utc(), max(0, limit)))

    def outbox_sent(self, id: int, result: dict | None = None) -> None:
        with self.transaction():
            self.db.execute("UPDATE outbox SET status='sent',sent_at=?,result=?,error_code=NULL WHERE id=? AND status='pending'", (_utc(), _json(result) if result is not None else None, id))

    def outbox_retry(self, id: int, available_at: datetime | str, error_code: str | None = None) -> None:
        if error_code is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", error_code):
            raise StoreError("invalid_error_code")
        with self.transaction():
            self.db.execute("UPDATE outbox SET available_at=?,error_code=?,attempts=attempts+1 WHERE id=? AND status='pending'", (_utc(available_at), error_code, id))

    def save_tariff_snapshot(self, snapshot: dict) -> str:
        document = _json(snapshot)
        digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO tariff_snapshots(digest,document,created_at) VALUES (?,?,?)", (digest, document, _utc()))
        return digest

    def record_api_call(self, event_start: dict, event_end: dict | None, cost_snapshot: dict) -> None:
        start = _plain(event_start)
        end = _plain(event_end) if event_end is not None else None
        cost = _plain(cost_snapshot)
        for field in ("amount", "cost"):
            if cost.get(field) is not None:
                try:
                    amount = Decimal(str(cost[field]))
                except (InvalidOperation, ValueError) as exc:
                    raise StoreError("invalid_cost") from exc
                if not amount.is_finite():
                    raise StoreError("invalid_cost")
                cost[field] = str(amount)
        run_id, request_id = start.get("run_id"), start.get("request_id")
        if not run_id or not request_id:
            raise StoreError("invalid_api_event")
        if end and (end.get("run_id") != run_id or end.get("request_id") != request_id):
            raise StoreError("invalid_api_event")
        job_id = start.get("job_id") or cost.get("job_id") or run_id
        with self.transaction():
            job = self.get_job(job_id)
            if not job:
                raise StoreError("api_job_unknown")
            digest = cost.get("tariff_digest") or cost.get("snapshot_digest")
            if digest and not self._one("SELECT digest FROM tariff_snapshots WHERE digest=?", (digest,)):
                raise StoreError("tariff_unknown")
            existing = self._one("SELECT * FROM api_calls WHERE run_id=? AND request_id=?", (run_id, request_id))
            if existing:
                if existing["job_id"] != job_id or existing["event_start"] != start:
                    raise StoreError("api_attempt_conflict")
                if existing["event_end"] is None and end is not None:
                    self._update("api_calls", existing["id"], {"ended_at": _utc(end["timestamp_utc"]), "event_end": _json(end), "cost_snapshot": _json(cost), "tariff_digest": digest})
                elif end is not None and (existing["event_end"] != end or existing["cost_snapshot"] != cost):
                    raise StoreError("api_attempt_conflict")
                return
            self.db.execute("INSERT INTO api_calls(job_id,run_id,request_id,started_at,ended_at,event_start,event_end,cost_snapshot,tariff_digest) VALUES (?,?,?,?,?,?,?,?,?)", (job_id, run_id, request_id, _utc(start["timestamp_utc"]), _utc(end["timestamp_utc"]) if end else None, _json(start), _json(end) if end else None, _json(cost), digest))

    def stats(self, owner_id: int | None = None, include_cost: bool = False, *, actor_id: int | None = None) -> dict:
        if actor_id is not None:
            self._admin(actor_id)
        elif owner_id is None:
            raise StoreError("admin_required")
        elif include_cost:
            self._admin(owner_id)
        else:
            self._auth(owner_id)
        jobs = self._many("SELECT * FROM jobs" + (" WHERE owner_id=?" if owner_id is not None else ""), (owner_id,) if owner_id is not None else ())
        calls = self._many("SELECT api_calls.* FROM api_calls JOIN jobs ON jobs.id=api_calls.job_id" + (" WHERE jobs.owner_id=?" if owner_id is not None else ""), (owner_id,) if owner_id is not None else ())
        result: dict[str, Any] = {"jobs": len(jobs), "statuses": dict(Counter(job["status"] for job in jobs)), "api_attempts": len(calls)}
        result["deliveries"] = self.db.execute("SELECT COUNT(*) FROM deliveries" + (" WHERE owner_id=?" if owner_id is not None else ""), (owner_id,) if owner_id is not None else ()).fetchone()[0]
        result["reused_deliveries"] = self.db.execute("SELECT COUNT(*) FROM deliveries WHERE reused=1" + (" AND owner_id=?" if owner_id is not None else ""), (owner_id,) if owner_id is not None else ()).fetchone()[0]
        if owner_id is not None:
            result["quota"] = self.quota(owner_id, utc_now(), actor_id=actor_id)
        # Длительности и ресурсы охватывают задания и собственные черновики подготовки.
        # Черновик учитывается отдельно, пока его вход не вошёл в ресурсы задания.
        works = [{"metrics": job["metrics"], "resources": job["resources"]} for job in jobs]
        accounted = {job["id"] for job in jobs}
        for draft in self._many("SELECT * FROM drafts" + (" WHERE owner_id=?" if owner_id is not None else ""), (owner_id,) if owner_id is not None else ()):
            preparation = _preparation(draft)
            if preparation is None or draft["id"] in accounted:
                continue
            seconds = _preparation_seconds(preparation)
            works.append({"metrics": {} if seconds is None else {"preparation_seconds": seconds},
                          "resources": _preparation_resources(preparation)})
        for key, field, reduce in (
            ("cpu_user_seconds", "known_total", sum),
            ("cpu_system_seconds", "known_total", sum),
            ("busy_gpu_seconds", "known_total", sum),
            ("ru_maxrss_kib", "known_peak", max),
            ("host_rss_peak_kib", "known_peak", max),
            ("vram_peak_bytes", "known_peak", max),
        ):
            known = [work["resources"][key] for work in works if _measurement(work["resources"].get(key)) is not None]
            unknown = sum(
                _measurement(work["resources"].get(key)) is None
                or any(not isinstance(report, dict) or _measurement(report.get(key)) is None
                       for report in (work["resources"].get("phase_reports") or ()))
                for work in works
            )
            result[key] = {field: reduce(known) if known else None, "unknown_works": unknown}
        for key in ("queue_wait_seconds", "preparation_seconds", "processing_seconds", "render_seconds"):
            durations = works if key == "preparation_seconds" else jobs
            known = [work["metrics"][key] for work in durations if _measurement(work["metrics"].get(key)) is not None]
            result[key] = {"known_total": sum(known) if known else None, "unknown_works": len(durations) - len(known)}
        deliveries = self._many("SELECT metrics FROM deliveries" + (" WHERE owner_id=?" if owner_id is not None else ""), (owner_id,) if owner_id is not None else ())
        known_delivery = [delivery["metrics"]["delivery_seconds"] for delivery in deliveries if delivery["metrics"].get("delivery_seconds") is not None]
        result["delivery_seconds"] = {"known_total": sum(known_delivery) if known_delivery else None, "unknown_deliveries": len(deliveries) - len(known_delivery)}
        for key in ("prompt_tokens", "completion_tokens", "cache_hit", "cache_miss", "reasoning_tokens"):
            known = []
            for call in calls:
                end = call["event_end"] or {}
                usage = end.get("usage") or end
                if usage.get(key) is not None:
                    known.append(usage[key])
            result[key] = {"known_total": sum(known) if known else None, "unknown_calls": len(calls) - len(known)}
        if include_cost:
            totals: dict[str, Decimal] = {}
            incomplete = 0
            for call in calls:
                cost = call["cost_snapshot"]
                amount = cost.get("amount", cost.get("cost"))
                currency = cost.get("currency")
                if amount is None or not currency:
                    incomplete += 1
                    continue
                value = Decimal(str(amount))
                if not value.is_finite():
                    raise StoreError("invalid_cost")
                totals[currency] = totals.get(currency, Decimal(0)) + value
                if cost.get("complete") is False or cost.get("incomplete") is True:
                    incomplete += 1
            result["estimated_api_cost"] = {"known_totals": {key: str(value) for key, value in totals.items()}, "incomplete_calls": incomplete}
        return result

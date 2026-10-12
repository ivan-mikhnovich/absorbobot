"""Четыре формата, потоковые части и долговечная повторная выдача."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from absorbobot.i18n import tr
from absorbobot.identity import work_title
from absorbobot.store import Store, StoreError, utc_now


FORMATS = ("notes.md", "notes.html", "notes.pdf", "notes.zip")
CHUNK = 1024 * 1024
ALBUM_LIMIT = 10


def album_chunks(files: list[dict], limit: int = ALBUM_LIMIT) -> list[list[dict]]:
    """Разбить вложения на альбомы: не больше лимита и без одинокого хвоста."""
    if limit < 2:
        raise ValueError("album limit must be at least two.")
    chunks, start = [], 0
    while start < len(files):
        size = min(limit, len(files) - start)
        if len(files) - start - size == 1:
            size -= 1
        chunks.append(files[start:start + size])
        start += size
    return chunks


def contained_file(root: Path, relative: str) -> Path:
    """Разрешить только существующий обычный файл внутри собственного дерева."""
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise StoreError("artifact_missing")
    return path


def package_result(tree: Path) -> Path:
    """Собрать только конспект, иллюстрации и транскрипцию, не входы или журналы."""
    output = tree / "output"
    if output.is_symlink() or not output.resolve().is_relative_to(tree.resolve()):
        raise StoreError("artifact_missing")
    files = [(contained_file(tree, f"output/{name}"), name) for name in FORMATS[:-1]]
    images = output / "images"
    if images.exists():
        if images.is_symlink() or not images.resolve().is_relative_to(output.resolve()):
            raise StoreError("artifact_missing")
        for path in sorted(images.rglob("*")):
            if path.is_symlink():
                raise StoreError("artifact_missing")
            if path.is_file():
                files.append((contained_file(tree, path.relative_to(tree).as_posix()),
                              path.relative_to(output).as_posix()))
    transcript = contained_file(tree, "output/.absorbent/transcript.json")
    files.append((transcript, "transcript.json"))
    temporary = output / f".notes-{uuid4()}.zip.tmp"
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, name in files:
                archive.write(path, arcname=name)
        os.replace(temporary, output / "notes.zip")
    finally:
        temporary.unlink(missing_ok=True)
    return output / "notes.zip"


def split_file(path: Path, destination: Path, part_bytes: int) -> tuple[list[Path], Path]:
    """Разбить оригинал потоково; manifest содержит размеры, SHA и порядок."""
    if part_bytes <= 0:
        raise ValueError("part_bytes must be positive.")
    destination.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    parts, entries = [], []
    total = 0
    with path.open("rb") as source:
        index = 1
        while True:
            data = source.read(min(CHUNK, part_bytes))
            if not data:
                break
            part = destination / f"{path.name}.part{index:03d}"
            temporary = part.with_name(f".{part.name}-{uuid4()}.tmp")
            part_digest = hashlib.sha256()
            written = 0
            with temporary.open("wb") as target:
                while data:
                    target.write(data)
                    part_digest.update(data)
                    digest.update(data)
                    written += len(data)
                    total += len(data)
                    if written == part_bytes:
                        break
                    data = source.read(min(CHUNK, part_bytes - written))
            os.replace(temporary, part)
            parts.append(part)
            entries.append({"filename": part.name, "bytes": written,
                            "sha256": part_digest.hexdigest()})
            index += 1
    manifest = destination / f"{path.name}.manifest.json"
    names = " ".join(entry["filename"] for entry in entries)
    document = {
        "schema_version": 1, "filename": path.name, "bytes": total,
        "sha256": digest.hexdigest(), "parts": entries,
        "instructions": {
            "linux_macos": f"cat {names} > {path.name}",
            "powershell": (
                f"$m = Get-Content -Raw '{manifest.name}' | ConvertFrom-Json; "
                "$out = [IO.File]::Create($m.filename); try { "
                "foreach ($p in $m.parts) { $in = [IO.File]::OpenRead($p.filename); "
                "try { $in.CopyTo($out) } finally { $in.Dispose() } } "
                "} finally { $out.Dispose() }; "
                "Get-FileHash -Algorithm SHA256 $m.filename"
            ),
        },
    }
    temporary = manifest.with_name(f".{manifest.name}-{uuid4()}.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, manifest)
    return parts, manifest


class Delivery:
    """Сохранить независимые отправки альбомов выдачи, не запускать ядро."""

    def __init__(self, settings, store: Store):
        self.settings, self.store = settings, store

    def tree(self, work_uuid: str) -> Path:
        if str(UUID(work_uuid)) != work_uuid:
            raise StoreError("invalid_uuid")
        root = (self.settings.storage_dir / "jobs").resolve()
        tree = root / work_uuid
        if tree.is_symlink() or not tree.resolve().is_relative_to(root):
            raise StoreError("artifact_missing")
        return tree

    async def prepare_files(self, payload: dict) -> list[dict]:
        """Подготовить файлы вне SQLite-транзакции; доступ проверяется повторно при commit."""
        source = self.store.get_job(payload["source_job_id"])
        if (not source or source["status"] != "completed" or source["files_deleted"]
                or not self.store.authorized(source["owner_id"])
                or not self.store.authorized(payload["owner_id"])):
            raise StoreError("reuse_unavailable")
        return await asyncio.to_thread(self._files, source["id"])

    def _files(self, source_id: str) -> list[dict]:
        """Обрабатывать большие файлы в потоке без доступа к SQLite."""
        tree = self.tree(source_id)
        files = []
        for name in FORMATS:
            path = contained_file(tree, f"output/{name}")
            if path.stat().st_size > self.settings.telegram_part_bytes:
                destination = tree / "output" / "parts" / str(self.settings.telegram_part_bytes)
                if not destination.resolve().is_relative_to(tree.resolve()):
                    raise StoreError("artifact_missing")
                parts, manifest = split_file(path, destination, self.settings.telegram_part_bytes)
                selected = [*parts, manifest]
            else:
                selected = [path]
            for document in selected:
                files.append({"file": document.relative_to(tree).as_posix(),
                              "filename": document.name})
        return files

    def _metrics(self, delivery: dict) -> dict:
        """Разобрать метрики записи выдачи: свежая возвращается словарём, найденная — строкой."""
        value = delivery.get("metrics")
        if isinstance(value, dict):
            return dict(value)
        try:
            parsed = json.loads(value) if value else {}
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _preparation(self, work_uuid: str | None) -> dict | None:
        """Подготовка входа получателя, а не оплаченная работа первоисточника."""
        if not work_uuid:
            return None
        draft = self.store.get_draft(work_uuid)
        state = draft.get("ui_state") if draft else None
        value = state.get("preparation") if isinstance(state, dict) else None
        return value if isinstance(value, dict) else None

    def enqueue(self, payload: dict, request_id: int, files: list[dict]) -> dict:
        owner, source_id, work_uuid = (payload[key] for key in
                                      ("owner_id", "source_job_id", "work_uuid"))
        with self.store.transaction():
            if not self.store.authorized(owner):
                raise StoreError("not_authorized")
            candidates = self.store.db.execute(
                "SELECT * FROM deliveries WHERE owner_id=? AND source_job_id=? "
                "AND work_uuid IS ? ORDER BY id DESC", (owner, source_id, work_uuid),
            ).fetchall()
            delivery = None
            for row in candidates:
                metrics = json.loads(row["metrics"])
                if metrics.get("request_outbox_id") == request_id:
                    return {"id": row["id"], "already_planned": True}
                if delivery is None and row["status"] == "pending" and "request_outbox_id" not in metrics:
                    delivery = dict(row)
            if work_uuid is None and not candidates:
                raise StoreError("not_owner")
            if delivery is None:
                delivery = self.store.record_delivery(owner, source_id, work_uuid=work_uuid)
            else:
                # Повторно проверить доступность исходного результата и совпадение входов.
                source = self.store.get_job(source_id)
                if (not source or source["status"] != "completed" or source["files_deleted"]
                        or not self.store.authorized(source["owner_id"])):
                    raise StoreError("reuse_unavailable")
                for name in FORMATS:
                    contained_file(self.tree(source_id), f"output/{name}")
                if work_uuid is not None:
                    draft = self.store.get_draft(work_uuid, owner)
                    if not draft or draft["input_signature"] != source["input_signature"]:
                        raise StoreError("reuse_unavailable")
            source = self.store.get_job(source_id)
            source_tree = self.tree(source_id)
            for file in files:
                contained_file(source_tree, file["file"])
            language = self.store.get_user(owner)["language"]
            draft = self.store.get_draft(work_uuid, owner) if work_uuid else None
            caption = tr(language, "delivery.caption", id=work_title(self.store, draft or source, viewer=owner),
                         version=source["engine_version"], commit=source["engine_commit"])
            reply = draft["source_message_id"] if draft else None
            for index, chunk in enumerate(album_chunks(files, ALBUM_LIMIT)):
                kwargs = {"chat_id": owner}
                if reply is not None:
                    kwargs["reply_parameters"] = {"message_id": reply, "allow_sending_without_reply": True}
                self.store.add_outbox("telegram", {
                    "owner_id": owner, "method": "send_media_group", "kwargs": kwargs,
                    "work_uuid": source_id, "delivery_id": delivery["id"],
                    "caption": caption, "files": chunk, "documents": len(chunk),
                }, dedupe_key=f"delivery:{delivery['id']}:{index}")
            metrics = self._metrics(delivery)
            metrics.update({"request_outbox_id": request_id,
                            "planned_at_utc": utc_now().isoformat(),
                            "documents": len(files)})
            if "preparation" not in metrics:
                # Повторная выдача не повторяет учёт подготовки получателя.
                preparation = self._preparation(work_uuid)
                if preparation is not None:
                    metrics["preparation"] = preparation
            self.store.db.execute("UPDATE deliveries SET metrics=? WHERE id=?",
                                  (json.dumps(metrics), delivery["id"]))
            return {"id": delivery["id"], "documents": len(files)}

    def reconcile(self) -> None:
        """Отметить завершённую выдачу только после отправки всех сохранённых частей."""
        with self.store.transaction():
            for row in self.store.db.execute("SELECT id,metrics FROM deliveries WHERE status='pending'").fetchall():
                metrics = json.loads(row["metrics"])
                if "request_outbox_id" not in metrics:
                    continue
                actions = self.store.db.execute(
                    "SELECT status,result FROM outbox WHERE dedupe_key LIKE ?",
                    (f"delivery:{row['id']}:%",),
                ).fetchall()
                if not actions or any(action["status"] != "sent" for action in actions):
                    continue
                skipped = any(json.loads(action["result"] or "{}").get("skipped") for action in actions)
                start = datetime.fromisoformat(metrics["planned_at_utc"])
                metrics["delivery_seconds"] = max(0, (utc_now() - start).total_seconds())
                self.store.db.execute("UPDATE deliveries SET status=?,metrics=? WHERE id=?",
                                      ("access_revoked" if skipped else "sent", json.dumps(metrics), row["id"]))

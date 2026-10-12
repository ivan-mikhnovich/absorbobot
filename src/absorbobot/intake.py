"""Безопасный импорт входов и семантическая идентичность выбранной обработки."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path, PurePath
import re
import resource
import stat
import sys
import threading
import time
from uuid import uuid4

from .config import ConfigError, check_storage, create_work_tree
from .i18n import tr
from .metrics import aggregate_reports
from .store import StoreError, utc_now
from .workers.youtube import YouTubeError, normalize_youtube_url, tree_bytes
from .workers.external import normalize_external_url


class IntakeError(ValueError):
    """Безопасный код отказа, пригодный для локализованного сообщения."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def input_signature(files: list[dict], settings: dict, models: dict) -> str:
    """Сравнить содержимое и обработку без владельца, путей и версии ядра."""
    active = [file for file in files if file.get("active", True) and not file.get("removed")]
    media = [file for file in active if file.get("role") == "media"]
    if len(media) != 1 or any(file.get("role") not in ("media", "material") for file in active):
        raise ValueError("Exactly one media file is required.")
    if any(not isinstance(file.get("sha256"), str) or
           not re.fullmatch(r"[a-f0-9]{64}", file["sha256"]) for file in active):
        raise ValueError("Invalid content digest.")
    parameters = {key: settings[key] for key in (
        "language", "output_language", "fps", "hash_threshold",
        "min_scene_seconds", "slides_dpi", "youtube_mode",
    ) if key in settings}
    for key in ("fps", "min_scene_seconds"):
        if key in parameters:
            value = parameters[key]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Invalid processing parameter.")
            if value <= 0 if key == "fps" else value < 0:
                raise ValueError("Invalid processing parameter.")
            parameters[key] = float(value)
    for key in ("hash_threshold", "slides_dpi"):
        if key in parameters:
            value = parameters[key]
            if type(value) is not int or (not 0 <= value <= 64 if key == "hash_threshold" else value <= 0):
                raise ValueError("Invalid processing parameter.")
    for key, allowed in (("language", ("auto", "ru", "en")),
                         ("output_language", ("ru", "en")), ("youtube_mode", ("video", "audio"))):
        if key in parameters and parameters[key] not in allowed:
            raise ValueError("Invalid processing parameter.")
    selected = {}
    for category, model in sorted(models.items()):
        spec, provider = model["spec"], model.get("provider") or {}
        selected[category] = {
            "backend": spec.get("backend"),
            "whisper_model": spec.get("whisper_model"),
            "api_model": spec.get("api_model", spec.get("model") or spec.get("name")),
            "provider": provider.get("name"),
            "base_url": provider.get("base_url"),
        }
        if any(value is not None and not isinstance(value, str) for value in selected[category].values()):
            raise ValueError("Invalid effective model specification.")
    materials = []
    for file in active:
        if file["role"] == "material":
            kind = file.get("material_type") or PurePath(file["relative_path"]).suffix
            if not isinstance(kind, str) or not re.fullmatch(r"\.?[a-zA-Z0-9]+", kind):
                raise ValueError("Invalid material type.")
            materials.append({"sha256": file["sha256"], "type": kind.lower().lstrip(".")})
    content = {"media": media[0]["sha256"], "materials": materials,
               "settings": parameters, "models": selected}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _extensions(values) -> set[str]:
    return {value.lower() if value.startswith(".") else "." + value.lower() for value in values}


def telegram_descriptor(message: dict, core: dict) -> dict:
    """Выбрать один файл, включая самый большой вариант Telegram photo."""
    kinds = [kind for kind in ("document", "video", "audio", "voice", "video_note", "photo")
             if message.get(kind)]
    if len(kinds) != 1:
        raise IntakeError("unsupported_input")
    kind = kinds[0]
    descriptor = message[kind]
    if kind == "photo":
        if not isinstance(descriptor, list) or not descriptor or any(not isinstance(item, dict) for item in descriptor):
            raise IntakeError("unsupported_input")
        def size(item):
            width, height, length = (item.get(key, 0) for key in ("width", "height", "file_size"))
            if any(type(value) is not int or value < 0 for value in (width, height, length)):
                raise IntakeError("invalid_file_metadata")
            return width * height, length
        descriptor = max(descriptor, key=size)
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("file_id"), str) or not descriptor["file_id"]:
        raise IntakeError("invalid_file_metadata")
    length = descriptor.get("file_size")
    if type(length) is not int or length <= 0:
        raise IntakeError("invalid_file_metadata")
    original = descriptor.get("file_name")
    if original is not None and (not isinstance(original, str) or len(original) > 4096 or "\x00" in original):
        raise IntakeError("invalid_file_metadata")
    if kind == "photo":
        extension = ".jpg"
    elif kind == "voice":
        extension = ".ogg"
    elif kind == "video_note":
        extension = ".mp4"
    else:
        extension = PurePath(original or "").suffix.lower()
        if not extension:
            extension = {
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
                "video/mp4": ".mp4", "video/x-matroska": ".mkv",
            }.get(descriptor.get("mime_type"), "")
    media_exts, material_exts = (_extensions(core[key]) for key in ("media_extensions", "material_extensions"))
    if kind == "photo" or kind == "document" and extension in material_exts:
        role, allowed = "material", material_exts
    else:
        role, allowed = "media", media_exts
    if extension not in allowed:
        raise IntakeError("unsupported_input")
    return {"file_id": descriptor["file_id"], "bytes": length, "role": role,
            "extension": extension, "original_name": original or kind + extension}


def _open_contained(source: Path, root: Path) -> int:
    """Открыть только native-файл под разрешённым корнем без symlink-гонки."""
    if not source.is_absolute() or root.is_symlink():
        raise IntakeError("unsafe_file_path")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root):
            raise IntakeError("unsafe_file_path")
        # Канонический путь не легализует ссылку из исходного native path.
        relative = source.relative_to(resolved_root)
        if any(part in ("", ".", "..") for part in relative.parts) or not relative.parts:
            raise IntakeError("unsafe_file_path")
        directory = os.open(resolved_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise IntakeError("unsafe_file_path")
        return fd
    except (OSError, ValueError):
        raise IntakeError("unsafe_file_path") from None


def copy_and_hash(source: Path, target: Path, *, source_root: Path, work_root: Path,
                  byte_limit: int, work_limit: int, min_free_bytes: int,
                  cancelled: threading.Event | None = None) -> dict:
    """Копировать ограниченными блоками, учитывая фактический размер и диск."""
    fd = _open_contained(source, source_root)
    try:
        existing = tree_bytes(work_root)
        if os.fstat(fd).st_size > byte_limit:
            raise IntakeError("input_too_large")
        if existing + os.fstat(fd).st_size > work_limit:
            raise IntakeError("work_limit")
        if target.parent.is_symlink() or not target.parent.resolve().is_relative_to(work_root.resolve()):
            raise IntakeError("unsafe_file_path")
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            output_fd = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=directory)
        finally:
            os.close(directory)
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(output_fd, "wb") as output:
                while True:
                    if cancelled is not None and cancelled.is_set():
                        raise IntakeError("cancelled")
                    chunk = os.read(fd, min(1024 * 1024, byte_limit - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > byte_limit:
                        raise IntakeError("input_too_large")
                    if existing + size > work_limit:
                        raise IntakeError("work_limit")
                    usage = os.statvfs(work_root)
                    if usage.f_bavail * usage.f_frsize - len(chunk) < min_free_bytes:
                        raise IntakeError("storage_unavailable")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size <= 0:
                raise IntakeError("invalid_file_metadata")
            return {"sha256": digest.hexdigest(), "bytes": size}
        except BaseException:
            # Неполная копия не остаётся в дереве задания даже при отмене.
            try:
                os.unlink(target)
            except OSError:
                pass
            raise
    finally:
        os.close(fd)


def _thread_cpu() -> tuple[float, float]:
    """CPU именно этого потока: RUSAGE_THREAD не смешивает соседние задания."""
    usage = resource.getrusage(resource.RUSAGE_THREAD)
    return usage.ru_utime, usage.ru_stime


def _missing_resources(phase_id: str) -> dict:
    """Неизмеренная операция подготовки: null и причина, а не выдуманный ноль."""
    return {"schema_version": 1, "phase_id": phase_id, "exit_code": None,
            "cpu_user_seconds": None, "cpu_system_seconds": None, "ru_maxrss_kib": None,
            "completeness": "partial", "reason": "missing_report"}


def _copy_resources(usage: dict) -> dict:
    """Отчёт копирующего потока: CPU точен, RAM процесса потоку не приписывается."""
    return {"schema_version": 1, "phase_id": "input_copy", "exit_code": None,
            "cpu_user_seconds": round(float(usage.get("cpu_user_seconds", 0.0)), 6),
            "cpu_system_seconds": round(float(usage.get("cpu_system_seconds", 0.0)), 6),
            "ru_maxrss_kib": None, "completeness": "partial", "reason": "thread_rss_unavailable"}


def _phase_resources(report: object, phase_id: str) -> dict:
    """Сохранить фактический отчёт sandbox wait4, подписав его именем операции."""
    if isinstance(report, dict) and report.get("schema_version") == 1 and type(report.get("exit_code")) is int:
        return {"schema_version": 1, "phase_id": phase_id, "exit_code": report["exit_code"],
                "cpu_user_seconds": report.get("cpu_user_seconds"),
                "cpu_system_seconds": report.get("cpu_system_seconds"),
                "ru_maxrss_kib": report.get("ru_maxrss_kib"),
                "completeness": report.get("completeness") if report.get("completeness") in ("complete", "partial") else "partial",
                "reason": report.get("reason")}
    return _missing_resources(phase_id)


def _preparation_wall(preparation: object) -> float:
    """Кумулятивная измеренная длительность подготовки входа или ноль."""
    value = preparation.get("wall_seconds") if isinstance(preparation, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 0.0
    return float(value)


def _worker_result(result: dict) -> dict:
    """Читать только небольшой машинный stdout, а не весь диагностический log."""
    path = Path(result["stdout_path"])
    with path.open("rb") as file:
        data = file.read(16385)
    if len(data) > 16384:
        raise IntakeError("analysis_failed")
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError
    except (ValueError, UnicodeError):
        raise IntakeError("analysis_failed") from None
    if result["exit_code"] != 0 or value.get("ok") is not True:
        code = value.get("error")
        raise IntakeError(code if code in _ERROR_CODES else "analysis_failed")
    return value


_ERROR_CODES = frozenset({
    "unsupported_input", "invalid_file_metadata", "input_too_large", "work_limit",
    "storage_unavailable", "unsafe_file_path", "invalid_duration", "media_too_long",
    "missing_audio", "invalid_material", "material_pages_exceeded", "analysis_failed",
    "invalid_youtube_url", "invalid_youtube_metadata", "youtube_live", "youtube_unavailable",
    "youtube_failed", "telegram_file_unavailable", "media_exists", "source_first",
    "invalid_url", "unsafe_url", "download_failed", "telegram_file_too_large",
    "cancelled", "timeout", "sandbox_unavailable", "handshake_failed", "invalid_runtime",
    "interrupted",
})


class Intake:
    """Исполнить долговечный импорт без квоты, API моделей и собственного scheduler."""

    def __init__(self, settings, store, bot, core: dict, sandbox):
        self.settings, self.store, self.bot = settings, store, bot
        self.core, self.sandbox = core, sandbox

    def _draft(self, owner: int, work_uuid: str) -> dict:
        draft = self.store.get_draft(work_uuid, owner)
        if not draft or draft["status"] not in ("collecting", "preparing", "ready") or draft["close_requested"]:
            raise StoreError("draft_consumed")
        return draft

    def _notice(self, owner: int, work_uuid: str, message_id: int, key: str, **values) -> None:
        if not self.store.authorized(owner):
            return
        language = self.store.get_user(owner)["language"]
        self.store.add_outbox("telegram", {
            "owner_id": owner, "method": "send_message",
            "kwargs": {"chat_id": owner, "text": tr(language, key, id=work_uuid,
                       max_media_seconds=self.settings.max_media_seconds,
                       max_material_pages=self.settings.max_material_pages, **values),
                       "reply_parameters": {"message_id": message_id, "allow_sending_without_reply": True}},
        }, dedupe_key=f"intake:{work_uuid}:{message_id}:{key}")

    def _clear_pending(self, owner: int, work_uuid: str, message_id: int) -> None:
        # Учёт допустим и для закрытого черновика, без возобновления его обработки.
        with self.store.transaction():
            draft = self.store.get_draft(work_uuid)
            if not draft or draft["owner_id"] != owner:
                return
            state = dict(draft["ui_state"])
            state["pending_import_ids"] = [value for value in state.get("pending_import_ids", []) if value != message_id]
            if (state.get("youtube_pending") or {}).get("message_id") == message_id:
                state.pop("youtube_pending", None)
            self.store.db.execute("UPDATE drafts SET ui_state=? WHERE id=? AND owner_id=?",
                                  (json.dumps(state, ensure_ascii=False, allow_nan=False), work_uuid, owner))

    def _record_operation(self, owner: int, work_uuid: str, operation: dict) -> None:
        """Дописать измеренную операцию подготовки; допустимо и для закрытого черновика."""
        with self.store.transaction():
            draft = self.store.get_draft(work_uuid)
            if not draft or draft["owner_id"] != owner:
                return
            state = dict(draft["ui_state"])
            previous = state.get("preparation")
            operations = [op for op in previous.get("operations", []) if isinstance(op, dict)] \
                if isinstance(previous, dict) else []
            operations.append(operation)
            state["preparation"] = {
                "schema_version": 1,
                "wall_seconds": round(_preparation_wall(previous) + operation["wall_seconds"], 6),
                "operations": operations,
                "resources": aggregate_reports([op.get("resources") for op in operations]),
                "updated_at": utc_now().isoformat(),
            }
            self.store.db.execute("UPDATE drafts SET ui_state=? WHERE id=? AND owner_id=?",
                                  (json.dumps(state, ensure_ascii=False, allow_nan=False), work_uuid, owner))

    @contextlib.asynccontextmanager
    async def _operation(self, owner: int, work_uuid: str, name: str):
        """Измерить операцию подготовки и сохранить её исход даже при отказе или отмене."""
        started, started_at = time.monotonic(), utc_now()
        outcome = {"status": "complete", "reason": None, "resources": None, "wall_seconds": None}
        try:
            yield outcome
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                outcome["status"], outcome["reason"] = "cancelled", "interrupted"
            else:
                outcome["status"], outcome["reason"] = "failed", getattr(exc, "code", None) or "failed"
            raise
        finally:
            wall = outcome["wall_seconds"]
            if isinstance(wall, bool) or not isinstance(wall, (int, float)) or wall < 0:
                wall = time.monotonic() - started
            try:
                self._record_operation(owner, work_uuid, {
                    "operation": name, "status": outcome["status"], "reason": outcome["reason"],
                    "started_at": started_at.isoformat(), "ended_at": utc_now().isoformat(),
                    "wall_seconds": round(float(wall), 6),
                    "resources": outcome["resources"] or _missing_resources("input_" + name),
                })
            except Exception:
                # Ошибка учёта не подменяет исходную причину отказа импорта.
                if outcome["status"] == "complete":
                    raise

    async def _copy(self, source: Path, target: Path, *, source_root: Path, work_root: Path,
                    byte_limit: int, usage: dict) -> dict:
        cancelled = threading.Event()

        def run() -> dict:
            started = time.monotonic()
            user, system = _thread_cpu()
            try:
                return copy_and_hash(source, target, source_root=source_root, work_root=work_root,
                                     byte_limit=byte_limit, work_limit=self.settings.max_work_bytes,
                                     min_free_bytes=self.settings.min_free_bytes, cancelled=cancelled)
            finally:
                after_user, after_system = _thread_cpu()
                usage.update(wall_seconds=time.monotonic() - started,
                             cpu_user_seconds=max(0.0, after_user - user),
                             cpu_system_seconds=max(0.0, after_system - system))

        task = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            try:
                await asyncio.shield(task)
            except IntakeError:
                pass
            raise

    def _limits(self, files: list[dict], role: str, size: int) -> tuple[int, int]:
        media = [file for file in files if file["role"] == "media" and not file["removed"]]
        materials = [file for file in files if file["role"] == "material" and not file["removed"]]
        if role == "media" and media:
            raise IntakeError("media_exists")
        if role == "material" and len(media) != 1:
            raise IntakeError("source_first")
        remaining = self.settings.max_material_pages
        if role == "material":
            if any(type(file["pages"]) is not int or file["pages"] <= 0 for file in materials):
                raise IntakeError("invalid_material")
            remaining -= sum(file["pages"] for file in materials)
            limit = self.settings.max_material_bytes - sum(file["bytes"] for file in materials)
            if remaining <= 0:
                raise IntakeError("material_pages_exceeded")
        else:
            limit = self.settings.max_media_bytes
        if size > limit:
            raise IntakeError("input_too_large")
        return limit, remaining

    async def _download_telegram(self, remote_path: str, target: Path, root: Path, limit: int) -> None:
        """Скачать cloud-вложение ограниченными блоками без токена в журнале."""
        if (not re.fullmatch(r"[A-Za-z0-9_/-]+\.[A-Za-z0-9]+", remote_path)
                or remote_path.startswith("/") or ".." in remote_path.split("/")):
            raise IntakeError("unsafe_file_path")
        existing, size = tree_bytes(root), 0
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                url = self.bot.session.api.file_url(self.bot.token, remote_path)
                async with asyncio.timeout(min(600, self.settings.max_processing_seconds)):
                    async for chunk in self.bot.session.stream_content(
                            url, timeout=600, chunk_size=65536, raise_for_status=True):
                        size += len(chunk)
                        if size > limit:
                            raise IntakeError("telegram_file_too_large" if size > 20000000 else "input_too_large")
                        if existing + size > self.settings.max_work_bytes:
                            raise IntakeError("work_limit")
                        usage = os.statvfs(root)
                        if usage.f_bavail * usage.f_frsize - len(chunk) < self.settings.min_free_bytes:
                            raise IntakeError("storage_unavailable")
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not size:
                raise IntakeError("invalid_file_metadata")
        except BaseException as exc:
            target.unlink(missing_ok=True)
            if isinstance(exc, (IntakeError, asyncio.CancelledError)):
                raise
            raise IntakeError("timeout" if isinstance(exc, TimeoutError) else "telegram_file_unavailable") from None

    async def handle(self, payload: dict) -> dict:
        owner, work_uuid, message = payload["owner_id"], payload["draft_id"], payload["message"]
        message_id = message.get("message_id")
        if type(message_id) is not int or message_id <= 0:
            raise IntakeError("invalid_file_metadata")
        completed, preparing, youtube, external = True, False, False, False
        cleanup: list[Path] = []
        try:
            if not self.store.authorized(owner):
                return {"status": "skipped", "reason": "access_revoked"}
            draft = self._draft(owner, work_uuid)
            if message.get("chat", {}).get("id") != owner:
                raise IntakeError("invalid_file_metadata")
            files = self.store.files(work_uuid)
            existing = next((file for file in files if not file["removed"] and file["source_message_id"] == message_id), None)
            if existing:
                return {"status": "imported", "file_id": existing["id"], "duplicate": True}
            if message.get("text"):
                try:
                    url = normalize_youtube_url(message["text"])
                    youtube = True
                except YouTubeError:
                    url = normalize_external_url(message["text"])
                    external = True
            if youtube:
                mode = message.get("youtube_mode", payload.get("youtube_mode", draft["settings"].get("youtube_mode", "video")))
                if mode not in ("video", "audio"):
                    raise IntakeError("invalid_youtube_url")
                extension = ".mkv" if mode == "video" else ".m4a"
                if extension not in _extensions(self.core["media_extensions"]):
                    raise IntakeError("unsupported_input")
                descriptor = {"role": "media", "bytes": 0, "extension": extension,
                              "original_name": "YouTube " + url.rsplit("=", 1)[1] + extension}
            elif external:
                descriptor = None
            else:
                descriptor = telegram_descriptor(message, self.core)
                if self.settings.bot_api_url == "https://api.telegram.org" and descriptor["bytes"] > 20000000:
                    raise IntakeError("telegram_file_too_large")
            if descriptor is not None:
                limit, remaining_pages = self._limits(files, descriptor["role"], descriptor["bytes"])
            # Предел metadata проверен до getFile, mount — до любого mkdir.
            check_storage(self.settings)
            root = create_work_tree(self.settings, work_uuid)
            self.store.update_draft(owner, work_uuid, status="preparing", input_signature=None, snapshot={})
            preparing = True
            self._notice(owner, work_uuid, message_id, "intake.preparing")
            self._draft(owner, work_uuid)
            if youtube:
                runtime = {"tool_env": sys.prefix, "python_executable": sys.executable,
                           "python_base_prefix": sys.base_prefix}
                async with self._operation(owner, work_uuid, "youtube") as outcome:
                    result = await self.sandbox.run(
                        work_uuid, "youtube", [runtime["python_executable"], "/workers/youtube.py",
                        url.rsplit("=", 1)[1], mode, str(limit), str(self.settings.max_media_seconds),
                        str(self.settings.max_work_bytes), str(self.settings.min_free_bytes)],
                        runtime=runtime, env={}, network=True, selected_key_names=(),
                        timeout=self.settings.max_processing_seconds)
                    outcome["resources"] = _phase_resources(result.get("resource_report"), "input_youtube")
                    data = _worker_result(result)
                    relative = Path(data["relative_path"])
                    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "tmp":
                        raise IntakeError("unsafe_file_path")
                    source, source_root = root / relative, root / "tmp"
            elif external:
                runtime = {"tool_env": sys.prefix, "python_executable": sys.executable,
                           "python_base_prefix": sys.base_prefix}
                material_bytes = sum(file["bytes"] for file in files if file["role"] == "material" and not file["removed"])
                request = root / "tmp" / (uuid4().hex + ".json")
                download_id = request.stem
                cleanup.extend(root / "tmp" / (download_id + extension)
                               for extension in _extensions(self.core["media_extensions"]) | _extensions(self.core["material_extensions"]))
                with request.open("x", encoding="utf-8") as handle:
                    os.chmod(request, 0o600)
                    json.dump({"url": url, "download_id": download_id, "media_extensions": self.core["media_extensions"],
                               "material_extensions": self.core["material_extensions"],
                               "max_media_bytes": self.settings.max_media_bytes,
                               "max_material_bytes": self.settings.max_material_bytes,
                               "remaining_material_bytes": max(0, self.settings.max_material_bytes - material_bytes),
                               "max_work_bytes": self.settings.max_work_bytes,
                               "min_free_bytes": self.settings.min_free_bytes}, handle)
                cleanup.append(request)
                async with self._operation(owner, work_uuid, "external") as outcome:
                    result = await self.sandbox.run(
                        work_uuid, "external", [sys.executable, "/workers/external.py", "/work/tmp/" + request.name],
                        runtime=runtime, env={}, network=True, selected_key_names=(),
                        timeout=min(600, self.settings.max_processing_seconds))
                    outcome["resources"] = _phase_resources(result.get("resource_report"), "input_external")
                    data = _worker_result(result)
                    relative = Path(data.get("relative_path", ""))
                    if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 2
                            or relative.parts[0] != "tmp" or relative.stem != download_id):
                        raise IntakeError("unsafe_file_path")
                    source, source_root = root / relative, root / "tmp"
                    if source not in cleanup:
                        raise IntakeError("unsafe_file_path")
                    role, extension, size = data.get("role"), data.get("extension"), data.get("bytes")
                    if (role not in ("media", "material") or type(size) is not int or size <= 0
                            or extension not in _extensions(self.core[role + "_extensions"])
                            or not isinstance(data.get("original_name"), str)):
                        raise IntakeError("invalid_file_metadata")
                    descriptor = {"role": role, "extension": extension, "bytes": size,
                                  "original_name": data["original_name"]}
                    limit, remaining_pages = self._limits(files, role, size)
            else:
                try:
                    remote = await self.bot.get_file(descriptor["file_id"])
                except Exception:
                    raise IntakeError("telegram_file_unavailable") from None
                remote_path = remote.get("file_path") if isinstance(remote, dict) else getattr(remote, "file_path", None)
                remote_size = remote.get("file_size") if isinstance(remote, dict) else getattr(remote, "file_size", None)
                if remote_size is not None and (type(remote_size) is not int or remote_size < 0 or remote_size > limit):
                    raise IntakeError("input_too_large")
                if not isinstance(remote_path, str) or not remote_path:
                    raise IntakeError("unsafe_file_path")
                if self.settings.bot_api_url == "https://api.telegram.org":
                    source, source_root = root / "tmp" / (uuid4().hex + descriptor["extension"]), root / "tmp"
                    cleanup.append(source)
                    limit = min(limit, 20000000)
                    async with self._operation(owner, work_uuid, "telegram") as outcome:
                        await self._download_telegram(remote_path, source, root, limit)
                        outcome["resources"] = _missing_resources("input_telegram")
                else:
                    source, source_root = Path(remote_path), self.settings.storage_dir / "telegram-api-files"
            self._draft(owner, work_uuid)
            target = root / "input" / (uuid4().hex + descriptor["extension"])
            async with self._operation(owner, work_uuid, "copy") as outcome:
                usage: dict = {}
                try:
                    content = await self._copy(source, target, source_root=source_root, work_root=root,
                                               byte_limit=limit, usage=usage)
                finally:
                    # Даже отменённое копирование сохраняет измеренный CPU своего потока.
                    outcome["wall_seconds"] = usage.get("wall_seconds")
                    outcome["resources"] = _copy_resources(usage)
            self._draft(owner, work_uuid)
            runtime = self.core["runtime"]
            async with self._operation(owner, work_uuid, "analyze") as outcome:
                result = await self.sandbox.run(
                    work_uuid, "analyze", [runtime["python_executable"], "/workers/analyze.py",
                    "/work/input/" + target.name, descriptor["role"], str(self.settings.max_media_seconds),
                    str(remaining_pages)], runtime=runtime, env={}, network=False, selected_key_names=(),
                    timeout=min(self.settings.max_processing_seconds, 180))
                outcome["resources"] = _phase_resources(result.get("resource_report"), "input_analyze")
                data = _worker_result(result)
                # Перепроверить consumer boundary, даже если worker сообщил успех.
                if descriptor["role"] == "media":
                    duration, pages = data.get("duration"), None
                    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
                        raise IntakeError("invalid_duration")
                    if duration > self.settings.max_media_seconds:
                        raise IntakeError("media_too_long")
                else:
                    duration, pages = None, data.get("pages")
                    if type(pages) is not int or pages <= 0:
                        raise IntakeError("invalid_material")
                    if pages > remaining_pages:
                        raise IntakeError("material_pages_exceeded")
            draft = self._draft(owner, work_uuid)
            # Store остаётся исключительно в event-loop thread.
            with self.store.transaction():
                self._limits(self.store.files(work_uuid), descriptor["role"], content["bytes"])
                file = self.store.add_file(owner, work_uuid, role=descriptor["role"],
                    original_name=descriptor["original_name"], relative_path="input/" + target.name,
                    **content, duration=duration, pages=pages, source_message_id=message_id)
                fields = {"status": "ready", "failure_reason": None}
                if descriptor["role"] == "media":
                    fields.update(source_message_id=message_id, source_chat_id=owner)
                if youtube:
                    # Режим YouTube меняет подпись входа; сбрасываем её явно, чтобы
                    # update_draft не подменил подтверждённый статус ready на collecting.
                    fields["settings"] = {**draft["settings"], "youtube_mode": mode}
                    fields["input_signature"] = None
                    fields["snapshot"] = {}
                self.store.update_draft(owner, work_uuid, **fields)
            self._notice(owner, work_uuid, message_id, "intake.imported", name=descriptor["original_name"])
            return {"status": "imported", "file_id": file["id"], "work_uuid": work_uuid}
        except asyncio.CancelledError:
            await self.sandbox.cancel(work_uuid)
            self._finish_failure(owner, work_uuid, "interrupted", preparing, terminal=True)
            raise
        except StoreError as exc:
            if exc.code in ("paused", "execution_busy"):
                completed = False
                raise
            return {"status": "skipped", "reason": exc.code}
        except Exception as exc:
            code = getattr(exc, "code", str(exc) if isinstance(exc, YouTubeError) else None)
            if isinstance(exc, ConfigError):
                code = "storage_unavailable"
            if code not in _ERROR_CODES:
                code = "analysis_failed" if preparing else "telegram_file_unavailable"
            self._finish_failure(owner, work_uuid, code, preparing,
                                 terminal=youtube or code in ("cancelled", "interrupted"))
            self._notice(owner, work_uuid, message_id, "intake.error." + code)
            return {"status": "rejected", "reason": code, "work_uuid": work_uuid}
        finally:
            for temporary in cleanup:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)
            if completed:
                self._clear_pending(owner, work_uuid, message_id)

    def _finish_failure(self, owner: int, work_uuid: str, code: str, preparing: bool, *, terminal: bool) -> None:
        if not preparing or not self.store.authorized(owner):
            return
        draft = self.store.get_draft(work_uuid, owner)
        if draft and draft["status"] == "preparing" and not draft["close_requested"]:
            has_media = any(file["role"] == "media" and not file["removed"]
                            for file in self.store.files(work_uuid))
            status = "failed" if terminal else ("ready" if has_media else "collecting")
            self.store.update_draft(owner, work_uuid, status=status, failure_reason=code)

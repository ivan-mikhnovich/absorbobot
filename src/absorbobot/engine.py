"""Один исполнитель: подготовка, FIFO, поздний reuse и отдельная доставка."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import yaml

from absorbobot.config import ConfigError, check_storage, create_work_tree, probe_core
from absorbobot.delivery import Delivery, package_result
from absorbobot.i18n import tr
from absorbobot.intake import Intake
from absorbobot.metrics import JournalIngestor, PhaseMetrics, aggregate_reports
from absorbobot.pricing import Pricing
from absorbobot.sandbox import Sandbox, SandboxError
from absorbobot.store import StoreError, TERMINAL_JOBS, utc_now


class Engine:
    """Не повторять оплаченный конвейер ради неудачной отправки Telegram."""

    def __init__(self, settings, store, bot, dialog, core):
        self.settings, self.store, self.bot = settings, store, bot
        self.dialog, self.core = dialog, core
        self.sandbox = Sandbox(settings, store)
        self.intake = Intake(settings, store, bot, core, self.sandbox)
        self.delivery = Delivery(settings, store)
        self.pricing = Pricing()
        self.store.save_tariff_snapshot(self.pricing.snapshot)
        self._active = None
        self._active_uuid = None
        self._active_kind = None
        self._stopping = False
        self._delivery_tasks = {}
        self._progress = {}

    def _notice(self, owner, work_uuid, key, *, source=None, purpose=None, dedupe=None, **values):
        user = self.store.get_user(owner)
        if user is None:
            return
        payload = {"owner_id": owner, "method": "send_message", "kwargs": {
            "chat_id": owner, "text": tr(user["language"], key, id=work_uuid, **values),
        }}
        if source:
            payload["kwargs"]["reply_parameters"] = {
                "message_id": source, "allow_sending_without_reply": True,
            }
        if purpose:
            payload["purpose"] = purpose
        self.store.add_outbox("telegram", payload, dedupe_key=dedupe)

    def _administrator_error(self, work_uuid, code):
        for owner in self.settings.admin_ids:
            if self.store.get_user(owner) is None:
                self.store.upsert_user(owner)
            self._notice(owner, work_uuid, "engine.admin_error", code=code,
                         dedupe=f"engine-error:{work_uuid}:{code}")

    def _cancel_notice(self, work_uuid):
        job = self.store.get_job(work_uuid)
        draft = self.store.get_draft(work_uuid)
        work = job or draft
        if not work:
            return
        if job and job["status"] != "cancelled":
            return
        if not job and draft["status"] != "closed":
            return
        actor = work.get("cancellation_actor_id") or work.get("close_actor_id")
        administrator = actor is not None and self.store.is_admin(actor)
        key = "engine.cancelled_admin" if administrator else "dialog.cancelled"
        self._notice(work["owner_id"], work_uuid, key, source=work.get("source_message_id"),
                     purpose="cancellation", dedupe=f"engine-cancelled:{work_uuid}")

    async def recover(self):
        await self.sandbox.recover(self.store.process_bindings())
        for work in self.store.recover_interrupted(utc_now()):
            self._notice(work["owner_id"], work["id"], "engine.interrupted",
                         source=work.get("source_message_id"),
                         dedupe=f"interrupted:{work['id']}")
        self.delivery.reconcile()

    async def _cancel(self, payload):
        work_uuid = payload["work_uuid"]
        if self._active_uuid == work_uuid and self._active is not None:
            self._active.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._active
            self._active = self._active_uuid = self._active_kind = None
        await self.sandbox.cancel(work_uuid)
        draft = self.store.get_draft(work_uuid)
        if draft and draft["status"] == "preparing" and draft["close_requested"]:
            # Копирующий thread уже joined, даже если у него не было process binding.
            self.store.bind_process(work_uuid, None)
        self._cancel_notice(work_uuid)

    async def shutdown(self):
        self._stopping = True
        if self._active is not None and not self._active.done():
            self._active.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._active
        await self.sandbox.shutdown()
        for task in list(self._delivery_tasks.values()):
            # Потоковая подготовка частей должна закончить собственные записи до выхода.
            await asyncio.shield(task)
        self._delivery_tasks.clear()

    def _domain_outbox(self):
        rows = self.store.db.execute(
            "SELECT * FROM outbox WHERE status='pending' AND available_at<=? "
            "AND kind IN ('intake','delivery','cancel_work') ORDER BY id LIMIT 100",
            (utc_now().isoformat(),),
        ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    async def run(self, stop):
        try:
            while not stop.is_set() and not self._stopping:
                self.delivery.reconcile()
                actions = self._domain_outbox()
                for item in actions:
                    if item["kind"] == "cancel_work":
                        await self._cancel(item["payload"])
                        self.store.outbox_sent(item["id"], {"stopped": True})
                    elif item["kind"] == "delivery" and item["id"] not in self._delivery_tasks:
                        self._delivery_tasks[item["id"]] = asyncio.create_task(self._deliver(item))
                for key, task in list(self._delivery_tasks.items()):
                    if task.done():
                        await task
                        del self._delivery_tasks[key]
                if self._active is not None and self._active.done():
                    await self._active
                    self._active = self._active_uuid = self._active_kind = None
                if self._active is None and not self.store.paused():
                    try:
                        check_storage(self.settings)
                    except ConfigError:
                        self._administrator_error("storage", "storage_unavailable")
                    else:
                        job = self.store.claim_next(utc_now())
                        if job:
                            self._active_uuid, self._active_kind = job["id"], "job"
                            self._active = asyncio.create_task(self._process(job))
                        else:
                            intake = next((item for item in actions if item["kind"] == "intake"), None)
                            if intake:
                                self._active_uuid, self._active_kind = intake["payload"]["draft_id"], "intake"
                                self._active = asyncio.create_task(self._import(intake))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
                except TimeoutError:
                    pass
        finally:
            await self.shutdown()

    async def _import(self, item):
        try:
            result = await self.intake.handle(item["payload"])
        except StoreError as exc:
            if exc.code not in ("paused", "execution_busy"):
                raise
            self.store.outbox_retry(item["id"], utc_now(), exc.code)
        except asyncio.CancelledError:
            self.store.outbox_sent(item["id"], {"interrupted": True})
            raise
        else:
            self.store.outbox_sent(item["id"], result)

    async def _deliver(self, item):
        try:
            files = await self.delivery.prepare_files(item["payload"])
            result = self.delivery.enqueue(item["payload"], item["id"], files)
        except (StoreError, OSError) as exc:
            owner = item["payload"]["owner_id"]
            source = item["payload"]["source_job_id"]
            if self.store.authorized(owner):
                self._notice(owner, source, "engine.delivery_unavailable",
                             dedupe=f"delivery-unavailable:{item['id']}")
            self.store.outbox_sent(item["id"], {"unavailable": getattr(exc, "code", "artifact_missing")})
        else:
            self.store.outbox_sent(item["id"], result)

    def _validate_runtime(self, job, core):
        snapshot = job["snapshot"]
        if core["version_info"] != snapshot["version_info"]:
            raise StoreError("core_changed")
        for category, selected in snapshot["models"].items():
            current = core["models"][category].get(selected["name"])
            if current != {key: value for key, value in selected.items() if key != "name"}:
                raise StoreError("models_changed")
        self.store._require_keys(snapshot)

    def _snapshot_config(self, job, tree):
        document = {"providers": {}}
        for category, selected in job["snapshot"]["models"].items():
            spec = {key: value for key, value in selected["spec"].items()
                    if key not in ("name", "api_model")}
            spec["default"] = True
            document[category] = {selected["name"]: spec}
            provider = selected.get("provider")
            if provider and provider["name"]:
                document["providers"][provider["name"]] = {
                    key: value for key, value in provider.items() if key != "name"
                }
        path = tree / "config.yaml"
        if path.exists():
            raise StoreError("output_not_fresh")
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return document

    def _argv(self, job, core):
        uv = shutil.which("uv")
        if not uv:
            raise StoreError("uv_unavailable")
        files = [file for file in self.store.files(job["id"]) if not file["removed"]]
        media = next(file for file in files if file["role"] == "media")
        argv = [uv, "tool", "run", "--offline", "--no-config",
                "--python", str(Path(core["runtime"]["python_executable"]).resolve()), "absorbent",
                "/work/" + media["relative_path"], "--config", "/work/config.yaml",
                "--output-dir", "/work/output", "--journal", "/work/events.jsonl",
                "--max-material-pages", str(self.settings.max_material_pages), "--skip-render"]
        selected = job["snapshot"]["settings"]
        for name in ("language", "output_language", "fps", "hash_threshold",
                     "min_scene_seconds", "slides_dpi"):
            argv += ["--" + name.replace("_", "-"), str(selected[name])]
        for category, model in job["snapshot"]["models"].items():
            argv += ["--stt" if category == "stt" else "--" + category + "-model", model["name"]]
        for file in files:
            if file["role"] == "material":
                argv += ["--slides", "/work/" + file["relative_path"]]
        return argv

    def _progress_action(self, job, stage, *, final=False):
        now = time.monotonic()
        previous = self._progress.get(job["id"], {})
        if not final and now - previous.get("last", -10) < 5:
            previous["pending"] = stage
            return
        owner = job["owner_id"]
        user = self.store.get_user(owner)
        if not user or not self.store.authorized(owner):
            return
        text = tr(user["language"], "engine.progress", id=job["id"],
                  stage=tr(user["language"], stage))
        outbox_id = previous.get("outbox_id")
        sent = self.store.db.execute("SELECT result FROM outbox WHERE id=? AND status='sent'", (outbox_id,)).fetchone() if outbox_id else None
        message_id = json.loads(sent["result"] or "{}").get("message_id") if sent else None
        if message_id:
            self.store.add_outbox("telegram", {"owner_id": owner, "method": "edit_message_text",
                "purpose": "progress", "work_uuid": job["id"],
                "kwargs": {"chat_id": owner, "message_id": message_id, "text": text}})
        elif not outbox_id:
            outbox_id = self.store.add_outbox("telegram", {"owner_id": owner, "method": "send_message",
                "purpose": "progress", "work_uuid": job["id"],
                "kwargs": {"chat_id": owner, "text": text, "reply_parameters": {
                    "message_id": job["source_message_id"], "allow_sending_without_reply": True}}})
        else:
            with self.store.transaction():
                pending = self.store.db.execute(
                    "SELECT payload FROM outbox WHERE id=? AND kind='telegram' AND status='pending'",
                    (outbox_id,),
                ).fetchone()
                if pending:
                    payload = json.loads(pending["payload"])
                    if (payload.get("owner_id") != owner
                            or payload.get("work_uuid") != job["id"]
                            or payload.get("purpose") != "progress"):
                        raise StoreError("progress_conflict")
                    payload["kwargs"]["text"] = text
                    self.store.db.execute(
                        "UPDATE outbox SET payload=? WHERE id=? AND status='pending'",
                        (json.dumps(payload, ensure_ascii=False, allow_nan=False), outbox_id),
                    )
        self._progress[job["id"]] = {"last": now, "outbox_id": outbox_id, "pending": None}

    def _journal(self, job, ingestor, path, metrics, *, final=False):
        events = ingestor.read(path, final=final)
        for event in events:
            if event.get("store_error"):
                raise StoreError("journal_persistence_failed")
            if event.get("kind") in ("foreign_run", "malformed"):
                raise StoreError("journal_invalid")
            if event.get("event") == "stage_end":
                metrics.setdefault("stages", {})[event["stage"]] = {
                    "status": event.get("status"),
                    "elapsed_seconds": event.get("elapsed_seconds"),
                }
        return events

    @staticmethod
    def _resources(reports: list[dict]) -> dict:
        """Свод фаз и полные отчёты: причина GPU и наблюдения чужих устройств не теряются."""
        values = aggregate_reports(reports)
        values["phase_reports"] = [report if isinstance(report, dict) else {} for report in reports]
        return values

    async def _phase(self, job, phase, argv, core, ingestor, reports, metrics, deadline, *, network, keys=()):
        tree = self.settings.storage_dir / "jobs" / job["id"]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxError("timeout")
        sampler = PhaseMetrics(self.settings, self.store, job["id"], phase)
        result = None
        try:
            async with sampler:
                task = asyncio.create_task(self.sandbox.run(
                    job["id"], phase, argv, runtime=core["runtime"],
                    network=network, selected_key_names=keys,
                    tex_roots=tuple(core["runtime"].get("tex_roots", ())),
                    timeout=remaining,
                ))
                try:
                    while not task.done():
                        events = self._journal(job, ingestor, tree / "events.jsonl", metrics)
                        for event in events:
                            stage = event.get("stage")
                            if event.get("event") == "stage_start" and stage:
                                self._progress_action(job, "stage." + stage)
                        pending = self._progress.get(job["id"], {}).get("pending")
                        if pending:
                            self._progress_action(job, pending)
                        await asyncio.sleep(0.25)
                    result = await task
                except BaseException:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    raise
                finally:
                    self._journal(job, ingestor, tree / "events.jsonl", metrics, final=task.done())
        finally:
            reports.append(sampler.finish(result.get("resource_report") if result else None))
        if result["exit_code"] != 0:
            raise StoreError("render_failed" if phase == "render" else "processing_failed")
        return result

    async def _process(self, job):
        started = time.monotonic()
        metrics = deepcopy(job["metrics"])
        deadline = started + self.settings.max_processing_seconds
        metrics["queue_wait_seconds"] = max(0, (utc_now() - datetime.fromisoformat(job["accepted_at"])).total_seconds())
        preparation = metrics.get("preparation") if isinstance(metrics.get("preparation"), dict) else {}
        input_wall = preparation.get("wall_seconds")
        input_wall = float(input_wall) if isinstance(input_wall, (int, float)) and not isinstance(input_wall, bool) and input_wall >= 0 else 0.0
        # Собственные отчёты подготовки входа попадают в свод раньше отчётов фаз ядра.
        reports = [operation.get("resources") for operation in preparation.get("operations", [])
                   if isinstance(operation, dict)]
        ingestor = JournalIngestor(self.store, job["id"], self.pricing, job["snapshot"]["models"])
        tree = self.settings.storage_dir / "jobs" / job["id"]
        try:
            core = await asyncio.to_thread(probe_core, self.settings)
            self._validate_runtime(job, core)
            self.core = core
            self.dialog.refresh_core(core)
            self.intake.core = core
            if not job["force_new"]:
                existing = self.store.find_reuse(job["input_signature"], exclude_job_id=job["id"])
                if existing:
                    self.store.await_reuse(job["id"], existing["id"], utc_now())
                    self.dialog.offer_late_reuse(job["owner_id"], job["id"])
                    return
            tree = create_work_tree(self.settings, job["id"])
            output = tree / "output"
            if any(output.iterdir()):
                raise StoreError("output_not_fresh")
            self._snapshot_config(job, tree)
            metrics["preparation_seconds"] = input_wall + (time.monotonic() - started)
            self.store.update_job(job["id"], status="running", started_at=utc_now(), metrics=metrics)
            self._progress_action(job, "status.running")
            keys = tuple(sorted({model["provider"]["api_key_env"] for model in job["snapshot"]["models"].values()
                                 if model.get("provider", {}).get("api_key_env")}))
            processing = time.monotonic()
            await self._phase(job, "synthesize", self._argv(job, core), core, ingestor, reports, metrics, deadline, network=True, keys=keys)
            metrics["processing_seconds"] = time.monotonic() - processing
            self._progress_action(job, "stage.render")
            rendering = time.monotonic()
            uv = shutil.which("uv")
            argv = [uv, "tool", "run", "--offline", "--no-config",
                    "--python", str(Path(core["runtime"]["python_executable"]).resolve()), "--from", "absorbent", "python",
                    "/workers/render.py", "/work/output/notes.md", job["snapshot"]["settings"]["output_language"]]
            await self._phase(job, "render", argv, core, ingestor, reports, metrics, deadline, network=False)
            metrics["render_seconds"] = time.monotonic() - rendering
            packaging = asyncio.create_task(asyncio.to_thread(package_result, tree))
            try:
                await asyncio.shield(packaging)
            except asyncio.CancelledError:
                await asyncio.shield(packaging)
                raise
            self.store.update_job(job["id"], status="completed", metrics=metrics,
                                  resources=self._resources(reports))
            self._progress_action(job, "status.completed", final=True)
            self.store.add_outbox("delivery", {"owner_id": job["owner_id"], "source_job_id": job["id"],
                "work_uuid": job["id"]}, dedupe_key=f"completed-delivery:{job['id']}")
        except asyncio.CancelledError:
            await self.sandbox.cancel(job["id"])
            current = self.store.get_job(job["id"])
            if current["status"] not in TERMINAL_JOBS:
                cancelled = current["status"] == "cancel_requested"
                self.store.update_job(job["id"], status="cancelled" if cancelled else "failed",
                                      failure_reason=None if cancelled else "interrupted",
                                      metrics=metrics, resources=self._resources(reports))
                if cancelled:
                    self._cancel_notice(job["id"])
                else:
                    self._notice(job["owner_id"], job["id"], "engine.interrupted",
                                 source=job["source_message_id"], dedupe=f"interrupted:{job['id']}")
            raise
        except (StoreError, SandboxError, ConfigError, OSError, ValueError) as exc:
            await self.sandbox.cancel(job["id"])
            current = self.store.get_job(job["id"])
            if current["status"] not in TERMINAL_JOBS:
                code = getattr(exc, "code", "storage_unavailable" if isinstance(exc, ConfigError) else "processing_failed")
                if current["status"] == "cancel_requested":
                    self.store.update_job(job["id"], status="cancelled", metrics=metrics,
                                          resources=self._resources(reports))
                    self._cancel_notice(job["id"])
                else:
                    self.store.update_job(job["id"], status="failed", failure_reason=code,
                                          metrics=metrics, resources=self._resources(reports))
                    self._notice(job["owner_id"], job["id"], "engine.failed", source=job["source_message_id"],
                                 dedupe=f"failed:{job['id']}")
                    self._administrator_error(job["id"], code)
        finally:
            if (tree / "events.jsonl").is_file():
                ingestor.read(tree / "events.jsonl", final=True)
            self._progress.pop(job["id"], None)

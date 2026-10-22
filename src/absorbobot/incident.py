"""Аварийная диагностика: инцидент, headless-агент и отчёт администраторам."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import FSInputFile

from .config import Settings, load_secrets
from .i18n import tr
from .store import INTERNAL_FAILURES, RETRYABLE_FAILURES, Store, StoreError, utc_now


MODEL = "deepseek/deepseek-flash"
COOLDOWN_SECONDS = 1800
JOB_STALL_SECONDS = 900
DIAGNOSE_TIMEOUT_SECONDS = 1800
OMP_CANDIDATES = ("omp",)
STATUSES = ("fixed", "partial", "not_fixed", "failed", "unknown")


def incident_dir(settings: Settings, incident_id: str) -> Path:
    """Каталог инцидента внутри state_dir, без выхода за его пределы."""
    root = settings.state_dir / "incidents"
    target = root / incident_id
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("incident_outside_state")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    return target


def _prompt(settings: Settings, incident: dict, context: dict) -> str:
    directory = incident_dir(settings, incident["id"])
    repo = Path.home() / "devel" / "absorbobot"
    job = incident.get("job_id") or "нет"
    return f"""# Аварийная диагностика Absorbobot

Ты — дежурный инженер Absorbobot. Работай автономно, не задавай вопросов и не
перекладывай ремонт на человека. Твоя задача — найти корневую причину сбоя,
исправить её, проверить исправление и оставить отчёт.

## Инцидент

- ID: `{incident['id']}`
- Вид: `{incident['kind']}`
- Задание: `{job}`
- Причина запуска: `{incident['fingerprint']}`
- Каталог инцидента: `{directory}`
- Журнал запуска агента: `{directory / 'output.log'}`
- Модель: `{MODEL}`

## Контекст

```json
{json.dumps(context, ensure_ascii=False, indent=2)}
```

## Ограничения

- Не отправляй сообщения в Telegram и не меняй `~/.config/absorbobot/secrets.env`.
- Не выполняй `git push`, не ставь теги и не публикуй релизы. Разрешён только
  локальный коммит в `{repo}`.
- Не трогай данные пользователей и не удаляй рабочие каталоги заданий, кроме
  временных файлов, созданных самим ремонтом.
- Если причина внешняя (диск, сеть, провайдер) и кодом не исправляется, честно
  поставь `not_fixed` и опиши, что нужно человеку.

## Порядок работы

1. Прочитай журнал службы (`journalctl --user -u absorbobot.service`), состояние
   инцидента, БД `{settings.state_dir / 'absorbobot.sqlite3'}` и каталог задания,
   если он указан. Не печатай секреты и ключи.
2. Воспроизведи причину или докажи её по коду и журналу. Правь репозиторий
   `{repo}`.
3. Запусти `uv run pytest -q` из репозитория. Коммит делай только при зелёных
   тестах; сообщение коммита — по-русски, автор и коммиттер — пользователь.
4. Установи исправление из полного SHA коммита:
   `uv tool install --force "git+file://{repo}@<SHA>"`.
   Если uv не принимает file-URL, установи из чистого дерева
   `uv tool install --force {repo}` и укажи это в отчёте.
5. Перезапусти `systemctl --user restart absorbobot.service` и проверь
   `~/.local/share/absorbobot/bin/absorbobot check --config ~/.config/absorbobot/config.yaml`.
   Убедись, что служба `active`, а в журнале нет новой ошибки.
6. Запиши `{directory / 'report.md'}` — полный отчёт: симптом, корневая причина,
   что изменено, чем проверено, остаточные риски.
7. Запиши `{directory / 'result.json'}` строго в таком виде:

```json
{{
  "status": "fixed",
  "summary": "одна-две фразы для администратора",
  "root_cause": "причина",
  "actions": ["что сделано"],
  "commits": ["полный SHA"],
  "tests": "что и с каким результатом запущено",
  "risks": ["что осталось"]
}}
```

`status` — одно из `fixed`, `partial`, `not_fixed`. `fixed` ставь только после
успешной установки, перезапуска и проверки. Если ты не смог исправить, всё
равно запиши отчёт и `result.json` со статусом `not_fixed`.
"""


class IncidentManager:
    """Создать инцидент и запустить отдельный unit диагностики."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    async def job_failure(self, job: dict, code: str, *, context: dict | None = None,
                          user_notified: bool = False) -> dict | None:
        if not job or code not in INTERNAL_FAILURES:
            return None
        incident, created = self.store.add_incident(
            "job", code, job_id=job["id"], context=context or {},
            cooldown_seconds=COOLDOWN_SECONDS, user_notified=user_notified,
        )
        if not created:
            return incident
        await self._prepare_and_start(incident, context or {})
        return self.store.get_incident(incident["id"])

    async def service_failure(self, *, start: bool = True) -> dict:
        context = self._service_context()
        incident, created = self.store.add_incident(
            "service", "service:service", context=context,
            cooldown_seconds=COOLDOWN_SECONDS,
        )
        if created:
            self._prepare(incident, context)
            if start:
                await self._start(incident)
        return incident

    async def start_manual(self, incident_id: str) -> None:
        """Подготовить и запустить инцидент, созданный командой /fix."""
        incident = self.store.get_incident(incident_id)
        if incident is None or incident["status"] != "queued":
            return
        context = dict(incident.get("context") or {})
        try:
            status = subprocess.run(
                ["systemctl", "--user", "show", "absorbobot.service", "-p", "ActiveState", "-p", "NRestarts"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            status = ""
        context.update({
            "manual": True,
            "service_status": status,
            "deployment_file": str(self.settings.state_dir / "deployment.json"),
            "state_dir": str(self.settings.state_dir),
            "storage_dir": str(self.settings.storage_dir),
        })
        try:
            self._prepare(incident, context)
        except (OSError, ValueError, StoreError) as exc:
            self.store.update_incident(incident["id"], status="failed",
                                       error=f"prompt_failed:{type(exc).__name__}")
            return
        await self._start(incident)

    def _service_context(self) -> dict:
        """Снимок службы и затронутых заданий для промпта агента."""
        cutoff = (utc_now() - timedelta(minutes=10)).isoformat(timespec="microseconds")
        rows = self.store.db.execute(
            "SELECT DISTINCT owner_id FROM jobs WHERE status IN ('preparing','running','cancel_requested') "
            "OR (status='failed' AND failure_reason='interrupted' AND completed_at>?)",
            (cutoff,),
        ).fetchall()
        owners = sorted({row[0] for row in rows})
        retry = self.store.db.execute(
            "SELECT id FROM jobs WHERE status='failed' AND failure_reason='interrupted' AND completed_at>? "
            "ORDER BY completed_at DESC LIMIT 20",
            (cutoff,),
        ).fetchall()
        try:
            status = subprocess.run(
                ["systemctl", "--user", "show", "absorbobot.service", "-p", "ActiveState", "-p", "NRestarts"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            status = ""
        return {
            "service": "absorbobot.service",
            "service_status": status,
            "affected_owners": owners,
            "retry_jobs": [row[0] for row in retry],
            "deployment_file": str(self.settings.state_dir / "deployment.json"),
            "state_dir": str(self.settings.state_dir),
            "storage_dir": str(self.settings.storage_dir),
        }

    def _prepare(self, incident: dict, context: dict) -> None:
        directory = incident_dir(self.settings, incident["id"])
        prompt_path = directory / "prompt.md"
        prompt_path.write_text(_prompt(self.settings, incident, context), encoding="utf-8")
        self.store.update_incident(
            incident["id"], prompt_path=str(prompt_path), model=MODEL,
            output_path=str(directory / "output.log"),
        )

    async def _start(self, incident: dict) -> None:
        try:
            await asyncio.to_thread(self._start_unit, incident["id"])
        except (OSError, subprocess.SubprocessError, StoreError) as exc:
            detail = " ".join(str(exc).split())[:300]
            self.store.update_incident(incident["id"], status="failed",
                                       error=f"diagnose_start_failed:{type(exc).__name__}:{detail}")
            self._start_failed(incident)

    def _start_failed(self, incident: dict) -> None:
        for admin in self.settings.admin_ids:
            user = self.store.get_user(admin)
            language = (user or {}).get("language", "ru")
            text = tr(language, "incident_admin_start_failed", id=incident["id"])
            self.store.add_outbox("telegram", {
                "owner_id": admin, "method": "send_message",
                "kwargs": {"chat_id": admin, "text": text},
            }, dedupe_key=f"diagnose-start-failed:{incident['id']}:{admin}")

    async def _prepare_and_start(self, incident: dict, context: dict) -> None:
        try:
            self._prepare(incident, context)
        except (OSError, ValueError, StoreError) as exc:
            self.store.update_incident(incident["id"], status="failed",
                                       error=f"prompt_failed:{type(exc).__name__}")
            return
        await self._start(incident)

    @staticmethod
    def _start_unit(incident_id: str) -> None:
        run = subprocess.run(
            ["systemctl", "--user", "start", "--no-block", f"absorbobot-diagnose@{incident_id}.service"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if run.returncode:
            raise OSError((run.stderr or "systemctl start failed").strip()[:500])


class TelegramNotifier:
    """Прямые сообщения администраторам, не зависящие от живого бота."""

    def __init__(self, settings: Settings):
        token = os.environ.get("ABSORBOBOT_TOKEN") or load_secrets(settings).get("ABSORBOBOT_TOKEN")
        if not token:
            raise StoreError("telegram_token_missing")
        session = AiohttpSession(api=TelegramAPIServer.from_base(
            settings.bot_api_url, is_local=settings.bot_api_url != "https://api.telegram.org"), timeout=30)
        self.bot = Bot(token, session=session)

    async def close(self) -> None:
        await self.bot.session.close()

    async def text(self, chat_id: int, text: str, keyboard: list[list[dict]] | None = None) -> None:
        chunks: list[str] = []
        while len(text.encode("utf-16-le")) // 2 > 3500:
            end = min(len(text), 1700)
            boundary = text.rfind("\n", 0, end)
            end = boundary + 1 if boundary > 0 else end
            chunks.append(text[:end])
            text = text[end:]
        chunks.append(text)
        for index, chunk in enumerate(chunks):
            markup = {"inline_keyboard": keyboard} if keyboard and index == len(chunks) - 1 else None
            await self.bot.send_message(chat_id, chunk, reply_markup=markup, parse_mode=None)

    async def document(self, chat_id: int, path: Path, caption: str) -> None:
        await self.bot.send_document(chat_id, FSInputFile(path), caption=caption[:1024], parse_mode=None)


def _admin_language(store: Store, admin_id: int) -> str:
    user = store.get_user(admin_id)
    return (user or {}).get("language", "ru")


def _status_text(language: str, status: str) -> str:
    return tr(language, "incident_status_" + status)


def _affected_owners(store: Store, incident: dict) -> list[int]:
    if incident["kind"] == "job" and incident.get("job_id"):
        job = store.get_job(incident["job_id"])
        return [job["owner_id"]] if job else []
    owners = incident.get("context", {}).get("affected_owners", [])
    return [owner for owner in owners if type(owner) is int and owner > 0]


async def _notify_launch(settings: Settings, store: Store, notifier: TelegramNotifier, incident: dict) -> None:
    if incident.get("admins_notified"):
        return
    for admin in settings.admin_ids:
        language = _admin_language(store, admin)
        text = tr(language, "incident_admin_launch", id=incident["id"],
                  reason=incident["fingerprint"], model=MODEL)
        try:
            await notifier.text(admin, text)
        except Exception:
            continue
    store.update_incident(incident["id"], admins_notified=True)


async def _notify_users(store: Store, notifier: TelegramNotifier, incident: dict) -> None:
    if incident.get("user_notified"):
        return
    for owner in _affected_owners(store, incident):
        user = store.get_user(owner)
        if not user:
            continue
        try:
            await notifier.text(owner, tr(user["language"], "engine.system_failure"))
        except Exception:
            continue
    store.update_incident(incident["id"], user_notified=True)


def _find_omp() -> Path | None:
    for candidate in OMP_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return Path(found)
    known = Path.home() / ".cache" / ".bun" / "bin" / "omp"
    return known if known.is_file() and os.access(known, os.X_OK) else None


def _run_agent(settings: Settings, incident: dict) -> dict[str, Any]:
    directory = incident_dir(settings, incident["id"])
    output_path = directory / "output.log"
    session_dir = directory / "sessions"
    session_dir.mkdir(mode=0o700, exist_ok=True)
    omp = _find_omp()
    if omp is None:
        return {"status": "failed", "error": "omp_not_found"}
    prompt = Path(incident["prompt_path"]).read_text(encoding="utf-8")
    repo = Path.home() / "devel" / "absorbobot"
    command = [
        str(omp), "-p", "--model", MODEL, "--approval-mode", "yolo",
        "--max-time", f"{DIAGNOSE_TIMEOUT_SECONDS // 60}m", "--cwd", str(repo),
        "--session-dir", str(session_dir), "--no-title", "--no-extensions", prompt,
    ]
    env = dict(os.environ)
    env.pop("NOTIFY_SOCKET", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    started = time.monotonic()
    timed_out = False
    with open(output_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, cwd=repo, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            exit_code = process.wait(timeout=DIAGNOSE_TIMEOUT_SECONDS + 300)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=30)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            exit_code = process.wait()
    elapsed = int(time.monotonic() - started)
    result: dict[str, Any] = {}
    result_path = directory / "result.json"
    if result_path.is_file() and result_path.stat().st_size <= 200_000:
        try:
            value = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                result = value
        except (OSError, ValueError):
            result = {}
    status = result.get("status")
    if status not in STATUSES[:3]:
        status = "failed" if timed_out or exit_code else "unknown"
    result.update({
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
        "output_path": str(output_path),
        "session_dir": str(session_dir),
    })
    if timed_out:
        result["error"] = "timeout"
    return result


async def _report(settings: Settings, store: Store, notifier: TelegramNotifier, incident: dict, result: dict) -> None:
    report_path = incident_dir(settings, incident["id"]) / "report.md"
    summary = result.get("summary") or result.get("error") or ""
    language = "ru"
    admins = settings.admin_ids
    for admin in admins:
        language = _admin_language(store, admin)
        text = tr(language, "incident_admin_report", id=incident["id"],
                  status=_status_text(language, result["status"]), summary=summary)
        try:
            await notifier.text(admin, text)
            if report_path.is_file():
                await notifier.document(admin, report_path,
                                        tr(language, "incident_report_file"))
            elif result.get("output_path"):
                log_path = Path(result["output_path"])
                if log_path.is_file():
                    await notifier.document(admin, log_path,
                                            tr(language, "incident_report_file"))
        except Exception:
            continue


async def _offer_retry(store: Store, notifier: TelegramNotifier, incident: dict, result: dict) -> None:
    if result.get("status") != "fixed" or incident.get("retry_offered"):
        return
    jobs: list[str] = []
    if incident["kind"] == "job" and incident.get("job_id"):
        jobs.append(incident["job_id"])
    else:
        jobs.extend(incident.get("context", {}).get("retry_jobs", []))
    offered = False
    for job_id in jobs:
        job = store.get_job(job_id)
        if not job or job["status"] != "failed" or job["failure_reason"] not in RETRYABLE_FAILURES:
            continue
        user = store.get_user(job["owner_id"])
        if not user:
            continue
        token = store.issue_token(job["owner_id"], "retry", job_id)
        keyboard = [[{"text": tr(user["language"], "button.retry"), "callback_data": token}]]
        try:
            await notifier.text(job["owner_id"], tr(user["language"], "incident.retry_offer"), keyboard)
            offered = True
        except Exception:
            continue
    if offered:
        store.update_incident(incident["id"], retry_offered=True)


async def _run(settings: Settings, incident_id: str) -> None:
    store = Store(settings)
    notifier: TelegramNotifier | None = None
    try:
        if incident_id == "onfailure":
            manager = IncidentManager(settings, store)
            incident = await manager.service_failure(start=False)
            if incident["status"] != "queued":
                return
            incident_id = incident["id"]
        incident = store.get_incident(incident_id)
        if incident is None:
            return
        incident = store.claim_incident(incident_id)
        if incident is None:
            return
        try:
            notifier = TelegramNotifier(settings)
        except Exception as exc:
            store.update_incident(incident_id, status="failed",
                                  error=f"notifier_failed:{type(exc).__name__}")
            return
        await _notify_launch(settings, store, notifier, incident)
        await _notify_users(store, notifier, incident)
        try:
            result = await asyncio.to_thread(_run_agent, settings, incident)
        except Exception as exc:
            result = {"status": "failed", "error": f"runner_failed:{type(exc).__name__}"}
        store.update_incident(
            incident_id, status=result["status"], finished_at=utc_now(),
            exit_code=result.get("exit_code"), error=result.get("error"),
            report_path=str(incident_dir(settings, incident_id) / "report.md")
            if (incident_dir(settings, incident_id) / "report.md").is_file() else None,
        )
        await _report(settings, store, notifier, incident, result)
        await _offer_retry(store, notifier, incident, result)
    finally:
        if notifier is not None:
            await notifier.close()
        store.close()


def run_incident(settings: Settings, incident_id: str) -> None:
    """Точка входа отдельного unit диагностики."""
    asyncio.run(_run(settings, incident_id))

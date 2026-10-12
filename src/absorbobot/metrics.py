"""Ресурсы фаз, оценка занятости GPU и разбор машинного журнала ядра."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from .store import StoreError

__all__ = ["JournalIngestor", "MetricsError", "PhaseMetrics", "aggregate_reports", "integrate_utilization"]

_PHASE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_REPORT_COMPLETENESS = ("complete", "partial")
_LINE_LIMIT = 1 << 20
_MAX_SHARED_OBSERVATIONS = 4096
# Допустимые ключи расхода: ровно то, что читает Store.stats по нормализованным именам.
_USAGE_ALIASES = {
    "prompt_tokens": ("prompt_tokens",),
    "completion_tokens": ("completion_tokens",),
    "cache_hit": ("cache_hit", "prompt_cache_hit_tokens", "cached_tokens"),
    "cache_miss": ("cache_miss", "prompt_cache_miss_tokens"),
    "reasoning_tokens": ("reasoning_tokens",),
    "total_tokens": ("total_tokens",),
}


class MetricsError(Exception):
    """Безопасная причина без секретов, argv и серверных путей."""

    def __init__(self, code: str, **details: Any):
        super().__init__(code)
        self.code = code
        self.details = details


def _finite(value: Any) -> float | int | None:
    """Конечное неотрицательное измерение или None, без подмены нулём."""
    if type(value) is int:
        return value if value >= 0 else None
    if type(value) is float and math.isfinite(value) and value >= 0:
        return value
    return None


def _json_safe(value: Any) -> Any:
    """Убрать несериализуемое: NaN/Infinity → None, прочее — как есть."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    return str(value)


def integrate_utilization(samples: list[tuple[float, float]]) -> float:
    """Оценка занятости по трапециям; не точное время ядер, а интеграл опроса."""
    points = []
    for offset, percent in samples:
        if (type(offset) in (int, float) and not isinstance(offset, bool)
                and math.isfinite(offset) and type(percent) in (int, float)
                and not isinstance(percent, bool) and math.isfinite(percent)):
            points.append((float(offset), float(percent)))
    points.sort()
    total = 0.0
    for (left, value), (right, next_value) in zip(points, points[1:]):
        if right > left:
            total += (value + next_value) / 2.0 * (right - left) / 100.0
    return total


class _ProcSource:
    """Только /proc: идентичность процесса — целые тики запуска, устойчивые к переиспользованию PID."""

    def __init__(self, root: Path | str = "/proc"):
        self.root = Path(root)

    def list_pids(self) -> list[int]:
        try:
            return [int(name) for name in os.listdir(self.root) if name.isdigit()]
        except OSError:
            return []

    def read(self, pid: int) -> dict[str, int] | None:
        try:
            text = (self.root / str(pid) / "stat").read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        try:
            _, tail = text.rsplit(")", 1)
            fields = tail.split()
            page = os.sysconf("SC_PAGE_SIZE")
            return {"ppid": int(fields[1]), "start_ticks": int(fields[19]),
                    "rss_kib": int(fields[21]) * page // 1024}
        except (ValueError, IndexError, OSError):
            return None


class _NoGpu:
    """Драйвер без процессных метрик: причина, а не выдуманный ноль."""

    def __init__(self, reason: str):
        self.reason = reason

    def available(self) -> bool:
        return False

    def sample(self, pids: set[int]) -> dict:
        return {"devices": [], "processes": [], "device_observations": [],
                "utilization_supported": False, "reason": self.reason}

    def close(self) -> None:
        return None


class _NvmlSource:
    """Тонкая обёртка nvidia-ml-py; процессные метрики отделены от общей загрузки карты."""

    def __init__(self):
        import pynvml

        pynvml.nvmlInit()
        self._nvml = pynvml
        self._devices = []
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            self._devices.append((index, handle, _text(pynvml.nvmlDeviceGetUUID(handle)),
                                  _text(pynvml.nvmlDeviceGetName(handle))))
        self._last_timestamp: dict[int, int] = {}

    def available(self) -> bool:
        return True

    def sample(self, pids: set[int]) -> dict:
        nvml = self._nvml
        devices = [{"index": index, "uuid": uuid, "name": name}
                   for index, _, uuid, name in self._devices]
        processes: list[dict] = []
        observations: list[dict] = []
        utilization_supported = True
        reason = None
        for index, handle, _, _ in self._devices:
            running = []
            for loader in (nvml.nvmlDeviceGetComputeRunningProcesses,
                           nvml.nvmlDeviceGetGraphicsRunningProcesses):
                try:
                    running.extend(loader(handle))
                except nvml.NVMLError:
                    continue
            for process in running:
                if process.pid not in pids:
                    continue
                try:
                    used = int(process.usedGpuMemory)
                except (TypeError, ValueError):
                    used = 0
                processes.append({"index": index, "pid": int(process.pid),
                                  "vram_bytes": max(0, used), "utilization": None})
            try:
                rates = nvml.nvmlDeviceGetUtilizationRates(handle)
                observations.append({"index": index, "uuid": _uuid_of(devices, index),
                                     "gpu_utilization": int(rates.gpu),
                                     "memory_utilization": int(rates.memory)})
            except nvml.NVMLError:
                pass
            last = self._last_timestamp.get(index, 0)
            try:
                samples = nvml.nvmlDeviceGetProcessUtilization(handle, last)
            except nvml.NVMLError:
                utilization_supported = False
                reason = "process_utilization_unsupported"
                samples = []
            highest = last
            for sample in samples:
                highest = max(highest, int(sample.timeStamp))
                if sample.pid not in pids:
                    continue
                processes.append({"index": index, "pid": int(sample.pid), "vram_bytes": 0,
                                  "utilization": int(sample.smUtil)})
            self._last_timestamp[index] = highest
        return {"devices": devices, "processes": processes, "device_observations": observations,
                "utilization_supported": utilization_supported, "reason": reason}

    def close(self) -> None:
        try:
            self._nvml.nvmlShutdown()
        except Exception:
            pass


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _uuid_of(devices: list[dict], index: int) -> str:
    for device in devices:
        if device["index"] == index:
            return device["uuid"]
    return ""


def _make_gpu() -> Any:
    try:
        return _NvmlSource()
    except ImportError:
        return _NoGpu("nvml_unavailable")
    except Exception:
        return _NoGpu("nvml_unavailable")


class PhaseMetrics:
    """Наблюдатель одной фазы: процессные ресурсы payload и занятость GPU узла."""

    def __init__(self, settings, store, work_uuid: str, phase_id: str, *,
                 proc: Any = None, gpu: Any = None,
                 binding_source: Callable[[], list[dict]] | None = None):
        try:
            if str(UUID(work_uuid)) != work_uuid:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise MetricsError("invalid_work_uuid") from None
        if not isinstance(phase_id, str) or not _PHASE_ID.fullmatch(phase_id):
            raise MetricsError("invalid_phase_id")
        self._settings = settings
        self._store = store
        self._work_uuid = work_uuid
        self._phase_id = phase_id
        self._proc = proc if proc is not None else _ProcSource()
        self._gpu = gpu
        self._binding_source = binding_source if binding_source is not None else store.process_bindings
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._started: float | None = None
        self._rss_peak: int | None = None
        self._rss_samples = 0
        self._vram_peak: dict[int, int] = {}
        self._utilization: dict[tuple[int, int], list[tuple[float, float]]] = {}
        self._shared: list[dict] = []
        self._gpu_devices: list[dict] = []
        self._gpu_seen = False
        self._gpu_supported: bool | None = None
        self._utilization_supported: bool | None = None
        self._gpu_reason: str | None = None

    async def __aenter__(self) -> PhaseMetrics:
        if self._task is not None:
            raise MetricsError("already_started")
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self._stop.set()
        task, self._task = self._task, None
        cancelled = False
        if task is not None:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
        if self._gpu is not None and hasattr(self._gpu, "close"):
            self._gpu.close()
        if cancelled:
            raise asyncio.CancelledError
        return False

    async def _loop(self) -> None:
        interval = getattr(self._settings, "metrics_interval_seconds", 1.0)
        if type(interval) not in (int, float) or isinstance(interval, bool) or not math.isfinite(interval):
            interval = 1.0
        interval = max(0.02, float(interval))
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._sample_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def _binding(self) -> dict | None:
        try:
            rows = self._binding_source()
        except Exception:
            return None
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            if row.get("work_uuid") == self._work_uuid:
                binding = row.get("binding")
                if isinstance(binding, dict):
                    return binding
                if row.get("namespace_pid") is not None:
                    return row
        return None

    def _tree(self, root: int) -> tuple[set[int], int]:
        table: dict[int, dict[str, int]] = {}
        for pid in self._proc.list_pids():
            info = self._proc.read(pid)
            if info is not None:
                table[pid] = info
        children: dict[int, list[int]] = {}
        for pid, info in table.items():
            children.setdefault(info["ppid"], []).append(pid)
        seen: set[int] = set()
        stack = [root]
        while stack:
            pid = stack.pop()
            if pid in seen or pid not in table:
                continue
            seen.add(pid)
            stack.extend(children.get(pid, ()))
        return seen, sum(table[pid]["rss_kib"] for pid in seen)

    def _emit_gpu(self, offset: float, pids: set[int], reading: dict) -> None:
        self._gpu_seen = True
        utilization_supported = reading.get("utilization_supported")
        self._utilization_supported = (utilization_supported is not False
                                       and self._utilization_supported is not False)
        if reading.get("reason") and self._gpu_reason is None:
            self._gpu_reason = str(reading["reason"])
        if isinstance(reading.get("devices"), list):
            self._gpu_devices = [device for device in reading["devices"] if isinstance(device, dict)]
        totals: dict[int, int] = {}
        processes = {}
        for entry in reading.get("processes", ()):
            if not isinstance(entry, dict) or entry.get("pid") not in pids:
                continue
            index = entry.get("index")
            if type(index) is not int:
                continue
            identity = (index, int(entry["pid"]))
            previous = processes.get(identity)
            if previous is None or (_finite(entry.get("vram_bytes")) or 0) > (_finite(previous.get("vram_bytes")) or 0):
                processes[identity] = entry
        for (index, pid), entry in processes.items():
            vram = _finite(entry.get("vram_bytes"))
            if vram is not None:
                totals[index] = totals.get(index, 0) + int(vram)
            utilization = _finite(entry.get("utilization"))
            if utilization is not None:
                self._utilization.setdefault((index, pid), []).append((offset, float(utilization)))
        for index, value in totals.items():
            if value > self._vram_peak.get(index, 0):
                self._vram_peak[index] = value
        for observation in reading.get("device_observations", ()):
            if not isinstance(observation, dict) or len(self._shared) >= _MAX_SHARED_OBSERVATIONS:
                continue
            record = {"index": observation.get("index"), "uuid": observation.get("uuid"),
                      "gpu_utilization": _finite(observation.get("gpu_utilization")),
                      "memory_utilization": _finite(observation.get("memory_utilization")),
                      "offset_seconds": offset}
            self._shared.append(record)

    def _sample_once(self, now: float | None = None) -> None:
        if now is None:
            now = time.monotonic()
        if self._started is None:
            self._started = now
        offset = now - self._started
        binding = self._binding()
        root = binding.get("namespace_pid") if isinstance(binding, dict) else None
        start = binding.get("namespace_start_time") if isinstance(binding, dict) else None
        pids: set[int] = set()
        if type(root) is int and type(start) is int and root > 1:
            info = self._proc.read(root)
            # Переиспользованный PID: тики запуска не совпали — наблюдение не приписывается.
            if info is not None and info.get("start_ticks") == start:
                pids, rss = self._tree(root)
                if rss:
                    self._rss_samples += 1
                    if self._rss_peak is None or rss > self._rss_peak:
                        self._rss_peak = rss
        if self._gpu is None:
            self._gpu = _make_gpu()
        if self._gpu is None:
            return
        self._gpu_supported = bool(self._gpu.available())
        try:
            reading = self._gpu.sample(pids)
        except Exception:
            self._gpu_supported = False
            self._gpu_reason = "gpu_query_failed"
            return
        if isinstance(reading, dict):
            self._emit_gpu(offset, pids, reading)
        else:
            self._gpu_supported = False
            self._gpu_reason = "gpu_query_failed"

    def _gpu_resources(self) -> dict:
        supported = bool(self._gpu_seen and self._gpu_supported)
        utilization_supported = bool(supported and self._utilization_supported is not False)
        if supported and utilization_supported:
            busy: float | None = round(
                sum(integrate_utilization(points) for points in self._utilization.values()), 6)
        else:
            busy = None
        reason = self._gpu_reason
        if not supported and reason is None:
            reason = "not_sampled" if not self._gpu_seen else "gpu_unavailable"
        devices = []
        for device in self._gpu_devices:
            index = device.get("index")
            devices.append({"index": index, "uuid": device.get("uuid"), "name": device.get("name"),
                            "vram_peak_bytes": self._vram_peak.get(index) if type(index) is int else None})
        vram_total = sum(self._vram_peak.values()) if self._vram_peak else None
        return {"supported": supported, "reason": reason,
                "utilization_supported": utilization_supported,
                "busy_gpu_seconds": busy, "vram_peak_bytes": vram_total, "devices": devices}

    @staticmethod
    def _valid_report(report: object) -> bool:
        if not isinstance(report, dict) or report.get("schema_version") != 1:
            return False
        if not isinstance(report.get("phase_id"), str) or type(report.get("exit_code")) is not int:
            return False
        if report.get("completeness") not in _REPORT_COMPLETENESS:
            return False
        for key in ("cpu_user_seconds", "cpu_system_seconds", "ru_maxrss_kib"):
            if _finite(report.get(key)) is None:
                return False
        return True

    def finish(self, report: dict | None = None) -> dict:
        """Свести payload-отчёт sandbox и наблюдения узла в JSON-безопасные ресурсы."""
        gpu = self._gpu_resources()
        result: dict[str, Any] = {
            "schema_version": 1, "phase_id": self._phase_id,
            "completeness": "complete", "reason": None, "exit_code": None,
            "cpu_user_seconds": None, "cpu_system_seconds": None, "ru_maxrss_kib": None,
            "host_rss_peak_kib": self._rss_peak, "host_rss_samples": self._rss_samples,
            "busy_gpu_seconds": gpu["busy_gpu_seconds"], "vram_peak_bytes": gpu["vram_peak_bytes"],
            "gpu": {"supported": gpu["supported"], "reason": gpu["reason"],
                    "utilization_supported": gpu["utilization_supported"], "devices": gpu["devices"]},
            "shared_device_observations": list(self._shared),
            "cloud_resources": {"cpu_seconds": None, "gpu_busy_seconds": None,
                                "reason": "cloud_resources_unknown"},
        }
        if self._valid_report(report):
            result["exit_code"] = report["exit_code"]
            result["cpu_user_seconds"] = float(report["cpu_user_seconds"])
            result["cpu_system_seconds"] = float(report["cpu_system_seconds"])
            result["ru_maxrss_kib"] = int(report["ru_maxrss_kib"])
            if report["completeness"] == "partial":
                result["completeness"] = "partial"
                result["reason"] = report.get("reason") if isinstance(report.get("reason"), str) else "partial_report"
        else:
            result["completeness"] = "partial"
            reason = report.get("reason") if isinstance(report, dict) else None
            result["reason"] = reason if isinstance(reason, str) else "missing_report"
        return _json_safe(result)

    @staticmethod
    def aggregate_reports(reports: list[dict]) -> dict:
        """Сложить CPU фактических фаз, а пик памяти взять максимумом, не суммой копий."""
        cpu_user: list[float] = []
        cpu_system: list[float] = []
        rss_peaks: list[int] = []
        host_peaks: list[int] = []
        busy: list[float] = []
        vram: list[int] = []
        incomplete: list[str] = []
        unknown = 0
        for index, report in enumerate(reports or ()):
            report = report if isinstance(report, dict) else {}
            phase = report.get("phase_id") if isinstance(report.get("phase_id"), str) else f"phase_{index}"
            user = _finite(report.get("cpu_user_seconds"))
            system = _finite(report.get("cpu_system_seconds"))
            peak = _finite(report.get("ru_maxrss_kib"))
            host = _finite(report.get("host_rss_peak_kib"))
            gpu = _finite(report.get("busy_gpu_seconds"))
            memory = _finite(report.get("vram_peak_bytes"))
            if user is None or system is None or peak is None:
                unknown += 1
            if user is not None:
                cpu_user.append(float(user))
            if system is not None:
                cpu_system.append(float(system))
            if peak is not None:
                rss_peaks.append(int(peak))
            if host is not None:
                host_peaks.append(int(host))
            if gpu is not None:
                busy.append(float(gpu))
            if memory is not None:
                vram.append(int(memory))
            if report.get("completeness") != "complete":
                incomplete.append(phase)
        return {
            "schema_version": 1,
            "phases": len(reports or ()),
            "cpu_user_seconds": sum(cpu_user) if cpu_user else None,
            "cpu_system_seconds": sum(cpu_system) if cpu_system else None,
            "ru_maxrss_kib": max(rss_peaks) if rss_peaks else None,
            "host_rss_peak_kib": max(host_peaks) if host_peaks else None,
            "busy_gpu_seconds": round(sum(busy), 6) if busy else None,
            "vram_peak_bytes": max(vram) if vram else None,
            "completeness": "complete" if not incomplete and unknown == 0 else "partial",
            "unknown_phases": unknown,
            "incomplete_phase_ids": incomplete,
        }


def aggregate_reports(reports: list[dict]) -> dict:
    """Модульный доступ к своду фаз: движок импортирует его напрямую."""
    return PhaseMetrics.aggregate_reports(reports)


def _catalog_entry(entry: dict) -> dict:
    """Свести один дескриптор модели к локальности и адресу провайдера."""
    provider = entry.get("provider")
    provider = provider if isinstance(provider, dict) else {}
    return {
        "local": bool(entry.get("local")),
        "base_url": provider.get("base_url") if isinstance(provider.get("base_url"), str) else None,
        "provider": provider.get("name") if isinstance(provider.get("name"), str) else None,
    }


def _model_catalog(models: Any) -> dict[str, dict]:
    """Имя модели и api_model → локальность и адрес провайдера, без догадок.

    Поддержаны обе фактические формы снимка: {категория: дескриптор} (движок) и
    {категория: {имя: дескриптор}} (каталог probe).
    """
    result: dict[str, dict] = {}
    if not isinstance(models, dict):
        return result
    for key, value in models.items():
        if not isinstance(value, dict):
            continue
        if "spec" in value or "provider" in value or "local" in value:
            candidates = [(value.get("name") if isinstance(value.get("name"), str) else key, value)]
        else:
            candidates = [(name, entry) for name, entry in value.items() if isinstance(entry, dict)]
        for name, entry in candidates:
            info = _catalog_entry(entry)
            if isinstance(name, str):
                result[name] = info
            spec = entry.get("spec")
            if isinstance(spec, dict):
                api_model = spec.get("api_model") or spec.get("model") or spec.get("name")
                if isinstance(api_model, str):
                    result[api_model] = info
    return result


class JournalIngestor:
    """Инкрементальный разбор JSONL ядра: этапы, уникальные HTTP-попытки и их цена."""

    def __init__(self, store, job_id: str, pricing: Any = None, models: Any = None):
        self._store = store
        self._job_id = str(job_id)
        self._pricing = pricing
        self._catalog = _model_catalog(models)
        self._state: dict[str, dict[str, Any]] = {}
        self._tariff_ready = False

    def _state_for(self, path: str) -> dict[str, Any]:
        return self._state.setdefault(
            path, {"offset": 0, "buffer": b"", "run_id": None, "last_seq": None, "pending": {}})

    def read(self, path: str | Path, final: bool = False) -> list[dict]:
        key = str(Path(path))
        state = self._state_for(key)
        updates: list[dict] = []
        try:
            with open(key, "rb") as stream:
                stream.seek(0, os.SEEK_END)
                end = stream.tell()
                if end < state["offset"]:
                    # Файл переписан: повтор с нуля; повторные строки гасит store.
                    state.update(offset=0, buffer=b"", run_id=None, last_seq=None, pending={})
                stream.seek(state["offset"])
                while state["offset"] < end:
                    chunk = stream.read(min(_LINE_LIMIT, end - state["offset"]))
                    if not chunk:
                        break
                    state["offset"] += len(chunk)
                    pieces = (state["buffer"] + chunk).split(b"\n")
                    state["buffer"] = pieces.pop()
                    for line in pieces:
                        if state.pop("discard_line", False):
                            continue
                        stripped = line.strip()
                        if stripped:
                            updates.extend(self._consume(state, stripped))
                    if len(state["buffer"]) > _LINE_LIMIT:
                        state["buffer"] = b""
                        state["discard_line"] = True
                        updates.append({"kind": "malformed"})
        except (FileNotFoundError, OSError):
            return updates
        if final and state["buffer"]:
            # Последний незавершённый JSON остаётся неизвестным, не нулевым расходом.
            state["buffer"] = b""
            updates.append({"kind": "truncated"})
        return updates

    def _consume(self, state: dict, raw: bytes) -> list[dict]:
        if len(raw) > _LINE_LIMIT:
            return [{"kind": "malformed"}]
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            return [{"kind": "malformed"}]
        if not isinstance(record, dict) or record.get("schema_version") != 1:
            return [{"kind": "malformed"}]
        event = record.get("event")
        run_id = record.get("run_id")
        seq = record.get("seq")
        if (not isinstance(event, str) or not isinstance(run_id, str)
                or type(seq) is not int or not isinstance(record.get("timestamp_utc"), str)):
            return [{"kind": "malformed"}]
        if state["run_id"] is None:
            state["run_id"] = run_id
        elif run_id != state["run_id"]:
            return [{"kind": "foreign_run", "run_id": run_id}]
        if state["last_seq"] is not None and seq <= state["last_seq"]:
            return []
        state["last_seq"] = seq
        handler = getattr(self, "_event_" + event, None)
        if handler is None:
            return []
        return handler(state, record, seq)

    def _event_run_start(self, state: dict, record: dict, seq: int) -> list[dict]:
        return [{"kind": "run_start", "event": "run_start", "run_id": record["run_id"], "seq": seq,
                 "version": record.get("version"), "commit": record.get("commit"),
                 "dirty": record.get("dirty"), "timestamp_utc": record["timestamp_utc"]}]

    def _event_run_end(self, state: dict, record: dict, seq: int) -> list[dict]:
        return [{"kind": "run_end", "event": "run_end", "status": record.get("status"),
                 "exit_code": record.get("exit_code"), "seq": seq,
                 "timestamp_utc": record["timestamp_utc"]}]

    def _event_stage_start(self, state: dict, record: dict, seq: int) -> list[dict]:
        return [{"kind": "stage_start", "event": "stage_start", "stage": record.get("stage"), "seq": seq,
                 "timestamp_utc": record["timestamp_utc"]}]

    def _event_stage_progress(self, state: dict, record: dict, seq: int) -> list[dict]:
        completed, total = _finite(record.get("completed")), _finite(record.get("total"))
        if (not isinstance(record.get("stage"), str) or record.get("unit") not in ("seconds", "images", "frames", "pages")
                or completed is None or completed < 0 or total is not None and (total < 0 or completed > total)):
            return [{"kind": "malformed"}]
        return [{"kind": "stage_progress", "event": "stage_progress", "stage": record["stage"],
                 "unit": record["unit"], "completed": completed, "total": total, "seq": seq,
                 "timestamp_utc": record["timestamp_utc"]}]

    def _event_stage_end(self, state: dict, record: dict, seq: int) -> list[dict]:
        return [{"kind": "stage_end", "event": "stage_end", "stage": record.get("stage"),
                 "status": record.get("status"), "elapsed_seconds": _finite(record.get("elapsed_seconds")),
                 "seq": seq, "timestamp_utc": record["timestamp_utc"]}]

    def _event_api_request_start(self, state: dict, record: dict, seq: int) -> list[dict]:
        request_id = record.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return [{"kind": "malformed"}]
        start = self._call_start(record)
        state["pending"][request_id] = start
        cost = self._pending_cost(start)
        error = self._record(start, None, cost)
        return [self._api_update(start, seq, ended=False, end=None, usage=None, cost=cost, error=error,
                                 event="api_request_start")]

    def _event_api_request_end(self, state: dict, record: dict, seq: int) -> list[dict]:
        request_id = record.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return [{"kind": "malformed"}]
        start = state["pending"].pop(request_id, None)
        if start is None:
            # Без парного start нельзя доказать попытку и нельзя выдумывать начало.
            return [{"kind": "api_call_orphan", "event": "api_request_end",
                     "request_id": request_id, "seq": seq}]
        end = self._call_end(record)
        cost = self._price(start, end)
        error = self._record(start, end, cost)
        return [self._api_update(start, seq, ended=True, end=end, usage=end["usage"], cost=cost,
                                 error=error, event="api_request_end")]

    def _api_update(self, start: dict, seq: int, *, ended: bool, end: dict | None,
                    usage: dict | None, cost: dict, error: str | None, event: str) -> dict:
        return {"kind": "api_call", "event": event, "request_id": start.get("request_id"),
                "stage": start.get("stage"), "provider": start.get("provider"), "model": start.get("model"),
                "seq": seq, "local": self._is_local(start.get("model"), start.get("provider")),
                "ended": ended, "status_code": end.get("status_code") if end else None,
                "usage": usage, "cost": cost, "store_error": error}

    def _call_start(self, record: dict) -> dict:
        return {"job_id": self._job_id, "run_id": record.get("run_id"),
                "request_id": record.get("request_id"), "stage": record.get("stage"),
                "provider": record.get("provider"), "model": record.get("model"),
                "timestamp_utc": record.get("timestamp_utc")}

    def _call_end(self, record: dict) -> dict:
        return {"job_id": self._job_id, "run_id": record.get("run_id"),
                "request_id": record.get("request_id"), "timestamp_utc": record.get("timestamp_utc"),
                "elapsed_seconds": _finite(record.get("elapsed_seconds")),
                "status_code": record.get("status_code"), "error_type": record.get("error_type"),
                "billing": record.get("billing"), "usage": self._normalize_usage(record.get("usage"))}

    @staticmethod
    def _normalize_usage(usage: Any) -> dict[str, Any] | None:
        """Привести расход к ключам, которые читает Store.stats, без второго счёта prompt."""
        if not isinstance(usage, dict):
            return None
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        result: dict[str, Any] = {}
        for key, aliases in _USAGE_ALIASES.items():
            value = None
            for alias in aliases:
                candidate = usage.get(alias)
                if candidate is None and alias == "cached_tokens":
                    candidate = details.get("cached_tokens")
                if candidate is not None:
                    value = _finite(candidate)
                    break
            result[key] = value
        return result

    def _is_local(self, model: Any, provider: Any) -> bool:
        info = self._catalog.get(model) if isinstance(model, str) else None
        if info is not None:
            return bool(info["local"])
        return False

    def _ensure_tariff(self) -> None:
        if self._tariff_ready:
            return
        self._tariff_ready = True
        snapshot = getattr(self._pricing, "snapshot", None) if self._pricing is not None else None
        if isinstance(snapshot, dict) and snapshot:
            try:
                self._store.save_tariff_snapshot(snapshot)
            except StoreError:
                pass

    def _pending_cost(self, start: dict) -> dict:
        return {"amount": None, "currency": "USD", "complete": False, "reason": "pending",
                "job_id": self._job_id,
                "local": self._is_local(start.get("model"), start.get("provider"))}

    def _price(self, start: dict, end: dict) -> dict:
        self._ensure_tariff()
        if self._pricing is None or not hasattr(self._pricing, "estimate"):
            return {"amount": None, "currency": "USD", "complete": False, "reason": "pricing_unavailable",
                    "job_id": self._job_id}
        local = self._is_local(start.get("model"), start.get("provider"))
        extra: dict[str, Any] = {}
        info = self._catalog.get(start.get("model")) if isinstance(start.get("model"), str) else None
        if info is not None:
            if info.get("base_url"):
                extra["base_url"] = info["base_url"]
            if info.get("provider"):
                extra["provider"] = info["provider"]
        try:
            cost = self._pricing.estimate(start, end, local=local, **extra)
        except Exception:
            return {"amount": None, "currency": "USD", "complete": False, "reason": "pricing_failed",
                    "job_id": self._job_id}
        if not isinstance(cost, dict):
            return {"amount": None, "currency": "USD", "complete": False, "reason": "pricing_failed",
                    "job_id": self._job_id}
        cost = dict(cost)
        cost.setdefault("currency", "USD")
        cost["job_id"] = self._job_id
        return cost

    def _record(self, start: dict, end: dict | None, cost: dict) -> str | None:
        try:
            self._store.record_api_call(start, end, cost)
        except StoreError as exc:
            return exc.code
        return None

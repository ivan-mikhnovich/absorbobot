import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from absorbobot.config import Settings
from absorbobot.metrics import JournalIngestor, PhaseMetrics, integrate_utilization
from absorbobot.store import Store

WORK = "11111111-1111-1111-1111-111111111111"
RUN = "47e80cfb-9889-477a-99af-1b2b9f1ae0d5"
OTHER_RUN = "e1d9043b-f576-4224-8842-9ce8a3047596"


@pytest.fixture
def store(tmp_path):
    settings = Settings(admin_ids=(1,), state_dir=tmp_path / "ssd", storage_dir=tmp_path / "mount" / "storage",
                        backing_mount=tmp_path / "mount", min_free_bytes=1)
    with Store(settings) as value:
        value.allow_user(1, 2, "Alice")
        yield value


def running_job(store, owner=2):
    draft = store.create_draft(owner, source_message_id=10)
    tree = store.settings.storage_dir / "jobs" / draft["id"]
    (tree / "input").mkdir(parents=True)
    (tree / "input" / "a.wav").write_bytes(b"x")
    store.add_file(owner, draft["id"], role="media", original_name="a.wav",
                   relative_path="input/a.wav", bytes=1, sha256="a" * 64, duration=1, source_message_id=10)
    snapshot = {"version_info": {"version": "0.1.2", "commit": "b" * 40, "dirty": False}, "models": {}}
    store.update_draft(owner, draft["id"], status="ready", input_signature="sig", snapshot=snapshot)
    job = store.enqueue(owner, draft["id"], datetime.now(timezone.utc), force_new=True)
    store.claim_next(datetime.now(timezone.utc))
    return store.update_job(job["id"], status="running")


# --- PhaseMetrics -------------------------------------------------------------------

class FakeProc:
    def __init__(self, table):
        self.table = {int(pid): dict(info) for pid, info in table.items()}

    def list_pids(self):
        return list(self.table)

    def read(self, pid):
        return self.table.get(pid)


class FakeGpu:
    def __init__(self, readings):
        self._readings = readings
        self._index = 0
        self.closed = False
        self.seen_pids = []

    def available(self):
        return True

    def sample(self, pids):
        self.seen_pids.append(set(pids))
        reading = self._readings[min(self._index, len(self._readings) - 1)]
        self._index += 1
        return reading

    def close(self):
        self.closed = True


class NullGpu(FakeGpu):
    """Пустой, но поддерживаемый источник: юнит-тесты не трогают NVML."""

    def __init__(self):
        super().__init__([{"devices": [], "processes": [], "device_observations": [],
                           "utilization_supported": True, "reason": None}])


def binding_source(namespace_pid=100, namespace_start_time=500):
    return lambda: [{"work_uuid": WORK,
                     "binding": {"namespace_pid": namespace_pid, "namespace_start_time": namespace_start_time}}]


def phase(proc=None, gpu=None, source=None):
    return PhaseMetrics(SimpleNamespace(metrics_interval_seconds=0.02), None, WORK, "synthesize",
                        proc=proc, gpu=NullGpu() if gpu is None else gpu,
                        binding_source=source or binding_source())


REPORT = {"schema_version": 1, "phase_id": "phase-uuid", "exit_code": 0,
          "cpu_user_seconds": 1.5, "cpu_system_seconds": 0.25, "ru_maxrss_kib": 4096,
          "completeness": "complete"}


def test_missing_report_is_partial_and_distinct_from_zero():
    metrics = phase()
    missing = metrics.finish(None)
    assert missing["completeness"] == "partial"
    assert missing["reason"] == "missing_report"
    assert missing["cpu_user_seconds"] is None and missing["ru_maxrss_kib"] is None
    assert missing["host_rss_peak_kib"] is None
    zero = phase().finish({**REPORT, "cpu_user_seconds": 0.0, "cpu_system_seconds": 0.0, "ru_maxrss_kib": 0})
    assert zero["completeness"] == "complete"
    assert zero["cpu_user_seconds"] == 0.0 and zero["ru_maxrss_kib"] == 0
    assert zero["cpu_user_seconds"] is not None


def test_phase_payload_cpu_and_tree_peak_are_separate_metrics():
    proc = FakeProc({100: {"ppid": 1, "start_ticks": 500, "rss_kib": 1000},
                     101: {"ppid": 100, "start_ticks": 501, "rss_kib": 200}})
    metrics = phase(proc=proc)
    metrics._sample_once(now=10.0)
    result = metrics.finish(REPORT)
    assert result["cpu_user_seconds"] == 1.5 and result["ru_maxrss_kib"] == 4096
    # Дерево — отдельное наблюдение, не сумма ru_maxrss близких процессов.
    assert result["host_rss_peak_kib"] == 1200
    assert result["host_rss_samples"] == 1


def test_pid_reuse_is_not_attributed():
    proc = FakeProc({100: {"ppid": 1, "start_ticks": 500, "rss_kib": 1000}})
    metrics = phase(proc=proc)
    metrics._sample_once(now=1.0)
    proc.table[100] = {"ppid": 1, "start_ticks": 999, "rss_kib": 99999}
    metrics._sample_once(now=2.0)
    result = metrics.finish(REPORT)
    assert result["host_rss_peak_kib"] == 1000
    assert result["host_rss_samples"] == 1


def test_external_process_is_excluded_from_tree_and_gpu():
    proc = FakeProc({100: {"ppid": 1, "start_ticks": 500, "rss_kib": 1000},
                     101: {"ppid": 100, "start_ticks": 501, "rss_kib": 500},
                     200: {"ppid": 1, "start_ticks": 700, "rss_kib": 8000}})
    gpu = FakeGpu([{"devices": [{"index": 0, "uuid": "GPU-1", "name": "RTX"}],
                    "processes": [{"index": 0, "pid": 101, "vram_bytes": 100, "utilization": None},
                                  {"index": 0, "pid": 200, "vram_bytes": 7000, "utilization": 90}],
                    "device_observations": [{"index": 0, "uuid": "GPU-1", "gpu_utilization": 30,
                                             "memory_utilization": 40}],
                    "utilization_supported": True, "reason": None}])
    metrics = phase(proc=proc, gpu=gpu)
    metrics._sample_once(now=1.0)
    result = metrics.finish(REPORT)
    assert result["host_rss_peak_kib"] == 1500
    assert result["vram_peak_bytes"] == 100
    assert gpu.seen_pids == [{100, 101}]


def test_unsupported_gpu_is_null_with_reason_not_zero():
    gpu = FakeGpu([{"devices": [], "processes": [], "device_observations": [],
                    "utilization_supported": False, "reason": "nvml_unavailable"}])
    gpu.available = lambda: False
    metrics = phase(proc=FakeProc({100: {"ppid": 1, "start_ticks": 500, "rss_kib": 10}}), gpu=gpu)
    metrics._sample_once(now=1.0)
    result = metrics.finish(REPORT)
    assert result["busy_gpu_seconds"] is None
    assert result["gpu"]["supported"] is False
    assert result["gpu"]["reason"] == "nvml_unavailable"


def test_busy_gpu_is_integrated_estimate_and_shared_observations_stay_separate():
    proc = FakeProc({100: {"ppid": 1, "start_ticks": 500, "rss_kib": 1000},
                     101: {"ppid": 100, "start_ticks": 501, "rss_kib": 10}})
    readings = [
        {"devices": [{"index": 0, "uuid": "GPU-1", "name": "RTX"}],
         "processes": [{"index": 0, "pid": 101, "vram_bytes": 50, "utilization": 0}],
         "device_observations": [{"index": 0, "uuid": "GPU-1", "gpu_utilization": 5, "memory_utilization": 6}],
         "utilization_supported": True, "reason": None},
        {"devices": [{"index": 0, "uuid": "GPU-1", "name": "RTX"}],
         "processes": [{"index": 0, "pid": 101, "vram_bytes": 80, "utilization": 100}],
         "device_observations": [{"index": 0, "uuid": "GPU-1", "gpu_utilization": 70, "memory_utilization": 80}],
         "utilization_supported": True, "reason": None},
    ]
    metrics = phase(proc=proc, gpu=FakeGpu(readings))
    metrics._sample_once(now=100.0)
    metrics._sample_once(now=110.0)
    result = metrics.finish(REPORT)
    assert result["busy_gpu_seconds"] == 5.0
    assert result["gpu"]["utilization_supported"] is True
    assert result["vram_peak_bytes"] == 80
    assert len(result["shared_device_observations"]) == 2
    assert result["shared_device_observations"][0]["offset_seconds"] == 0.0
    assert result["shared_device_observations"][1]["gpu_utilization"] == 70
    json.dumps(result)


def test_aggregate_sums_cpu_and_takes_peak_memory():
    reports = [
        {"phase_id": "a", "completeness": "complete", "cpu_user_seconds": 1.0, "cpu_system_seconds": 0.5,
         "ru_maxrss_kib": 100, "host_rss_peak_kib": 200, "busy_gpu_seconds": 1.0, "vram_peak_bytes": 500},
        {"phase_id": "b", "completeness": "complete", "cpu_user_seconds": 2.0, "cpu_system_seconds": 0.25,
         "ru_maxrss_kib": 300, "host_rss_peak_kib": 150, "busy_gpu_seconds": 2.0, "vram_peak_bytes": 700},
    ]
    total = PhaseMetrics.aggregate_reports(reports)
    assert total["cpu_user_seconds"] == 3.0 and total["cpu_system_seconds"] == 0.75
    assert total["ru_maxrss_kib"] == 300 and total["host_rss_peak_kib"] == 200
    assert total["busy_gpu_seconds"] == 3.0 and total["vram_peak_bytes"] == 700
    assert total["completeness"] == "complete"


def test_aggregate_marks_partial_when_a_phase_report_is_missing():
    partial = PhaseMetrics.aggregate_reports([
        {"phase_id": "a", "completeness": "complete", "cpu_user_seconds": 1.0, "cpu_system_seconds": 0.5,
         "ru_maxrss_kib": 100, "host_rss_peak_kib": 200, "busy_gpu_seconds": None, "vram_peak_bytes": None},
        {"phase_id": "b", "completeness": "partial", "cpu_user_seconds": None, "cpu_system_seconds": None,
         "ru_maxrss_kib": None, "host_rss_peak_kib": None, "busy_gpu_seconds": None, "vram_peak_bytes": None},
    ])
    assert partial["completeness"] == "partial"
    assert partial["unknown_phases"] == 1 and partial["incomplete_phase_ids"] == ["b"]
    assert partial["cpu_user_seconds"] == 1.0 and partial["ru_maxrss_kib"] == 100


def test_integrate_utilization_trapezoid_and_empty():
    assert integrate_utilization([]) == 0.0
    assert integrate_utilization([(0.0, 0.0)]) == 0.0
    assert integrate_utilization([(0.0, 0.0), (10.0, 100.0)]) == 5.0
    assert integrate_utilization([(10.0, 100.0), (0.0, 0.0), (20.0, 100.0)]) == 15.0


def test_async_context_samples_real_process_tree():
    from absorbobot.metrics import _ProcSource
    ticks = _ProcSource().read(os.getpid())["start_ticks"]

    async def scenario():
        metrics = PhaseMetrics(SimpleNamespace(metrics_interval_seconds=0.02), None, WORK, "synthesize",
                               gpu=NullGpu(), binding_source=binding_source(os.getpid(), ticks))
        async with metrics:
            await asyncio.sleep(0.15)
        return metrics.finish(REPORT)

    result = asyncio.run(scenario())
    assert isinstance(result["host_rss_peak_kib"], int) and result["host_rss_peak_kib"] > 0
    assert result["host_rss_samples"] >= 1
    json.dumps(result)


# --- JournalIngestor ----------------------------------------------------------------

def line(run_id, seq, event, **fields):
    payload = {"schema_version": 1, "run_id": run_id, "seq": seq, "event": event,
               "timestamp_utc": "2026-10-10T22:24:42.000000+00:00", **fields}
    return json.dumps(payload, ensure_ascii=False)


def api_start(seq, request_id, stage="synthesize", provider="deepseek", model="deepseek-v4-flash"):
    return line(RUN, seq, "api_request_start", request_id=request_id, stage=stage, provider=provider, model=model)


def api_end(seq, request_id, usage, status_code=200, stage="synthesize", provider="deepseek",
            model="deepseek-v4-flash"):
    return line(RUN, seq, "api_request_end", request_id=request_id, stage=stage, provider=provider, model=model,
                elapsed_seconds=1.5, status_code=status_code, usage=usage, error_type=None,
                billing="reported" if usage else "unknown")


USAGE = {"prompt_tokens": 1151, "completion_tokens": 231, "cache_hit": 384, "cache_miss": 767,
         "reasoning_tokens": 121, "total_tokens": 1382}


class FakePricing:
    def __init__(self, snapshot=None, amount="0.00012", fail=False):
        self.snapshot = snapshot
        self.amount = amount
        self.fail = fail
        self.calls = []
        if snapshot is not None:
            document = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self.digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
        else:
            self.digest = None

    def estimate(self, start, end, local=False, **extra):
        self.calls.append((start.get("model"), local, extra))
        if self.fail:
            raise RuntimeError("pricing boom")
        return {"amount": "0" if local else self.amount, "currency": "USD",
                "complete": True, "reason": "local" if local else None,
                "tariff_digest": self.digest, "multiplier": 0.5 if local else 1.0}


def write(tmp_path, name, *lines):
    path = tmp_path / name
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def calls(store):
    return [dict(row) for row in store.db.execute("SELECT * FROM api_calls ORDER BY id")]


def test_journal_records_unique_attempts_with_job_uuid_attribution(store, tmp_path):
    job = running_job(store)
    path = write(tmp_path, "events.jsonl",
                 line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False),
                 line(RUN, 2, "stage_start", stage="synthesize"),
                 api_start(3, "r1"), api_end(4, "r1", USAGE),
                 api_start(5, "r2", provider="llama-swap", model="qwen3.8-27B"),
                 api_end(6, "r2", None, status_code=502, provider="llama-swap", model="qwen3.8-27B"),
                 line(RUN, 7, "stage_end", stage="synthesize", status="ok", elapsed_seconds=3.6),
                 line(RUN, 8, "run_end", status="ok", exit_code=0))
    pricing = FakePricing()
    catalog = {"llm": {"name": "qwen3.8-27B", "spec": {"api_model": "qwen3.8-27B"},
                       "provider": {"name": "llama-swap", "base_url": "http://127.0.0.1:12434/v1"},
                       "local": True}}
    updates = JournalIngestor(store, job["id"], pricing, catalog).read(path, final=True)
    kinds = [update["kind"] for update in updates]
    assert kinds.count("api_call") == 4
    rows = calls(store)
    assert len(rows) == 2
    assert {row["request_id"] for row in rows} == {"r1", "r2"}
    assert all(row["job_id"] == job["id"] for row in rows)
    assert all(row["run_id"] == RUN for row in rows)
    end = json.loads(rows[0]["event_end"])
    assert set(end["usage"]) == {"prompt_tokens", "completion_tokens", "cache_hit", "cache_miss",
                                 "reasoning_tokens", "total_tokens"}
    assert end["usage"]["cache_hit"] == 384 and end["usage"]["reasoning_tokens"] == 121
    local = json.loads(rows[1]["cost_snapshot"])
    assert local["amount"] == "0"
    assert pricing.calls[1][1] is True
    stats = store.stats(2)
    assert stats["cache_hit"]["known_total"] == 384 and stats["cache_miss"]["known_total"] == 767


def test_sdk_retry_attempts_are_counted_separately(store, tmp_path):
    job = running_job(store)
    path = write(tmp_path, "retry.jsonl",
                 line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False))
    for index, request_id in enumerate(("r1", "r2", "r3"), start=2):
        path.write_text(path.read_text() + api_start(index * 2, request_id, provider="llama-swap",
                                                     model="gemma4-26b-vision") + "\n"
                        + api_end(index * 2 + 1, request_id, None, status_code=502, provider="llama-swap",
                                  model="gemma4-26b-vision") + "\n", encoding="utf-8")
    JournalIngestor(store, job["id"], FakePricing(), {"models": {}}).read(path, final=True)
    assert len(calls(store)) == 3


def test_pending_call_is_null_then_end_replaces_it_once(store, tmp_path):
    job = running_job(store)
    path = write(tmp_path, "pending.jsonl",
                 line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False),
                 api_start(2, "r1"))
    ingestor = JournalIngestor(store, job["id"], FakePricing(amount="0.5"), {"models": {}})
    ingestor.read(path)
    pending = calls(store)[0]
    assert pending["ended_at"] is None and pending["event_end"] is None
    assert json.loads(pending["cost_snapshot"])["amount"] is None
    assert json.loads(pending["cost_snapshot"])["reason"] == "pending"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(api_end(3, "r1", USAGE) + "\n")
    ingestor.read(path, final=True)
    finished = calls(store)[0]
    assert finished["ended_at"] is not None
    assert json.loads(finished["cost_snapshot"])["amount"] == "0.5"


def test_reopen_replay_is_idempotent_and_freezes_recorded_cost(store, tmp_path):
    job = running_job(store)
    path = write(tmp_path, "replay.jsonl",
                 line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False),
                 api_start(2, "r1"), api_end(3, "r1", USAGE))
    JournalIngestor(store, job["id"], FakePricing(amount="0.5"), {"models": {}}).read(path, final=True)
    assert len(calls(store)) == 1
    before = calls(store)[0]["cost_snapshot"]
    later = FakePricing(amount="9.9")
    second = JournalIngestor(store, job["id"], later, {"models": {}})
    replayed = second.read(path, final=True)
    rows = calls(store)
    assert len(rows) == 1
    assert rows[0]["cost_snapshot"] == before
    assert json.loads(rows[0]["cost_snapshot"])["amount"] == "0.5"
    # Повтор строки с тем же (run_id, request_id) не переписывает уже списанную сумму.
    assert any(update.get("store_error") == "api_attempt_conflict" for update in replayed)


def test_tariff_snapshot_is_saved_before_cost_is_recorded(store, tmp_path):
    job = running_job(store)
    snapshot = {"observed": "2026-10-10", "rates": {"flash": {"hit": "0.006"}}}
    pricing = FakePricing(snapshot=snapshot, amount="0.25")
    path = write(tmp_path, "tariff.jsonl",
                 line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False),
                 api_start(2, "r1"), api_end(3, "r1", USAGE))
    JournalIngestor(store, job["id"], pricing, {"models": {}}).read(path, final=True)
    row = calls(store)[0]
    assert row["tariff_digest"] == pricing.digest
    assert store.db.execute("SELECT COUNT(*) FROM tariff_snapshots WHERE digest=?", (pricing.digest,)).fetchone()[0] == 1
    assert json.loads(row["cost_snapshot"])["amount"] == "0.25"


def test_partial_trailing_line_is_streamed_not_lost(store, tmp_path):
    job = running_job(store)
    path = tmp_path / "stream.jsonl"
    path.write_text(line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False) + "\n"
                    + api_start(2, "r1"), encoding="utf-8")
    ingestor = JournalIngestor(store, job["id"], FakePricing(), {"models": {}})
    ingestor.read(path)
    assert calls(store) == []
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n" + api_end(3, "r1", USAGE) + "\n")
    updates = ingestor.read(path, final=True)
    assert len(calls(store)) == 1
    assert any(update["kind"] == "api_call" and update["ended"] for update in updates)


def test_foreign_run_and_malformed_lines_are_guarded(store, tmp_path):
    job = running_job(store)
    path = write(tmp_path, "guard.jsonl",
                 line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False),
                 "{not json",
                 line(OTHER_RUN, 2, "stage_start", stage="foreign"),
                 line(RUN, 3, "stage_start", stage="synthesize"),
                 api_start(4, "r1"), api_end(5, "r1", USAGE))
    updates = JournalIngestor(store, job["id"], FakePricing(), {"models": {}}).read(path, final=True)
    assert any(update["kind"] == "malformed" for update in updates)
    assert any(update["kind"] == "foreign_run" for update in updates)
    assert not any(update.get("stage") == "foreign" for update in updates)
    assert len(calls(store)) == 1 and calls(store)[0]["request_id"] == "r1"


def test_final_discards_truncated_tail(store, tmp_path):
    job = running_job(store)
    path = tmp_path / "final.jsonl"
    path.write_text(line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False) + "\n"
                    + api_start(2, "r1")[:20], encoding="utf-8")
    updates = JournalIngestor(store, job["id"], FakePricing(), {"models": {}}).read(path, final=True)
    assert updates[-1]["kind"] == "truncated"
    assert calls(store) == []


def test_pricing_failure_keeps_cost_unknown(store, tmp_path):
    job = running_job(store)
    path = write(tmp_path, "fail.jsonl",
                 line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False),
                 api_start(2, "r1"), api_end(3, "r1", USAGE))
    JournalIngestor(store, job["id"], FakePricing(fail=True), {"models": {}}).read(path, final=True)
    cost = json.loads(calls(store)[0]["cost_snapshot"])
    assert cost["amount"] is None and cost["reason"] == "pricing_failed"


def test_model_catalog_forwards_base_url_and_provider(store, tmp_path):
    job = running_job(store)
    catalog = {"llm": {"name": "deepseek-v4-flash", "spec": {"api_model": "deepseek-v4-flash"},
                       "provider": {"name": "deepseek", "base_url": "https://api.deepseek.com"},
                       "local": False}}
    pricing = FakePricing()
    path = write(tmp_path, "catalog.jsonl",
                 line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False),
                 api_start(2, "r1"), api_end(3, "r1", USAGE))
    JournalIngestor(store, job["id"], pricing, catalog).read(path, final=True)
    model, local, extra = pricing.calls[0]
    assert model == "deepseek-v4-flash" and local is False
    assert extra == {"base_url": "https://api.deepseek.com", "provider": "deepseek"}


def test_local_catalog_model_is_not_charged_cloud(store, tmp_path):
    job = running_job(store)
    catalog = {"llm": {"name": "local-model", "spec": {"api_model": "local-model"},
                       "provider": {"name": "llama-swap", "base_url": "http://127.0.0.1:8080/v1"},
                       "local": True}}
    pricing = FakePricing()
    path = write(tmp_path, "local.jsonl",
                 line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False),
                 api_start(2, "r1", provider="llama-swap", model="local-model"),
                 api_end(3, "r1", USAGE, provider="llama-swap", model="local-model"))
    updates = JournalIngestor(store, job["id"], pricing, catalog).read(path, final=True)
    assert pricing.calls[0][1] is True
    ended = [update for update in updates if update["kind"] == "api_call" and update["ended"]]
    assert ended[0]["local"] is True and ended[0]["cost"]["amount"] == "0"


def test_duplicate_gpu_process_records_do_not_double_vram():
    proc = FakeProc({100: {"ppid": 1, "start_ticks": 500, "rss_kib": 1000}})
    gpu = FakeGpu([{"devices": [{"index": 0, "uuid": "GPU-1"}],
        "processes": [{"index": 0, "pid": 100, "vram_bytes": 100, "utilization": None},
                      {"index": 0, "pid": 100, "vram_bytes": 80, "utilization": None}],
        "device_observations": [], "utilization_supported": False,
        "reason": "process_utilization_unsupported"}])
    metrics = phase(proc=proc, gpu=gpu)
    metrics._sample_once(now=1)
    report = metrics.finish(REPORT)
    assert report["vram_peak_bytes"] == 100
    assert report["busy_gpu_seconds"] is None


def test_provider_name_cannot_turn_unknown_cloud_cost_into_local_zero(store, tmp_path):
    from absorbobot.pricing import Pricing
    job = running_job(store)
    path = write(tmp_path, "unknown-endpoint.jsonl",
        line(RUN, 1, "run_start", version="0.1.1", commit="a" * 40, dirty=False),
        api_start(2, "r1", provider="llama-swap", model="external-model"),
        api_end(3, "r1", USAGE, provider="llama-swap", model="external-model"))
    JournalIngestor(store, job["id"], Pricing(), {}).read(path, final=True)
    cost = json.loads(calls(store)[0]["cost_snapshot"])
    assert cost["amount"] is None and cost["complete"] is False
    assert store.stats(2, include_cost=True, actor_id=1)["estimated_api_cost"]["incomplete_calls"] == 1


def test_sampler_exit_joins_inflight_thread_before_freezing_gpu_peak():
    import threading

    async def scenario():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release, finished = threading.Event(), threading.Event()

        class BlockingGpu(FakeGpu):
            def sample(self, pids):
                loop.call_soon_threadsafe(entered.set)
                try:
                    assert release.wait(2)
                    return super().sample(pids)
                finally:
                    finished.set()

        gpu = BlockingGpu([{"devices": [{"index": 0, "uuid": "GPU-1"}],
            "processes": [{"index": 0, "pid": 100, "vram_bytes": 100, "utilization": None}],
            "device_observations": [], "utilization_supported": False,
            "reason": "process_utilization_unsupported"}])
        metrics = phase(proc=FakeProc({100: {"ppid": 1, "start_ticks": 500, "rss_kib": 1000}}), gpu=gpu)
        await metrics.__aenter__()
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.create_task(metrics.__aexit__())
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            if closing.done():
                return metrics.finish(REPORT)
            release.set()
            await closing
            return metrics.finish(REPORT)
        finally:
            release.set()
            await asyncio.to_thread(finished.wait, 2)
            await closing

    result = asyncio.run(scenario())
    assert result["vram_peak_bytes"] == 100
    assert result["host_rss_peak_kib"] == 1000

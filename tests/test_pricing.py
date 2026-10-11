"""Стоимость вызовов DeepSeek: границы тарифа, кэш, локальный API и неизменный снимок."""

import hashlib
import json

import pytest

from absorbobot.pricing import (
    CALENDAR_UNKNOWN,
    HOLIDAY,
    OFF_PEAK,
    PEAK,
    WEEKEND,
    Pricing,
    digest_of,
    load_default_snapshot,
)

FLASH = "deepseek-v4-flash"
PRO = "deepseek-v4-pro"
OFFICIAL = "https://api.deepseek.com/v1"
CONTRACT_KEYS = {"amount", "currency", "complete", "reason", "tariff_digest",
                 "tariff_snapshot", "multiplier", "calendar_status"}
USAGE = {"cache_hit": 2000, "cache_miss": 1000, "completion_tokens": 500}


def start_event(model=FLASH, ts="2026-01-12T02:00:00+00:00", base_url=OFFICIAL, provider="deepseek"):
    """Событие api_request_start так, как его пишет ядро Absorbent."""
    return {"schema_version": 1, "run_id": "run", "seq": 1, "event": "api_request_start",
            "request_id": "req", "timestamp_utc": ts, "stage": "synthesize",
            "provider": provider, "model": model, "base_url": base_url}


def end_event(usage, ts="2026-01-12T02:00:05+00:00"):
    """Событие api_request_end с usage или None при транспортном сбое."""
    return {"schema_version": 1, "run_id": "run", "seq": 2, "event": "api_request_end",
            "request_id": "req", "timestamp_utc": ts, "stage": "synthesize",
            "provider": "deepseek", "model": FLASH, "elapsed_seconds": 5.0,
            "status_code": 200, "error_type": None, "billing": "reported", "usage": usage}


@pytest.mark.parametrize("clock, status, multiplier, amount", [
    ("00:59:59", OFF_PEAK, "0.5", "0.000456"),
    ("01:00:00", PEAK, "1", "0.000912"),
    ("03:59:59", PEAK, "1", "0.000912"),
    ("04:00:00", OFF_PEAK, "0.5", "0.000456"),
    ("05:59:59", OFF_PEAK, "0.5", "0.000456"),
    ("06:00:00", PEAK, "1", "0.000912"),
    ("09:59:59", PEAK, "1", "0.000912"),
    ("10:00:00", OFF_PEAK, "0.5", "0.000456"),
])
def test_monday_utc_window_boundaries(clock, status, multiplier, amount):
    pricing = Pricing()
    result = pricing.estimate(start_event(ts=f"2026-01-12T{clock}+00:00"), end_event(USAGE))
    assert result["calendar_status"] == status
    assert result["multiplier"] == multiplier
    assert result["amount"] == amount
    assert result["complete"] is True and result["reason"] is None
    assert result["currency"] == "USD"


@pytest.mark.parametrize("date", ["2026-01-10", "2026-01-11", "2026-02-14"])
def test_weekend_and_makeup_workday_stay_off_peak(date):
    # 2026-02-14 — суббота-отработка рядом с праздником: выходной остаётся вне пика.
    pricing = Pricing()
    result = pricing.estimate(start_event(ts=f"{date}T02:00:00+00:00"), end_event(USAGE))
    assert result["calendar_status"] == WEEKEND
    assert result["multiplier"] == "0.5"
    assert result["amount"] == "0.000456"


@pytest.mark.parametrize("date", ["2026-10-01", "2026-10-05", "2026-02-16", "2026-05-04", "2026-06-19"])
def test_chinese_holiday_is_off_peak(date):
    pricing = Pricing()
    result = pricing.estimate(start_event(ts=f"{date}T02:00:00+00:00"), end_event(USAGE))
    assert result["calendar_status"] == HOLIDAY
    assert result["multiplier"] == "0.5"
    assert result["amount"] == "0.000456"


def test_plain_weekday_inside_window_is_peak():
    pricing = Pricing()
    result = pricing.estimate(start_event(ts="2026-03-02T02:00:00+00:00"), end_event(USAGE))
    assert result["calendar_status"] == PEAK
    assert result["amount"] == "0.000912"


def test_unknown_future_year_is_partial_estimate_not_exact():
    pricing = Pricing()
    result = pricing.estimate(start_event(ts="2027-06-07T02:00:00+00:00"), end_event(USAGE))
    assert result["calendar_status"] == CALENDAR_UNKNOWN
    assert result["complete"] is False
    assert result["reason"] == "calendar_unknown"
    # Праздники неизвестного года не выдумываются: оценка консервативно по пиковым ставкам.
    assert result["amount"] == "0.000912"


def test_official_endpoint_priced_and_other_cloud_named_deepseek_rejected():
    pricing = Pricing()
    good = pricing.estimate(start_event(base_url=OFFICIAL), end_event(USAGE))
    assert good["amount"] == "0.000912" and good["complete"] is True
    impostor = start_event(base_url="https://api.deepseek.example.net/v1", provider="deepseek")
    bad = pricing.estimate(impostor, end_event(USAGE))
    assert bad["amount"] is None and bad["complete"] is False
    assert bad["reason"] == "unknown_provider"
    missing_endpoint = pricing.estimate(start_event(base_url=None), end_event(USAGE))
    assert missing_endpoint["amount"] is None
    assert missing_endpoint["reason"] == "unknown_provider"


def test_local_api_zero_without_usage_and_cloud_missing_usage_is_null():
    pricing = Pricing()
    local = pricing.estimate(start_event(model="qwen3-27b"), None, local=True)
    assert local["amount"] == "0" and local["complete"] is True
    assert local["reason"] == "local_no_charge" and local["multiplier"] == "0"
    # Отличие локального нуля от неизвестного облачного расхода.
    cloud = pricing.estimate(start_event(), None)
    assert cloud["amount"] is None and cloud["complete"] is False
    assert cloud["reason"] == "missing_usage"


def test_end_without_usage_object_is_not_priced():
    pricing = Pricing()
    result = pricing.estimate(start_event(), end_event(None))
    assert result["amount"] is None and result["reason"] == "missing_usage"


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"])
def test_flash_aliases_share_rates(model):
    pricing = Pricing()
    result = pricing.estimate(start_event(model=model), end_event(USAGE))
    assert result["amount"] == "0.000912"


def test_pro_uses_its_own_rates():
    pricing = Pricing()
    result = pricing.estimate(start_event(model=PRO), end_event(USAGE))
    assert result["amount"] == "0.003388"


def test_unknown_model_is_not_priced():
    pricing = Pricing()
    result = pricing.estimate(start_event(model="gpt-4o"), end_event(USAGE))
    assert result["amount"] is None and result["reason"] == "unknown_model"


def test_missing_partition_is_distinct_from_missing_usage():
    pricing = Pricing()
    only_total = {"prompt_tokens": 3000, "completion_tokens": 500}
    result = pricing.estimate(start_event(), end_event(only_total))
    assert result["amount"] is None and result["reason"] == "missing_partition"
    empty = pricing.estimate(start_event(), end_event({}))
    assert empty["amount"] is None and empty["reason"] == "missing_usage"


def test_cache_partitions_replace_prompt_tokens_not_extend_them():
    pricing = Pricing()
    usage = {"cache_hit": 2000, "cache_miss": 1000, "prompt_tokens": 3000, "completion_tokens": 500}
    result = pricing.estimate(start_event(), end_event(usage))
    assert result["amount"] == "0.000912"


def test_partition_mismatch_is_rejected():
    pricing = Pricing()
    usage = {"cache_hit": 2000, "cache_miss": 1000, "prompt_tokens": 5000, "completion_tokens": 500}
    result = pricing.estimate(start_event(), end_event(usage))
    assert result["amount"] is None and result["reason"] == "partition_mismatch"


@pytest.mark.parametrize("bad", [
    {"cache_hit": -1, "cache_miss": 1000, "completion_tokens": 500},
    {"cache_hit": float("nan"), "cache_miss": 1000, "completion_tokens": 500},
    {"cache_hit": float("inf"), "cache_miss": 1000, "completion_tokens": 500},
    {"cache_hit": 2000, "cache_miss": 1000, "completion_tokens": -5},
])
def test_invalid_measurements_are_not_priced(bad):
    pricing = Pricing()
    result = pricing.estimate(start_event(), end_event(bad))
    assert result["amount"] is None and result["reason"] == "invalid_usage"


def test_reasoning_tokens_are_already_inside_completion():
    pricing = Pricing()
    with_reasoning = {"cache_hit": 0, "cache_miss": 1000, "completion_tokens": 100,
                      "reasoning_tokens": 40}
    plain = {"cache_hit": 0, "cache_miss": 1000, "completion_tokens": 100}
    priced = pricing.estimate(start_event(), end_event(with_reasoning))
    reference = pricing.estimate(start_event(), end_event(plain))
    assert priced["amount"] == reference["amount"] == "0.00042"


def test_missing_timestamp_is_not_priced():
    pricing = Pricing()
    start = start_event()
    del start["timestamp_utc"]
    result = pricing.estimate(start, end_event(USAGE))
    assert result["amount"] is None and result["reason"] == "unknown_time"
    assert result["calendar_status"] == CALENDAR_UNKNOWN and result["multiplier"] is None


def test_estimate_result_has_contract_keys():
    pricing = Pricing()
    result = pricing.estimate(start_event(), end_event(USAGE))
    assert set(result) == CONTRACT_KEYS


def test_snapshot_is_immutable_copy_with_stable_digest():
    snapshot = load_default_snapshot()
    pricing = Pricing(snapshot)
    # Изменение входного словаря после создания тарифа не влияет на цены.
    snapshot["tiers"]["flash"]["peak"]["cache_hit"] = "999"
    assert pricing.snapshot["tiers"]["flash"]["peak"]["cache_hit"] == "0.006"
    # Изменение выданной копии не влияет на тариф и digest.
    copy = pricing.snapshot
    copy["models"]["deepseek-v4-flash"] = "changed"
    assert pricing.snapshot["models"]["deepseek-v4-flash"] == "flash"
    default = load_default_snapshot()
    expected = hashlib.sha256(json.dumps(default, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    assert pricing.digest == expected == digest_of(default)
    assert Pricing().digest == pricing.digest


def test_estimate_reports_digest_and_returns_snapshot_copy():
    pricing = Pricing()
    result = pricing.estimate(start_event(), end_event(USAGE))
    assert result["tariff_digest"] == pricing.digest
    result["tariff_snapshot"]["tiers"]["flash"]["peak"]["output"] = "0"
    assert pricing.snapshot["tiers"]["flash"]["peak"]["output"] == "1.20"


def test_historical_cost_uses_the_given_snapshot_never_current_rates():
    frozen = load_default_snapshot()
    frozen["tiers"]["flash"]["peak"] = {"cache_hit": "0.001", "cache_miss": "0.10", "output": "0.50"}
    pricing = Pricing(frozen)
    result = pricing.estimate(start_event(), end_event(USAGE))
    # 2000*0.001 + 1000*0.10 + 500*0.50 = 352, делённое на миллион.
    assert result["amount"] == "0.000352"
    assert result["tariff_digest"] == Pricing(frozen).digest
    assert result["tariff_digest"] != Pricing().digest

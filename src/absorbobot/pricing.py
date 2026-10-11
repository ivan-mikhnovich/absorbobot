"""Дата-привязанный расчёт оценочной стоимости вызовов DeepSeek по журналу ядра."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_SNAPSHOT_PATH = Path(__file__).with_name("assets") / "pricing-2026-10-10.json"

# Официальный хост DeepSeek; другой облачный провайдер с именем «deepseek» им не считается.
OFFICIAL_DEEPSEEK_HOST = "api.deepseek.com"
OFFICIAL_DEEPSEEK_NAMES = ("deepseek",)

CURRENCY = "USD"
TOKENS_PER_MILLION = Decimal(1_000_000)

PEAK = "peak"
OFF_PEAK = "offpeak"
WEEKEND = "weekend"
HOLIDAY = "holiday"
CALENDAR_UNKNOWN = "unknown"

# Причины неполной цены; неизвестное не подменяется нулём.
REASON_MISSING_USAGE = "missing_usage"
REASON_MISSING_PARTITION = "missing_partition"
REASON_PARTITION_MISMATCH = "partition_mismatch"
REASON_INVALID_USAGE = "invalid_usage"
REASON_UNKNOWN_MODEL = "unknown_model"
REASON_UNKNOWN_PROVIDER = "unknown_provider"
REASON_UNKNOWN_TIME = "unknown_time"
REASON_CALENDAR_UNKNOWN = "calendar_unknown"
REASON_LOCAL = "local_no_charge"

# Часовой пояс материкового Китая без перехода на летнее время.
_TZ_CHINA = timezone(timedelta(hours=8), "Asia/Shanghai")

# Отсутствие измерения, недопустимое измерение и обычное отсутствие различаются явно.
_INVALID = object()


class PricingError(Exception):
    """Некорректный снимок тарифа без выдуманных значений."""


def load_default_snapshot() -> dict:
    """Прочитать встроенный датированный снимок тарифа как обычный словарь."""
    with DEFAULT_SNAPSHOT_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def canonical_document(document: dict) -> str:
    """Канонический JSON, по которому считается digest снимка."""
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def digest_of(document: dict) -> str:
    """SHA-256 канонического снимка; совпадает с digest Store.save_tariff_snapshot."""
    return hashlib.sha256(canonical_document(document).encode("utf-8")).hexdigest()


def _amount_str(value: Decimal) -> str:
    """Десятичная сумма без экспоненты и хвостовых нулей, например «0.000912»."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def _token(value):
    """Целое неотрицательное число токенов, None при отсутствии, _INVALID при мусоре."""
    if value is None:
        return None
    if isinstance(value, bool):
        return _INVALID
    if isinstance(value, int):
        return value if value >= 0 else _INVALID
    if isinstance(value, float):
        if math.isfinite(value) and value >= 0 and value.is_integer():
            return int(value)
        return _INVALID
    if isinstance(value, Decimal):
        if value.is_finite() and value >= 0 and value == value.to_integral_value():
            return int(value)
        return _INVALID
    return _INVALID


def _parse_instant(value):
    """Разобрать RFC3339-метку журнала; None, если момент неизвестен."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _host_of(base_url) -> str:
    """Хост из base_url или голого имени хоста без схемы, порта и учётных данных."""
    text = str(base_url).strip()
    parts = urlsplit(text)
    netloc = parts.netloc or parts.path.split("/")[0]
    host = netloc.split("@")[-1].split(":")[0]
    return host.rstrip(".").lower()


def _clock_seconds(value: str) -> int:
    """Секунды от начала суток для «HH:MM» снимка."""
    hour, minute = str(value).split(":")
    return int(hour) * 3600 + int(minute) * 60


class Pricing:
    """Неизменяемый снимок тарифа; прошлые суммы считаются по нему, а не по сети."""

    def __init__(self, snapshot: dict | None = None) -> None:
        document = load_default_snapshot() if snapshot is None else copy.deepcopy(snapshot)
        if not isinstance(document, dict):
            raise PricingError("некорректный снимок тарифа")
        try:
            self._tiers = document["tiers"]
            self._models = document["models"]
            provider = document["providers"]["deepseek"]
            self._official_host = str(provider["host"]).lower()
            self._official_names = frozenset(provider["names"])
            self._off_peak = Decimal(str(document["off_peak_factor"]))
            self._windows = [(_clock_seconds(window["start"]), _clock_seconds(window["end"]))
                             for window in document["peak_windows_utc"]]
            self._holidays = {int(year): frozenset(days)
                              for year, days in document["calendar"]["holidays"].items()}
        except (KeyError, TypeError, ValueError) as exc:
            raise PricingError("некорректный снимок тарифа") from exc
        self._document = document
        self._digest = digest_of(document)

    @property
    def snapshot(self) -> dict:
        """Свежая копия снимка: её изменение не меняет тариф и digest."""
        return copy.deepcopy(self._document)

    @property
    def digest(self) -> str:
        """SHA-256 канонического снимка для cost_snapshot и Store.tariff_snapshots."""
        return self._digest

    def official_provider(self, base_url=None, provider=None) -> bool:
        """Тариф применим только при подтверждённом официальном endpoint."""
        return bool(base_url) and _host_of(base_url) == self._official_host

    def estimate(self, start: dict, end: dict | None, local: bool = False, *,
                 base_url: str | None = None, provider: str | None = None) -> dict:
        """Оценить один HTTP-вызов по паре событий журнала, не сворачивая повторы."""
        start = start or {}
        result = self._blank()
        if local:
            # Локальный API не тарифицируется, но электричество не рассчитано и не выдаётся за ноль денег.
            result.update(amount="0", complete=True, reason=REASON_LOCAL,
                          multiplier="0", calendar_status=None)
            return result
        status, multiplier, calendar_known = self._classify(start.get("timestamp_utc"))
        result["calendar_status"] = status
        result["multiplier"] = None if multiplier is None else _amount_str(multiplier)
        if base_url is None:
            base_url = start.get("base_url") or start.get("provider_base_url")
        if provider is None:
            provider = start.get("provider")
        if not self.official_provider(base_url=base_url, provider=provider):
            result["reason"] = REASON_UNKNOWN_PROVIDER
            return result
        model = start.get("model") or (end or {}).get("model")
        if not isinstance(model, str) or model not in self._models:
            result["reason"] = REASON_UNKNOWN_MODEL
            return result
        if multiplier is None:
            result["reason"] = REASON_UNKNOWN_TIME
            return result
        partition = self._partition(end)
        if isinstance(partition, str):
            result["reason"] = partition
            return result
        hit, miss, output = partition
        amount = self._cost(self._models[model], hit, miss, output, multiplier)
        result.update(amount=_amount_str(amount), complete=calendar_known,
                      reason=None if calendar_known else REASON_CALENDAR_UNKNOWN)
        return result

    def _blank(self) -> dict:
        return {"amount": None, "currency": CURRENCY, "complete": False, "reason": None,
                "tariff_digest": self._digest, "tariff_snapshot": self.snapshot,
                "multiplier": None, "calendar_status": None}

    def _classify(self, timestamp):
        """Режим тарифа по моменту вызова; неизвестный год помечается частичным."""
        moment = _parse_instant(timestamp)
        if moment is None:
            return CALENDAR_UNKNOWN, None, False
        utc = moment.astimezone(timezone.utc)
        china = moment.astimezone(_TZ_CHINA)
        seconds = utc.hour * 3600 + utc.minute * 60 + utc.second
        if utc.weekday() >= 5:
            status, multiplier = WEEKEND, self._off_peak
        elif any(begin <= seconds < finish for begin, finish in self._windows):
            status, multiplier = PEAK, Decimal(1)
        else:
            status, multiplier = OFF_PEAK, self._off_peak
        holidays = self._holidays.get(china.year)
        if holidays is None:
            # Календарь года неизвестен: дни недели известны, праздники — нет.
            return CALENDAR_UNKNOWN, multiplier, False
        if china.date().isoformat() in holidays:
            return HOLIDAY, self._off_peak, True
        return status, multiplier, True

    def _partition(self, end):
        """Разделить usage на вход по кэшу и выход; вернуть кортеж или причину отказа."""
        if not isinstance(end, dict):
            return REASON_MISSING_USAGE
        usage = end.get("usage")
        if not isinstance(usage, dict):
            return REASON_MISSING_USAGE
        hit = _token(usage.get("cache_hit"))
        miss = _token(usage.get("cache_miss"))
        output = _token(usage.get("completion_tokens"))
        if output is None:
            output = _token(usage.get("output_tokens"))
        total = _token(usage.get("prompt_tokens"))
        if total is None:
            total = _token(usage.get("input_tokens"))
        if _INVALID in (hit, miss, output, total):
            return REASON_INVALID_USAGE
        input_evidence = any(usage.get(key) is not None
                             for key in ("cache_hit", "cache_miss", "prompt_tokens", "input_tokens"))
        if hit is None or miss is None:
            return REASON_MISSING_PARTITION if input_evidence else REASON_MISSING_USAGE
        if output is None:
            return REASON_MISSING_USAGE
        if total is not None and total != hit + miss:
            return REASON_PARTITION_MISMATCH
        return hit, miss, output

    def _cost(self, tier: str, hit: int, miss: int, output: int, multiplier: Decimal) -> Decimal:
        """Стоимость по пиковым ставкам с учётом множителя; reasoning уже внутри completion."""
        rates = self._tiers[tier]["peak"]
        billed = (Decimal(hit) * Decimal(rates["cache_hit"])
                  + Decimal(miss) * Decimal(rates["cache_miss"])
                  + Decimal(output) * Decimal(rates["output"]))
        return billed / TOKENS_PER_MILLION * multiplier

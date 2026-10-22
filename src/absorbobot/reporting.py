"""Оформление живого отчёта: таблица этапов, полосы прогресса и rich-блоки Telegram."""
from __future__ import annotations

from .i18n import tr

# Кастомные эмодзи владельца бота: идентификаторы документов Telegram и запасные глифы,
# которые видны там, где кастомный эмодзи недоступен (обычный текст, чужой клиент).
STAGES = {
    "input": {"emoji": "6096080291347042802", "glyph": "📥", "label": "report.stage.input"},
    "prepare": {"emoji": "5449652666365921217", "glyph": "☑️", "label": "report.stage.prepare"},
    "stt": {"emoji": "5402595016101078333", "glyph": "🎶", "label": "report.stage.stt"},
    "summary": {"emoji": "5408875405044124105", "glyph": "✍️", "label": "report.stage.summary"},
    "frames": {"emoji": "5854710508065658472", "glyph": "📸", "label": "report.stage.frames"},
    # Нарезка кадров и их обработка разведены в таблице, но делят один знак.
    "describe": {"emoji": "5854710508065658472", "glyph": "📸", "label": "report.stage.describe"},
    "synthesis": {"emoji": "5287363255457566209", "glyph": "📰", "label": "report.stage.synthesis"},
    "render": {"emoji": "5251558986443932784", "glyph": "📄", "label": "report.stage.render"},
    "delivery": {"emoji": "5202121542045026196", "glyph": "👽", "label": "report.stage.delivery"},
}

# Стадии Absorbent и состояния работы, сведённые к строкам отчёта.
STAGE_OF = {
    "input.external": "input",
    "input.telegram": "input",
    "input.media": "input",
    "input.copy": "input",
    "input.analyze": "input",
    "stage.prepare_slides": "prepare",
    "stage.describe_slides": "prepare",
    "stage.stt": "stt",
    "stage.summarize": "summary",
    "stage.extract_frames": "frames",
    "stage.select_keyframes": "frames",
    "stage.describe_keyframes": "describe",
    "stage.synthesize": "synthesis",
    "stage.render": "render",
    "stage.package": "render",
    "stage.delivery": "delivery",
}

# Строки таблицы обработки Absorbent в порядке конвейера: все этапы видны сразу,
# даже ещё не начатые. Приём файла в таблицу не входит: скачивание — не обработка.
REPORT_ORDER = ("prepare", "frames", "stt", "summary", "describe",
                "synthesis", "render", "delivery")

# Исходы, после которых живой отчёт больше не нужен: его убирают, а причину
# присылают отдельным сообщением.
TERMINAL_STAGES = frozenset({
    "input.ready", "input.failed", "status.failed", "status.cancelled",
    "status.cancel_requested", "status.delivered",
})

BAR_WIDTH = 8
BAR_FILLED, BAR_EMPTY = "▮", "▯"


def stage_key(stage: str) -> str | None:
    """Строка таблицы для стадии журнала ядра или состояния работы."""
    return STAGE_OF.get(stage)


def progress_bar(completed: float, total: float, width: int = BAR_WIDTH) -> str:
    """Полоса заполнения: не выходит за границы даже при чужих счётчиках."""
    if width < 1:
        raise ValueError("bar_width")
    if not isinstance(completed, (int, float)) or isinstance(completed, bool):
        completed = 0
    if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0:
        return BAR_EMPTY * width
    ratio = min(1.0, max(0.0, completed / total))
    filled = min(width, max(0, int(ratio * width)))
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


def _amount(value: float, factor: float, precision: int) -> str:
    return f"{value / factor:.{precision}f}"


def _values(language: str, detail: dict) -> str:
    """Счётчик стадии: фактические числа движка, без выдуманных процентов."""
    completed, total, unit = detail.get("completed"), detail.get("total"), detail.get("unit")
    if not isinstance(completed, (int, float)) or isinstance(completed, bool):
        return ""
    if not isinstance(unit, str) or not unit:
        return ""
    factor = 1000000 if unit == "bytes" else 1
    precision = 1 if unit == "bytes" else 0
    values = _amount(completed, factor, precision)
    if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
        values += " / " + _amount(total, factor, precision)
    return values + " " + tr(language, "progress.unit." + unit)


def _percent(detail: dict) -> str | None:
    completed, total = detail.get("completed"), detail.get("total")
    if not isinstance(completed, (int, float)) or isinstance(completed, bool):
        return None
    if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0:
        return None
    return f"{min(100, completed / total * 100):.0f}"


def _seconds(language: str, elapsed: int | None) -> str:
    if not isinstance(elapsed, int) or elapsed < 0:
        return ""
    return tr(language, "report.seconds", seconds=elapsed)


def _emoji(sign: dict | None) -> object:
    """Кастомный эмодзи rich-блока, а без него — запасной глиф."""
    if sign is not None and sign["emoji"]:
        return {"type": "custom_emoji", "custom_emoji_id": sign["emoji"],
                "alternative_text": sign["glyph"]}
    return sign["glyph"] if sign is not None else "•"


def _bold(text: str) -> dict:
    return {"type": "bold", "text": text}


def _code(text: str) -> dict:
    return {"type": "code", "text": text}


def _cell(text: object, *, header: bool = False) -> dict:
    cell = {"align": "left", "valign": "middle", "text": text}
    if header:
        cell["is_header"] = True
    return cell


def _row(language: str, key: str, row: dict | None) -> tuple[list, str, object, str, str]:
    """Собрать обе формы строки: rich-ячейки и текстовый запасной вид."""
    sign = STAGES[key]
    label = tr(language, sign["label"])
    status = row.get("status") if isinstance(row, dict) else "pending"
    detail = row.get("detail") if isinstance(row, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    elapsed = row.get("elapsed") if isinstance(row, dict) else None
    stage_rich = [_emoji(sign), " ", _bold(label)]
    stage_plain = f"{sign['glyph']} {label}"
    percent = _percent(detail)
    if status == "done":
        progress_rich = [_code(progress_bar(1, 1)), " ", _bold("100 %")]
        progress_plain = f"{progress_bar(1, 1)} 100 %"
        values = _values(language, detail) or _seconds(language, elapsed)
    elif status == "skipped":
        progress_rich = tr(language, "report.status.skipped")
        progress_plain = str(progress_rich)
        values = ""
    elif status == "failed":
        progress_rich = tr(language, "report.status.failed")
        progress_plain = str(progress_rich)
        values = ""
    elif status == "active" and percent is not None:
        progress_rich = [_code(progress_bar(detail["completed"], detail["total"])), " ",
                         _bold(percent + " %")]
        progress_plain = f"{progress_bar(detail['completed'], detail['total'])} {percent} %"
        values = _values(language, detail)
    elif status == "active":
        progress_rich = "—"
        progress_plain = "—"
        values = _values(language, detail) or _seconds(language, elapsed)
    else:
        progress_rich = "—"
        progress_plain = "—"
        values = ""
    return stage_rich, stage_plain, progress_rich, progress_plain, values or "—"


def progress_import(language: str, detail: dict | None, elapsed: int) -> tuple[dict, str]:
    """Строка приёма файла: скачивание не входит в таблицу обработки Absorbent."""
    sign = STAGES["input"]
    label = tr(language, sign["label"])
    detail = detail if isinstance(detail, dict) else {}
    rich = [_emoji(sign), " ", _bold(label)]
    plain = [f"{sign['glyph']} {label}"]
    percent = _percent(detail)
    if percent is not None:
        bar = progress_bar(detail["completed"], detail["total"])
        rich += [" ", _code(bar), " ", _bold(percent + " %")]
        plain.append(f"{bar} {percent} %")
    values = _values(language, detail)
    if values:
        rich += [" · ", values]
        plain.append(values)
    if isinstance(elapsed, int) and elapsed >= 0:
        tail = tr(language, "report.elapsed", seconds=elapsed)
        rich += [" · ", tail]
        plain.append(tail)
    return {"blocks": [{"type": "paragraph", "text": rich}]}, " · ".join(plain)


def progress_report(language: str, title: str, rows: dict[str, dict], *,
                    elapsed: int) -> tuple[dict, str]:
    """Таблица всех этапов одним rich-сообщением и её текстовый запасной вид."""
    header = [_cell(_bold(tr(language, "report.table.stage")), header=True),
              _cell(_bold(tr(language, "report.table.progress")), header=True),
              _cell(_bold(tr(language, "report.table.details")), header=True)]
    cells = [header]
    plain_lines = [f"{title} · {tr(language, 'report.elapsed', seconds=int(elapsed))}"]
    for key in REPORT_ORDER:
        stage_rich, stage_plain, progress_rich, progress_plain, values = _row(
            language, key, rows.get(key))
        cells.append([_cell(stage_rich), _cell(progress_rich), _cell(values)])
        plain_lines.append(f"{stage_plain} · {progress_plain} · {values}")
    blocks = [
        {"type": "paragraph",
         "text": [_bold(title), " · " + tr(language, "report.elapsed", seconds=int(elapsed))]},
        {"type": "table", "is_bordered": True, "is_striped": True, "is_compact": True,
         "cells": cells},
    ]
    return {"blocks": blocks}, "\n".join(plain_lines)

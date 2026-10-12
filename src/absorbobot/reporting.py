"""Оформление живого отчёта: знаки этапов, полоса прогресса и rich-разметка Telegram."""
from __future__ import annotations

from .i18n import catalogues, tr

# Кастомные эмодзи владельца бота: идентификаторы документов Telegram и запасные глифы,
# которые видны там, где кастомный эмодзи недоступен (обычный текст, чужой клиент).
STAGES = {
    "input": {"emoji": "6096080291347042802", "glyph": "📥", "label": "report.stage.input"},
    "prepare": {"emoji": "5449652666365921217", "glyph": "☑️", "label": "report.stage.prepare"},
    "stt": {"emoji": "5402595016101078333", "glyph": "🎶", "label": "report.stage.stt"},
    "summary": {"emoji": "5408875405044124105", "glyph": "✍️", "label": "report.stage.summary"},
    "frames": {"emoji": "5854710508065658472", "glyph": "📸", "label": "report.stage.frames"},
    "synthesis": {"emoji": "5287363255457566209", "glyph": "📰", "label": "report.stage.synthesis"},
    "render": {"emoji": "5251558986443932784", "glyph": "📄", "label": "report.stage.render"},
    "delivery": {"emoji": "5202121542045026196", "glyph": "👽", "label": "report.stage.delivery"},
    "queued": {"emoji": None, "glyph": "🕒", "label": "report.stage.queued"},
    "completed": {"emoji": None, "glyph": "✅", "label": "report.stage.completed"},
    "failed": {"emoji": None, "glyph": "❌", "label": "report.stage.failed"},
    "cancelled": {"emoji": None, "glyph": "🚫", "label": "report.stage.cancelled"},
}

# Стадии Absorbent и состояния работы, сведённые к знакам отчёта.
STAGE_OF = {
    "input.queued": "input",
    "input.external": "input",
    "input.telegram": "input",
    "input.media": "input",
    "input.copy": "input",
    "input.analyze": "prepare",
    "input.ready": "completed",
    "input.failed": "failed",
    "status.queued": "queued",
    "status.preparing": "prepare",
    "status.running": "prepare",
    "status.completed": "completed",
    "status.delivered": "delivery",
    "status.reused": "completed",
    "status.awaiting_reuse_choice": "queued",
    "status.cancel_requested": "cancelled",
    "status.cancelled": "cancelled",
    "status.failed": "failed",
    "stage.prepare_slides": "prepare",
    "stage.describe_slides": "prepare",
    "stage.stt": "stt",
    "stage.summarize": "summary",
    "stage.extract_frames": "frames",
    "stage.select_keyframes": "frames",
    "stage.describe_keyframes": "frames",
    "stage.synthesize": "synthesis",
    "stage.render": "render",
    "stage.package": "render",
    "stage.delivery": "delivery",
}

# Исходы, после которых живой отчёт больше не нужен: его убирают, а причину
# присылают отдельным сообщением.
TERMINAL_STAGES = frozenset({
    "input.ready", "input.failed", "status.failed", "status.cancelled",
    "status.cancel_requested", "status.delivered",
})

BAR_WIDTH = 12
BAR_FILLED, BAR_EMPTY = "▮", "▯"


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


def _values(language: str, detail: dict) -> str | None:
    """Счётчик стадии: фактические числа движка, без выдуманных процентов."""
    completed, total, unit = detail.get("completed"), detail.get("total"), detail.get("unit")
    if not isinstance(completed, (int, float)) or isinstance(completed, bool):
        return None
    if not isinstance(unit, str) or not unit:
        return None
    factor = 1000000 if unit == "bytes" else 1
    precision = 1 if unit in ("bytes", "seconds") else 0
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


def mark(stage: str) -> dict | None:
    """Знак стадии: ``None`` у неизвестной стадии, её подпись берут из каталога языков."""
    return STAGES.get(STAGE_OF.get(stage, ""))


def progress_line(language: str, stage: str, *, detail: dict | None = None,
                  elapsed: int | None = None) -> tuple[str, str]:
    """Собрать одну живую строку: разметку ``rich`` и её текстовый запасной вид."""
    sign = mark(stage)
    if sign is not None:
        label = tr(language, sign["label"])
    elif stage in catalogues()[language]:
        label = tr(language, stage)
    else:
        # Незнакомый этап ядра не должен срывать задание: показать общую подпись.
        label = tr(language, "report.stage.unknown")
    markdown = [f"![](tg://emoji?id={sign['emoji']}) **{label}**" if sign is not None and sign["emoji"]
                else f"{sign['glyph'] if sign is not None else '•'} **{label}**"]
    plain = [f"{sign['glyph'] if sign is not None else '•'} {label}"]
    detail = detail if isinstance(detail, dict) else {}
    percent = _percent(detail)
    if percent is not None:
        bar = progress_bar(detail["completed"], detail["total"])
        markdown.append(f"`{bar}` **{percent} %**")
        plain.append(f"{bar} {percent} %")
    values = _values(language, detail)
    if values:
        markdown.append(values)
        plain.append(values)
    if elapsed is not None:
        tail = tr(language, "report.elapsed", seconds=int(elapsed))
        markdown.append(tail)
        plain.append(tail)
    return " · ".join(markdown), " · ".join(plain)

"""Отдельный рендер без ключей и сети через установленную библиотеку ядра."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import traceback
from pathlib import Path

# Забор кодового блока: строки внутри него — буквальный текст, а не markdown.
_FENCE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")

# Начало пункта списка: [md_tables::separate_blocks()] ядра вставляет перед
# такой строкой пустую, разрывая формулу, внутри которой она оказалась.
_LIST_START = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:\s|$)")


def _collapse_display_math(text: str) -> str:
    """Собрать выключные формулы, которые pandoc разорвал бы как абзац.

    Модель может вернуть формулу между ``\\[`` и ``\\]`` (или двумя ``$$``) с
    пустой строкой в середине. Pandoc заканчивает на пустой строке абзац, а с
    ним и формулу: остаток становится текстом, и LuaLaTeX обрывает сборку PDF
    ошибкой «Missing $ inserted». Пустую строку мало удалить: подготовка
    markdown в ядре вставляет её обратно перед строкой, начинающейся с маркера
    списка (в формуле это знак ``+``). Поэтому строки такой формулы собираются
    в одну: перенос строки в математическом режиме TeX и так пробел, а
    разделители остаются исходными. Формулы, которые pandoc читает целиком,
    не трогаются — исходный markdown остаётся читаемым.
    """
    lines = text.split("\n")
    out: list[str] = []
    fence = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if fence:
            out.append(line)
            if stripped.startswith(fence) and not stripped.strip(fence[0]):
                fence = ""
            i += 1
            continue
        tail = ""
        closer = ""
        if stripped.startswith("\\[") and "\\]" not in stripped[2:]:
            tail, closer = stripped[2:], "\\]"
            opener = "\\["
        elif stripped.startswith("$$") and stripped.count("$$") == 1:
            tail, closer = stripped[2:], "$$"
            opener = "$$"
        if not closer:
            if _FENCE.match(line):
                fence = stripped[:3]
            out.append(line)
            i += 1
            continue
        end = i + 1
        while end < len(lines) and closer not in lines[end]:
            end += 1
        if end == len(lines):
            out.append(line)
            i += 1
            continue
        body = lines[i + 1:end]
        if any(not part.strip() or _LIST_START.match(part) for part in body):
            head = lines[end].split(closer, 1)[0]
            parts = [part.strip() for part in (tail, *body, head) if part.strip()]
            indent = line[:len(line) - len(line.lstrip())]
            out.append(f"{indent}{opener} {' '.join(parts)} {closer}")
        else:
            out.extend(lines[i:end + 1])
        i = end + 1
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[1] not in ("ru", "en"):
        print(json.dumps({"error": "invalid_render_arguments"}))
        return 2
    from absorbent.render import render_all

    notes = Path(args[0])
    if not notes.is_file():
        print(json.dumps({"error": "artifact_missing"}))
        return 2
    text = notes.read_text(encoding="utf-8")
    notes.write_text(_collapse_display_math(text), encoding="utf-8")
    try:
        html, pdf = render_all(notes, lang=args[1])
    except subprocess.CalledProcessError as exc:
        # Вывод упавшей программы (например, журнал LaTeX) ядро забирает себе,
        # поэтому без него в sandbox log остаётся только команда.
        traceback.print_exc(file=sys.stderr)
        for stream in (exc.stdout, exc.stderr):
            if stream:
                print(stream, file=sys.stderr)
        print(json.dumps({"error": "render_failed"}))
        return 1
    except Exception:
        # Исходная диагностика сторонних программ остаётся только в sandbox log.
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": "render_failed"}))
        return 1
    if not html.is_file() or not pdf.is_file():
        print(json.dumps({"error": "artifact_missing"}))
        return 1
    print(json.dumps({"status": "rendered"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

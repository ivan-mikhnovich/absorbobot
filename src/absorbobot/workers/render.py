"""Отдельный рендер без ключей и сети через установленную библиотеку ядра."""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


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
    try:
        html, pdf = render_all(notes, lang=args[1])
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

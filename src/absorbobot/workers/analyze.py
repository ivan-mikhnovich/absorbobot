"""Проверить запись и материалы внутри изолированного рабочего каталога."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4


class AnalysisError(ValueError):
    """Безопасный код отказа без внешнего stderr или серверного пути."""


def media_metadata(info: dict, max_seconds: float) -> dict:
    """Принимать только известную конечную длительность и аудиодорожку."""
    value = (info.get("format") or {}).get("duration")
    try:
        if isinstance(value, bool):
            raise ValueError
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        raise AnalysisError("invalid_duration") from None
    if not math.isfinite(duration) or duration <= 0:
        raise AnalysisError("invalid_duration")
    if duration > max_seconds:
        raise AnalysisError("media_too_long")
    if not any(isinstance(stream, dict) and stream.get("codec_type") == "audio"
               for stream in info.get("streams", [])):
        raise AnalysisError("missing_audio")
    return {"duration": duration, "pages": None}


def pdf_pages(text: str, remaining_pages: int) -> int:
    """Отказать до растеризации, не обрезая превышающую предел колоду."""
    matches = re.findall(r"^Pages:\s+([0-9]+)\s*$", text, flags=re.MULTILINE)
    if len(matches) != 1 or int(matches[0]) <= 0:
        raise AnalysisError("invalid_material")
    pages = int(matches[0])
    if pages > remaining_pages:
        raise AnalysisError("material_pages_exceeded")
    return pages


def _command(argv: list[str], directory: Path, *, timeout: float = 120) -> str:
    # Потенциально большой диагностический вывод остаётся на storage.
    output = directory / (uuid4().hex + ".stdout")
    errors = directory / (uuid4().hex + ".stderr")
    try:
        with output.open("xb") as stdout, errors.open("xb") as stderr:
            process = subprocess.run(argv, stdout=stdout, stderr=stderr,
                                     env={**os.environ, "LC_ALL": "C"},
                                     timeout=timeout, check=False)
        if process.returncode or output.stat().st_size > 65536:
            raise AnalysisError("invalid_material")
        return output.read_text(encoding="utf-8")
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise AnalysisError("analysis_failed") from None


def analyze(source: Path, role: str, max_seconds: float, remaining_pages: int,
            *, work_root: Path = Path("/work")) -> dict:
    root = work_root.resolve(strict=True)
    source = source.resolve(strict=True)
    if not source.is_relative_to(root / "input") or not source.is_file():
        raise AnalysisError("unsafe_file_path")
    directory = root / "tmp" / ("analyze-" + uuid4().hex)
    directory.mkdir(mode=0o700)
    if role == "media":
        try:
            text = _command(["ffprobe", "-v", "error", "-show_entries",
                             "format=duration:stream=codec_type", "-of", "json", str(source)],
                            directory, timeout=30)
        except AnalysisError as exc:
            # Повреждённая запись — это отказ по длительности, а не по материалу.
            raise AnalysisError("invalid_duration" if str(exc) == "invalid_material"
                                else "analysis_failed") from None
        try:
            return media_metadata(json.loads(text), max_seconds)
        except (json.JSONDecodeError, AttributeError, TypeError):
            raise AnalysisError("invalid_duration") from None
    if role != "material":
        raise AnalysisError("unsupported_input")
    # Категории форматов определяет установленное ядро, а не второй каталог бота.
    from absorbent.slides import MATERIAL_EXTENSIONS, PHOTO_EXTENSIONS
    if source.suffix.lower() not in MATERIAL_EXTENSIONS:
        raise AnalysisError("unsupported_input")
    if source.suffix.lower() in PHOTO_EXTENSIONS:
        from PIL import Image
        try:
            with Image.open(source) as image:
                image.verify()
        except Exception:
            raise AnalysisError("invalid_material") from None
        if remaining_pages < 1:
            raise AnalysisError("material_pages_exceeded")
        return {"duration": None, "pages": 1}
    pdf = source
    if source.suffix.lower() != ".pdf":
        profile = directory / "profile"
        _command(["soffice", "--headless", "--nologo", "--nodefault", "--norestore",
                  "-env:UserInstallation=" + profile.as_uri(), "--convert-to", "pdf",
                  "--outdir", str(directory), str(source)], directory)
        pdf = directory / (source.stem + ".pdf")
        if not pdf.is_file() or pdf.is_symlink():
            raise AnalysisError("invalid_material")
    text = _command(["pdfinfo", str(pdf)], directory, timeout=30)
    return {"duration": None, "pages": pdf_pages(text, remaining_pages)}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) != 4:
            raise AnalysisError("unsupported_input")
        result = analyze(Path(args[0]), args[1], float(args[2]), int(args[3]))
        print(json.dumps({"ok": True, **result}, allow_nan=False))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, AnalysisError) else "analysis_failed"
        print(json.dumps({"ok": False, "error": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

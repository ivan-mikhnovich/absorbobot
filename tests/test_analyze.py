"""Границы извлечения представительного кадра записи."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from absorbobot.workers.analyze import extract_preview


FFMPEG = shutil.which("ffmpeg")
PREVIEW_MAX_BYTES = 262144


def work_root(tmp_path):
    root = tmp_path / "work"
    (root / "input").mkdir(parents=True)
    (root / "tmp").mkdir()
    return root


def make_video(path, *, size="320x240", duration=1):
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size={size}:rate=10:duration={duration}",
                    "-c:v", "mpeg4", "-q:v", "5", str(path)], check=True)


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg недоступен")
def test_extract_preview_is_bounded_jpeg_under_tmp(tmp_path):
    pytest.importorskip("PIL")
    root = work_root(tmp_path)
    source = root / "input" / "media.mp4"
    make_video(source, size="1280x720", duration=2)
    directory = root / "tmp" / "analyze-test"
    directory.mkdir()
    preview = extract_preview(source, 2.0, directory)
    assert preview is not None
    assert preview["preview_relative_path"] == "tmp/analyze-test/preview.jpg"
    assert 0 < preview["preview_width"] <= 640 and 0 < preview["preview_height"] <= 640
    target = root / preview["preview_relative_path"]
    assert 0 < target.stat().st_size <= PREVIEW_MAX_BYTES
    # Промежуточный кадр не остаётся в дереве задания.
    assert not (directory / "preview.frame.jpg").exists()


def test_extract_preview_returns_none_without_real_frame(tmp_path):
    root = work_root(tmp_path)
    source = root / "tmp" / "broken.bin"
    source.write_bytes(b"not a video")
    directory = root / "tmp" / "analyze-test"
    directory.mkdir()
    assert extract_preview(source, 2.0, directory) is None
    assert not (directory / "preview.jpg").exists()

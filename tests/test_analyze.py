"""Границы извлечения представительного кадра записи."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from absorbobot.workers.analyze import AnalysisError, audio_tracks, extract_preview, media_metadata


FFMPEG = shutil.which("ffmpeg")
PREVIEW_MAX_BYTES = 262144


def test_audio_tracks_keep_absolute_indexes_and_start_offsets():
    info = {
        "format": {"duration": "120.0", "start_time": "0.5"},
        "streams": [
            {"index": 0, "codec_type": "video"},
            {"index": 3, "codec_type": "audio", "channels": 2, "start_time": "1.0",
             "tags": {"title": "  Зал\n", "language": "ru"},
             "disposition": {"default": 1}},
            {"index": 4, "codec_type": "audio", "channels": 1, "start_time": "0.25",
             "tags": {"title": "перевод\x00", "language": "en"},
             "disposition": {"default": 0}},
        ],
    }
    tracks = audio_tracks(info)
    assert [track["index"] for track in tracks] == [3, 4]
    assert tracks[0]["title"] == "Зал"
    assert tracks[0]["language"] == "ru"
    assert tracks[0]["default"] is True
    assert tracks[0]["channels"] == 2
    # Смещение считается от начала контейнера, а не от абсолютного времени потока.
    assert tracks[0]["offset"] == 0.5
    assert tracks[1]["offset"] == -0.25
    assert tracks[1]["title"] == "перевод"


def test_media_metadata_includes_tracks_and_still_rejects_missing_audio():
    info = {
        "format": {"duration": "12.0", "start_time": "0"},
        "streams": [{"index": 1, "codec_type": "audio", "channels": 1,
                     "start_time": "0", "tags": {}, "disposition": {}}],
    }
    metadata = media_metadata(info, 60)
    assert metadata["duration"] == 12.0
    assert metadata["audio_tracks"][0]["index"] == 1
    with pytest.raises(AnalysisError, match="missing_audio"):
        media_metadata({"format": {"duration": "12.0"}, "streams": []}, 60)


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

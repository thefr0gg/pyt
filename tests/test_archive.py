"""Tests for pyt.archive.DownloadArchive."""

import os
import threading

import pytest

from pyt.archive import DownloadArchive

# ── basic behaviour ───────────────────────────────────────────────────────────


def test_new_archive_reports_not_downloaded(tmp_path):
    archive = DownloadArchive(str(tmp_path / "archive.txt"))
    assert archive.is_downloaded("dQw4w9WgXcQ") is False


def test_mark_then_is_downloaded(tmp_path):
    archive = DownloadArchive(str(tmp_path / "archive.txt"))
    archive.mark_downloaded("dQw4w9WgXcQ")
    assert archive.is_downloaded("dQw4w9WgXcQ") is True


def test_unknown_id_still_false_after_marking_another(tmp_path):
    archive = DownloadArchive(str(tmp_path / "archive.txt"))
    archive.mark_downloaded("aaa")
    assert archive.is_downloaded("bbb") is False


# ── file format ───────────────────────────────────────────────────────────────


def test_file_written_in_ytdlp_format(tmp_path):
    path = tmp_path / "archive.txt"
    archive = DownloadArchive(str(path))
    archive.mark_downloaded("abc123")
    assert path.read_text(encoding="utf-8").strip() == "youtube abc123"


def test_double_mark_writes_only_one_line(tmp_path):
    path = tmp_path / "archive.txt"
    archive = DownloadArchive(str(path))
    archive.mark_downloaded("abc123")
    archive.mark_downloaded("abc123")
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines.count("youtube abc123") == 1


def test_multiple_ids_each_on_own_line(tmp_path):
    path = tmp_path / "archive.txt"
    archive = DownloadArchive(str(path))
    archive.mark_downloaded("id1")
    archive.mark_downloaded("id2")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert "youtube id1" in lines
    assert "youtube id2" in lines


# ── loading existing file ─────────────────────────────────────────────────────


def test_loads_existing_archive(tmp_path):
    path = tmp_path / "archive.txt"
    path.write_text("youtube aaa\nyoutube bbb\n", encoding="utf-8")
    archive = DownloadArchive(str(path))
    assert archive.is_downloaded("aaa") is True
    assert archive.is_downloaded("bbb") is True


def test_ignores_non_youtube_lines(tmp_path):
    path = tmp_path / "archive.txt"
    path.write_text(
        "# comment\nsome_other_service video123\nyoutube good1\n", encoding="utf-8"
    )
    archive = DownloadArchive(str(path))
    assert archive.is_downloaded("good1") is True
    assert archive.is_downloaded("video123") is False


def test_missing_file_creates_on_first_write(tmp_path):
    path = tmp_path / "archive.txt"
    assert not path.exists()
    archive = DownloadArchive(str(path))
    archive.mark_downloaded("x")
    assert path.exists()


# ── thread safety ─────────────────────────────────────────────────────────────


def test_concurrent_marks_no_duplicates(tmp_path):
    path = tmp_path / "archive.txt"
    archive = DownloadArchive(str(path))
    ids = [f"id{i}" for i in range(50)]

    def worker(vid):
        archive.mark_downloaded(vid)

    threads = [threading.Thread(target=worker, args=(v,)) for v in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for vid in ids:
        assert archive.is_downloaded(vid) is True

    # Verify each id appears exactly once in the file
    lines = path.read_text(encoding="utf-8").splitlines()
    for vid in ids:
        assert lines.count(f"youtube {vid}") == 1

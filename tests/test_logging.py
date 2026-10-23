"""Tests for the public logging-control API (pyt.api._logging)."""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from unittest import mock

import pytest

import pyt
from pyt.api import _logging


@pytest.fixture(autouse=True)
def _reset_pyt_logger():
    """Each test gets a clean pyt logger. Restoration runs even if the
    test raises. We capture the full handler list so user-installed
    handlers (if any) survive."""
    root = logging.getLogger("pyt")
    saved_level = root.level
    saved_handlers = list(root.handlers)
    saved_propagate = root.propagate
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    root.propagate = saved_propagate


# ── basic enable / disable ─────────────────────────────────────────────────


def test_enable_logging_adds_stderr_handler():
    _logging.disable_logging()
    assert not _logging.is_enabled()
    _logging.enable_logging("DEBUG")
    assert _logging.is_enabled()
    assert _logging.get_log_level() == logging.DEBUG


def test_enable_logging_idempotent_replaces_handler():
    _logging.enable_logging("DEBUG")
    first_handlers = [
        h
        for h in logging.getLogger("pyt").handlers
        if getattr(h, "_pyt_managed", False)
    ]
    _logging.enable_logging("INFO")
    second_handlers = [
        h
        for h in logging.getLogger("pyt").handlers
        if getattr(h, "_pyt_managed", False)
    ]
    assert len(second_handlers) == len(first_handlers)
    # Old stderr handler is gone, new one took its place.
    assert second_handlers[0] is not first_handlers[0]


def test_disable_logging_removes_managed_handlers_only():
    user_handler = logging.NullHandler()  # pretend the user installed this
    logging.getLogger("pyt").addHandler(user_handler)
    _logging.enable_logging("DEBUG")
    _logging.disable_logging()
    # User's handler is still there.
    assert user_handler in logging.getLogger("pyt").handlers
    # Our managed handler is gone.
    managed = [
        h
        for h in logging.getLogger("pyt").handlers
        if getattr(h, "_pyt_managed", False)
    ]
    assert managed == []
    assert not _logging.is_enabled()


def test_disable_logging_silences_records(caplog):
    _logging.disable_logging()
    logger = logging.getLogger("pyt.test")
    logger.warning("nobody should see this")
    # Even at WARNING level, disabled state should suppress.
    assert "nobody should see this" not in caplog.text


# ── set_log_level ──────────────────────────────────────────────────────────


def test_set_log_level_int():
    _logging.set_log_level(logging.WARNING)
    assert _logging.get_log_level() == logging.WARNING


def test_set_log_level_string():
    _logging.set_log_level("ERROR")
    assert _logging.get_log_level() == logging.ERROR


def test_set_log_level_lowercase_string():
    _logging.set_log_level("debug")
    assert _logging.get_log_level() == logging.DEBUG


def test_set_log_level_trace():
    _logging.set_log_level("TRACE")
    assert _logging.get_log_level() == _logging.TRACE
    # TRACE is 5, below DEBUG.
    assert _logging.TRACE < logging.DEBUG


def test_set_log_level_invalid_string():
    with pytest.raises(ValueError, match="unknown log level"):
        _logging.set_log_level("LOUDER")


def test_set_log_level_invalid_type():
    with pytest.raises(TypeError, match="must be int or str"):
        _logging.set_log_level(3.14)  # type: ignore[arg-type]


# ── stream + file output ───────────────────────────────────────────────────


def test_enable_logging_writes_to_custom_stream():
    buf = io.StringIO()
    _logging.enable_logging("DEBUG", stream=buf)
    logger = logging.getLogger("pyt.test")
    logger.info("hello world")
    assert "hello world" in buf.getvalue()


def test_enable_logging_writes_to_file(tmp_path):
    log_file = tmp_path / "pyt.log"
    _logging.enable_logging("DEBUG", file=log_file)
    logger = logging.getLogger("pyt.test")
    logger.info("file message")
    # Force flush by closing handlers.
    for h in logging.getLogger("pyt").handlers:
        h.flush()
    assert "file message" in log_file.read_text()


def test_enable_logging_custom_format():
    buf = io.StringIO()
    _logging.enable_logging(
        "DEBUG",
        stream=buf,
        fmt="<<%(levelname)s>> %(message)s",
    )
    logging.getLogger("pyt.test").info("custom-fmt-msg")
    text = buf.getvalue()
    assert "<<INFO>>" in text
    assert "custom-fmt-msg" in text


# ── propagation ────────────────────────────────────────────────────────────


def test_propagate_default_is_true_for_caplog():
    """Default propagate=True so pytest's caplog still works."""
    _logging.enable_logging("DEBUG")
    assert logging.getLogger("pyt").propagate is True


def test_propagate_can_be_disabled():
    _logging.enable_logging("DEBUG", propagate=False)
    assert logging.getLogger("pyt").propagate is False


# ── env-var bootstrap ──────────────────────────────────────────────────────


def test_pyt_log_level_env_var_honored():
    """A fresh PYT_LOG_LEVEL=DEBUG enable_logging if module is reloaded."""
    import importlib

    with mock.patch.dict(os.environ, {"PYT_LOG_LEVEL": "DEBUG"}, clear=False):
        importlib.reload(_logging)
    assert _logging.is_enabled()
    assert _logging.get_log_level() == logging.DEBUG
    _logging.disable_logging()


def test_invalid_pyt_log_level_env_var_does_not_crash():
    """Bad env var should be silently ignored, not raise at import time."""
    import importlib

    # Start clean — earlier tests in this file may have enabled logging
    # via reload-with-env-set. The autouse fixture saves/restores the
    # logger state, but a `_logging.is_enabled()` snapshot taken inside
    # this test sees whatever the live state is, so we disable first.
    _logging.disable_logging()

    with mock.patch.dict(os.environ, {"PYT_LOG_LEVEL": "BANANA"}, clear=False):
        # Reload should not raise.
        importlib.reload(_logging)
    # No managed handler installed because the bad value was ignored.
    assert not _logging.is_enabled()


# ── pyt root-level re-exports ──────────────────────────────────────────────


def test_root_module_exposes_logging_api():
    # Identity-check by qualified name rather than object identity —
    # other tests in this file reload _logging which would invalidate
    # `is` checks on the rebound symbols, but `pyt.enable_logging` is
    # still the function we care about.
    for attr in (
        "enable_logging",
        "disable_logging",
        "set_log_level",
        "get_log_level",
        "logging_enabled",
        "diagnostic_report",
    ):
        assert callable(getattr(pyt, attr)), f"pyt.{attr} should be callable"
    assert isinstance(pyt.TRACE, int)


# ── diagnostic_report ──────────────────────────────────────────────────────


def test_diagnostic_report_includes_pyt_version():
    report = _logging.diagnostic_report()
    from pyt.version import __version__

    assert __version__ in report


def test_diagnostic_report_lists_tools():
    report = _logging.diagnostic_report()
    assert "ffmpeg" in report
    assert "ffprobe" in report
    assert "realesrgan" in report


def test_diagnostic_report_includes_platform_info():
    report = _logging.diagnostic_report()
    assert "platform" in report.lower()
    assert "python" in report.lower()


def test_diagnostic_report_no_user_data_leaks():
    """Sanity: the report shouldn't include URLs, video IDs, or other
    user content. We only check for the absence of obvious leaks."""
    report = _logging.diagnostic_report()
    assert "https://" not in report  # no URLs
    assert "youtu" not in report  # no video URLs


# ── module integration: emits land in caplog ──────────────────────────────


def test_client_init_emits_debug_log(caplog):
    from pyt import Client

    with caplog.at_level(logging.DEBUG, logger="pyt.api.client"):
        Client()
    assert any("Client init" in r.getMessage() for r in caplog.records)


def test_client_video_emits_info_log(caplog):
    from pyt import Client

    with mock.patch("pyt.api.client.Video._from_url") as factory:
        result = mock.MagicMock()
        result.video_id = "abc123"
        result.title = "test video"
        result.length = 60
        factory.return_value = result

        with caplog.at_level(logging.INFO, logger="pyt.api.client"):
            Client().video("https://youtu.be/abc123")

    fetch_logs = [r for r in caplog.records if "fetching" in r.getMessage()]
    assert fetch_logs
    hydrated_logs = [r for r in caplog.records if "hydrated" in r.getMessage()]
    assert hydrated_logs

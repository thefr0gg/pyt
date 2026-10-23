"""Public logging control for pyt.

Three functions exposed at the package root:

    pyt.enable_logging(level="DEBUG", file="/tmp/pyt.log")
    pyt.set_log_level("INFO")
    pyt.disable_logging()

These all operate on the ``pyt`` logger root, so every module's
``logger = logging.getLogger(__name__)`` (which inherits from
``pyt.<module>``) is affected uniformly.

The CLI's ``-v`` / ``-vv`` flags route through the same code path,
so behavior stays consistent between programmatic and CLI use.

Off by default — pyt is a library; importing it does not produce
log output until the consumer asks for it. We add a
:class:`logging.NullHandler` to the ``pyt`` logger at import time
to suppress the stdlib's "No handlers could be found" warning when
loggers fire and nothing has been enabled.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional, Union

# Sentinel so multiple enable_logging calls replace the previous
# stderr handler instead of stacking them up.
_PYT_HANDLER_ATTR = "_pyt_managed"


def _pyt_root() -> logging.Logger:
    return logging.getLogger("pyt")


def _ensure_null_handler() -> None:
    """Library hygiene: a NullHandler keeps Python from complaining
    about loggers without handlers when the consumer never enabled
    logging.
    """
    root = _pyt_root()
    has_null = any(isinstance(h, logging.NullHandler) for h in root.handlers)
    if not has_null:
        root.addHandler(logging.NullHandler())


_ensure_null_handler()


# Levels we accept by string in the public API. Includes the symbolic
# names plus our own "TRACE" alias for level 5 (extra-verbose, e.g.
# per-HTTP-chunk logging from the SABR session).
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

_LEVEL_NAMES = {
    "TRACE": TRACE,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _coerce_level(level: Union[int, str]) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        try:
            return _LEVEL_NAMES[level.upper()]
        except KeyError:
            raise ValueError(
                f"unknown log level {level!r}. "
                f"Choose from: {', '.join(_LEVEL_NAMES)}"
            )
    raise TypeError(f"log level must be int or str, not {type(level).__name__}")


def enable_logging(
    level: Union[int, str] = logging.INFO,
    *,
    file: Optional[Union[str, Path]] = None,
    stream=None,
    fmt: Optional[str] = None,
    propagate: bool = True,
) -> None:
    """Turn on pyt's structured logging.

    :param level: ``"TRACE"``, ``"DEBUG"``, ``"INFO"``, ``"WARNING"``,
        ``"ERROR"``, ``"CRITICAL"``, or the corresponding ``logging.*`` int.
        Default is ``INFO`` — high-signal lifecycle events without
        per-chunk noise.
    :param file: optional path to also write logs to. Useful for
        attaching to bug reports.
    :param stream: stream to log to (default ``sys.stderr``).
    :param fmt: custom logging format. If ``None``, uses a sensible
        default that includes timestamp, level, module, and message.
    :param propagate: whether records bubble to the root logger.
        Default ``True`` so caplog and other test fixtures see the
        records. Set to ``False`` if you've configured your own
        handlers on the root logger and don't want pyt's records
        showing up there twice.

    Idempotent: calling again replaces the previous pyt-managed
    handlers rather than stacking. ``logging.NullHandler`` is left
    untouched so :func:`disable_logging` works cleanly.
    """
    int_level = _coerce_level(level)
    root = _pyt_root()
    root.setLevel(int_level)
    root.propagate = propagate

    # Remove any handler we previously added — but leave the
    # NullHandler and any user-installed handlers alone.
    root.handlers = [
        h for h in root.handlers if not getattr(h, _PYT_HANDLER_ATTR, False)
    ]

    fmt_str = fmt or ("%(asctime)s %(levelname)-7s %(name)s :: %(message)s")
    formatter = logging.Formatter(fmt_str, datefmt="%H:%M:%S")

    stream_handler = logging.StreamHandler(stream or sys.stderr)
    stream_handler.setFormatter(formatter)
    setattr(stream_handler, _PYT_HANDLER_ATTR, True)
    root.addHandler(stream_handler)

    if file is not None:
        file_handler = logging.FileHandler(str(file), encoding="utf-8")
        file_handler.setFormatter(formatter)
        setattr(file_handler, _PYT_HANDLER_ATTR, True)
        root.addHandler(file_handler)


def disable_logging() -> None:
    """Remove pyt-managed handlers and silence the pyt logger.

    Idempotent. After this call, pyt produces no log output until
    :func:`enable_logging` is called again. Any handlers the consumer
    added themselves are left in place.
    """
    root = _pyt_root()
    root.handlers = [
        h for h in root.handlers if not getattr(h, _PYT_HANDLER_ATTR, False)
    ]
    # Set to a high level so even the user's own handlers (if any)
    # don't see records they would otherwise have shown.
    root.setLevel(logging.CRITICAL + 10)
    _ensure_null_handler()


def set_log_level(level: Union[int, str]) -> None:
    """Adjust the pyt logger's level without changing handlers.

    Useful for switching between INFO and DEBUG mid-run. If logging
    hasn't been enabled, this just sets the level — no handler is
    added, so output stays silent.
    """
    _pyt_root().setLevel(_coerce_level(level))


def get_log_level() -> int:
    """Return the current effective level on the pyt logger."""
    return _pyt_root().getEffectiveLevel()


def is_enabled() -> bool:
    """True iff there's a pyt-managed handler attached.

    Callers can use this to skip building expensive log payloads when
    nothing's listening.
    """
    return any(getattr(h, _PYT_HANDLER_ATTR, False) for h in _pyt_root().handlers)


# ── diagnostic dump for bug reports ────────────────────────────────────────


def diagnostic_report() -> str:
    """Capture environment + installed-tool state for bug reports.

    Returns a self-contained text block users can paste into an issue.
    Does not include URLs, video IDs, or any user content — purely
    environment and tool state.
    """
    import platform

    from pyt.version import __version__
    from pyt.api import _paths
    from pyt.api import doctor

    lines = []
    lines.append(f"pyt version    : {__version__}")
    lines.append(f"python version : {sys.version.split()[0]}")
    lines.append(
        f"platform       : {platform.system()} {platform.release()} "
        f"({platform.machine()})"
    )
    lines.append(f"managed bin    : {_paths.pyt_bin_dir()}")
    lines.append("")
    lines.append("Tools:")
    for tool in doctor.detect_all():
        if tool.found:
            lines.append(f"  {tool.name:<13} OK   {tool.version or 'version unknown'}")
            lines.append(f"                     path: {tool.path}")
        else:
            lines.append(f"  {tool.name:<13} MISSING")

    lines.append("")
    lines.append(f"PYT_LOG_LEVEL  : {os.environ.get('PYT_LOG_LEVEL', '<unset>')}")
    lines.append(f"effective level: {logging.getLevelName(get_log_level())}")

    return "\n".join(lines)


# ── env-var bootstrap ─────────────────────────────────────────────────────
#
# Setting PYT_LOG_LEVEL=DEBUG before importing pyt is a low-friction way
# to enable logging when investigating an issue without touching the
# code that uses pyt. We honor it eagerly here.
_env_level = os.environ.get("PYT_LOG_LEVEL")
if _env_level:
    try:
        enable_logging(_env_level)
    except (ValueError, TypeError):
        # Bad env var — don't blow up at import; just ignore.
        pass

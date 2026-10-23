"""Detect a JavaScript runtime for shelling out to BotGuard / PO-token
generator scripts.

Looks up ``node``, ``bun``, and ``deno`` (in that order — Node has the
largest ecosystem and is what most YouTube generators target). The
order is preference, not capability: any of the three can run a
modern ES2020+ script.

Why a JS runtime at all? PO tokens are produced by a BotGuard JS
challenge. Re-implementing it in pure Python means tracking Google's
private API in two languages instead of one. Shelling out to a real
JS engine lets us reuse the existing community generators
(``bgutil-pot``, ``youtube-po-token-generator``, etc.) without
shipping a JS interpreter.

The detection result is cached at module level so repeated lookups
during a single process are free. ``invalidate()`` is provided for
tests that mock ``shutil.which``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # nosec — argv constructed by us
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# Preference order. Node first because most community PO-token
# generators (bgutil-pot, youtube-po-token-generator) ship as Node
# packages. Bun and Deno are CLI-compatible enough for the kind of
# self-contained scripts pyt invokes.
_KNOWN_RUNTIMES: Tuple[Tuple[str, str], ...] = (
    ("node", "--version"),
    ("bun", "--version"),
    ("deno", "--version"),
)


@dataclass(frozen=True)
class JsRuntime:
    """A detected JavaScript runtime."""

    name: str  # "node" / "bun" / "deno"
    path: str  # absolute path
    version: str  # raw stdout from --version


_cached: Optional[JsRuntime] = None
_cache_populated: bool = False


def detect_runtime(*, refresh: bool = False) -> Optional[JsRuntime]:
    """Return the first JS runtime found on PATH, or ``None``.

    Result is cached for the process lifetime; pass ``refresh=True``
    after installing a runtime mid-run.
    """
    global _cached, _cache_populated
    if _cache_populated and not refresh:
        return _cached

    for binary, version_flag in _KNOWN_RUNTIMES:
        path = shutil.which(binary)
        if not path:
            continue
        version = _safe_version(path, version_flag)
        if version is None:
            # Found on PATH but doesn't respond — broken install or
            # something masquerading as the binary. Try the next one.
            logger.debug("js runtime: %s found at %s but not callable", binary, path)
            continue
        rt = JsRuntime(name=binary, path=path, version=version)
        logger.debug("js runtime: detected %s %s at %s", rt.name, rt.version, rt.path)
        _cached = rt
        _cache_populated = True
        return rt

    logger.debug("js runtime: none of node/bun/deno on PATH")
    _cached = None
    _cache_populated = True
    return None


def detect_all_runtimes() -> List[JsRuntime]:
    """Return every JS runtime present, in preference order.

    Useful for the doctor command — we want to show the user what's
    available, not just the one we'd pick.
    """
    out: List[JsRuntime] = []
    for binary, version_flag in _KNOWN_RUNTIMES:
        path = shutil.which(binary)
        if not path:
            continue
        version = _safe_version(path, version_flag)
        if version is None:
            continue
        out.append(JsRuntime(name=binary, path=path, version=version))
    return out


def invalidate() -> None:
    """Clear the cached detection result. Call after installing a
    runtime mid-process or in tests that mock ``shutil.which``."""
    global _cached, _cache_populated
    _cached = None
    _cache_populated = False


def _safe_version(path: str, flag: str) -> Optional[str]:
    try:
        result = subprocess.run(  # nosec
            [path, flag],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    if text:
        return text.splitlines()[0][:60]
    text = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    return text.splitlines()[0][:60] if text else None


def run_script(
    runtime: JsRuntime,
    script_path: str,
    args: Optional[List[str]] = None,
    *,
    timeout: int = 60,
    stdin: Optional[bytes] = None,
) -> str:
    """Execute a JS script with *runtime* and return stdout.

    Generators communicate by printing the token to stdout. Script
    failure (non-zero exit) raises :class:`subprocess.CalledProcessError`
    so the caller can translate to a domain error.
    """
    cmd: List[str] = [runtime.path, script_path]
    if args:
        cmd.extend(args)
    logger.debug("js runtime: running %s", " ".join(cmd))
    result = subprocess.run(  # nosec
        cmd,
        capture_output=True,
        check=True,
        timeout=timeout,
        input=stdin,
    )
    return result.stdout.decode("utf-8", errors="replace").strip()

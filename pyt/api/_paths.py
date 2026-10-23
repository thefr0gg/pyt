"""Managed binary location for pyt-installed tools.

The ``doctor`` command can download ffmpeg and realesrgan-ncnn-vulkan
on the user's behalf. We drop them in ``~/.pyt/bin/`` rather than
asking users to mess with their shell PATH — and prepend that
directory to ``os.environ["PATH"]`` at import time so subsequent
``shutil.which("ffmpeg")`` calls (and any subprocess we spawn) pick
the managed copy up automatically.

The mutation is process-local: it never touches the user's shell
profile or system PATH.
"""

from __future__ import annotations

import os
from pathlib import Path


def pyt_data_dir() -> Path:
    """Root directory for pyt-managed state. ``~/.pyt`` on every platform.

    We deliberately don't use XDG / Library/Application Support / AppData
    here — pyt is a small CLI and a single, predictable location keeps
    the doctor command's "where did you put it?" question trivial.
    """
    return Path.home() / ".pyt"


def pyt_bin_dir() -> Path:
    """Where the doctor command installs binaries (``~/.pyt/bin``)."""
    return pyt_data_dir() / "bin"


def ensure_bin_on_path() -> None:
    """Prepend ``~/.pyt/bin`` to ``os.environ["PATH"]``.

    Idempotent: safe to call multiple times. No-op when the directory
    is already first in PATH.
    """
    bin_dir = str(pyt_bin_dir())
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    if parts and parts[0] == bin_dir:
        return
    parts = [bin_dir] + [p for p in parts if p != bin_dir]
    os.environ["PATH"] = os.pathsep.join(parts)


# Bootstrap: any pyt code that imports something from pyt.api gets the
# managed bin dir on PATH automatically. Importing this module from
# pyt.api.__init__ wires it for the whole API surface.
ensure_bin_on_path()

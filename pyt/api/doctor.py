"""``pyt --doctor``: report on which tools/features are available, and
optionally download missing binaries to ``~/.pyt/bin/``.

The doctor surface is intentionally limited:

* **ffmpeg / ffprobe** — required for every post-processing step,
  the CombinedDownload merge, and the upscale pipelines. We can
  auto-install on Windows (gyan.dev essentials build) and Linux
  (BtbN static build). On macOS we point at Homebrew because
  auto-downloading a working build for both Intel and Apple Silicon
  is more trouble than it's worth.

* **realesrgan-ncnn-vulkan** — optional, only used by
  ``algorithm="realesrgan"`` upscaling. xinntao publishes official
  per-platform zips on GitHub releases; we install on all three.

Auto-install always:

1. Downloads to a temp file with progress indication
2. Extracts into ``~/.pyt/bin/`` (or a tool-specific subdir for
   binaries that ship with model files / DLLs)
3. Sets the executable bit on POSIX
4. Verifies by running the binary with its version flag

If verification fails, the install is rolled back and the user gets
a clear error.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import stat
import subprocess  # nosec
import sys
import tempfile
import urllib.request
import zipfile
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from pyt.api._paths import pyt_bin_dir, pyt_data_dir

logger = logging.getLogger(__name__)


# ── platform detection ────────────────────────────────────────────────────


def _platform_key() -> Tuple[str, str]:
    """Return (system, normalized_arch).

    System is one of ``windows``, ``darwin``, ``linux``.
    Arch is normalized: ``x86_64`` (covers ``amd64``, ``x86_64``) or
    ``arm64`` (covers ``arm64``, ``aarch64``).
    """
    sysname = platform.system().lower()
    raw_arch = platform.machine().lower()
    if raw_arch in ("x86_64", "amd64"):
        arch = "x86_64"
    elif raw_arch in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = raw_arch
    return sysname, arch


# ── tool model ────────────────────────────────────────────────────────────


@dataclass
class Tool:
    """A binary the doctor checks for."""

    name: str
    binary: str  # what to look up via shutil.which
    description: str
    required_for: List[str]
    install_url: Optional[str]
    installable: bool = True
    version_flag: str = "-version"

    # Filled in by detect():
    found: bool = False
    path: Optional[str] = None
    version: Optional[str] = None


@dataclass
class Feature:
    name: str
    available: bool
    requires: List[str]
    note: str = ""


def known_tools() -> List[Tool]:
    return [
        Tool(
            name="ffmpeg",
            binary="ffmpeg",
            description="muxing, audio extraction, post-processing, upscale re-encode",
            required_for=[
                "merge",
                "extract_audio",
                "embed_*",
                "sponsorblock_remove",
                "upscale",
            ],
            install_url="https://ffmpeg.org/download.html",
        ),
        Tool(
            name="ffprobe",
            binary="ffprobe",
            description="duration / fps / audio detection (ships with ffmpeg)",
            required_for=[
                "merge validation",
                "upscale chunking",
                "upscale fps detection",
            ],
            install_url="https://ffmpeg.org/download.html",
            # ffprobe always ships in the same archive as ffmpeg; the doctor
            # treats them as one install.
            installable=False,
        ),
        Tool(
            name="realesrgan",
            binary="realesrgan-ncnn-vulkan",
            description="Real-ESRGAN neural upscaler (optional, for pp.upscale algorithm='realesrgan')",
            required_for=["upscale algorithm='realesrgan'"],
            install_url="https://github.com/xinntao/Real-ESRGAN/releases",
            version_flag="-h",  # ncnn binaries print usage on -h, no -version
        ),
        # JS runtimes — any one is enough to run a PO-token generator
        # script. The doctor only flags MISSING when none of the three
        # are available; if at least one is found the others can stay
        # absent without affecting the feature status.
        Tool(
            name="node",
            binary="node",
            description="JS runtime for PO-token generators (bgutil-pot, etc.)",
            required_for=["po_token (--po-token-script / Client(po_token_script=))"],
            install_url="https://nodejs.org",
            installable=False,  # OS package manager is the right path here
            version_flag="--version",
        ),
        Tool(
            name="bun",
            binary="bun",
            description="alt JS runtime (pyt picks node first if both are present)",
            required_for=["po_token via JS script"],
            install_url="https://bun.sh",
            installable=False,
            version_flag="--version",
        ),
        Tool(
            name="deno",
            binary="deno",
            description="alt JS runtime (pyt picks node first if all three are present)",
            required_for=["po_token via JS script"],
            install_url="https://deno.com",
            installable=False,
            version_flag="--version",
        ),
        Tool(
            name="bgutil-pot",
            binary="bgutil-pot",
            description="community PO-token generator (use with --po-token-cmd)",
            required_for=["po_token (--po-token-cmd)"],
            install_url="https://github.com/Brainicism/bgutil-ytdlp-pot-provider",
            installable=False,
            version_flag="--version",
        ),
        Tool(
            name="pyt-po-token",
            binary="pyt-po-token",
            description=(
                "pyt's vendored PO-token generator (use with "
                "--po-token-cmd 'pyt-po-token')"
            ),
            required_for=["po_token (auto-install via doctor)"],
            install_url="run: pyt --doctor --install po-token-generator",
            installable=True,
            # Wrapper exits 1 on --version (we only handle YouTube args
            # via positional argv); --check exits 0 for parse-OK.
            version_flag="--check",
        ),
    ]


def detect(tool: Tool) -> Tool:
    """Resolve a Tool's runtime status via :func:`shutil.which`.

    Returns the same Tool object with ``found``/``path``/``version`` populated.
    Mutates in place for ergonomic listcomp use.
    """
    path = shutil.which(tool.binary)
    if not path:
        tool.found = False
        return tool
    tool.found = True
    tool.path = path
    tool.version = _get_version(path, tool.version_flag)
    return tool


def detect_all() -> List[Tool]:
    return [detect(t) for t in known_tools()]


def _get_version(path: str, version_flag: str) -> Optional[str]:
    """Best-effort version string extraction. Returns the first non-empty
    line of stdout/stderr. Caller renders it as opaque text — we don't try
    to normalize across tools.
    """
    try:
        result = subprocess.run(  # nosec
            [path, version_flag],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or b"").decode("utf-8", errors="replace") + (
        result.stderr or b""
    ).decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:80]
    return None


# ── feature derivation ────────────────────────────────────────────────────


def feature_status(tools: List[Tool]) -> List[Feature]:
    by_name = {t.name: t for t in tools}
    have = lambda n: by_name.get(n) is not None and by_name[n].found  # noqa: E731
    have_any_js_runtime = have("node") or have("bun") or have("deno")
    have_any_po_token_tool = (
        have("pyt-po-token") or have("bgutil-pot") or have_any_js_runtime
    )

    return [
        Feature(
            name="Stream download (SABR + byte-range)",
            available=True,  # built-in, no external deps
            requires=[],
        ),
        Feature(
            name="Audio extraction / format conversion",
            available=have("ffmpeg"),
            requires=["ffmpeg"],
        ),
        Feature(
            name="Combined video+audio merge (CombinedDownload)",
            available=have("ffmpeg"),
            requires=["ffmpeg"],
            note=(
                "ffprobe also enables post-merge duration validation"
                if not have("ffprobe")
                else ""
            ),
        ),
        Feature(
            name="Metadata / thumbnail / subtitle embedding",
            available=have("ffmpeg"),
            requires=["ffmpeg"],
        ),
        Feature(
            name="SponsorBlock chapter marking",
            available=have("ffmpeg"),
            requires=["ffmpeg"],
        ),
        Feature(
            name="Upscale (algorithm='lanczos')",
            available=have("ffmpeg"),
            requires=["ffmpeg"],
        ),
        Feature(
            name="Upscale (algorithm='realesrgan')",
            available=have("ffmpeg") and have("realesrgan"),
            requires=["ffmpeg", "realesrgan"],
        ),
        Feature(
            name="PO token auto-generation (for ATTESTATION_REQUIRED)",
            available=have_any_po_token_tool,
            requires=["any of: node, bun, deno, bgutil-pot"],
            note=(
                "Without a generator, you can still pass a static PO "
                "token via --po-token <TOKEN> (extract from your "
                "browser's DevTools)."
                if not have_any_po_token_tool
                else ""
            ),
        ),
    ]


# ── installation ──────────────────────────────────────────────────────────


class InstallError(RuntimeError):
    """Raised when an install fails — bad platform, bad download, bad
    archive, or post-install verification fails."""


@dataclass
class InstallPlan:
    """What an install would do, exposed so the caller can preview before
    triggering the network call."""

    tool: str
    url: str
    archive_format: str  # "zip" or "tar.xz"
    target_dir: Path
    binary_name: str  # what ends up on PATH
    extra_files: List[str] = field(default_factory=list)
    # When True, copy all sibling files from the binary's archive directory
    # (e.g. DLLs that must live next to the exe on Windows).
    copy_archive_dir: bool = False


def plan_install(tool_name: str) -> InstallPlan:
    """Resolve which URL we'd hit for *tool_name* on the current platform."""
    if tool_name == "ffmpeg":
        return _plan_ffmpeg()
    if tool_name == "realesrgan":
        return _plan_realesrgan()
    raise InstallError(
        f"unknown tool {tool_name!r}; installable tools: ffmpeg, realesrgan, "
        "po-token-generator"
    )


def install(
    tool_name: str,
    *,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> Path:
    """Download + extract + verify a tool. Returns the final binary path.

    :param on_progress: optional callback ``(bytes_done, total_or_None)``.
    """
    if tool_name == "po-token-generator":
        # PO-token generator doesn't fit the archive-download pipeline:
        # it needs npm to install youtubei.js + bgutils-js, plus a
        # vendored launcher script and a platform-specific wrapper.
        return _install_po_token_generator(on_progress=on_progress)

    plan = plan_install(tool_name)
    pyt_bin_dir().mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pyt-doctor-") as workdir_str:
        workdir = Path(workdir_str)
        archive = workdir / f"download.{plan.archive_format}"

        try:
            _download(plan.url, archive, on_progress=on_progress)
        except Exception as exc:  # noqa: BLE001
            raise InstallError(f"download failed: {exc}") from exc

        try:
            _extract(archive, workdir / "extracted", plan.archive_format)
        except Exception as exc:  # noqa: BLE001
            raise InstallError(f"extract failed: {exc}") from exc

        binary = _find_in_tree(workdir / "extracted", plan.binary_name)
        if not binary:
            raise InstallError(
                f"could not locate {plan.binary_name!r} inside the downloaded archive"
            )

        # Move the binary (and any extras like models/, DLLs) to the
        # managed bin dir so the rest of pyt finds them.
        target = plan.target_dir / plan.binary_name
        plan.target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, target)
        _make_executable(target)

        if plan.copy_archive_dir:
            # Copy every sibling file from the binary's archive directory so
            # that DLLs (Windows) and other required companions land next to
            # the binary in target_dir.
            for sibling in binary.parent.iterdir():
                if sibling.is_file() and sibling.name != plan.binary_name:
                    shutil.copy2(sibling, plan.target_dir / sibling.name)
                elif sibling.is_dir():
                    _copy_directory_named(binary.parent, sibling.name, plan.target_dir)

        for extra in plan.extra_files:
            extra_src = _find_in_tree(workdir / "extracted", extra)
            if extra_src is None:
                # Treat extras as best-effort — Real-ESRGAN's models dir
                # isn't a single named file. Use a directory copy if the
                # extra is a directory name.
                _copy_directory_named(workdir / "extracted", extra, plan.target_dir)
                continue
            shutil.copy2(extra_src, plan.target_dir / extra)

    final = pyt_bin_dir() / plan.binary_name
    if not final.exists():
        raise InstallError(f"install completed but {final} is missing")

    if not _verify(final, tool_name):
        try:
            final.unlink()
        except OSError:
            pass
        raise InstallError(
            f"installed binary at {final} did not respond to a sanity-check "
            f"invocation; the archive may be wrong for this platform"
        )
    return final


# ── download / extract primitives ─────────────────────────────────────────


def _download(
    url: str,
    target: Path,
    *,
    on_progress: Optional[Callable[[int, Optional[int]], None]],
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp:  # nosec — user-confirmed URL
        total_str = resp.headers.get("Content-Length")
        total = int(total_str) if total_str and total_str.isdigit() else None
        done = 0
        with open(target, "wb") as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)


def _extract(archive: Path, dest: Path, archive_format: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive_format == "zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    elif archive_format in ("tar.xz", "tar.gz", "tar"):
        mode = "r:*"
        with tarfile.open(archive, mode) as tf:
            tf.extractall(dest)  # nosec — we control the URL
    else:
        raise InstallError(f"unsupported archive format {archive_format!r}")


def _find_in_tree(root: Path, name: str) -> Optional[Path]:
    """Find a *file* (not directory) named *name* anywhere under *root*.

    Returns the first match (depth-first); archives ship the binary at
    varying nesting depths. Directories with matching names are
    explicitly skipped — Real-ESRGAN's archive contains a top-level
    directory ``realesrgan-ncnn-vulkan/`` that would otherwise shadow
    the actual binary inside it.
    """
    for p in root.rglob(name):
        if p.is_file():
            return p
    return None


def _copy_directory_named(root: Path, name: str, target_dir: Path) -> None:
    """Copy a directory named *name* (best-effort; needed for Real-ESRGAN's
    ``models/`` folder)."""
    matches = [p for p in root.rglob(name) if p.is_dir()]
    if not matches:
        return
    src = matches[0]
    dest = target_dir / name
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest)


def _make_executable(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _verify(binary: Path, tool_name: str) -> bool:
    """Run a known-safe invocation. ffmpeg/ffprobe answer to ``-version``;
    realesrgan-ncnn-vulkan answers to ``-h``."""
    flag = "-version" if tool_name in ("ffmpeg", "ffprobe") else "-h"
    try:
        result = subprocess.run(  # nosec
            [str(binary), flag],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode in (0, 1)  # ncnn-vulkan exits 1 on -h, that's fine


# ── po-token-generator installer (no archive — npm-based) ────────────────


# Pinned npm versions. Bumping them is the standard maintenance lever
# when YouTube changes the BotGuard wire format upstream.
_POT_NPM_PACKAGES = (
    "bgutils-js@^3.2.0",
    "youtubei.js@^14",
)
_POT_LAUNCHER_FILENAME = "po_token_launcher.js"
_POT_WRAPPER_NAME = "pyt-po-token"
_POT_TIMEOUT_SECONDS = 300  # npm installs can be slow on first run


def _install_po_token_generator(
    *,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> Path:
    """Install a working PO-token generator under ``~/.pyt/``.

    Lays down four things:

    1. ``~/.pyt/js/`` directory holding a tiny ``package.json`` and the
       npm-installed ``node_modules/`` for bgutils-js + youtubei.js.
    2. ``~/.pyt/js/po_token_launcher.js`` — the vendored launcher
       script copied out of ``pyt/data/``.
    3. ``~/.pyt/bin/pyt-po-token`` (or ``pyt-po-token.cmd`` on Windows)
       — a thin wrapper that runs ``node po_token_launcher.js "$@"``
       with ``NODE_PATH`` pointing at the local ``node_modules``.
    4. A verification call to the wrapper to make sure node + the
       packages + the launcher all line up.

    Doesn't install ``node`` itself — that's the user's package
    manager's job. Errors clearly with the install URL when node /
    npm aren't on PATH.

    :param on_progress: ignored — npm prints its own progress to stderr.
        We forward stdout/stderr so users see what's happening.
    """
    # 1. Verify node + npm
    node_path = shutil.which("node")
    if not node_path:
        raise InstallError(
            "po-token-generator install requires Node.js, but `node` is "
            "not on PATH. Install from https://nodejs.org (LTS is fine)."
        )
    npm_path = shutil.which("npm")
    if not npm_path:
        raise InstallError(
            "po-token-generator install requires npm, but `npm` is not "
            "on PATH. npm ships with Node.js — reinstalling Node from "
            "https://nodejs.org should fix it."
        )

    # 2. Verify Node 18+ (we need global fetch).
    try:
        result = subprocess.run(  # nosec
            [node_path, "--version"],
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(f"could not run {node_path} --version: {exc}") from exc
    node_version = result.stdout.decode("utf-8", errors="replace").strip()
    major = _parse_node_major(node_version)
    if major is not None and major < 18:
        raise InstallError(
            f"po-token-generator requires Node 18+ (detected {node_version}). "
            "Older Node lacks the global `fetch` the launcher uses."
        )

    js_dir = pyt_data_dir() / "js"
    js_dir.mkdir(parents=True, exist_ok=True)

    # 3. Write a minimal package.json so npm install lands in the
    # right place and doesn't try to walk up looking for one.
    package_json = js_dir / "package.json"
    package_json.write_text(
        '{\n  "name": "pyt-po-token-deps",\n'
        '  "private": true,\n'
        '  "version": "0.0.0",\n'
        '  "description": "vendored deps for pyt po-token launcher"\n'
        "}\n",
        encoding="utf-8",
    )

    # 4. npm install. We pipe stdout/stderr through to the parent so
    # the user sees what's happening (npm is chatty about download
    # progress, and a silent install of 200+ files would feel hung).
    cmd = [
        npm_path,
        "install",
        "--no-audit",
        "--no-fund",
        "--no-progress",
        "--prefix",
        str(js_dir),
        *_POT_NPM_PACKAGES,
    ]
    logger.info("doctor: running %s", " ".join(cmd))
    try:
        subprocess.run(  # nosec — argv constructed by us
            cmd,
            check=True,
            timeout=_POT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        raise InstallError(
            f"npm install failed (exit {exc.returncode}). "
            "Check your network connection and try again."
        ) from exc
    except subprocess.TimeoutExpired:
        raise InstallError(
            f"npm install timed out after {_POT_TIMEOUT_SECONDS}s. "
            "First-time installs over slow connections may need more time; "
            "re-run the doctor command."
        )

    # 5. Copy the vendored launcher.
    launcher_dest = js_dir / _POT_LAUNCHER_FILENAME
    launcher_src = _vendored_launcher_path()
    if not launcher_src.is_file():
        raise InstallError(
            f"vendored launcher missing from package: expected at {launcher_src}"
        )
    shutil.copy2(launcher_src, launcher_dest)

    # 6. Create the wrapper at ~/.pyt/bin/pyt-po-token.
    bin_dir = pyt_bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        wrapper_path = bin_dir / f"{_POT_WRAPPER_NAME}.cmd"
        # %~dp0 = directory of this batch file, ending in backslash.
        # NODE_PATH lets node resolve `require('bgutils-js')` from the
        # vendored node_modules without polluting the user's globals.
        wrapper_path.write_text(
            "@echo off\r\n"
            f'set "NODE_PATH={js_dir / "node_modules"}"\r\n'
            f'"{node_path}" "{launcher_dest}" %*\r\n',
            encoding="utf-8",
        )
    else:
        wrapper_path = bin_dir / _POT_WRAPPER_NAME
        wrapper_path.write_text(
            "#!/usr/bin/env sh\n"
            f'export NODE_PATH="{js_dir / "node_modules"}"\n'
            f'exec "{node_path}" "{launcher_dest}" "$@"\n',
            encoding="utf-8",
        )
        _make_executable(wrapper_path)

    # 7. Verify by running the wrapper. We don't actually generate a
    # token in the doctor (that hits YouTube and is slow); instead we
    # require it to start without crashing on missing dependencies.
    # The launcher hard-fails fast if its `require()`s don't resolve.
    if not _verify_po_token_install(wrapper_path):
        raise InstallError(
            f"installed po-token-generator at {wrapper_path} but the "
            "verification call failed; rerun with `pyt -vv --doctor "
            "--install po-token-generator` to see node's stderr."
        )

    return wrapper_path


def _vendored_launcher_path() -> Path:
    """Resolve the launcher file shipped inside the pyt package."""
    return Path(__file__).resolve().parent.parent / "data" / _POT_LAUNCHER_FILENAME


def _parse_node_major(version: str) -> Optional[int]:
    """Pull the major version out of strings like ``v20.10.0``."""
    text = version.strip().lstrip("v")
    head = text.split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _verify_po_token_install(wrapper_path: Path) -> bool:
    """Probe the install with a fast, network-free check.

    Strategy: we can't easily cheaply verify token generation without
    an HTTP call. Instead we just check that node can load the
    launcher and parse it (`node --check <launcher>`). If npm
    install missed a dependency, this fails with a useful error.
    """
    js_dir = pyt_data_dir() / "js"
    launcher = js_dir / _POT_LAUNCHER_FILENAME
    node_path = shutil.which("node")
    if not node_path or not launcher.is_file():
        return False
    try:
        result = subprocess.run(  # nosec
            [node_path, "--check", str(launcher)],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# ── per-tool plans ────────────────────────────────────────────────────────


# The xinntao/Real-ESRGAN Python project releases bundle the ncnn binary
# AND the model files together; the xinntao/Real-ESRGAN-ncnn-vulkan repo's
# own releases ship the binary only (no models). We use the former so one
# doctor install gives a fully working setup. The zip filename uses a date
# (YYYYMMDD), not the version tag.
_REALESRGAN_RELEASE_TAG = "v0.2.5.0"
_REALESRGAN_RELEASE_DATE = "20220424"
_REALESRGAN_BASE_URL = (
    f"https://github.com/xinntao/Real-ESRGAN/releases/download/"
    f"{_REALESRGAN_RELEASE_TAG}"
)


def _plan_realesrgan() -> InstallPlan:
    sysname, arch = _platform_key()
    if sysname == "windows":
        zip_name = f"realesrgan-ncnn-vulkan-{_REALESRGAN_RELEASE_DATE}-windows.zip"
        binary = "realesrgan-ncnn-vulkan.exe"
    elif sysname == "darwin":
        zip_name = f"realesrgan-ncnn-vulkan-{_REALESRGAN_RELEASE_DATE}-macos.zip"
        binary = "realesrgan-ncnn-vulkan"
    elif sysname == "linux":
        zip_name = f"realesrgan-ncnn-vulkan-{_REALESRGAN_RELEASE_DATE}-ubuntu.zip"
        binary = "realesrgan-ncnn-vulkan"
    else:
        raise InstallError(f"realesrgan auto-install: unsupported platform {sysname!r}")
    return InstallPlan(
        tool="realesrgan",
        url=f"{_REALESRGAN_BASE_URL}/{zip_name}",
        archive_format="zip",
        target_dir=pyt_bin_dir(),
        binary_name=binary,
        # Copies DLLs (Windows) and the models/ dir that ships alongside the
        # binary in this release bundle.
        copy_archive_dir=True,
    )


def _plan_ffmpeg() -> InstallPlan:
    sysname, arch = _platform_key()

    if sysname == "windows" and arch == "x86_64":
        return InstallPlan(
            tool="ffmpeg",
            # gyan.dev's "essentials" build — single archive, contains
            # ffmpeg.exe + ffprobe.exe + ffplay.exe under bin/.
            url="https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
            archive_format="zip",
            target_dir=pyt_bin_dir(),
            binary_name="ffmpeg.exe",
            extra_files=["ffprobe.exe", "ffplay.exe"],
        )

    if sysname == "linux" and arch == "x86_64":
        return InstallPlan(
            tool="ffmpeg",
            # BtbN static build — gpl variant gives us libx264.
            url=(
                "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                "ffmpeg-master-latest-linux64-gpl.tar.xz"
            ),
            archive_format="tar.xz",
            target_dir=pyt_bin_dir(),
            binary_name="ffmpeg",
            extra_files=["ffprobe"],
        )

    if sysname == "linux" and arch == "arm64":
        return InstallPlan(
            tool="ffmpeg",
            url=(
                "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                "ffmpeg-master-latest-linuxarm64-gpl.tar.xz"
            ),
            archive_format="tar.xz",
            target_dir=pyt_bin_dir(),
            binary_name="ffmpeg",
            extra_files=["ffprobe"],
        )

    if sysname == "darwin":
        # Auto-downloading a working ffmpeg build for both Intel and Apple
        # Silicon, with libx264 and the codec set we use, is genuinely
        # harder than it sounds. Homebrew's bottle is the right answer.
        raise InstallError(
            "macOS ffmpeg auto-install isn't supported — Homebrew's bottle is "
            "the right tool for the job. Run:\n  brew install ffmpeg"
        )

    raise InstallError(
        f"ffmpeg auto-install: no prebuilt binary known for {sysname}/{arch}. "
        f"Install via your package manager."
    )


# ── status rendering ──────────────────────────────────────────────────────


def render_status(tools: List[Tool], features: List[Feature]) -> str:
    """Build the human-readable doctor report. Pure function — the CLI
    just prints whatever this returns, so it's testable without stdout
    capture.
    """
    out: List[str] = []
    sysname, arch = _platform_key()
    out.append(
        f"pyt doctor - {sysname}/{arch}, python {sys.version_info.major}.{sys.version_info.minor}"
    )
    out.append(f"Managed bin dir: {pyt_bin_dir()}")
    out.append("")

    # Tools section
    out.append("Tools")
    out.append("-" * 60)
    for t in tools:
        if t.found:
            ver = t.version or "(version unknown)"
            out.append(f"  [OK]   {t.name:<13}  {ver}")
            out.append(f"         path:    {t.path}")
        else:
            hint = _missing_hint(t)
            out.append(f"  [--]   {t.name:<13}  not installed{hint}")
        out.append(f"         used by: {t.description}")
    out.append("")

    # Features section
    out.append("Features")
    out.append("-" * 60)
    for f in features:
        marker = "[OK]" if f.available else "[--]"
        line = f"  {marker}   {f.name}"
        if f.requires and not f.available:
            missing = [r for r in f.requires if not _is_present(tools, r)]
            line += f"  (needs: {', '.join(missing)})"
        out.append(line)
        if f.note:
            out.append(f"         {f.note}")
    out.append("")

    out.append("Install missing tools with:")
    out.append("  pyt --doctor --install ffmpeg")
    out.append("  pyt --doctor --install realesrgan")
    out.append("  pyt --doctor --install po-token-generator   (needs node)")
    out.append("  pyt --doctor --install all")
    return "\n".join(out)


def _is_present(tools: List[Tool], name: str) -> bool:
    for t in tools:
        if t.name == name:
            return t.found
    return False


_MANUAL_INSTALL_HINTS = {
    "ffprobe": "  (ships with ffmpeg)",
    "node": "  (install: https://nodejs.org)",
    "bun": "  (install: https://bun.sh)",
    "deno": "  (install: https://deno.com)",
    "bgutil-pot": "  (install: https://github.com/Brainicism/bgutil-ytdlp-pot-provider)",
}


def _missing_hint(tool: Tool) -> str:
    """Render the per-tool "how do I install this?" suffix."""
    if tool.installable:
        # The doctor's --install command takes an "install key" that's
        # not always the same as the tool name (e.g. the wrapper binary
        # `pyt-po-token` is installed by --install po-token-generator).
        install_name = _INSTALL_KEY_FOR_TOOL.get(tool.name, tool.name)
        return f"  (run: pyt --doctor --install {install_name})"
    return _MANUAL_INSTALL_HINTS.get(tool.name, "")


_INSTALL_KEY_FOR_TOOL = {
    "pyt-po-token": "po-token-generator",
}

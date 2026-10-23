"""Tests for the doctor module: tool detection, feature derivation,
URL selection, and the install orchestration. No real network — the
download function is always mocked."""

from __future__ import annotations

import io
import os
import subprocess
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from pyt.api import _paths
from pyt.api import doctor

# ── _paths bootstrap ───────────────────────────────────────────────────────


def test_pyt_bin_dir_under_home():
    bin_dir = _paths.pyt_bin_dir()
    assert bin_dir.parts[-2:] == (".pyt", "bin")
    # Must live under the home dir.
    assert str(bin_dir).startswith(str(Path.home()))


def test_ensure_bin_on_path_is_idempotent(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    _paths.ensure_bin_on_path()
    first = os.environ["PATH"]
    _paths.ensure_bin_on_path()
    assert os.environ["PATH"] == first
    # Bin dir is the FIRST entry.
    assert os.environ["PATH"].split(os.pathsep)[0] == str(_paths.pyt_bin_dir())


def test_ensure_bin_on_path_does_not_duplicate(monkeypatch):
    bin_dir = str(_paths.pyt_bin_dir())
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}{bin_dir}{os.pathsep}/opt/bin")
    _paths.ensure_bin_on_path()
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts.count(bin_dir) == 1
    assert parts[0] == bin_dir


# ── platform key normalization ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Windows", "AMD64", ("windows", "x86_64")),
        ("Windows", "x86_64", ("windows", "x86_64")),
        ("Linux", "x86_64", ("linux", "x86_64")),
        ("Linux", "aarch64", ("linux", "arm64")),
        ("Darwin", "arm64", ("darwin", "arm64")),
        ("Darwin", "x86_64", ("darwin", "x86_64")),
    ],
)
def test_platform_key_normalizes_arch(system, machine, expected):
    with mock.patch("pyt.api.doctor.platform.system", return_value=system), mock.patch(
        "pyt.api.doctor.platform.machine", return_value=machine
    ):
        assert doctor._platform_key() == expected


# ── tool detection ─────────────────────────────────────────────────────────


def test_detect_returns_path_and_version_when_found():
    tool = doctor.Tool(
        name="ffmpeg",
        binary="ffmpeg",
        description="x",
        required_for=[],
        install_url=None,
    )
    with mock.patch(
        "pyt.api.doctor.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch(
        "pyt.api.doctor.subprocess.run",
        return_value=mock.Mock(stdout=b"ffmpeg version 7.1\n", stderr=b""),
    ):
        result = doctor.detect(tool)
    assert result.found
    assert result.path == "/usr/bin/ffmpeg"
    assert result.version == "ffmpeg version 7.1"


def test_detect_returns_not_found_when_missing():
    tool = doctor.Tool(
        name="ffmpeg",
        binary="ffmpeg",
        description="x",
        required_for=[],
        install_url=None,
    )
    with mock.patch("pyt.api.doctor.shutil.which", return_value=None):
        result = doctor.detect(tool)
    assert not result.found
    assert result.path is None
    assert result.version is None


def test_get_version_returns_none_on_subprocess_failure():
    with mock.patch(
        "pyt.api.doctor.subprocess.run",
        side_effect=OSError("permission denied"),
    ):
        assert doctor._get_version("/bad/path", "-version") is None


def test_get_version_truncates_long_lines():
    long_line = "ffmpeg version 7.1 " + "x" * 200
    with mock.patch(
        "pyt.api.doctor.subprocess.run",
        return_value=mock.Mock(stdout=long_line.encode(), stderr=b""),
    ):
        v = doctor._get_version("/usr/bin/ffmpeg", "-version")
    assert v is not None
    assert len(v) <= 80


# ── feature derivation ─────────────────────────────────────────────────────


def _fake_tool(name, found):
    return doctor.Tool(
        name=name,
        binary=name,
        description="",
        required_for=[],
        install_url=None,
        found=found,
    )


def test_features_all_available_when_everything_installed():
    # Add the JS-runtime tools too — PO-token generation is gated on
    # at least one of them being present.
    tools = [
        _fake_tool(n, True)
        for n in (
            "ffmpeg",
            "ffprobe",
            "realesrgan",
            "node",
            "bun",
            "deno",
            "bgutil-pot",
        )
    ]
    features = doctor.feature_status(tools)
    assert all(f.available for f in features)


def test_features_disable_realesrgan_when_binary_missing():
    tools = [
        _fake_tool("ffmpeg", True),
        _fake_tool("ffprobe", True),
        _fake_tool("realesrgan", False),
    ]
    features = doctor.feature_status(tools)
    by_name = {f.name: f for f in features}
    assert not by_name["Upscale (algorithm='realesrgan')"].available
    # Lanczos still works because it only needs ffmpeg.
    assert by_name["Upscale (algorithm='lanczos')"].available


def test_features_disable_almost_everything_without_ffmpeg():
    tools = [
        _fake_tool("ffmpeg", False),
        _fake_tool("ffprobe", False),
        _fake_tool("realesrgan", True),
    ]
    features = doctor.feature_status(tools)
    by_name = {f.name: f for f in features}
    # Stream download works even without ffmpeg.
    assert by_name["Stream download (SABR + byte-range)"].available
    # But everything else requires ffmpeg.
    assert not by_name["Audio extraction / format conversion"].available
    assert not by_name["Upscale (algorithm='lanczos')"].available
    assert not by_name["Upscale (algorithm='realesrgan')"].available


# ── URL planning per platform ──────────────────────────────────────────────


def test_plan_realesrgan_windows():
    with mock.patch("pyt.api.doctor._platform_key", return_value=("windows", "x86_64")):
        plan = doctor._plan_realesrgan()
    assert plan.tool == "realesrgan"
    # Must come from the Python project release — that bundle includes models.
    assert "xinntao/Real-ESRGAN/releases/download/" in plan.url
    assert plan.url.endswith("-windows.zip")
    assert plan.binary_name == "realesrgan-ncnn-vulkan.exe"
    assert plan.copy_archive_dir is True


def test_plan_realesrgan_linux():
    with mock.patch("pyt.api.doctor._platform_key", return_value=("linux", "x86_64")):
        plan = doctor._plan_realesrgan()
    assert "xinntao/Real-ESRGAN/releases/download/" in plan.url
    assert plan.url.endswith("-ubuntu.zip")
    assert plan.binary_name == "realesrgan-ncnn-vulkan"


def test_plan_realesrgan_macos():
    with mock.patch("pyt.api.doctor._platform_key", return_value=("darwin", "arm64")):
        plan = doctor._plan_realesrgan()
    assert "xinntao/Real-ESRGAN/releases/download/" in plan.url
    assert plan.url.endswith("-macos.zip")
    assert plan.binary_name == "realesrgan-ncnn-vulkan"


def test_plan_ffmpeg_windows():
    with mock.patch("pyt.api.doctor._platform_key", return_value=("windows", "x86_64")):
        plan = doctor._plan_ffmpeg()
    assert "gyan.dev" in plan.url
    assert plan.archive_format == "zip"
    assert plan.binary_name == "ffmpeg.exe"
    assert "ffprobe.exe" in plan.extra_files


def test_plan_ffmpeg_linux_x86_64():
    with mock.patch("pyt.api.doctor._platform_key", return_value=("linux", "x86_64")):
        plan = doctor._plan_ffmpeg()
    assert "BtbN" in plan.url
    assert "linux64" in plan.url
    assert plan.archive_format == "tar.xz"
    assert plan.binary_name == "ffmpeg"


def test_plan_ffmpeg_linux_arm64():
    with mock.patch("pyt.api.doctor._platform_key", return_value=("linux", "arm64")):
        plan = doctor._plan_ffmpeg()
    assert "linuxarm64" in plan.url


def test_plan_ffmpeg_macos_points_to_homebrew():
    with mock.patch("pyt.api.doctor._platform_key", return_value=("darwin", "arm64")):
        with pytest.raises(doctor.InstallError, match="brew install ffmpeg"):
            doctor._plan_ffmpeg()


def test_plan_ffmpeg_unknown_platform():
    with mock.patch("pyt.api.doctor._platform_key", return_value=("plan9", "alien")):
        with pytest.raises(doctor.InstallError, match="no prebuilt binary"):
            doctor._plan_ffmpeg()


def test_plan_install_unknown_tool():
    with pytest.raises(doctor.InstallError, match="unknown tool"):
        doctor.plan_install("noisemaker")


# ── install orchestration ──────────────────────────────────────────────────


def _make_realesrgan_zip(target_zip: Path, binary_name: str) -> None:
    """Build a fake Real-ESRGAN-shaped zip mirroring the real bundle layout:
    a top-level dir with the binary, companion DLLs, and a models/ subdir."""
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target_zip, "w") as zf:
        zf.writestr(f"realesrgan-ncnn-vulkan/{binary_name}", b"\x7fELF" + b"\x00" * 40)
        zf.writestr("realesrgan-ncnn-vulkan/vcomp140.dll", b"\x4d\x5a" + b"\x00" * 10)
        zf.writestr("realesrgan-ncnn-vulkan/vcomp140d.dll", b"\x4d\x5a" + b"\x00" * 10)
        zf.writestr("realesrgan-ncnn-vulkan/models/realesrgan-x4plus.bin", b"weights")
        zf.writestr("realesrgan-ncnn-vulkan/models/realesrgan-x4plus.param", b"params")


def test_install_realesrgan_full_pipeline(tmp_path, monkeypatch):
    """End-to-end test of the install orchestration with a fake archive,
    fake download, and fake verify. No real network."""
    fake_pyt_bin = tmp_path / ".pyt" / "bin"
    monkeypatch.setattr(doctor, "pyt_bin_dir", lambda: fake_pyt_bin)

    binary_name = "realesrgan-ncnn-vulkan.exe"

    def fake_download(url, target, *, on_progress=None):
        _make_realesrgan_zip(target, binary_name)
        if on_progress:
            on_progress(100, 100)

    plan = doctor.InstallPlan(
        tool="realesrgan",
        url="https://example.test/realesrgan.zip",
        archive_format="zip",
        target_dir=fake_pyt_bin,
        binary_name=binary_name,
        copy_archive_dir=True,
    )

    with mock.patch("pyt.api.doctor.plan_install", return_value=plan), mock.patch(
        "pyt.api.doctor._download", side_effect=fake_download
    ), mock.patch("pyt.api.doctor._verify", return_value=True):
        result = doctor.install("realesrgan")

    assert result == fake_pyt_bin / binary_name
    assert result.exists()
    # Companion DLLs must land next to the binary.
    assert (fake_pyt_bin / "vcomp140.dll").is_file()
    assert (fake_pyt_bin / "vcomp140d.dll").is_file()
    # Model files must land in models/ so the binary can find them.
    assert (fake_pyt_bin / "models" / "realesrgan-x4plus.bin").is_file()
    assert (fake_pyt_bin / "models" / "realesrgan-x4plus.param").is_file()


def test_install_raises_when_binary_missing_from_archive(tmp_path, monkeypatch):
    """A wrong/corrupt archive should fail the install rather than
    silently producing an empty bin dir."""
    fake_pyt_bin = tmp_path / ".pyt" / "bin"
    monkeypatch.setattr(doctor, "pyt_bin_dir", lambda: fake_pyt_bin)

    def fake_download(url, target, *, on_progress=None):
        # Empty zip — no binary inside.
        with zipfile.ZipFile(target, "w") as zf:
            zf.writestr("README.md", b"oops")

    plan = doctor.InstallPlan(
        tool="realesrgan",
        url="https://example.test/x.zip",
        archive_format="zip",
        target_dir=fake_pyt_bin,
        binary_name="realesrgan-ncnn-vulkan",
        extra_files=[],
    )

    with mock.patch("pyt.api.doctor.plan_install", return_value=plan), mock.patch(
        "pyt.api.doctor._download", side_effect=fake_download
    ):
        with pytest.raises(doctor.InstallError, match="could not locate"):
            doctor.install("realesrgan")


def test_install_raises_when_verify_fails(tmp_path, monkeypatch):
    """If the binary lands but doesn't respond to its sanity check, we
    roll back the install."""
    fake_pyt_bin = tmp_path / ".pyt" / "bin"
    monkeypatch.setattr(doctor, "pyt_bin_dir", lambda: fake_pyt_bin)

    binary_name = "realesrgan-ncnn-vulkan"

    def fake_download(url, target, *, on_progress=None):
        _make_realesrgan_zip(target, binary_name)

    plan = doctor.InstallPlan(
        tool="realesrgan",
        url="https://example.test/x.zip",
        archive_format="zip",
        target_dir=fake_pyt_bin,
        binary_name=binary_name,
        extra_files=[],
    )

    with mock.patch("pyt.api.doctor.plan_install", return_value=plan), mock.patch(
        "pyt.api.doctor._download", side_effect=fake_download
    ), mock.patch("pyt.api.doctor._verify", return_value=False):
        with pytest.raises(doctor.InstallError, match="sanity-check"):
            doctor.install("realesrgan")
    # Rollback: the binary should NOT be left behind.
    assert not (fake_pyt_bin / binary_name).exists()


# ── po-token-generator install ─────────────────────────────────────────────


def test_install_po_token_generator_requires_node(tmp_path, monkeypatch):
    """`pyt --doctor --install po-token-generator` errors clearly when
    Node isn't on PATH — we don't auto-install runtimes."""
    monkeypatch.setattr(doctor, "pyt_data_dir", lambda: tmp_path / ".pyt")
    monkeypatch.setattr(doctor, "pyt_bin_dir", lambda: tmp_path / ".pyt" / "bin")

    with mock.patch("pyt.api.doctor.shutil.which", return_value=None):
        with pytest.raises(doctor.InstallError, match="Node.js"):
            doctor._install_po_token_generator()


def test_install_po_token_generator_requires_npm(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "pyt_data_dir", lambda: tmp_path / ".pyt")
    monkeypatch.setattr(doctor, "pyt_bin_dir", lambda: tmp_path / ".pyt" / "bin")

    def _which(name):
        return "/usr/bin/node" if name == "node" else None

    with mock.patch("pyt.api.doctor.shutil.which", side_effect=_which):
        with pytest.raises(doctor.InstallError, match="npm"):
            doctor._install_po_token_generator()


def test_install_po_token_generator_rejects_old_node(tmp_path, monkeypatch):
    """Node 16 lacks global fetch — refuse to install on it."""
    monkeypatch.setattr(doctor, "pyt_data_dir", lambda: tmp_path / ".pyt")
    monkeypatch.setattr(doctor, "pyt_bin_dir", lambda: tmp_path / ".pyt" / "bin")

    def _which(name):
        return f"/usr/bin/{name}" if name in ("node", "npm") else None

    def _run(cmd, **kwargs):
        if cmd[0] == "/usr/bin/node" and "--version" in cmd:
            return mock.Mock(stdout=b"v16.20.0\n", returncode=0)
        return mock.Mock(returncode=0)

    with mock.patch("pyt.api.doctor.shutil.which", side_effect=_which), mock.patch(
        "pyt.api.doctor.subprocess.run", side_effect=_run
    ):
        with pytest.raises(doctor.InstallError, match="Node 18"):
            doctor._install_po_token_generator()


def test_install_po_token_generator_full_pipeline(tmp_path, monkeypatch):
    """End-to-end with mocked subprocesses: npm install runs, the
    launcher gets copied, and the wrapper script ends up at
    ~/.pyt/bin/pyt-po-token."""
    fake_pyt = tmp_path / ".pyt"
    monkeypatch.setattr(doctor, "pyt_data_dir", lambda: fake_pyt)
    monkeypatch.setattr(doctor, "pyt_bin_dir", lambda: fake_pyt / "bin")

    def _which(name):
        return f"/usr/bin/{name}" if name in ("node", "npm") else None

    npm_calls = []

    def _run(cmd, **kwargs):
        if cmd[0] == "/usr/bin/node" and "--version" in cmd:
            return mock.Mock(stdout=b"v20.10.0\n", returncode=0)
        if cmd[0] == "/usr/bin/node" and "--check" in cmd:
            return mock.Mock(returncode=0)
        if cmd[0] == "/usr/bin/npm" and "install" in cmd:
            npm_calls.append(cmd)
            # Pretend npm wrote node_modules.
            (fake_pyt / "js" / "node_modules").mkdir(parents=True, exist_ok=True)
            return mock.Mock(returncode=0)
        return mock.Mock(returncode=0)

    with mock.patch("pyt.api.doctor.shutil.which", side_effect=_which), mock.patch(
        "pyt.api.doctor.subprocess.run", side_effect=_run
    ):
        wrapper_path = doctor._install_po_token_generator()

    # Wrapper sits at the expected location.
    expected_name = "pyt-po-token.cmd" if os.name == "nt" else "pyt-po-token"
    assert wrapper_path == fake_pyt / "bin" / expected_name
    assert wrapper_path.is_file()

    # Wrapper content references the launcher and a node binary.
    text = wrapper_path.read_text()
    assert "po_token_launcher.js" in text
    assert "/usr/bin/node" in text or "node" in text

    # Launcher was copied alongside the deps.
    assert (fake_pyt / "js" / "po_token_launcher.js").is_file()

    # package.json was written so npm install lands in the right dir.
    pkg = (fake_pyt / "js" / "package.json").read_text()
    assert '"name": "pyt-po-token-deps"' in pkg

    # npm install was invoked with the right packages and prefix.
    assert len(npm_calls) == 1
    cmd = npm_calls[0]
    assert "--prefix" in cmd
    assert str(fake_pyt / "js") in cmd
    assert any("bgutils-js" in arg for arg in cmd)
    assert any("youtubei.js" in arg for arg in cmd)


def test_install_po_token_generator_npm_install_failure(tmp_path, monkeypatch):
    """If npm install fails, the install raises InstallError with a
    helpful retry message."""
    fake_pyt = tmp_path / ".pyt"
    monkeypatch.setattr(doctor, "pyt_data_dir", lambda: fake_pyt)
    monkeypatch.setattr(doctor, "pyt_bin_dir", lambda: fake_pyt / "bin")

    def _which(name):
        return f"/usr/bin/{name}" if name in ("node", "npm") else None

    def _run(cmd, **kwargs):
        if cmd[0] == "/usr/bin/node" and "--version" in cmd:
            return mock.Mock(stdout=b"v20.10.0\n", returncode=0)
        if cmd[0] == "/usr/bin/npm":
            raise subprocess.CalledProcessError(1, cmd, stderr=b"network error")
        return mock.Mock(returncode=0)

    with mock.patch("pyt.api.doctor.shutil.which", side_effect=_which), mock.patch(
        "pyt.api.doctor.subprocess.run", side_effect=_run
    ):
        with pytest.raises(doctor.InstallError, match="npm install failed"):
            doctor._install_po_token_generator()


def test_install_po_token_generator_verify_failure_rolls_back(tmp_path, monkeypatch):
    """Verification failure (node --check launcher.js exits non-zero)
    surfaces as an InstallError. We don't auto-clean here because the
    user's diagnostic value of having the failing files on disk is
    higher than the cleanliness value of removing them."""
    fake_pyt = tmp_path / ".pyt"
    monkeypatch.setattr(doctor, "pyt_data_dir", lambda: fake_pyt)
    monkeypatch.setattr(doctor, "pyt_bin_dir", lambda: fake_pyt / "bin")

    def _which(name):
        return f"/usr/bin/{name}" if name in ("node", "npm") else None

    def _run(cmd, **kwargs):
        if cmd[0] == "/usr/bin/node" and "--version" in cmd:
            return mock.Mock(stdout=b"v20.10.0\n", returncode=0)
        if cmd[0] == "/usr/bin/node" and "--check" in cmd:
            return mock.Mock(returncode=1)  # syntax check failed
        if cmd[0] == "/usr/bin/npm":
            (fake_pyt / "js" / "node_modules").mkdir(parents=True, exist_ok=True)
            return mock.Mock(returncode=0)
        return mock.Mock(returncode=0)

    with mock.patch("pyt.api.doctor.shutil.which", side_effect=_which), mock.patch(
        "pyt.api.doctor.subprocess.run", side_effect=_run
    ):
        with pytest.raises(doctor.InstallError, match="verification call failed"):
            doctor._install_po_token_generator()


def test_install_po_token_generator_dispatched_via_install_function(
    tmp_path, monkeypatch
):
    """`doctor.install("po-token-generator")` reaches the special-case
    path rather than trying plan_install."""
    monkeypatch.setattr(doctor, "pyt_data_dir", lambda: tmp_path / ".pyt")
    monkeypatch.setattr(doctor, "pyt_bin_dir", lambda: tmp_path / ".pyt" / "bin")

    with mock.patch(
        "pyt.api.doctor._install_po_token_generator",
        return_value=tmp_path / ".pyt" / "bin" / "pyt-po-token",
    ) as mock_install:
        doctor.install("po-token-generator")

    mock_install.assert_called_once()


def test_parse_node_major():
    assert doctor._parse_node_major("v20.10.0") == 20
    assert doctor._parse_node_major("18.0.0") == 18
    assert doctor._parse_node_major("garbage") is None
    assert doctor._parse_node_major("v") is None


def test_install_translates_download_failure(tmp_path, monkeypatch):
    fake_pyt_bin = tmp_path / ".pyt" / "bin"
    monkeypatch.setattr(doctor, "pyt_bin_dir", lambda: fake_pyt_bin)

    plan = doctor.InstallPlan(
        tool="realesrgan",
        url="https://example.test/x.zip",
        archive_format="zip",
        target_dir=fake_pyt_bin,
        binary_name="x",
        extra_files=[],
    )

    def fake_download(url, target, *, on_progress=None):
        raise ConnectionError("network unavailable")

    with mock.patch("pyt.api.doctor.plan_install", return_value=plan), mock.patch(
        "pyt.api.doctor._download", side_effect=fake_download
    ):
        with pytest.raises(doctor.InstallError, match="download failed"):
            doctor.install("realesrgan")


# ── render_status (pure function — testable without stdout) ────────────────


def test_render_status_lists_all_tools():
    tools = [
        _fake_tool("ffmpeg", True),
        _fake_tool("ffprobe", True),
        _fake_tool("realesrgan", False),
    ]
    tools[0].path = "/usr/bin/ffmpeg"
    tools[0].version = "ffmpeg version 7.1"
    tools[1].path = "/usr/bin/ffprobe"
    tools[1].version = "ffprobe version 7.1"

    rendered = doctor.render_status(tools, doctor.feature_status(tools))
    assert "ffmpeg" in rendered
    assert "ffprobe" in rendered
    assert "realesrgan" in rendered
    assert "[OK]" in rendered
    assert "[--]" in rendered  # realesrgan not installed
    # Install hint is present for missing installable tools.
    assert "pyt --doctor --install realesrgan" in rendered


def test_render_status_shows_managed_bin_dir():
    rendered = doctor.render_status([], [])
    assert "Managed bin dir" in rendered
    assert ".pyt" in rendered  # whatever path it resolves to


# ── _verify ───────────────────────────────────────────────────────────────


def test_verify_accepts_returncode_zero(tmp_path):
    binary = tmp_path / "ffmpeg"
    binary.write_bytes(b"fake")
    with mock.patch(
        "pyt.api.doctor.subprocess.run",
        return_value=mock.Mock(returncode=0),
    ):
        assert doctor._verify(binary, "ffmpeg") is True


def test_verify_accepts_returncode_one_for_realesrgan(tmp_path):
    """realesrgan-ncnn-vulkan exits 1 on -h. That's normal."""
    binary = tmp_path / "realesrgan-ncnn-vulkan"
    binary.write_bytes(b"fake")
    with mock.patch(
        "pyt.api.doctor.subprocess.run",
        return_value=mock.Mock(returncode=1),
    ):
        assert doctor._verify(binary, "realesrgan") is True


def test_verify_rejects_returncode_two(tmp_path):
    binary = tmp_path / "ffmpeg"
    binary.write_bytes(b"fake")
    with mock.patch(
        "pyt.api.doctor.subprocess.run",
        return_value=mock.Mock(returncode=2),
    ):
        assert doctor._verify(binary, "ffmpeg") is False


def test_verify_returns_false_on_subprocess_failure(tmp_path):
    binary = tmp_path / "ffmpeg"
    binary.write_bytes(b"fake")
    with mock.patch(
        "pyt.api.doctor.subprocess.run",
        side_effect=OSError("permission denied"),
    ):
        assert doctor._verify(binary, "ffmpeg") is False

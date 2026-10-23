"""Tests for PO-token providers, JS-runtime detection, and the
auto-retry path in :class:`Client` / :class:`CombinedDownload`."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from pyt.api import _jsruntime, po_token
from pyt.api.errors import AttestationRequired, ConfigError

# ── JS runtime detection ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_runtime_cache():
    _jsruntime.invalidate()
    yield
    _jsruntime.invalidate()


def _which_factory(*available):
    paths = {n: f"/usr/bin/{n}" for n in available}
    return lambda name: paths.get(name)


def test_detect_runtime_finds_node():
    with mock.patch(
        "pyt.api._jsruntime.shutil.which", side_effect=_which_factory("node")
    ), mock.patch(
        "pyt.api._jsruntime.subprocess.run",
        return_value=mock.Mock(stdout=b"v20.10.0\n", stderr=b""),
    ):
        rt = _jsruntime.detect_runtime()
    assert rt is not None
    assert rt.name == "node"
    assert rt.version == "v20.10.0"


def test_detect_runtime_prefers_node_over_bun():
    with mock.patch(
        "pyt.api._jsruntime.shutil.which",
        side_effect=_which_factory("node", "bun", "deno"),
    ), mock.patch(
        "pyt.api._jsruntime.subprocess.run",
        return_value=mock.Mock(stdout=b"v20.0\n", stderr=b""),
    ):
        rt = _jsruntime.detect_runtime()
    assert rt is not None
    assert rt.name == "node"


def test_detect_runtime_falls_through_to_bun():
    with mock.patch(
        "pyt.api._jsruntime.shutil.which", side_effect=_which_factory("bun")
    ), mock.patch(
        "pyt.api._jsruntime.subprocess.run",
        return_value=mock.Mock(stdout=b"1.0.30\n", stderr=b""),
    ):
        rt = _jsruntime.detect_runtime()
    assert rt is not None
    assert rt.name == "bun"


def test_detect_runtime_returns_none_when_nothing_present():
    with mock.patch("pyt.api._jsruntime.shutil.which", return_value=None):
        rt = _jsruntime.detect_runtime()
    assert rt is None


def test_detect_runtime_skips_unresponsive_binary():
    """If a binary is on PATH but doesn't respond to --version, we
    keep looking. Some shims claim to be node but are wrappers around
    something else."""

    def _run(cmd, **kw):
        if "node" in cmd[0]:
            raise OSError("permission denied")
        return mock.Mock(stdout=b"1.0.0\n", stderr=b"")

    with mock.patch(
        "pyt.api._jsruntime.shutil.which", side_effect=_which_factory("node", "bun")
    ), mock.patch("pyt.api._jsruntime.subprocess.run", side_effect=_run):
        rt = _jsruntime.detect_runtime()
    assert rt is not None
    assert rt.name == "bun"


def test_detect_all_runtimes_returns_every_present():
    with mock.patch(
        "pyt.api._jsruntime.shutil.which", side_effect=_which_factory("node", "deno")
    ), mock.patch(
        "pyt.api._jsruntime.subprocess.run",
        return_value=mock.Mock(stdout=b"x\n", stderr=b""),
    ):
        runtimes = _jsruntime.detect_all_runtimes()
    names = [rt.name for rt in runtimes]
    assert names == ["node", "deno"]  # in preference order


def test_detect_runtime_caches_result():
    """Detection runs shutil.which once; subsequent calls hit the cache."""
    counter = {"calls": 0}

    def _which(name):
        counter["calls"] += 1
        return None

    with mock.patch("pyt.api._jsruntime.shutil.which", side_effect=_which):
        _jsruntime.detect_runtime()
        _jsruntime.detect_runtime()
        _jsruntime.detect_runtime()
    # Three runtimes checked on the first call; cache hits after.
    assert counter["calls"] == 3


def test_invalidate_forces_redetection():
    counter = {"calls": 0}

    def _which(name):
        counter["calls"] += 1
        return None

    with mock.patch("pyt.api._jsruntime.shutil.which", side_effect=_which):
        _jsruntime.detect_runtime()
        _jsruntime.invalidate()
        _jsruntime.detect_runtime()
    assert counter["calls"] == 6  # 3 per detection × 2 detections


# ── StaticProvider ─────────────────────────────────────────────────────────


def test_static_provider_returns_token():
    p = po_token.StaticProvider("xyz")
    assert p.get() == "xyz"
    assert p.get() == "xyz"  # cached


def test_static_provider_strips_whitespace():
    p = po_token.StaticProvider("  xyz\n")
    assert p.get() == "xyz"


def test_static_provider_rejects_empty():
    with pytest.raises(ConfigError, match="non-empty"):
        po_token.StaticProvider("")
    with pytest.raises(ConfigError, match="non-empty"):
        po_token.StaticProvider("   ")


def test_static_provider_invalidate_is_noop():
    p = po_token.StaticProvider("abc")
    p.invalidate()
    # Still returns the same value — there's nothing else to return.
    assert p.get() == "abc"


# ── CallableProvider ──────────────────────────────────────────────────────


def test_callable_provider_invokes_fn():
    p = po_token.CallableProvider(lambda: "from-callable")
    assert p.get() == "from-callable"


def test_callable_provider_caches_within_ttl():
    counter = {"calls": 0}

    def _gen():
        counter["calls"] += 1
        return f"token-{counter['calls']}"

    p = po_token.CallableProvider(_gen, ttl_seconds=60)
    assert p.get() == "token-1"
    assert p.get() == "token-1"  # cached
    assert counter["calls"] == 1


def test_callable_provider_force_refresh():
    counter = {"calls": 0}

    def _gen():
        counter["calls"] += 1
        return f"token-{counter['calls']}"

    p = po_token.CallableProvider(_gen)
    assert p.get() == "token-1"
    assert p.get(force_refresh=True) == "token-2"
    assert counter["calls"] == 2


def test_callable_provider_invalidate_then_get_regenerates():
    counter = {"calls": 0}

    def _gen():
        counter["calls"] += 1
        return f"token-{counter['calls']}"

    p = po_token.CallableProvider(_gen)
    p.get()
    p.invalidate()
    p.get()
    assert counter["calls"] == 2


def test_callable_provider_propagates_failure_as_config_error():
    def _gen():
        raise RuntimeError("network down")

    p = po_token.CallableProvider(_gen)
    with pytest.raises(ConfigError, match="callable.*failed"):
        p.get()


def test_callable_provider_rejects_empty_token():
    p = po_token.CallableProvider(lambda: "   ")
    with pytest.raises(ConfigError, match="empty token"):
        p.get()


def test_callable_provider_rejects_non_callable():
    with pytest.raises(ConfigError, match="must be callable"):
        po_token.CallableProvider("not-a-callable")  # type: ignore[arg-type]


# ── CommandProvider ──────────────────────────────────────────────────────


def test_command_provider_runs_string_command():
    with mock.patch(
        "pyt.api.po_token.subprocess.run",
        return_value=mock.Mock(stdout=b"my-token\n", returncode=0),
    ) as run:
        p = po_token.CommandProvider("bgutil-pot --visitor-data abc")
        token = p.get()
    assert token == "my-token"
    # shlex-split the command.
    assert run.call_args.args[0] == ["bgutil-pot", "--visitor-data", "abc"]


def test_command_provider_runs_list_command():
    with mock.patch(
        "pyt.api.po_token.subprocess.run",
        return_value=mock.Mock(stdout=b"my-token\n", returncode=0),
    ) as run:
        po_token.CommandProvider(["bgutil-pot", "--vd", "x"]).get()
    assert run.call_args.args[0] == ["bgutil-pot", "--vd", "x"]


def test_command_provider_takes_last_nonempty_line():
    """Some generators print log lines before the token. Take the last."""
    with mock.patch(
        "pyt.api.po_token.subprocess.run",
        return_value=mock.Mock(
            stdout=b"info: starting\ninfo: visitor data ok\nactual-token-here\n",
            returncode=0,
        ),
    ):
        token = po_token.CommandProvider("x").get()
    assert token == "actual-token-here"


def test_command_provider_rejects_empty_command():
    with pytest.raises(ConfigError, match="non-empty"):
        po_token.CommandProvider("")


def test_command_provider_translates_subprocess_failure():
    err = subprocess.CalledProcessError(1, ["x"], stderr=b"boom")
    with mock.patch("pyt.api.po_token.subprocess.run", side_effect=err):
        p = po_token.CommandProvider("x")
        with pytest.raises(ConfigError, match="command.*failed"):
            p.get()


# ── ScriptProvider ───────────────────────────────────────────────────────


def test_script_provider_requires_existing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        po_token.ScriptProvider(tmp_path / "missing.js")


def test_script_provider_runs_with_detected_runtime(tmp_path):
    script = tmp_path / "gen.js"
    script.write_text("console.log('token-from-js')\n")

    fake_rt = _jsruntime.JsRuntime(name="node", path="/usr/bin/node", version="v20")
    with mock.patch(
        "pyt.api.po_token.detect_runtime", return_value=fake_rt
    ), mock.patch(
        "pyt.api.po_token.run_script",
        return_value="token-from-js",
    ) as rs:
        token = po_token.ScriptProvider(script).get()
    assert token == "token-from-js"
    rs.assert_called_once()
    # run_script was called with the resolved script path.
    assert rs.call_args.args[0] == fake_rt
    assert Path(rs.call_args.args[1]) == script.resolve()


def test_script_provider_raises_when_no_runtime(tmp_path):
    script = tmp_path / "gen.js"
    script.write_text("console.log('x')\n")

    with mock.patch("pyt.api.po_token.detect_runtime", return_value=None):
        p = po_token.ScriptProvider(script)
        with pytest.raises(ConfigError, match="JavaScript runtime"):
            p.get()


# ── coerce_provider ──────────────────────────────────────────────────────


def test_coerce_provider_static():
    p = po_token.coerce_provider(po_token="abc")
    assert isinstance(p, po_token.StaticProvider)


def test_coerce_provider_callable():
    p = po_token.coerce_provider(po_token_provider=lambda: "x")
    assert isinstance(p, po_token.CallableProvider)


def test_coerce_provider_command():
    p = po_token.coerce_provider(po_token_cmd="bgutil-pot")
    assert isinstance(p, po_token.CommandProvider)


def test_coerce_provider_returns_none_when_unset():
    assert po_token.coerce_provider() is None


def test_coerce_provider_rejects_multiple_sources():
    with pytest.raises(ConfigError, match="at most one"):
        po_token.coerce_provider(po_token="x", po_token_cmd="y")


# ── Client integration ──────────────────────────────────────────────────


def test_client_accepts_po_token_static():
    from pyt import Client

    c = Client(po_token="my-static-token")
    assert c.po_token == "my-static-token"


def test_client_accepts_po_token_callable():
    from pyt import Client

    c = Client(po_token_provider=lambda: "from-callable")
    assert c.po_token == "from-callable"


def test_client_rejects_multiple_po_token_sources():
    from pyt import Client

    with pytest.raises(ConfigError, match="at most one"):
        Client(po_token="x", po_token_provider=lambda: "y")


def test_client_no_po_token():
    from pyt import Client

    c = Client()
    assert c.po_token is None
    assert not c.has_po_token_provider


def test_client_refresh_returns_none_without_provider():
    from pyt import Client

    c = Client()
    assert c.refresh_po_token() is None


def test_client_refresh_invalidates_cache():
    from pyt import Client

    counter = {"n": 0}

    def _gen():
        counter["n"] += 1
        return f"token-{counter['n']}"

    c = Client(po_token_provider=_gen)
    assert c.po_token == "token-1"
    assert c.refresh_po_token() == "token-2"


# ── CombinedDownload auto-retry ──────────────────────────────────────────


def _make_combined_video(cipher_signature, *, video_size=100, audio_size=100):
    """Build a Video + two raw streams for CombinedDownload PO-token tests."""
    from pyt.api._streams_hydrator import _RawStream
    from pyt.api.streams import StreamSet
    from pyt.api._sabr_config import _SabrConfig
    from pyt.api.video import Video, _hydrate_meta

    pr = cipher_signature._vid_info
    meta = _hydrate_meta(
        pr, url=f"https://youtube.com/watch?v={cipher_signature.video_id}"
    )
    video = Video(
        player_response=pr, meta=meta, client_name="ANDROID_VR", client_cfg={}
    )
    v_raw = _RawStream(
        itag=136,
        url="https://ex.com/video",
        mime_type="video/mp4",
        kind="video",
        subtype="mp4",
        video_codec="avc1.4d401f",
        audio_codec=None,
        is_adaptive=True,
        is_progressive=False,
        is_otf=False,
        bitrate=1500000,
        filesize=video_size,
        filesize_approx=video_size,
        resolution="720p",
        fps=30,
        abr=None,
        default_filename="v.mp4",
    )
    a_raw = _RawStream(
        itag=140,
        url="https://ex.com/audio",
        mime_type="audio/mp4",
        kind="audio",
        subtype="mp4",
        video_codec=None,
        audio_codec="mp4a.40.2",
        is_adaptive=True,
        is_progressive=False,
        is_otf=False,
        bitrate=128000,
        filesize=audio_size,
        filesize_approx=audio_size,
        resolution=None,
        fps=None,
        abr="128kbps",
        default_filename="a.mp4",
    )
    video._streams = StreamSet._from_raw([v_raw, a_raw], video=video)
    video._sabr_config = _SabrConfig()
    return video, video._streams[0], video._streams[1]


def test_combined_download_translates_attestation_required(cipher_signature, tmp_path):
    """SabrAttestationRequired from the inner SABR session should
    surface as the modern AttestationRequired error."""
    from pyt.api import CombinedDownload
    from pyt.sabr.session import SabrAttestationRequired

    video, v_stream, a_stream = _make_combined_video(
        cipher_signature, video_size=100, audio_size=100
    )

    plan = CombinedDownload(
        video=video,
        video_stream=v_stream,
        audio_stream=a_stream,
        output_path=str(tmp_path),
        filename="out.mkv",
        container="mkv",
    )
    video._sabr_config.sabr_url = "https://sabr.example/play"
    video._sabr_config.duration = 10

    fake_session = mock.MagicMock()
    fake_session.iter_chunks.side_effect = SabrAttestationRequired(
        "PO token required (status=3)"
    )

    with mock.patch("pyt.api.combined.SabrSession", return_value=fake_session):
        with pytest.raises(AttestationRequired):
            plan.run()


def test_combined_download_retries_with_refreshed_po_token(cipher_signature, tmp_path):
    """When a Client with a provider hits AttestationRequired, the
    PO token is refreshed and the SABR session is rebuilt once."""
    from pyt.api import CombinedDownload
    from pyt.sabr.session import SabrAttestationRequired

    video, v_stream, a_stream = _make_combined_video(
        cipher_signature, video_size=4, audio_size=4
    )

    # Stand in for a Client that has a provider.
    fake_client = mock.MagicMock()
    fake_client.has_po_token_provider = True
    fake_client.refresh_po_token.return_value = "fresh-token-after-retry"
    video._client = fake_client

    plan = CombinedDownload(
        video=video,
        video_stream=v_stream,
        audio_stream=a_stream,
        output_path=str(tmp_path),
        filename="out.mkv",
        container="mkv",
    )
    video._sabr_config.sabr_url = "https://sabr.example/play"

    # First call raises AttestationRequired; second call delivers bytes.
    fake_session_1 = mock.MagicMock()
    fake_session_1.iter_chunks.side_effect = SabrAttestationRequired(
        "PO token required"
    )
    fake_session_2 = mock.MagicMock()
    fake_session_2.iter_chunks.return_value = iter(
        [
            (v_stream.itag, b"VVVV"),
            (a_stream.itag, b"AAAA"),
        ]
    )
    sessions = [fake_session_1, fake_session_2]

    def _make_session(*args, **kwargs):
        return sessions.pop(0)

    def fake_merge(video_path, audio_path, output_path, *, container):
        Path(output_path).write_bytes(b"merged")
        return Path(output_path)

    with mock.patch(
        "pyt.api.combined.SabrSession", side_effect=_make_session
    ), mock.patch("pyt.api.combined.run_ffmpeg_merge", side_effect=fake_merge):
        result = plan.run()

    fake_client.refresh_po_token.assert_called_once()
    assert video._sabr_config.po_token == "fresh-token-after-retry"
    assert result == tmp_path / "out.mkv"


def test_combined_download_propagates_when_no_provider(cipher_signature, tmp_path):
    """If the parent Client has no PO token provider, AttestationRequired
    propagates without retry."""
    from pyt.api import CombinedDownload
    from pyt.sabr.session import SabrAttestationRequired

    video, v_stream, a_stream = _make_combined_video(
        cipher_signature, video_size=4, audio_size=4
    )
    # No _client attribute means no provider.

    plan = CombinedDownload(
        video=video,
        video_stream=v_stream,
        audio_stream=a_stream,
        output_path=str(tmp_path),
        filename="out.mkv",
        container="mkv",
    )
    video._sabr_config.sabr_url = "https://sabr.example/play"

    fake_session = mock.MagicMock()
    fake_session.iter_chunks.side_effect = SabrAttestationRequired("PO token required")
    with mock.patch("pyt.api.combined.SabrSession", return_value=fake_session):
        with pytest.raises(AttestationRequired):
            plan.run()


# ── CLI flag plumbing ─────────────────────────────────────────────────────


def test_cli_resolves_po_token_passthrough():
    from pyt import cli

    args = mock.MagicMock(po_token="raw-token", po_token_cmd=None, po_token_script=None)
    assert cli._resolve_po_token(args) == "raw-token"


def test_cli_resolves_po_token_cmd():
    from pyt import cli

    with mock.patch(
        "pyt.api.po_token.subprocess.run",
        return_value=mock.Mock(stdout=b"cmd-generated-token\n", returncode=0),
    ):
        args = mock.MagicMock(
            po_token=None,
            po_token_cmd="bgutil-pot",
            po_token_script=None,
        )
        assert cli._resolve_po_token(args) == "cmd-generated-token"


def test_cli_rejects_multiple_po_token_sources(capsys):
    from pyt import cli

    args = mock.MagicMock(
        po_token="a",
        po_token_cmd="b",
        po_token_script=None,
    )
    with pytest.raises(SystemExit) as exit_info:
        cli._resolve_po_token(args)
    assert exit_info.value.code == 1

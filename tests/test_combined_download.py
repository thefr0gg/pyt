"""Tests for the multi-format SABR orchestrator (pyt.api.combined)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from pyt.api import CombinedDownload, NoMatchingStream
from pyt.api._merge import (
    FFmpegMergeError,
    FFmpegNotFound,
    pick_merge_container,
    pick_merge_container_for_streams,
)
from pyt.api.video import Video, _hydrate_meta

# ── pick_merge_container ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "video_codec,audio_codec,expected",
    [
        ("avc1.640028", "mp4a.40.2", "mp4"),
        ("av01.0.08M.08", "mp4a.40.2", "mp4"),
        ("vp9", "opus", "webm"),
        ("vp09.00.50.08", "opus", "webm"),
        ("avc1.640028", "opus", "mkv"),  # cross-family
        ("vp9", "mp4a.40.2", "mkv"),  # cross-family
        (None, "mp4a.40.2", "mkv"),  # missing codec info
        ("avc1.640028", None, "mkv"),
    ],
)
def test_pick_merge_container(video_codec, audio_codec, expected):
    assert pick_merge_container(video_codec, audio_codec) == expected


def test_pick_merge_container_for_streams():
    v = mock.MagicMock(video_codec="avc1.640028")
    a = mock.MagicMock(audio_codec="mp4a.40.2")
    assert pick_merge_container_for_streams(v, a) == "mp4"


# ── run_ffmpeg_merge: -fflags + post-merge duration validation ─────────────


def test_run_ffmpeg_merge_passes_discardcorrupt_flag(tmp_path):
    """Without -fflags +discardcorrupt the muxer halts on the first bad
    packet from libdav1d/libaom. Verify the flag is on the command line."""
    from pyt.api._merge import run_ffmpeg_merge

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return mock.Mock(returncode=0, stderr=b"")

    video_in = tmp_path / "v.mp4"
    audio_in = tmp_path / "a.m4a"
    output = tmp_path / "out.mp4"
    for p in (video_in, audio_in):
        p.write_bytes(b"x")

    with mock.patch(
        "pyt.api._merge.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch("pyt.api._merge.subprocess.run", side_effect=fake_run), mock.patch(
        "pyt.api._merge.probe_duration", return_value=None
    ):
        # probe_duration returns None for both inputs and output → validation
        # is a no-op, so we can test just the argv shape here.
        run_ffmpeg_merge(video_in, audio_in, output)

    cmd = captured["cmd"]
    assert "-fflags" in cmd
    assert cmd[cmd.index("-fflags") + 1] == "+discardcorrupt"
    assert "-err_detect" in cmd
    assert cmd[cmd.index("-err_detect") + 1] == "ignore_err"


def test_run_ffmpeg_merge_validates_output_duration(tmp_path):
    """ffmpeg returning 0 doesn't mean the output is good. If the output
    is much shorter than the inputs, fail loudly."""
    from pyt.api._merge import run_ffmpeg_merge

    video_in = tmp_path / "v.mp4"
    audio_in = tmp_path / "a.m4a"
    output = tmp_path / "out.mp4"
    for p in (video_in, audio_in):
        p.write_bytes(b"x")

    # Simulate the user's report: 4.5s output from 127s inputs.
    duration_for = {
        str(video_in): 127.4,
        str(audio_in): 127.5,
        str(output): 4.5,
    }

    def fake_run(cmd, **kwargs):
        # Pretend ffmpeg "succeeded" — the real bug is silent truncation.
        Path(cmd[-1]).write_bytes(b"truncated")
        return mock.Mock(returncode=0, stderr=b"")

    def fake_probe(path):
        return duration_for.get(str(path))

    with mock.patch(
        "pyt.api._merge.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch("pyt.api._merge.subprocess.run", side_effect=fake_run), mock.patch(
        "pyt.api._merge.probe_duration", side_effect=fake_probe
    ):
        with pytest.raises(FFmpegMergeError) as exc_info:
            run_ffmpeg_merge(video_in, audio_in, output)

    msg = str(exc_info.value)
    assert "4.5" in msg or "4.5s" in msg
    assert "127" in msg
    # Output is removed so the user doesn't accidentally use the broken file.
    assert not output.exists()


def test_run_ffmpeg_merge_accepts_normal_output(tmp_path):
    """Output within tolerance of inputs is fine — don't false-positive."""
    from pyt.api._merge import run_ffmpeg_merge

    video_in = tmp_path / "v.mp4"
    audio_in = tmp_path / "a.m4a"
    output = tmp_path / "out.mp4"
    for p in (video_in, audio_in):
        p.write_bytes(b"x")

    # ffmpeg's -shortest can trim a few hundred ms; this is well within tolerance.
    duration_for = {
        str(video_in): 127.4,
        str(audio_in): 127.0,
        str(output): 127.0,
    }

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"good")
        return mock.Mock(returncode=0, stderr=b"")

    def fake_probe(path):
        return duration_for.get(str(path))

    with mock.patch(
        "pyt.api._merge.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch("pyt.api._merge.subprocess.run", side_effect=fake_run), mock.patch(
        "pyt.api._merge.probe_duration", side_effect=fake_probe
    ):
        result = run_ffmpeg_merge(video_in, audio_in, output)

    assert result == output
    assert output.exists()


def test_run_ffmpeg_merge_skips_validation_without_ffprobe(tmp_path):
    """No ffprobe = we can't validate. Don't fail merges over that."""
    from pyt.api._merge import run_ffmpeg_merge

    video_in = tmp_path / "v.mp4"
    audio_in = tmp_path / "a.m4a"
    output = tmp_path / "out.mp4"
    for p in (video_in, audio_in):
        p.write_bytes(b"x")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"y")
        return mock.Mock(returncode=0, stderr=b"")

    with mock.patch(
        "pyt.api._merge.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch("pyt.api._merge.subprocess.run", side_effect=fake_run), mock.patch(
        "pyt.api._merge.probe_duration", return_value=None
    ):
        result = run_ffmpeg_merge(video_in, audio_in, output)

    assert result == output


def test_probe_duration_parses_format_duration(tmp_path):
    from pyt.api._merge import probe_duration

    with mock.patch(
        "pyt.api._merge.shutil.which", return_value="/usr/bin/ffprobe"
    ), mock.patch(
        "pyt.api._merge.subprocess.run",
        return_value=mock.Mock(stdout=b"127.456\n"),
    ):
        assert probe_duration(tmp_path / "v.mp4") == pytest.approx(127.456)


def test_probe_duration_returns_none_when_ffprobe_missing(tmp_path):
    from pyt.api._merge import probe_duration

    with mock.patch("pyt.api._merge.shutil.which", return_value=None):
        assert probe_duration(tmp_path / "v.mp4") is None


# ── StreamSet.best_pair ────────────────────────────────────────────────────


def _make_video(cipher_signature):
    from pyt.api._streams_hydrator import _RawStream
    from pyt.api.streams import StreamSet
    from pyt.api._sabr_config import _SabrConfig

    pr = cipher_signature._vid_info
    meta = _hydrate_meta(
        pr, url=f"https://youtube.com/watch?v={cipher_signature.video_id}"
    )
    video = Video(
        player_response=pr, meta=meta, client_name="ANDROID_VR", client_cfg={}
    )
    raw = [
        _RawStream(
            itag=18,
            url="https://ex.com/18",
            mime_type="video/mp4",
            kind="video",
            subtype="mp4",
            video_codec="avc1",
            audio_codec="mp4a",
            is_adaptive=False,
            is_progressive=True,
            is_otf=False,
            bitrate=500000,
            filesize=5000000,
            filesize_approx=5000000,
            resolution="360p",
            fps=30,
            abr=None,
            default_filename="v.mp4",
        ),
        _RawStream(
            itag=136,
            url="https://ex.com/136",
            mime_type="video/mp4",
            kind="video",
            subtype="mp4",
            video_codec="avc1",
            audio_codec=None,
            is_adaptive=True,
            is_progressive=False,
            is_otf=False,
            bitrate=1500000,
            filesize=15000000,
            filesize_approx=15000000,
            resolution="720p",
            fps=30,
            abr=None,
            default_filename="v.mp4",
        ),
        _RawStream(
            itag=140,
            url="https://ex.com/140",
            mime_type="audio/mp4",
            kind="audio",
            subtype="mp4",
            video_codec=None,
            audio_codec="mp4a",
            is_adaptive=True,
            is_progressive=False,
            is_otf=False,
            bitrate=128000,
            filesize=3000000,
            filesize_approx=3000000,
            resolution=None,
            fps=None,
            abr="128kbps",
            default_filename="a.mp4",
        ),
    ]
    video._streams = StreamSet._from_raw(raw, video=video)
    video._sabr_config = _SabrConfig()
    return video


def test_best_pair_returns_adaptive_video_and_audio(cipher_signature):
    video = _make_video(cipher_signature)
    v_stream, a_stream = video.streams.best_pair()
    assert v_stream.kind == "video"
    assert v_stream.is_adaptive
    assert a_stream.kind == "audio"


def test_best_pair_caps_at_prefer_resolution(cipher_signature):
    video = _make_video(cipher_signature)
    v_stream, _ = video.streams.best_pair(prefer_resolution="720p")
    if v_stream.resolution:
        assert int(v_stream.resolution.rstrip("p")) <= 720


def test_best_pair_falls_back_when_resolution_too_high(cipher_signature):
    """Asking for a resolution higher than what's available should return
    the highest available rather than raising."""
    video = _make_video(cipher_signature)
    v_stream, _ = video.streams.best_pair(prefer_resolution="9999p")
    assert v_stream is not None  # didn't raise


def test_best_pair_raises_when_no_adaptive_video(cipher_signature):
    """Filter the streams down to audio-only, then ask for a pair."""
    video = _make_video(cipher_signature)
    audio_only = video.streams.audio
    with pytest.raises(NoMatchingStream, match="adaptive video"):
        audio_only.best_pair()


# ── Video.download_best ────────────────────────────────────────────────────


def test_download_best_returns_combined_download(cipher_signature):
    video = _make_video(cipher_signature)
    plan = video.download_best("/tmp")
    assert isinstance(plan, CombinedDownload)


def test_combined_download_validates_video_kind(cipher_signature):
    video = _make_video(cipher_signature)
    audio = video.streams.audio.best()
    other_audio = audio  # both wrong-kind to satisfy the audio param too
    with pytest.raises(ValueError, match="kind='video'"):
        CombinedDownload(
            video=video,
            video_stream=audio,  # wrong kind
            audio_stream=other_audio,
        )


def test_combined_download_validates_audio_kind(cipher_signature):
    video = _make_video(cipher_signature)
    v_stream = video.streams.video.adaptive.first()
    if v_stream is None:
        pytest.skip("fixture has no adaptive video stream")
    with pytest.raises(ValueError, match="kind='audio'"):
        CombinedDownload(
            video=video,
            video_stream=v_stream,
            audio_stream=v_stream,  # wrong kind
        )


def test_combined_download_rejects_progressive(cipher_signature):
    video = _make_video(cipher_signature)
    progressive = video.streams.progressive.first()
    if progressive is None:
        pytest.skip("fixture has no progressive stream")
    audio = video.streams.audio.first()
    with pytest.raises(ValueError, match="progressive"):
        CombinedDownload(
            video=video,
            video_stream=progressive,  # progressive video, not allowed
            audio_stream=audio,
        )


# ── End-to-end: run() with mocked SABR + ffmpeg ─────────────────────────────


def _build_combined(cipher_signature, target_dir, *, video_size=64, audio_size=32):
    """Build a CombinedDownload with specific filesizes, pinned to a tmp dir."""
    from pyt.api._streams_hydrator import _RawStream
    from pyt.api.streams import StreamSet
    from pyt.api._sabr_config import _SabrConfig

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

    v_stream = video._streams[0]
    a_stream = video._streams[1]

    plan = CombinedDownload(
        video=video,
        video_stream=v_stream,
        audio_stream=a_stream,
        output_path=str(target_dir),
        filename="out.mkv",
        container="mkv",
    )
    return video, v_stream, a_stream, plan


def test_run_drives_sabr_and_invokes_ffmpeg(cipher_signature, tmp_path):
    video, v_stream, a_stream, plan = _build_combined(
        cipher_signature, tmp_path, video_size=10, audio_size=6
    )
    # Force a SABR URL so we actually exercise the multiplex path.
    video._sabr_config.sabr_url = "https://sabr.example/play"
    video._sabr_config.duration = 10
    video._sabr_config.po_token = None

    fake_session = mock.MagicMock()
    fake_session.iter_chunks.return_value = iter(
        [
            (v_stream.itag, b"V" * 10),
            (a_stream.itag, b"A" * 6),
        ]
    )

    merge_called = {}

    def fake_merge(video_path, audio_path, output_path, *, container):
        merge_called["video"] = Path(video_path).read_bytes()
        merge_called["audio"] = Path(audio_path).read_bytes()
        merge_called["output"] = Path(output_path)
        merge_called["container"] = container
        Path(output_path).write_bytes(b"merged")
        return Path(output_path)

    with mock.patch(
        "pyt.api.combined.SabrSession", return_value=fake_session
    ) as session_cls, mock.patch(
        "pyt.api.combined.run_ffmpeg_merge", side_effect=fake_merge
    ):
        result = plan.run()

    # SabrSession was constructed with both formats.
    formats_kwarg = session_cls.call_args.kwargs["formats"]
    assert (v_stream.itag, True) in formats_kwarg
    assert (a_stream.itag, False) in formats_kwarg

    # Bytes ended up in their respective .part inputs to ffmpeg.
    assert merge_called["video"] == b"V" * 10
    assert merge_called["audio"] == b"A" * 6
    assert merge_called["container"] == "mkv"
    assert result == tmp_path / "out.mkv"
    assert result.exists()

    # Source .part files cleaned up on success.
    assert not list(tmp_path.glob("*.part"))


def test_run_falls_back_to_range_when_sabr_short(cipher_signature, tmp_path):
    video, v_stream, a_stream, plan = _build_combined(
        cipher_signature, tmp_path, video_size=10, audio_size=6
    )
    video._sabr_config.sabr_url = "https://sabr.example/play"
    video._sabr_config.duration = 10

    # SABR delivers only 4 video bytes and nothing for audio, then ends.
    fake_session = mock.MagicMock()
    fake_session.iter_chunks.return_value = iter(
        [
            (v_stream.itag, b"V" * 4),
        ]
    )

    # Range-stream returns the missing tails.
    def fake_stream(url, *, timeout, max_retries, start_byte, extra_headers):
        if url == v_stream.url:
            assert start_byte == 4
            yield b"V" * 6
        elif url == a_stream.url:
            assert start_byte == 0
            yield b"A" * 6
        else:
            raise AssertionError(f"unexpected url {url!r}")

    captured = {}

    def fake_merge(video_path, audio_path, output_path, *, container):
        captured["video"] = Path(video_path).read_bytes()
        captured["audio"] = Path(audio_path).read_bytes()
        Path(output_path).write_bytes(b"x")
        return Path(output_path)

    with mock.patch(
        "pyt.api.combined.SabrSession", return_value=fake_session
    ), mock.patch(
        "pyt.api.combined.request.stream", side_effect=fake_stream
    ), mock.patch(
        "pyt.api.combined.run_ffmpeg_merge", side_effect=fake_merge
    ):
        plan.run()

    assert captured["video"] == b"V" * 10
    assert captured["audio"] == b"A" * 6


def test_run_uses_range_only_when_no_sabr_url(cipher_signature, tmp_path):
    """No serverAbrStreamingUrl means we skip SABR entirely and finish
    everything via the byte-range path."""
    video, v_stream, a_stream, plan = _build_combined(
        cipher_signature, tmp_path, video_size=10, audio_size=6
    )
    video._sabr_config.sabr_url = None  # no SABR

    def fake_stream(url, *, timeout, max_retries, start_byte, extra_headers):
        assert start_byte == 0
        if url == v_stream.url:
            yield b"V" * 10
        elif url == a_stream.url:
            yield b"A" * 6
        else:
            raise AssertionError(f"unexpected url {url!r}")

    captured = {}

    def fake_merge(video_path, audio_path, output_path, *, container):
        captured["video"] = Path(video_path).read_bytes()
        captured["audio"] = Path(audio_path).read_bytes()
        Path(output_path).write_bytes(b"x")
        return Path(output_path)

    with mock.patch("pyt.api.combined.SabrSession") as session_cls, mock.patch(
        "pyt.api.combined.request.stream", side_effect=fake_stream
    ), mock.patch("pyt.api.combined.run_ffmpeg_merge", side_effect=fake_merge):
        plan.run()

    # SabrSession was never constructed.
    session_cls.assert_not_called()
    assert captured["video"] == b"V" * 10
    assert captured["audio"] == b"A" * 6


def test_run_then_chains_pipeline_steps(cipher_signature, tmp_path):
    video, v_stream, a_stream, plan = _build_combined(
        cipher_signature, tmp_path, video_size=2, audio_size=2
    )
    video._sabr_config.sabr_url = "https://sabr.example/play"

    fake_session = mock.MagicMock()
    fake_session.iter_chunks.return_value = iter(
        [
            (v_stream.itag, b"VV"),
            (a_stream.itag, b"AA"),
        ]
    )

    from pyt.api.pipeline import PipelineStep

    seen = []

    class _Recording(PipelineStep):
        def apply(self, path, *, stream, video):
            seen.append((self.name, str(path)))
            return path + f".{self.name}"

    chained = plan.then(_Recording(name="one"), _Recording(name="two"))
    chained = chained | _Recording(name="three")

    def fake_merge(video_path, audio_path, output_path, *, container):
        Path(output_path).write_bytes(b"x")
        return Path(output_path)

    with mock.patch(
        "pyt.api.combined.SabrSession", return_value=fake_session
    ), mock.patch("pyt.api.combined.run_ffmpeg_merge", side_effect=fake_merge):
        result = chained.run()

    assert [name for name, _ in seen] == ["one", "two", "three"]
    assert result == Path(str(tmp_path / "out.mkv") + ".one.two.three")


def test_run_ffmpeg_failure_raises_download_error(cipher_signature, tmp_path):
    from pyt.api.errors import DownloadError

    video, v_stream, a_stream, plan = _build_combined(
        cipher_signature, tmp_path, video_size=2, audio_size=2
    )
    video._sabr_config.sabr_url = "https://sabr.example/play"

    fake_session = mock.MagicMock()
    fake_session.iter_chunks.return_value = iter(
        [
            (v_stream.itag, b"VV"),
            (a_stream.itag, b"AA"),
        ]
    )

    with mock.patch(
        "pyt.api.combined.SabrSession", return_value=fake_session
    ), mock.patch(
        "pyt.api.combined.run_ffmpeg_merge",
        side_effect=FFmpegMergeError(1, "boom"),
    ):
        with pytest.raises(DownloadError, match="ffmpeg merge failed"):
            plan.run()

    # On merge failure the .part files are left on disk for the user.
    parts = list(tmp_path.glob("*.part"))
    assert len(parts) == 2


def test_run_ffmpeg_missing_raises_download_error(cipher_signature, tmp_path):
    from pyt.api.errors import DownloadError

    video, v_stream, a_stream, plan = _build_combined(
        cipher_signature, tmp_path, video_size=2, audio_size=2
    )
    video._sabr_config.sabr_url = "https://sabr.example/play"

    fake_session = mock.MagicMock()
    fake_session.iter_chunks.return_value = iter(
        [
            (v_stream.itag, b"VV"),
            (a_stream.itag, b"AA"),
        ]
    )

    with mock.patch(
        "pyt.api.combined.SabrSession", return_value=fake_session
    ), mock.patch(
        "pyt.api.combined.run_ffmpeg_merge",
        side_effect=FFmpegNotFound("not on PATH"),
    ):
        with pytest.raises(DownloadError, match="not on PATH"):
            plan.run()


def test_progress_callback_invoked_for_both_formats(cipher_signature, tmp_path):
    video, v_stream, a_stream, plan = _build_combined(
        cipher_signature, tmp_path, video_size=5, audio_size=4
    )
    video._sabr_config.sabr_url = "https://sabr.example/play"

    progress_events = []
    video._sabr_config.on_progress = (
        lambda stream, chunk, remaining: progress_events.append(
            (stream.itag, len(chunk), remaining)
        )
    )

    fake_session = mock.MagicMock()
    fake_session.iter_chunks.return_value = iter(
        [
            (v_stream.itag, b"VVVVV"),
            (a_stream.itag, b"AAAA"),
        ]
    )

    def fake_merge(video_path, audio_path, output_path, *, container):
        Path(output_path).write_bytes(b"x")
        return Path(output_path)

    with mock.patch(
        "pyt.api.combined.SabrSession", return_value=fake_session
    ), mock.patch("pyt.api.combined.run_ffmpeg_merge", side_effect=fake_merge):
        plan.run()

    itags_seen = {ev[0] for ev in progress_events}
    assert v_stream.itag in itags_seen
    assert a_stream.itag in itags_seen
    # Final remaining for each format is 0.
    last_v = [ev for ev in progress_events if ev[0] == v_stream.itag][-1]
    last_a = [ev for ev in progress_events if ev[0] == a_stream.itag][-1]
    assert last_v[2] == 0
    assert last_a[2] == 0

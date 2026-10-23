"""Tests for the modern :mod:`pyt.api` surface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from pyt.api import (
    Channel,
    Client,
    ConfigError,
    Download,
    NoMatchingStream,
    StreamRef,
    StreamSet,
    Video,
    VideoMeta,
    pipeline as pp,
)
from pyt.api.video import _hydrate_meta

# ── helpers ────────────────────────────────────────────────────────────────


def _fake_raw_streams():
    """Minimal synthetic raw streams sufficient for StreamSet tests."""
    from pyt.api._streams_hydrator import _RawStream

    return [
        # Progressive mp4 (audio+video)
        _RawStream(
            itag=18,
            url="https://example.com/18",
            mime_type="video/mp4",
            kind="video",
            subtype="mp4",
            video_codec="avc1.42001E",
            audio_codec="mp4a.40.2",
            is_adaptive=False,
            is_progressive=True,
            is_otf=False,
            bitrate=500000,
            filesize=5000000,
            filesize_approx=5000000,
            resolution="360p",
            fps=30,
            abr=None,
            default_filename="video.mp4",
        ),
        # Adaptive video mp4 720p
        _RawStream(
            itag=136,
            url="https://example.com/136",
            mime_type="video/mp4",
            kind="video",
            subtype="mp4",
            video_codec="avc1.4d401f",
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
            default_filename="video.mp4",
        ),
        # Adaptive video mp4 1080p
        _RawStream(
            itag=137,
            url="https://example.com/137",
            mime_type="video/mp4",
            kind="video",
            subtype="mp4",
            video_codec="avc1.640028",
            audio_codec=None,
            is_adaptive=True,
            is_progressive=False,
            is_otf=False,
            bitrate=3000000,
            filesize=30000000,
            filesize_approx=30000000,
            resolution="1080p",
            fps=30,
            abr=None,
            default_filename="video.mp4",
        ),
        # Adaptive audio mp4
        _RawStream(
            itag=140,
            url="https://example.com/140",
            mime_type="audio/mp4",
            kind="audio",
            subtype="mp4",
            video_codec=None,
            audio_codec="mp4a.40.2",
            is_adaptive=True,
            is_progressive=False,
            is_otf=False,
            bitrate=128000,
            filesize=3000000,
            filesize_approx=3000000,
            resolution=None,
            fps=None,
            abr="128kbps",
            default_filename="audio.mp4",
        ),
        # Adaptive audio webm (higher bitrate → best())
        _RawStream(
            itag=251,
            url="https://example.com/251",
            mime_type="audio/webm",
            kind="audio",
            subtype="webm",
            video_codec=None,
            audio_codec="opus",
            is_adaptive=True,
            is_progressive=False,
            is_otf=False,
            bitrate=160000,
            filesize=3500000,
            filesize_approx=3500000,
            resolution=None,
            fps=None,
            abr="160kbps",
            default_filename="audio.webm",
        ),
    ]


def _make_video(cipher_signature):
    """Build a v3 :class:`Video` from the legacy fixture without network calls."""
    from pyt.api.streams import StreamSet
    from pyt.api._sabr_config import _SabrConfig

    pr = cipher_signature._vid_info
    url = f"https://youtube.com/watch?v={cipher_signature.video_id}"
    meta = _hydrate_meta(pr, url=url)
    video = Video(
        player_response=pr, meta=meta, client_name="ANDROID_VR", client_cfg={}
    )
    video._streams = StreamSet._from_raw(_fake_raw_streams(), video=video)
    video._sabr_config = _SabrConfig()
    return video


def _make_player_response(
    *,
    is_live=False,
    is_live_content=False,
    is_upcoming=False,
    hls_url=None,
    scheduled_ts=None,
):
    """Return a minimal player-response dict for live-stream tests."""
    streaming_data = {"hlsManifestUrl": hls_url} if hls_url else {}
    playability = (
        {"offlineSlate": {"scheduledStartTime": str(scheduled_ts)}}
        if scheduled_ts is not None
        else {}
    )
    return {
        "videoDetails": {
            "videoId": "liveXYZ",
            "title": "Live Test",
            "channelId": "UC123",
            "author": "TestChannel",
            "lengthSeconds": "0",
            "isLive": is_live,
            "isLiveContent": is_live_content,
            "isUpcoming": is_upcoming,
        },
        "streamingData": streaming_data,
        "playabilityStatus": playability,
    }


def _make_age_restricted_video(age_restricted: bool = True):
    """Return a Video whose is_age_restricted flag is pre-set."""
    from pyt.api.models import VideoMeta, Channel

    meta = VideoMeta(
        video_id="agexYZ",
        url="https://youtube.com/watch?v=agexYZ",
        title="Age Restricted Test",
        author=Channel(id="UC1", name="Test"),
        length=timedelta(minutes=10),
        is_age_restricted=age_restricted,
    )
    fake_pr = {
        "videoDetails": {
            "videoId": "agexYZ",
            "title": "Age Restricted Test",
            "lengthSeconds": "600",
            "channelId": "UC1",
            "author": "Test",
        }
    }
    return Video(
        player_response=fake_pr, meta=meta, client_name="ANDROID_VR", client_cfg={}
    )


# ── Client ─────────────────────────────────────────────────────────────────


def test_client_rejects_both_cookie_sources():
    with pytest.raises(ConfigError, match="not both"):
        Client(cookies="x.txt", cookies_from_browser="firefox")


def test_client_rejects_unknown_browser():
    with pytest.raises(ConfigError, match="unknown browser"):
        Client(cookies_from_browser="netscape-navigator")


def test_client_proxies_dict_format():
    c = Client(proxy="socks5://127.0.0.1:9050")
    assert c._proxies_dict() == {
        "http": "socks5://127.0.0.1:9050",
        "https": "socks5://127.0.0.1:9050",
    }


def test_client_no_proxy_returns_none():
    assert Client()._proxies_dict() is None


def test_client_video_calls_from_url():
    with mock.patch("pyt.api.client.Video._from_url") as factory:
        factory.return_value = mock.sentinel.video
        result = Client(po_token="tok").video("https://youtu.be/abc")
    assert result is mock.sentinel.video
    factory.assert_called_once()
    kwargs = factory.call_args.kwargs
    assert kwargs["po_token"] == "tok"
    assert kwargs["use_oauth"] is False


# ── _hydrate_meta + Video ──────────────────────────────────────────────────


def test_hydrate_meta_from_cipher_fixture(cipher_signature):
    url = "https://youtube.com/watch?v=2lAe1cqCOXo"
    player_response = cipher_signature._vid_info
    meta = _hydrate_meta(player_response, url=url)
    assert isinstance(meta, VideoMeta)
    assert meta.video_id == cipher_signature.video_id
    assert meta.url == url
    assert isinstance(meta.title, str) and meta.title
    assert isinstance(meta.author, Channel)
    assert isinstance(meta.length, timedelta) and meta.length.total_seconds() > 0
    assert meta.author.name


def test_video_exposes_typed_props(cipher_signature):
    pr = cipher_signature._vid_info
    meta = _hydrate_meta(pr, url="https://youtube.com/watch?v=2lAe1cqCOXo")
    video = Video(
        player_response=pr, meta=meta, client_name="ANDROID_VR", client_cfg={}
    )
    assert video.video_id == cipher_signature.video_id
    assert video.title == meta.title
    assert isinstance(video.length, timedelta)


# ── StreamSet ──────────────────────────────────────────────────────────────


def test_streamset_audio_filter(cipher_signature):
    video = _make_video(cipher_signature)
    audio = video.streams.audio
    assert isinstance(audio, StreamSet)
    assert len(audio) > 0
    assert all(s.is_audio_only for s in audio)


def test_streamset_video_filter(cipher_signature):
    video = _make_video(cipher_signature)
    assert all(s.kind == "video" for s in video.streams.video)


def test_streamset_progressive_disjoint_from_adaptive(cipher_signature):
    video = _make_video(cipher_signature)
    prog = set(s.itag for s in video.streams.progressive)
    adap = set(s.itag for s in video.streams.adaptive)
    assert prog & adap == set()


def test_streamset_filter_kwargs(cipher_signature):
    video = _make_video(cipher_signature)
    mp4 = video.streams.filter(subtype="mp4")
    assert all(s.subtype == "mp4" for s in mp4)


def test_streamset_best_returns_streamref(cipher_signature):
    video = _make_video(cipher_signature)
    best = video.streams.audio.best()
    assert isinstance(best, StreamRef)
    assert best.is_audio_only


def test_streamset_best_raises_when_empty(cipher_signature):
    video = _make_video(cipher_signature)
    empty = video.streams.filter(subtype="not-a-real-format")
    with pytest.raises(NoMatchingStream):
        empty.best()


def test_streamset_one_raises_on_multiple(cipher_signature):
    video = _make_video(cipher_signature)
    audio = video.streams.audio
    if len(audio) > 1:
        with pytest.raises(NoMatchingStream):
            audio.one()


def test_streamset_one_raises_on_zero(cipher_signature):
    video = _make_video(cipher_signature)
    with pytest.raises(NoMatchingStream):
        video.streams.filter(subtype="ogg-no-such").one()


def test_streamset_first_returns_optional(cipher_signature):
    video = _make_video(cipher_signature)
    assert video.streams.first() is not None
    assert video.streams.filter(subtype="bogus").first() is None


def test_streamset_filter_chain_is_immutable(cipher_signature):
    """Filtering returns a new set; the original is unchanged."""
    video = _make_video(cipher_signature)
    all_streams = video.streams
    audio = all_streams.audio
    assert len(all_streams) >= len(audio)


def test_streamset_order_by_resolution_ascending(cipher_signature):
    video = _make_video(cipher_signature)
    ordered = video.streams.video.order_by("resolution")
    seen = [s.resolution for s in ordered if s.resolution]
    parsed = [int(r.rstrip("p")) for r in seen]
    assert parsed == sorted(parsed)


def test_streamset_at_least_resolution(cipher_signature):
    video = _make_video(cipher_signature)
    hd = video.streams.video.at_least("720p")
    for s in hd:
        if s.resolution:
            assert int(s.resolution.rstrip("p")) >= 720


# ── Download builder ───────────────────────────────────────────────────────


def test_download_run_transfers_bytes(cipher_signature):
    video = _make_video(cipher_signature)
    stream = video.streams.audio.best()
    fake_path = "/tmp/fake.mp4"
    with mock.patch(
        "pyt.api.download.Download._transfer_bytes", return_value=fake_path
    ):
        result = stream.download_to("/tmp", filename="x").run()
    assert isinstance(result, Path)
    assert result == Path(fake_path)


def test_download_then_appends_steps(cipher_signature):
    video = _make_video(cipher_signature)
    stream = video.streams.audio.best()
    step = pp.embed_metadata()
    chained = stream.download_to() | step
    assert isinstance(chained, Download)
    assert chained._steps == [step]


def test_download_pipeline_runs_steps_in_order(cipher_signature):
    video = _make_video(cipher_signature)
    stream = video.streams.audio.best()

    calls = []

    class _Recording(pp.PipelineStep):
        def apply(self, path, *, stream, video):
            calls.append((self.name, path))
            return path + f".{self.name}"

    one = _Recording(name="one")
    two = _Recording(name="two")

    with mock.patch(
        "pyt.api.download.Download._transfer_bytes", return_value="/tmp/raw.mp4"
    ):
        result = (stream.download_to() | one | two).run()
    assert calls == [("one", "/tmp/raw.mp4"), ("two", "/tmp/raw.mp4.one")]
    assert result == Path("/tmp/raw.mp4.one.two")


def test_download_step_failure_wrapped_as_postprocess_error(cipher_signature):
    from pyt.api.errors import PostProcessError

    video = _make_video(cipher_signature)
    stream = video.streams.audio.best()

    class _Boom(pp.PipelineStep):
        def apply(self, path, *, stream, video):
            raise RuntimeError("ffmpeg said no")

    with mock.patch(
        "pyt.api.download.Download._transfer_bytes", return_value="/tmp/raw.mp4"
    ):
        with pytest.raises(PostProcessError) as exc_info:
            (stream.download_to() | _Boom(name="boom")).run()
    assert exc_info.value.step == "boom"
    assert exc_info.value.partial_output_path == "/tmp/raw.mp4"


# ── Pipeline factories ────────────────────────────────────────────────────


def test_sponsorblock_requires_one_of_mark_or_remove():
    with pytest.raises(ValueError, match="one of mark"):
        pp.sponsorblock()


def test_sponsorblock_rejects_both():
    with pytest.raises(ValueError, match="not both"):
        pp.sponsorblock(mark=["sponsor"], remove=["intro"])


def test_pipeline_steps_are_named():
    assert pp.embed_metadata().name == "embed_metadata"
    assert pp.embed_thumbnail().name == "embed_thumbnail"
    assert pp.extract_audio().name == "extract_audio"
    assert pp.embed_subtitles().name == "embed_subtitles"
    assert pp.sponsorblock(mark=["sponsor"]).name == "sponsorblock"


# ── Deprecation warnings ───────────────────────────────────────────────────


def test_legacy_youtube_emits_deprecation_warning():
    from pyt.legacy import YouTube

    with mock.patch("pyt.extract.video_id", return_value="abc"):
        with pytest.warns(DeprecationWarning, match="pyt.Client"):
            YouTube("https://youtu.be/abc")


def test_legacy_playlist_emits_deprecation_warning():
    from pyt.legacy import Playlist

    with pytest.warns(DeprecationWarning, match="pyt.Client"):
        Playlist("https://youtube.com/playlist?list=PLxxx")


def test_legacy_channel_emits_deprecation_warning():
    from pyt.legacy import Channel as LegacyChannel

    with mock.patch("pyt.contrib.playlist.extract.playlist_id", return_value="xxx"):
        with pytest.warns(DeprecationWarning, match="pyt.Client"):
            LegacyChannel("https://www.youtube.com/@somechannel")


def test_legacy_search_emits_deprecation_warning():
    from pyt.legacy import Search

    with pytest.warns(DeprecationWarning, match="pyt.Client"):
        Search("test")


def test_register_on_progress_callback_emits_deprecation_warning(cipher_signature):
    with pytest.warns(DeprecationWarning, match="register_on_progress"):
        cipher_signature.register_on_progress_callback(lambda *a, **k: None)


def test_register_on_complete_callback_emits_deprecation_warning(cipher_signature):
    with pytest.warns(DeprecationWarning, match="register_on_complete"):
        cipher_signature.register_on_complete_callback(lambda *a, **k: None)


def test_client_video_does_not_emit_deprecation_warning():
    """The modern API path must not surface a DeprecationWarning."""
    import warnings as warnings_mod
    from pyt import Client

    fake_pr = {
        "videoDetails": {
            "videoId": "dQw4w9WgXcQ",
            "title": "x",
            "lengthSeconds": "1",
            "channelId": "UC1",
            "author": "Test",
        }
    }

    with mock.patch("pyt.api._player.fetch_player_response") as mock_fetch, mock.patch(
        "pyt.extract.video_id", return_value="dQw4w9WgXcQ"
    ):
        mock_fetch.return_value = (fake_pr, "ANDROID_VR", {}, False)
        with warnings_mod.catch_warnings():
            warnings_mod.simplefilter("error", DeprecationWarning)
            Client().video("https://youtu.be/dQw4w9WgXcQ")


# ── pyt.legacy facade ─────────────────────────────────────────────────────


def test_legacy_module_re_exports_classes():
    from pyt import legacy

    assert legacy.YouTube is __import__("pyt.__main__", fromlist=["YouTube"]).YouTube
    assert hasattr(legacy, "Stream")
    assert hasattr(legacy, "Playlist")
    assert hasattr(legacy, "Channel")
    assert hasattr(legacy, "Search")


# ── Modern Playlist / SearchResults shape ──────────────────────────────────


def test_client_playlist_returns_modern_wrapper():
    from pyt.api.playlists import Playlist as ModernPlaylist
    from pyt import Client

    fake_page = {"metadata": {"playlistMetadataRenderer": {"title": "Hello"}}}

    with mock.patch("pyt.innertube.InnerTube.browse", return_value=fake_page):
        result = Client().playlist("https://youtube.com/playlist?list=PLxxx")

        assert isinstance(result, ModernPlaylist)
        assert result.playlist_id == "PLxxx"
        assert result.title == "Hello"


def test_modern_playlist_iter_yields_videos():
    from pyt.api.playlists import Playlist as ModernPlaylist

    fake_client = mock.MagicMock()
    fake_client.video.side_effect = lambda u: f"video<{u}>"

    fake_videos = [
        {"video_id": "url1", "title": "A", "duration_text": None},
        {"video_id": "url2", "title": "B", "duration_text": None},
    ]

    pl = ModernPlaylist(playlist_id="PLxxx", client=fake_client)
    with mock.patch(
        "pyt.api.playlists.iter_playlist_videos", return_value=iter(fake_videos)
    ):
        assert list(pl.videos()) == [
            "video<https://www.youtube.com/watch?v=url1>",
            "video<https://www.youtube.com/watch?v=url2>",
        ]
    assert fake_client.video.call_count == 2


def test_client_channel_returns_channel_feed():
    from pyt.api.playlists import ChannelFeed
    from pyt import Client

    fake_home = {"metadata": {"channelMetadataRenderer": {"title": "Some Channel"}}}

    with mock.patch(
        "pyt.api.playlists.resolve_channel_id", return_value="UCxxx"
    ), mock.patch("pyt.innertube.InnerTube.browse", return_value=fake_home):
        result = Client().channel("https://youtube.com/@someone")

        assert isinstance(result, ChannelFeed)
        assert result.channel_id == "UCxxx"
        assert result.name == "Some Channel"


def test_client_search_returns_modern_wrapper():
    from pyt.api.playlists import SearchResults
    from pyt import Client

    fake_legacy = mock.MagicMock()
    fake_legacy.query = "rickroll"

    with mock.patch("pyt.contrib.search.Search", return_value=fake_legacy):
        result = Client().search("rickroll")

    assert isinstance(result, SearchResults)
    assert result.query == "rickroll"


# ── Live stream / VOD / upcoming ───────────────────────────────────────────


def test_hydrate_meta_active_live_stream():
    from pyt.api.video import _hydrate_meta

    pr = _make_player_response(
        is_live=True,
        is_live_content=True,
        hls_url="https://manifest.example.com/live.m3u8",
    )
    meta = _hydrate_meta(pr, url="https://youtube.com/watch?v=liveXYZ")

    assert meta.is_live is True
    assert meta.is_live_content is True
    assert meta.hls_manifest_url == "https://manifest.example.com/live.m3u8"


def test_hydrate_meta_vod_not_marked_as_live():
    """A finished live broadcast (isLiveContent=True, isLive=False) must
    NOT set is_live — it's a downloadable VOD."""
    from pyt.api.video import _hydrate_meta

    pr = _make_player_response(is_live=False, is_live_content=True)
    meta = _hydrate_meta(pr, url="https://youtube.com/watch?v=liveXYZ")

    assert meta.is_live is False
    assert meta.is_live_content is True
    assert meta.hls_manifest_url is None


def test_hydrate_meta_upcoming_raises_livestreamupcoming():
    from pyt.api.errors import LiveStreamUpcoming
    from pyt.api.video import _hydrate_meta

    scheduled_ts = 1704124800  # 2024-01-01 18:00:00 UTC
    pr = _make_player_response(is_upcoming=True, scheduled_ts=scheduled_ts)

    with pytest.raises(LiveStreamUpcoming) as exc_info:
        _hydrate_meta(pr, url="https://youtube.com/watch?v=liveXYZ")

    err = exc_info.value
    assert err.video_id == "liveXYZ"
    assert err.scheduled_start is not None
    assert err.scheduled_start == datetime.fromtimestamp(scheduled_ts, tz=timezone.utc)


def test_hydrate_meta_upcoming_no_start_time():
    """Upcoming with no offlineSlate still raises LiveStreamUpcoming
    without a start time rather than crashing."""
    from pyt.api.errors import LiveStreamUpcoming
    from pyt.api.video import _hydrate_meta

    pr = _make_player_response(is_upcoming=True)
    with pytest.raises(LiveStreamUpcoming) as exc_info:
        _hydrate_meta(pr, url="https://youtube.com/watch?v=liveXYZ")
    assert exc_info.value.scheduled_start is None


def test_video_is_live_content_property():
    from pyt.api.video import _hydrate_meta

    pr = _make_player_response(is_live=False, is_live_content=True)
    meta = _hydrate_meta(pr, url="https://youtube.com/watch?v=liveXYZ")
    video = Video(
        player_response=pr, meta=meta, client_name="ANDROID_VR", client_cfg={}
    )
    assert video.is_live_content is True
    assert video.is_live is False
    assert video.hls_manifest_url is None


def test_record_live_blocks_on_non_live(cipher_signature):
    from pyt.api.errors import LiveStreamNotSupported

    video = _make_video(cipher_signature)
    assert not video.is_live
    with pytest.raises(LiveStreamNotSupported):
        video.record_live()


def test_record_live_calls_ffmpeg(tmp_path):
    from pyt.api.video import _hydrate_meta

    hls_url = "https://manifest.example.com/live.m3u8"
    pr = _make_player_response(is_live=True, is_live_content=True, hls_url=hls_url)
    meta = _hydrate_meta(pr, url="https://youtube.com/watch?v=liveXYZ")
    video = Video(
        player_response=pr, meta=meta, client_name="ANDROID_VR", client_cfg={}
    )

    with mock.patch(
        "pyt.api.video.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch("pyt.api.video.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0)
        result = video.record_live(output_path=str(tmp_path))

    assert result.suffix == ".ts"
    assert result.parent == tmp_path
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "/usr/bin/ffmpeg"
    assert hls_url in call_args
    assert "-c" in call_args and "copy" in call_args


def test_record_live_passes_timeout(tmp_path):
    from pyt.api.video import _hydrate_meta

    hls_url = "https://manifest.example.com/live.m3u8"
    pr = _make_player_response(is_live=True, hls_url=hls_url)
    meta = _hydrate_meta(pr, url="https://youtube.com/watch?v=liveXYZ")
    video = Video(
        player_response=pr, meta=meta, client_name="ANDROID_VR", client_cfg={}
    )

    with mock.patch(
        "pyt.api.video.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch("pyt.api.video.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0)
        video.record_live(output_path=str(tmp_path), timeout=60)

    cmd = mock_run.call_args[0][0]
    assert "-t" in cmd
    assert "60" in cmd


def test_record_live_raises_when_ffmpeg_missing(tmp_path):
    from pyt.api.video import _hydrate_meta

    pr = _make_player_response(is_live=True, hls_url="https://x.example.com/live.m3u8")
    meta = _hydrate_meta(pr, url="https://youtube.com/watch?v=liveXYZ")
    video = Video(
        player_response=pr, meta=meta, client_name="ANDROID_VR", client_cfg={}
    )

    with mock.patch("pyt.api.video.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="ffmpeg not found"):
            video.record_live(output_path=str(tmp_path))


# ── Age restriction ────────────────────────────────────────────────────────


def test_age_restricted_video_has_property():
    video = _make_age_restricted_video(age_restricted=True)
    assert video.is_age_restricted is True


def test_non_age_restricted_video_has_property():
    video = _make_age_restricted_video(age_restricted=False)
    assert video.is_age_restricted is False


def test_streams_raises_age_restricted_immediately_without_auth():
    """Accessing .streams without cookies should raise AgeRestricted before
    any network call rather than failing deep in bypass_age_gate()."""
    from pyt.api.errors import AgeRestricted

    video = _make_age_restricted_video(age_restricted=True)
    # _client is None → no auth configured → must raise immediately.
    assert video._client is None
    with pytest.raises(AgeRestricted) as exc_info:
        _ = video.streams
    err = exc_info.value
    assert err.video_id == "agexYZ"
    assert "cookies_from_browser" in str(err)


def test_streams_does_not_raise_age_restricted_when_cookies_configured():
    """When the client has cookies, the guard must not short-circuit —
    the stream fetch should be allowed to proceed."""
    from pyt.api._sabr_config import _SabrConfig

    video = _make_age_restricted_video(age_restricted=True)

    fake_client = mock.MagicMock()
    fake_client._cookies_browser = "chrome"
    fake_client._cookies_path = None
    fake_client._use_oauth = False
    fake_client._current_po_token.return_value = None
    fake_client._on_progress = None
    fake_client._on_complete = None
    video._client = fake_client

    with mock.patch("pyt.api.video.hydrate_streams", return_value=([], _SabrConfig())):
        streams = video.streams
    assert streams is not None


def test_age_restricted_is_not_video_unavailable():
    """AgeRestricted must NOT be a subclass of VideoUnavailable — an age-
    gated video is available, it just needs authentication."""
    from pyt.api.errors import AgeRestricted, VideoUnavailable

    err = AgeRestricted("agexYZ")
    assert not isinstance(err, VideoUnavailable)
    assert err.video_id == "agexYZ"


def test_age_restricted_error_message_mentions_cookies():
    from pyt.api.errors import AgeRestricted

    msg = str(AgeRestricted("agexYZ"))
    assert "cookies_from_browser" in msg
    assert "cookies" in msg


def test_hydrate_meta_sets_is_age_restricted(cipher_signature):
    """For a normal (non-age-restricted) fixture, is_age_restricted must
    be False — we're checking the happy-path wiring here."""
    meta = _hydrate_meta(
        cipher_signature._vid_info,
        url="https://youtube.com/watch?v=2lAe1cqCOXo",
    )
    assert meta.is_age_restricted is False

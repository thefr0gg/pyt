import argparse
import logging
import os

from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from pyt import cli
from pyt.api import DownloadError, PytError
from pyt.api._streams_hydrator import _RawStream
from pyt.api.streams import StreamSet
from pyt.captions import Caption

parse_args = cli._parse_args


def _caption(code, name="name"):
    return Caption(
        {
            "url": f"url-{code}",
            "name": {"simpleText": name},
            "languageCode": code,
            "vssId": f".{code}",
        }
    )


def _raw(
    itag,
    kind="video",
    subtype="mp4",
    resolution=None,
    abr=None,
    progressive=False,
    video_codec=None,
    audio_codec=None,
    filesize=1000,
    bitrate=100,
):
    return _RawStream(
        itag=itag,
        url=f"https://example.com/{itag}",
        mime_type=f"{kind}/{subtype}",
        kind=kind,
        subtype=subtype,
        video_codec=video_codec,
        audio_codec=audio_codec,
        is_adaptive=not progressive,
        is_progressive=progressive,
        is_otf=False,
        bitrate=bitrate,
        filesize=filesize,
        filesize_approx=filesize,
        resolution=resolution,
        fps=30 if kind == "video" else None,
        abr=abr,
        default_filename=f"video-{itag}.{subtype}",
    )


def _video(*raws):
    video = MagicMock()
    video.video_id = "vid123"
    video.title = "A Title"
    video.streams = StreamSet._from_raw(list(raws), video=video)
    return video


def _catalog():
    return _video(
        _raw(
            18,
            progressive=True,
            resolution="360p",
            video_codec="avc1",
            audio_codec="mp4a",
        ),
        _raw(
            22,
            progressive=True,
            resolution="720p",
            video_codec="avc1",
            audio_codec="mp4a",
        ),
        _raw(137, resolution="1080p", video_codec="avc1.640028"),
        _raw(248, subtype="webm", resolution="1080p", video_codec="vp9"),
        _raw(313, subtype="webm", resolution="2160p", video_codec="vp9"),
        _raw(140, kind="audio", subtype="mp4", abr="128kbps", audio_codec="mp4a.40.2"),
        _raw(251, kind="audio", subtype="webm", abr="160kbps", audio_codec="opus"),
    )


def _argv(*extra):
    return parse_args(
        argparse.ArgumentParser(), ["http://youtube.com/watch?v=9bZkp7q19f0", *extra]
    )


@pytest.fixture
def run_main():
    """Run ``cli.main()`` with *args* and a mocked :class:`Client`."""

    def _run(args):
        with (
            mock.patch("pyt.cli._parse_args", return_value=args),
            mock.patch("pyt.cli.Client") as client_cls,
        ):
            cli.main()
        return client_cls

    return _run


# ── argument parsing ──────────────────────────────────────────────────────


@mock.patch("pyt.cli._parse_args")
def test_main_invalid_url(_parse_args):  # noqa: PT019
    _parse_args.return_value = parse_args(argparse.ArgumentParser(), ["crikey"])
    with pytest.raises(SystemExit):
        cli.main()


def test_parse_args_falsey():
    args = _argv()
    assert args.url == "http://youtube.com/watch?v=9bZkp7q19f0"
    assert args.build_playback_report is False
    assert args.itag is None
    assert args.list is False
    # `-v` is a count flag: 0 = unset, 1 = -v / DEBUG, 2 = -vv / TRACE.
    assert args.verbose == 0


def test_parse_args_truthy():
    args = _argv("--build-playback-report", "-c", "en", "-l", "--itag=10", "-vvv")
    assert args.build_playback_report is True
    assert args.itag == 10
    assert args.list is True
    # -vvv = 3 occurrences; we cap at TRACE (2+) for behavior, but the
    # raw count is preserved for any caller that wants to inspect it.
    assert args.verbose == 3


@mock.patch("pyt.cli.setup_logger", return_value=None)
def test_main_logging_setup(setup_logger):
    args = parse_args(argparse.ArgumentParser(), ["http://fakeurl", "-v"])
    with mock.patch("pyt.cli._parse_args", return_value=args):
        with pytest.raises(SystemExit):
            cli.main()
    setup_logger.assert_called_with(logging.DEBUG, log_filename=None)


# ── main() dispatch ───────────────────────────────────────────────────────


def test_main_builds_client_from_flags(run_main):
    args = _argv("--proxy", "http://p:1", "--itag=10")
    with mock.patch("pyt.cli._perform_args_on_video"):
        client_cls = run_main(args)
    kwargs = client_cls.call_args.kwargs
    assert kwargs["proxy"] == "http://p:1"
    assert kwargs["on_progress"] is cli.on_progress


@pytest.mark.parametrize(
    "flags, target",
    [
        (["--itag=10"], "download_by_itag"),
        (["-r", "320p"], "download_by_resolution"),
        (["-a", "mp4"], "download_audio"),
        (["-f", "best"], "ffmpeg_process"),
        (["-c", "en"], "download_caption"),
        (["-l"], "display_streams"),
        (["--build-playback-report"], "build_playback_report"),
    ],
)
def test_main_dispatches_flag(run_main, flags, target):
    with (
        mock.patch(f"pyt.cli.{target}") as handler,
        mock.patch("pyt.cli.shutil.which", return_value=None),
    ):
        client_cls = run_main(_argv(*flags))
    client_cls.return_value.video.assert_called_once()
    handler.assert_called()


def test_main_video_error_is_reported(run_main, capsys):
    args = _argv("--itag=10")
    with (
        mock.patch("pyt.cli.Client") as client_cls,
        mock.patch("pyt.cli._parse_args", return_value=args),
    ):
        client_cls.return_value.video.side_effect = PytError("boom")
        cli.main()
    assert "boom" in capsys.readouterr().out


def test_main_playlist_downloads_each_video(run_main):
    args = parse_args(
        argparse.ArgumentParser(), ["https://www.youtube.com/playlist?list=PLyn"]
    )
    with (
        mock.patch("pyt.cli._perform_args_on_video") as perform,
        mock.patch("pyt.cli.Client") as client_cls,
        mock.patch("pyt.cli._parse_args", return_value=args),
    ):
        client = client_cls.return_value
        client.playlist.return_value.title = "My List"
        client.playlist.return_value.video_urls = ["u1", "u2"]
        cli.main()

    assert [c.args[0] for c in client.video.call_args_list] == ["u1", "u2"]
    assert perform.call_count == 2
    first = perform.call_args_list[0]
    assert first.kwargs["playlist_index"] == 1
    assert first.kwargs["playlist_title"] == "My List"
    assert args.target == "My List"


def test_main_playlist_video_error_continues(capsys):
    args = parse_args(
        argparse.ArgumentParser(), ["https://www.youtube.com/playlist?list=PLyn"]
    )
    with (
        mock.patch(
            "pyt.cli._perform_args_on_video", side_effect=[PytError("bad one"), None]
        ) as perform,
        mock.patch("pyt.cli.Client") as client_cls,
        mock.patch("pyt.cli._parse_args", return_value=args),
    ):
        client = client_cls.return_value
        client.playlist.return_value.title = "t"
        client.playlist.return_value.video_urls = ["u1", "u2"]
        cli.main()

    assert perform.call_count == 2
    out = capsys.readouterr().out
    assert " x " in out
    assert "bad one" in out


def test_main_channel_uses_channel_name(run_main):
    args = parse_args(argparse.ArgumentParser(), ["https://www.youtube.com/@someone"])
    with (
        mock.patch("pyt.cli._perform_args_on_video") as perform,
        mock.patch("pyt.cli.Client") as client_cls,
        mock.patch("pyt.cli._parse_args", return_value=args),
    ):
        client = client_cls.return_value
        client.channel.return_value.name = "Some One"
        client.channel.return_value.video_urls = ["u1"]
        cli.main()

    client.channel.assert_called_once()
    assert perform.call_args.kwargs["playlist_title"] == "Some One"


# ── progress / tables ─────────────────────────────────────────────────────


def test_draw_progress_bar(capsys):
    cli._draw_progress(bytes_recv=25, filesize=100, speed=1024 * 50, eta=1.5)
    out, _ = capsys.readouterr()
    assert "25 B/100 B" in out
    assert "eta" in out


def test_draw_progress_bar_falls_back_to_ascii_on_cp1252(monkeypatch):
    import io

    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(cli.sys, "stdout", out)
    cli._draw_progress(bytes_recv=50, filesize=100, speed=0, eta=0)  # must not raise
    out.flush()
    assert b"#" in out.buffer.getvalue()


def test_make_stdout_safe_replaces_unencodable(monkeypatch):
    import io

    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(cli.sys, "stdout", out)
    cli._make_stdout_safe()
    out.write("a — b ━")  # would raise UnicodeEncodeError before
    out.flush()
    assert out.buffer.getvalue().startswith(b"a ")


def test_on_progress():
    stream = MagicMock(filesize=10, itag=1)
    cli._DL_STATE.clear()
    with mock.patch("pyt.cli._draw_progress") as draw:
        cli.on_progress(stream, b"", 7)
    draw.assert_called_once()
    assert draw.call_args.args[:2] == (3, 10)


def test_on_progress_restarts_clock_per_stream():
    cli._DL_STATE.clear()
    with mock.patch("pyt.cli._draw_progress"):
        cli.on_progress(MagicMock(filesize=10, itag=1), b"", 7)
        first = cli._DL_STATE["start"]
        cli.on_progress(MagicMock(filesize=10, itag=2), b"", 7)
    assert cli._DL_STATE["itag"] == 2
    assert cli._DL_STATE["start"] >= first


def test_display_streams(capsys):
    cli.display_streams(_catalog())
    out = capsys.readouterr().out
    assert "137" in out and "140" in out
    assert "progressive" in out and "audio only" in out and "video only" in out


def test_print_available_captions(capsys):
    cli._print_available_captions({"en": _caption("en"), "fr": _caption("fr")})
    out = capsys.readouterr().out
    assert "en" in out
    assert "fr" in out


# ── captions ──────────────────────────────────────────────────────────────


@mock.patch("pyt.cli._print_available_captions")
def test_download_caption_with_none(print_available):
    video = MagicMock()
    video.captions = {"en": _caption("en")}
    cli.download_caption(video, None)
    print_available.assert_called_with(video.captions)


def test_download_caption_with_language_found():
    video = MagicMock(title="video title")
    caption = _caption("en")
    caption.download = MagicMock(return_value="file_path")
    video.captions = {"en": caption}
    cli.download_caption(video, "en")
    caption.download.assert_called_with(title="video title", output_path=None)


@mock.patch("pyt.cli._print_available_captions")
def test_download_caption_with_lang_not_found(print_available):
    video = MagicMock()
    video.captions = {"en": _caption("en")}
    cli.download_caption(video, "blah")
    print_available.assert_called_with(video.captions)


# ── stream selection ──────────────────────────────────────────────────────


def test_by_itag():
    video = _catalog()
    assert cli._by_itag(video, 137).itag == 137
    assert cli._by_itag(video, 999) is None


def test_by_resolution_prefers_progressive_mp4():
    video = _catalog()
    assert cli._by_resolution(video, "720p").itag == 22
    # 1080p exists only as adaptive; -r wants a complete file.
    assert cli._by_resolution(video, "1080p") is None


def test_best_progressive():
    assert cli._best_progressive(_catalog()).itag == 22
    assert cli._best_progressive(_video(_raw(140, kind="audio", abr="1kbps"))) is None


def test_best_audio_by_subtype():
    video = _catalog()
    assert cli._best_audio(video, "mp4").itag == 140
    assert cli._best_audio(video).itag == 251
    assert cli._best_audio(video, "ogg") is None


def test_best_video_only_best_prefers_mp4_avc():
    # 2160p is webm-only, but mp4 is preferred when any mp4 exists.
    assert cli._best_video_only(_catalog(), "best").itag == 137


def test_best_video_only_best_falls_back_to_all_when_no_mp4():
    video = _video(
        _raw(248, subtype="webm", resolution="1080p", video_codec="vp9"),
        _raw(313, subtype="webm", resolution="2160p", video_codec="vp9"),
    )
    assert cli._best_video_only(video, "best").itag == 313


def test_best_video_only_best_prefers_avc_at_top_resolution():
    video = _video(
        _raw(399, resolution="1080p", video_codec="av01.0.08M.08"),
        _raw(137, resolution="1080p", video_codec="avc1.640028"),
    )
    assert cli._best_video_only(video, "best").itag == 137


def test_best_video_only_specific_resolution():
    video = _catalog()
    assert cli._best_video_only(video, "1080p").itag == 137
    assert cli._best_video_only(video, "2160p").itag == 313
    assert cli._best_video_only(video, "144p") is None


def test_best_video_only_ignores_progressive():
    assert (
        cli._best_video_only(_video(_raw(18, progressive=True, resolution="360p")))
        is None
    )


# ── single-stream downloads ───────────────────────────────────────────────


@mock.patch("pyt.cli.display_streams")
def test_download_when_itag_not_found(display_streams):
    video = _catalog()
    with pytest.raises(SystemExit):
        cli.download_by_itag(video, 123)
    display_streams.assert_called_with(video)


@mock.patch("pyt.cli._download")
def test_download_when_itag_is_found(download):
    video = _catalog()
    cli.download_by_itag(video, 140, target="out")
    stream = download.call_args.args[0]
    assert stream.itag == 140
    assert download.call_args.kwargs["target"] == "out"
    assert download.call_args.kwargs["video"] is video


@mock.patch("pyt.cli._download")
def test_download_by_resolution(download):
    video = _catalog()
    cli.download_by_resolution(video, "720p", target="test_target")
    download.assert_called_with(
        mock.ANY, target="test_target", filename=None, pp_chain=None, video=video
    )
    assert download.call_args.args[0].itag == 22


@mock.patch("pyt.cli._download")
def test_download_by_resolution_not_exists(download):
    with pytest.raises(SystemExit):
        cli.download_by_resolution(_catalog(), "DOESNT EXIST", target="t")
    download.assert_not_called()


@mock.patch("pyt.cli._download")
def test_download_audio(download):
    video = _catalog()
    cli.download_audio(video, "mp4", "target")
    assert download.call_args.args[0].itag == 140
    assert download.call_args.kwargs["target"] == "target"


@mock.patch("pyt.cli._download")
def test_download_audio_none(download):
    with pytest.raises(SystemExit):
        cli.download_audio(_catalog(), "filetype", "target")
    download.assert_not_called()


@mock.patch("pyt.cli._download")
def test_download_highest_progressive(download):
    cli.download_highest_resolution_progressive(_catalog())
    assert download.call_args.args[0].itag == 22


def test_download_stream_file_exists(tmp_path, capsys):
    stream = MagicMock(filesize=3, filesize_approx=3, default_filename="f.mp4")
    (tmp_path / "f.mp4").write_bytes(b"abc")
    cli._download(stream=stream, target=str(tmp_path))
    assert "Already exists at" in capsys.readouterr().out
    stream.download_to.assert_not_called()


def test_download_stream_downloads_and_runs_pp_chain(tmp_path):
    stream = MagicMock(filesize=3, filesize_approx=3, default_filename="f.mp4")
    stream.download_to.return_value.run.return_value = tmp_path / "f.mp4"
    video = MagicMock()
    pp = MagicMock()
    pp.run.return_value = "processed.mp4"

    result = cli._download(
        stream=stream, target=str(tmp_path), pp_chain=[pp], video=video
    )

    stream.download_to.assert_called_once_with(str(tmp_path), filename=None)
    pp.run.assert_called_once_with(str(tmp_path / "f.mp4"), stream, video)
    assert result == "processed.mp4"


def test_download_interrupt_removes_partial_file(tmp_path):
    stream = MagicMock(filesize=3, filesize_approx=3, default_filename="f.mp4")
    (tmp_path / "f.mp4").write_bytes(b"a")  # partial: size != filesize
    stream.download_to.return_value.run.side_effect = KeyboardInterrupt
    with pytest.raises(SystemExit):
        cli._download(stream=stream, target=str(tmp_path))
    assert not (tmp_path / "f.mp4").exists()


# ── merged download ───────────────────────────────────────────────────────


@mock.patch("pyt.cli.shutil.which", return_value="/usr/bin/ffmpeg")
@mock.patch("pyt.cli.CombinedDownload")
def test_ffmpeg_process_best_merges_avc_with_best_audio(combined, _which):
    video = _catalog()
    combined.return_value.run.return_value = os.path.join("out", "A Title.mp4")

    result = cli.ffmpeg_process(video, "best", "out")

    kwargs = combined.call_args.kwargs
    assert kwargs["video"] is video
    assert kwargs["video_stream"].itag == 137
    assert kwargs["audio_stream"].itag == 140  # same container family
    assert kwargs["output_path"] == "out"
    assert result == os.path.join("out", "A Title.mp4")


@mock.patch("pyt.cli.shutil.which", return_value="/usr/bin/ffmpeg")
@mock.patch("pyt.cli.CombinedDownload")
def test_ffmpeg_process_audio_subtype_fallback(combined, _which):
    video = _video(
        _raw(137, resolution="1080p", video_codec="avc1"),
        _raw(251, kind="audio", subtype="webm", abr="160kbps", audio_codec="opus"),
    )
    combined.return_value.run.return_value = "x.mkv"
    cli.ffmpeg_process(video, "best", "out")
    assert combined.call_args.kwargs["audio_stream"].itag == 251


@mock.patch("pyt.cli.CombinedDownload")
def test_ffmpeg_process_resolution_missing_exits(combined):
    with pytest.raises(SystemExit):
        cli.ffmpeg_process(_catalog(), "XYZp", "out")
    combined.assert_not_called()


@mock.patch("pyt.cli.CombinedDownload")
def test_ffmpeg_process_no_audio_exits(combined):
    video = _video(_raw(137, resolution="1080p", video_codec="avc1"))
    with pytest.raises(SystemExit):
        cli.ffmpeg_process(video, "best", "out")
    combined.assert_not_called()


@mock.patch("pyt.cli.shutil.which", return_value=None)
@mock.patch("pyt.cli.CombinedDownload")
def test_merged_download_requires_ffmpeg(combined, _which):
    video = _catalog()
    with pytest.raises(SystemExit):
        cli.ffmpeg_process(video, "best", "out")
    combined.assert_not_called()


@mock.patch("pyt.cli.shutil.which", return_value="/usr/bin/ffmpeg")
@mock.patch("pyt.cli.CombinedDownload")
def test_merged_download_runs_pp_chain_on_video_stream(combined, _which):
    video = _catalog()
    combined.return_value.run.return_value = "merged.mp4"
    pp = MagicMock()
    pp.run.return_value = "final.mp4"

    result = cli.ffmpeg_process(video, "best", "out", pp_chain=[pp])

    path, stream, vid = pp.run.call_args.args
    assert path == "merged.mp4"
    assert stream.itag == 137
    assert vid is video
    assert result == "final.mp4"


@mock.patch("pyt.cli.shutil.which", return_value="/usr/bin/ffmpeg")
@mock.patch("pyt.cli._download", return_value="prog.mp4")
@mock.patch("pyt.cli.CombinedDownload")
def test_merged_download_falls_back_to_progressive(combined, download, _which):
    video = _catalog()
    combined.return_value.run.side_effect = DownloadError("403")

    result = cli.ffmpeg_process(video, "best", "out", final_filename="name.mp4")

    assert result == "prog.mp4"
    stream = download.call_args.args[0]
    assert stream.itag == 22
    assert download.call_args.kwargs["filename"] == "name.mp4"


@mock.patch("pyt.cli.shutil.which", return_value="/usr/bin/ffmpeg")
@mock.patch("pyt.cli._download")
@mock.patch("pyt.cli.CombinedDownload")
def test_merged_download_error_without_progressive_exits(combined, download, _which):
    video = _video(
        _raw(137, resolution="1080p", video_codec="avc1"),
        _raw(140, kind="audio", subtype="mp4", abr="128kbps", audio_codec="mp4a"),
    )
    combined.return_value.run.side_effect = DownloadError("nope")
    with pytest.raises(SystemExit):
        cli.ffmpeg_process(video, "best", "out")
    download.assert_not_called()


# ── per-video orchestration ───────────────────────────────────────────────


@mock.patch("pyt.cli.ffmpeg_process")
def test_perform_args_should_ffmpeg_process(ffmpeg_process):
    video = _catalog()
    args = _argv("-f", "best")
    cli._perform_args_on_video(video, args)
    ffmpeg_process.assert_called_with(
        video=video,
        resolution="best",
        target=None,
        pp_chain=[],
        final_filename=None,
    )


@mock.patch("pyt.cli.ffmpeg_process")
@mock.patch("pyt.cli.shutil.which", return_value="/usr/bin/ffmpeg")
def test_perform_args_default_merges_best(_which, ffmpeg_process):
    video = _catalog()
    cli._perform_args_on_video(video, _argv())
    assert ffmpeg_process.call_args.kwargs["resolution"] == "best"


@mock.patch("pyt.cli.download_highest_resolution_progressive")
@mock.patch("pyt.cli.shutil.which", return_value=None)
def test_perform_args_default_without_ffmpeg_uses_progressive(_which, download):
    cli._perform_args_on_video(_catalog(), _argv())
    download.assert_called_once()


@mock.patch("pyt.cli._download")
def test_perform_args_extract_audio_alone_downloads_best_audio(download):
    video = _catalog()
    with mock.patch("pyt.cli.shutil.which", return_value="/usr/bin/ffmpeg"):
        cli._perform_args_on_video(video, _argv("-x"))
    assert download.call_args.args[0].itag == 251
    assert len(download.call_args.kwargs["pp_chain"]) == 1


def test_perform_args_skips_archived_video():
    archive = MagicMock()
    archive.is_downloaded.return_value = True
    with mock.patch("pyt.cli.ffmpeg_process") as ffmpeg_process:
        cli._perform_args_on_video(_catalog(), _argv("-f"), archive=archive)
    ffmpeg_process.assert_not_called()
    archive.mark_downloaded.assert_not_called()


@mock.patch("pyt.cli._download")
def test_perform_args_marks_archive_after_download(download):
    archive = MagicMock()
    archive.is_downloaded.return_value = False
    cli._perform_args_on_video(_catalog(), _argv("--itag=140"), archive=archive)
    archive.mark_downloaded.assert_called_once_with("vid123")


@mock.patch("pyt.cli._download")
def test_perform_args_applies_output_template(download, tmp_path):
    video = _catalog()
    video.author.name = "Chan"
    video.author.id = "UC1"
    video.published_at = None
    template = cli.OutputTemplate("{author}/{title}.{ext}")
    base = str(tmp_path / "base")
    cli._perform_args_on_video(
        video, _argv("--itag=140", "-t", base), template=template
    )
    kwargs = download.call_args.kwargs
    assert kwargs["filename"] == "A Title.mp4"
    assert kwargs["target"] == os.path.join(base, "Chan")


@patch("pyt.cli.dump_json")
def test_perform_args_dump_json_skips_download(dump_json):
    with mock.patch("pyt.cli._download") as download:
        cli._perform_args_on_video(_catalog(), _argv("-j"))
    dump_json.assert_called_once()
    download.assert_not_called()


def test_dump_json(capsys):
    import json
    from datetime import datetime, timedelta

    video = _catalog()
    video.video_id = "abc"
    video.title = "T"
    video.author.name = "A"
    video.author.id = "UC"
    video.author.url = "https://c"
    video.length = timedelta(seconds=90)
    video.views = 5
    video.published_at = datetime(2020, 1, 2)
    video.description = "d"
    video.metadata.keywords = ["k"]
    video.thumbnail_url = "https://t"
    video.url = "https://u"

    cli.dump_json(video)
    info = json.loads(capsys.readouterr().out)

    assert info["length"] == 90
    assert info["author"] == "A"
    assert info["publish_date"] == "2020-01-02T00:00:00"
    assert {s["itag"] for s in info["streams"]} >= {18, 140}


def test_build_playback_report(tmp_path, monkeypatch):
    import gzip
    import json

    monkeypatch.chdir(tmp_path)
    video = MagicMock(video_id="abc", url="https://u")
    video.player_response = {"videoDetails": {"videoId": "abc"}}

    cli.build_playback_report(video)

    (report,) = list(tmp_path.glob("yt-video-abc-*.json.gz"))
    data = json.loads(gzip.open(report).read())
    assert data["url"] == "https://u"
    assert data["video_info"]["videoDetails"]["videoId"] == "abc"

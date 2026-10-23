import os
import pytest
import logging
import argparse

from pyt.exceptions import PytError
from pyt import cli
from pyt.captions import Caption
from pyt.query import StreamQuery, CaptionQuery

from unittest import mock
from unittest.mock import patch, MagicMock

parse_args = cli._parse_args


@mock.patch("pyt.cli._parse_args")
def test_main_invalid_url(_parse_args):  # noqa: PT019
    parser = argparse.ArgumentParser()
    args = parse_args(
        parser,
        [
            "crikey",
        ],
    )
    _parse_args.return_value = args
    with pytest.raises(SystemExit):
        cli.main()


@mock.patch("pyt.cli.display_streams")
@mock.patch("pyt.cli.YouTube")
def test_download_when_itag_not_found(youtube, display_streams):
    # Given
    youtube.streams = mock.Mock()
    youtube.streams.get_by_itag.return_value = None
    # When
    with pytest.raises(SystemExit):
        cli.download_by_itag(youtube, 123)
    # Then
    youtube.streams.get_by_itag.assert_called_with(123)
    display_streams.assert_called_with(youtube)


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.streams.Stream")
def test_download_when_itag_is_found(youtube, stream):
    stream.itag = 123
    stream.exists_at_path.return_value = False
    youtube.streams = StreamQuery([stream])
    with patch.object(
        youtube.streams, "get_by_itag", wraps=youtube.streams.get_by_itag
    ) as wrapped_itag:
        cli.download_by_itag(youtube, 123)
        wrapped_itag.assert_called_with(123)
    youtube.register_on_progress_callback.assert_called_with(cli.on_progress)
    stream.download.assert_called()


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.streams.Stream")
def test_display_stream(youtube, stream, capsys):
    # Given
    stream.itag = 123
    youtube.streams = StreamQuery([stream])
    # When
    cli.display_streams(youtube)
    # Then — itag must appear in the table output
    captured = capsys.readouterr()
    assert "123" in captured.out


@mock.patch("pyt.cli._print_available_captions")
@mock.patch("pyt.cli.YouTube")
def test_download_caption_with_none(youtube, print_available):
    # Given
    caption = Caption(
        {
            "url": "url1",
            "name": {"simpleText": "name1"},
            "languageCode": "en",
            "vssId": ".en",
        }
    )
    youtube.captions = CaptionQuery([caption])
    # When
    cli.download_caption(youtube, None)
    # Then
    print_available.assert_called_with(youtube.captions)


@mock.patch("pyt.cli.YouTube")
def test_download_caption_with_language_found(youtube):
    youtube.title = "video title"
    caption = Caption(
        {
            "url": "url1",
            "name": {"simpleText": "name1"},
            "languageCode": "en",
            "vssId": ".en",
        }
    )
    caption.download = MagicMock(return_value="file_path")
    youtube.captions = CaptionQuery([caption])
    cli.download_caption(youtube, "en")
    caption.download.assert_called_with(title="video title", output_path=None)


@mock.patch("pyt.cli._print_available_captions")
@mock.patch("pyt.cli.YouTube")
def test_download_caption_with_lang_not_found(youtube, print_available):
    # Given
    caption = Caption(
        {
            "url": "url1",
            "name": {"simpleText": "name1"},
            "languageCode": "en",
            "vssId": ".en",
        }
    )
    youtube.captions = CaptionQuery([caption])
    # When
    cli.download_caption(youtube, "blah")
    # Then
    print_available.assert_called_with(youtube.captions)


def test_print_available_captions(capsys):
    # Given
    caption1 = Caption(
        {
            "url": "url1",
            "name": {"simpleText": "name1"},
            "languageCode": "en",
            "vssId": ".en",
        }
    )
    caption2 = Caption(
        {
            "url": "url2",
            "name": {"simpleText": "name2"},
            "languageCode": "fr",
            "vssId": ".fr",
        }
    )
    query = CaptionQuery([caption1, caption2])
    # When
    cli._print_available_captions(query)
    # Then
    captured = capsys.readouterr()
    assert "en" in captured.out
    assert "fr" in captured.out


def test_draw_progress_bar(capsys):
    cli._draw_progress(bytes_recv=25, filesize=100, speed=1024 * 50, eta=1.5)
    out, _ = capsys.readouterr()
    assert "25 B/100 B" in out
    assert "eta" in out


@mock.patch("pyt.streams.Stream")
def test_on_progress(stream):
    stream.filesize = 10
    cli._DL_STATE.clear()
    cli._draw_progress = MagicMock()
    cli.on_progress(stream, b"", 7)
    cli._draw_progress.assert_called_once()


def test_parse_args_falsey():
    parser = argparse.ArgumentParser()
    args = cli._parse_args(parser, ["http://youtube.com/watch?v=9bZkp7q19f0"])
    assert args.url == "http://youtube.com/watch?v=9bZkp7q19f0"
    assert args.build_playback_report is False
    assert args.itag is None
    assert args.list is False
    # `-v` is a count flag now: 0 = unset, 1 = -v / DEBUG, 2 = -vv / TRACE.
    assert args.verbose == 0


def test_parse_args_truthy():
    parser = argparse.ArgumentParser()
    args = cli._parse_args(
        parser,
        [
            "http://youtube.com/watch?v=9bZkp7q19f0",
            "--build-playback-report",
            "-c",
            "en",
            "-l",
            "--itag=10",
            "-vvv",
        ],
    )
    assert args.url == "http://youtube.com/watch?v=9bZkp7q19f0"
    assert args.build_playback_report is True
    assert args.itag == 10
    assert args.list is True
    # -vvv = 3 occurrences; we cap at TRACE (2+) for behavior, but the
    # raw count is preserved for any caller that wants to inspect it.
    assert args.verbose == 3


@mock.patch("pyt.cli.setup_logger", return_value=None)
def test_main_logging_setup(setup_logger):
    # Given
    parser = argparse.ArgumentParser()
    args = parse_args(parser, ["http://fakeurl", "-v"])
    cli._parse_args = MagicMock(return_value=args)
    # When
    with pytest.raises(SystemExit):
        cli.main()
    # Then
    setup_logger.assert_called_with(logging.DEBUG, log_filename=None)


@mock.patch("pyt.cli.YouTube", return_value=None)
def test_main_download_by_itag(youtube):
    parser = argparse.ArgumentParser()
    args = parse_args(parser, ["http://youtube.com/watch?v=9bZkp7q19f0", "--itag=10"])
    cli._parse_args = MagicMock(return_value=args)
    cli.download_by_itag = MagicMock()
    cli.main()

    youtube.assert_called()
    cli.download_by_itag.assert_called()


@mock.patch("pyt.cli.YouTube", return_value=None)
def test_main_build_playback_report(youtube):
    parser = argparse.ArgumentParser()
    args = parse_args(
        parser,
        ["http://youtube.com/watch?v=9bZkp7q19f0", "--build-playback-report"],
    )
    cli._parse_args = MagicMock(return_value=args)
    cli.build_playback_report = MagicMock()
    cli.main()
    youtube.assert_called()
    cli.build_playback_report.assert_called()


@mock.patch("pyt.cli.YouTube", return_value=None)
def test_main_display_streams(youtube):
    parser = argparse.ArgumentParser()
    args = parse_args(parser, ["http://youtube.com/watch?v=9bZkp7q19f0", "-l"])
    cli._parse_args = MagicMock(return_value=args)
    cli.display_streams = MagicMock()
    cli.main()
    youtube.assert_called()
    cli.display_streams.assert_called()


@mock.patch("pyt.cli.YouTube", return_value=None)
def test_main_download_caption(youtube):
    parser = argparse.ArgumentParser()
    args = parse_args(parser, ["http://youtube.com/watch?v=9bZkp7q19f0", "-c", "en"])
    cli._parse_args = MagicMock(return_value=args)
    cli.download_caption = MagicMock()
    cli.main()
    youtube.assert_called()
    cli.download_caption.assert_called()


@mock.patch("pyt.cli.YouTube", return_value=None)
@mock.patch("pyt.cli.download_by_resolution")
def test_download_by_resolution_flag(youtube, download_by_resolution):
    parser = argparse.ArgumentParser()
    args = parse_args(parser, ["http://youtube.com/watch?v=9bZkp7q19f0", "-r", "320p"])
    cli._parse_args = MagicMock(return_value=args)
    cli.main()
    youtube.assert_called()
    download_by_resolution.assert_called()


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.cli.Playlist")
@mock.patch("pyt.cli._perform_args_on_youtube")
def test_download_with_playlist(perform_args_on_youtube, playlist, youtube):
    # Given
    cli.safe_filename = MagicMock(return_value="safe_title")
    parser = argparse.ArgumentParser()
    args = parse_args(parser, ["https://www.youtube.com/playlist?list=PLyn"])
    cli._parse_args = MagicMock(return_value=args)
    videos = [youtube]
    playlist_instance = playlist.return_value
    playlist_instance.videos = videos
    # When
    cli.main()
    # Then
    playlist.assert_called()
    perform_args_on_youtube.assert_called_with(
        youtube,
        args,
        archive=None,
        template=None,
        playlist_index=1,
        playlist_title=playlist_instance.title,
    )


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.cli.Playlist")
@mock.patch("pyt.cli._perform_args_on_youtube")
def test_download_with_playlist_video_error(
    perform_args_on_youtube, playlist, youtube, capsys
):
    # Given
    cli.safe_filename = MagicMock(return_value="safe_title")
    parser = argparse.ArgumentParser()
    args = parse_args(parser, ["https://www.youtube.com/playlist?list=PLyn"])
    cli._parse_args = MagicMock(return_value=args)
    videos = [youtube]
    playlist_instance = playlist.return_value
    playlist_instance.videos = videos
    perform_args_on_youtube.side_effect = PytError()
    # When
    cli.main()
    # Then
    playlist.assert_called()
    captured = capsys.readouterr()
    assert " x " in captured.out


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.query.StreamQuery")
@mock.patch("pyt.streams.Stream")
@mock.patch("pyt.cli._download")
def test_download_by_resolution(download, stream, stream_query, youtube):
    # Given
    stream_query.get_by_resolution.return_value = stream
    youtube.streams = stream_query
    # When
    cli.download_by_resolution(youtube=youtube, resolution="320p", target="test_target")
    # Then
    download.assert_called_with(
        stream, target="test_target", filename=None, pp_chain=None, youtube=youtube
    )


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.query.StreamQuery")
@mock.patch("pyt.cli._download")
def test_download_by_resolution_not_exists(download, stream_query, youtube):
    stream_query.get_by_resolution.return_value = None
    youtube.streams = stream_query
    with pytest.raises(SystemExit):
        cli.download_by_resolution(
            youtube=youtube, resolution="DOESNT EXIST", target="test_target"
        )
    download.assert_not_called()


@mock.patch("pyt.streams.Stream")
def test_download_stream_file_exists(stream, capsys):
    # Given
    stream.filesize_approx = 1048576
    stream.exists_at_path.return_value = True
    # When
    cli._download(stream=stream)
    # Then
    captured = capsys.readouterr()
    assert "Already exists at" in captured.out
    stream.download.assert_not_called()


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.cli.ffmpeg_process")
def test_perform_args_should_ffmpeg_process(ffmpeg_process, youtube):
    # Given
    parser = argparse.ArgumentParser()
    args = parse_args(parser, ["http://youtube.com/watch?v=9bZkp7q19f0", "-f", "best"])
    cli._parse_args = MagicMock(return_value=args)
    # When
    cli._perform_args_on_youtube(youtube, args)
    # Then
    ffmpeg_process.assert_called_with(
        youtube=youtube,
        resolution="best",
        target=None,
        pp_chain=[],
        final_filename=None,
    )


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.cli._ffmpeg_downloader")
def test_ffmpeg_process_best_should_download(  # noqa: PT019
    _ffmpeg_downloader, youtube
):
    # Given
    target = "/target"
    streams = MagicMock()
    youtube.streams = streams
    video_stream = MagicMock()
    streams.filter.return_value.order_by.return_value.last.return_value = video_stream
    # No H.264 stream found at best resolution → falls back to video_stream
    streams.filter.return_value.first.return_value = None
    audio_stream = MagicMock()
    streams.get_audio_only.return_value = audio_stream
    # When
    cli.ffmpeg_process(youtube, "best", target)
    # Then
    _ffmpeg_downloader.assert_called_with(
        audio_stream=audio_stream,
        video_stream=video_stream,
        target=target,
        pp_chain=None,
        youtube=youtube,
        final_filename=None,
    )


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.cli._ffmpeg_downloader")
def test_ffmpeg_process_res_should_download(_ffmpeg_downloader, youtube):  # noqa: PT019
    # Given
    target = "/target"
    streams = MagicMock()
    youtube.streams = streams
    video_stream = MagicMock()
    streams.filter.return_value.first.return_value = video_stream
    audio_stream = MagicMock()
    streams.get_audio_only.return_value = audio_stream
    # When
    cli.ffmpeg_process(youtube, "XYZp", target)
    # Then
    _ffmpeg_downloader.assert_called_with(
        audio_stream=audio_stream,
        video_stream=video_stream,
        target=target,
        pp_chain=None,
        youtube=youtube,
        final_filename=None,
    )


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.cli._ffmpeg_downloader")
def test_ffmpeg_process_res_none_should_not_download(  # noqa: PT019
    _ffmpeg_downloader, youtube
):
    # Given
    target = "/target"
    streams = MagicMock()
    youtube.streams = streams
    streams.filter.return_value.first.return_value = None
    audio_stream = MagicMock()
    streams.get_audio_only.return_value = audio_stream
    # When
    with pytest.raises(SystemExit):
        cli.ffmpeg_process(youtube, "XYZp", target)
    # Then
    _ffmpeg_downloader.assert_not_called()


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.cli._ffmpeg_downloader")
def test_ffmpeg_process_audio_none_should_fallback_download(  # noqa: PT019
    _ffmpeg_downloader, youtube
):
    # Given
    target = "/target"
    streams = MagicMock()
    youtube.streams = streams
    stream = MagicMock()
    streams.filter.return_value.order_by.return_value.last.return_value = stream
    # No H.264 stream found at best resolution → falls back to stream
    streams.filter.return_value.first.return_value = None
    streams.get_audio_only.return_value = None
    # When
    cli.ffmpeg_process(youtube, "best", target)
    # Then
    _ffmpeg_downloader.assert_called_with(
        audio_stream=stream,
        video_stream=stream,
        target=target,
        pp_chain=None,
        youtube=youtube,
        final_filename=None,
    )


@mock.patch("pyt.cli.YouTube")
@mock.patch("pyt.cli._ffmpeg_downloader")
def test_ffmpeg_process_audio_fallback_none_should_exit(  # noqa: PT019
    _ffmpeg_downloader, youtube
):
    # Given
    target = "/target"
    streams = MagicMock()
    youtube.streams = streams
    video_stream = MagicMock()
    # video stream found; audio fallback also returns None
    streams.filter.return_value.order_by.return_value.last.side_effect = [
        video_stream,
        None,
    ]
    streams.get_audio_only.return_value = None
    # When
    with pytest.raises(SystemExit):
        cli.ffmpeg_process(youtube, "best", target)
    # Then
    _ffmpeg_downloader.assert_not_called()


@mock.patch("pyt.cli.os.unlink", return_value=None)
@mock.patch("pyt.cli.shutil.which", return_value="/usr/bin/ffmpeg")
@mock.patch("pyt.cli.subprocess.run")
@mock.patch("pyt.cli._download", return_value=None)
@mock.patch("pyt.cli._unique_name", return_value=None)
def test_ffmpeg_downloader(unique_name, download, run, _which, unlink):
    # Given: avc1 video + aac audio → merge container should be mp4
    target = "target"
    audio_stream = MagicMock()
    audio_stream.subtype = "m4a"
    audio_stream.audio_codec = "mp4a.40.2"
    video_stream = MagicMock()
    video_stream.id = "video_id"
    video_stream.subtype = "mp4"
    video_stream.video_codec = "avc1.640028"
    video_stream.title = "safe_title"  # safe_filename needs a real string
    unique_name.side_effect = ["video_name", "audio_name"]
    run.return_value = mock.Mock(returncode=0, stderr=b"")

    # When
    cli._ffmpeg_downloader(
        audio_stream=audio_stream, video_stream=video_stream, target=target
    )

    # Then
    download.assert_called()
    # The merge code now also invokes ffprobe for duration validation, so
    # call_args (the last call) is the ffprobe one. Find the ffmpeg invocation
    # in the call list explicitly.
    ffmpeg_calls = [
        c for c in run.call_args_list if c.args and c.args[0] and "-map" in c.args[0]
    ]
    assert ffmpeg_calls, "ffmpeg merge invocation not found in subprocess.run calls"
    args, kwargs = ffmpeg_calls[0]
    cmd = args[0]
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert "-map" in cmd and "0:v:0" in cmd and "1:a:0" in cmd
    assert "-c:v" in cmd and "-c:a" in cmd
    assert "+faststart" in cmd  # mp4 container → faststart enabled
    assert cmd[-1] == os.path.join("target", "safe_title.mp4")
    assert kwargs.get("check") is False
    assert kwargs.get("capture_output") is True
    # Sources must be unlinked on success (video + audio).
    assert unlink.call_count >= 2


@mock.patch("pyt.cli.os.unlink", return_value=None)
@mock.patch("pyt.cli.shutil.which", return_value="/usr/bin/ffmpeg")
@mock.patch("pyt.cli.subprocess.run")
@mock.patch("pyt.cli._download", return_value=None)
@mock.patch("pyt.cli._unique_name", return_value=None)
def test_ffmpeg_downloader_failure_keeps_sources(
    unique_name, download, run, _which, unlink
):
    """If ffmpeg fails, the sources must NOT be deleted — user can retry."""
    audio_stream = MagicMock()
    audio_stream.subtype = "m4a"
    audio_stream.audio_codec = "mp4a.40.2"
    video_stream = MagicMock()
    video_stream.subtype = "mp4"
    video_stream.video_codec = "avc1.640028"
    video_stream.title = "safe_title"  # safe_filename needs a real string
    unique_name.side_effect = ["video_name", "audio_name"]
    run.return_value = mock.Mock(returncode=1, stderr=b"some ffmpeg error")

    with pytest.raises(SystemExit):
        cli._ffmpeg_downloader(
            audio_stream=audio_stream, video_stream=video_stream, target="target"
        )
    # Sources must be intact on failure.
    unlink.assert_not_called()


def test_pick_merge_container():
    avc_stream = MagicMock(video_codec="avc1.64001f")
    av1_stream = MagicMock(video_codec="av01.0.05M.08")
    vp9_stream = MagicMock(video_codec="vp09.00.50.08")
    aac_stream = MagicMock(audio_codec="mp4a.40.2")
    opus_stream = MagicMock(audio_codec="opus")

    assert cli._pick_merge_container(avc_stream, aac_stream) == "mp4"
    assert cli._pick_merge_container(av1_stream, aac_stream) == "mp4"
    assert cli._pick_merge_container(vp9_stream, opus_stream) == "webm"
    # Cross-family combos must fall back to the safe container.
    assert cli._pick_merge_container(vp9_stream, aac_stream) == "mkv"
    assert cli._pick_merge_container(avc_stream, opus_stream) == "mkv"


@mock.patch("pyt.cli.download_audio")
@mock.patch("pyt.cli.YouTube.__init__", return_value=None)
def test_download_audio_args(youtube, download_audio):
    # Given
    parser = argparse.ArgumentParser()
    args = parse_args(parser, ["http://youtube.com/watch?v=9bZkp7q19f0", "-a", "mp4"])
    cli._parse_args = MagicMock(return_value=args)
    # When
    cli.main()
    # Then
    youtube.assert_called()
    download_audio.assert_called()


@mock.patch("pyt.cli._download")
@mock.patch("pyt.cli.YouTube")
def test_download_audio(youtube, download):
    # Given
    youtube_instance = youtube.return_value
    audio_stream = MagicMock()
    youtube_instance.streams.filter.return_value.order_by.return_value.last.return_value = (
        audio_stream
    )
    # When
    cli.download_audio(youtube_instance, "filetype", "target")
    # Then
    download.assert_called_with(
        audio_stream,
        target="target",
        filename=None,
        pp_chain=None,
        youtube=youtube_instance,
    )


@mock.patch("pyt.cli._download")
@mock.patch("pyt.cli.YouTube")
def test_download_audio_none(youtube, download):
    # Given
    youtube_instance = youtube.return_value
    youtube_instance.streams.filter.return_value.order_by.return_value.last.return_value = (
        None
    )
    # When
    with pytest.raises(SystemExit):
        cli.download_audio(youtube_instance, "filetype", "target")
    # Then
    download.assert_not_called()


@mock.patch("pyt.cli.YouTube.__init__", return_value=None)
def test_perform_args_on_youtube(youtube):
    parser = argparse.ArgumentParser()
    args = parse_args(parser, ["http://youtube.com/watch?v=9bZkp7q19f0"])
    cli._parse_args = MagicMock(return_value=args)
    cli._perform_args_on_youtube = MagicMock()
    cli.main()
    youtube.assert_called()
    cli._perform_args_on_youtube.assert_called()


@mock.patch("pyt.cli.os.path.exists", return_value=False)
def test_unique_name(path_exists):
    assert cli._unique_name("base", "subtype", "video", "target") == "base_video_0"


@mock.patch("pyt.cli.os.path.exists")
def test_unique_name_counter(path_exists):
    path_exists.side_effect = [True, False]
    assert cli._unique_name("base", "subtype", "video", "target") == "base_video_1"

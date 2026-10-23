"""Tests for pyt.postprocessors — all post-processor classes."""

import os
import tempfile

import pytest
from unittest import mock

from pyt.postprocessors.base import BasePostProcessor, PostProcessorError
from pyt.postprocessors.audio import AudioExtractor, SUPPORTED_FORMATS, _CODEC_MAP
from pyt.postprocessors.metadata import FFmpegMetadataEmbedder
from pyt.postprocessors.thumbnail import EmbedThumbnailPostProcessor
from pyt.postprocessors.subtitle import EmbedSubtitlePostProcessor
from pyt.postprocessors.sponsorblock import (
    SponsorBlockPP,
    ALL_CATEGORIES,
    _fetch_segments,
    _LABELS,
)

# ── helpers ───────────────────────────────────────────────────────────────────

FAKE_FFMPEG = "/usr/bin/ffmpeg"


def _yt(
    title="Title",
    author="Author",
    video_id="vid123",
    publish_date=None,
    description="Desc",
    thumbnail_url="http://img/t.jpg",
    captions=None,
):
    from datetime import datetime

    yt = mock.MagicMock()
    yt.title = title
    yt.author = author
    yt.video_id = video_id
    yt.publish_date = publish_date or datetime(2024, 1, 15)
    yt.description = description
    yt.thumbnail_url = thumbnail_url
    yt.captions = captions or {}
    return yt


def _stream(subtype="mp4"):
    s = mock.MagicMock()
    s.subtype = subtype
    return s


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda name: FAKE_FFMPEG if name == "ffmpeg" else None
    )


@pytest.fixture
def no_ffmpeg(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)


# ══════════════════════════════════════════════════════════════════════════════
# BasePostProcessor / PostProcessorError
# ══════════════════════════════════════════════════════════════════════════════


def test_post_processor_error_is_exception():
    err = PostProcessorError("boom")
    assert isinstance(err, Exception)
    assert str(err) == "boom"


def test_base_post_processor_is_abstract():
    with pytest.raises(TypeError):
        BasePostProcessor()  # type: ignore


def test_concrete_subclass_works():
    class Concrete(BasePostProcessor):
        def run(self, file_path, stream, youtube):
            return file_path

    pp = Concrete()
    assert pp.run("x.mp4", None, None) == "x.mp4"


# ══════════════════════════════════════════════════════════════════════════════
# FFmpegPostProcessor (via AudioExtractor)
# ══════════════════════════════════════════════════════════════════════════════


def test_ffmpeg_not_found_raises(no_ffmpeg):
    with pytest.raises(PostProcessorError, match="ffmpeg not found"):
        AudioExtractor()


def test_ffmpeg_found_does_not_raise(fake_ffmpeg):
    ae = AudioExtractor(format="mp3")
    assert ae._ffmpeg == FAKE_FFMPEG


def test_run_ffmpeg_raises_on_nonzero_exit(fake_ffmpeg, tmp_path):
    ae = AudioExtractor()
    result = mock.MagicMock()
    result.returncode = 1
    result.stderr = b"some error"
    with mock.patch("subprocess.run", return_value=result):
        with pytest.raises(PostProcessorError, match="ffmpeg exited"):
            ae._run_ffmpeg(["-i", "input.mp4", "output.mp3"])


def test_run_ffmpeg_success_does_not_raise(fake_ffmpeg):
    ae = AudioExtractor()
    result = mock.MagicMock()
    result.returncode = 0
    with mock.patch("subprocess.run", return_value=result):
        ae._run_ffmpeg(["-i", "input.mp4", "output.mp3"])  # no exception


def test_temp_path_in_same_dir_as_original(fake_ffmpeg, tmp_path):
    ae = AudioExtractor()
    original = str(tmp_path / "video.mp4")
    tmp = ae._temp_path(original, ".mp3")
    assert os.path.dirname(tmp) == str(tmp_path)
    assert tmp.endswith(".mp3")
    os.unlink(tmp)  # mkstemp creates the file


def test_replace_file_moves_src_to_dst(fake_ffmpeg, tmp_path):
    ae = AudioExtractor()
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("content")
    result = ae._replace_file(str(src), str(dst))
    assert result == str(dst)
    assert dst.read_text() == "content"
    assert not src.exists()


def test_replace_file_overwrites_existing_dst(fake_ffmpeg, tmp_path):
    ae = AudioExtractor()
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("new")
    dst.write_text("old")
    ae._replace_file(str(src), str(dst))
    assert dst.read_text() == "new"


# ══════════════════════════════════════════════════════════════════════════════
# AudioExtractor
# ══════════════════════════════════════════════════════════════════════════════


def test_supported_formats_list():
    for fmt in ("mp3", "m4a", "aac", "flac", "opus", "vorbis", "wav", "alac"):
        assert fmt in SUPPORTED_FORMATS


def test_unsupported_format_raises(fake_ffmpeg):
    with pytest.raises(PostProcessorError, match="Unsupported audio format"):
        AudioExtractor(format="xyz")


def test_codec_map_integrity():
    for fmt, (codec, ext) in _CODEC_MAP.items():
        assert ext.startswith(".")
        assert codec  # non-empty


@pytest.mark.parametrize(
    "fmt,expected_codec,expected_ext",
    [
        ("mp3", "libmp3lame", ".mp3"),
        ("flac", "flac", ".flac"),
        ("opus", "libopus", ".opus"),
        ("wav", "pcm_s16le", ".wav"),
    ],
)
def test_audio_extractor_codec_selection(
    fmt, expected_codec, expected_ext, fake_ffmpeg
):
    ae = AudioExtractor(format=fmt)
    assert ae._codec == expected_codec
    assert ae._ext == expected_ext


def test_audio_extractor_run_invokes_ffmpeg_with_correct_args(fake_ffmpeg, tmp_path):
    input_file = tmp_path / "video.mp4"
    input_file.write_bytes(b"fake")
    out_path = str(tmp_path / "video.mp3")

    ae = AudioExtractor(format="mp3")

    captured_args = []

    def fake_run(cmd, **kwargs):
        captured_args.extend(cmd)
        r = mock.MagicMock()
        r.returncode = 0
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch.object(ae, "_temp_path", return_value=out_path):
            with mock.patch("os.unlink"):
                with mock.patch("os.replace"):
                    ae.run(str(input_file), _stream(), _yt())

    assert "-vn" in captured_args
    assert "-acodec" in captured_args
    assert "libmp3lame" in captured_args


def test_audio_extractor_quality_flag_included(fake_ffmpeg, tmp_path):
    input_file = tmp_path / "video.mp4"
    input_file.write_bytes(b"fake")

    ae = AudioExtractor(format="mp3", quality="0")
    out_path = str(tmp_path / "video.mp3")

    captured_args = []

    def fake_run(cmd, **kwargs):
        captured_args.extend(cmd)
        r = mock.MagicMock()
        r.returncode = 0
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch.object(ae, "_temp_path", return_value=out_path):
            with mock.patch("os.unlink"):
                with mock.patch("os.replace"):
                    ae.run(str(input_file), _stream(), _yt())

    assert "-q:a" in captured_args
    assert "0" in captured_args


def test_audio_extractor_deletes_original_on_success(fake_ffmpeg, tmp_path):
    input_file = tmp_path / "video.mp4"
    input_file.write_bytes(b"fake")
    out_path = str(tmp_path / "video.mp3")

    ae = AudioExtractor(format="mp3")
    deleted = []

    def fake_run(cmd, **kwargs):
        r = mock.MagicMock()
        r.returncode = 0
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch.object(ae, "_temp_path", return_value=out_path):
            with mock.patch("os.unlink", side_effect=lambda p: deleted.append(p)):
                with mock.patch("os.replace"):
                    ae.run(str(input_file), _stream(), _yt())

    assert str(input_file) in deleted


# ══════════════════════════════════════════════════════════════════════════════
# FFmpegMetadataEmbedder
# ══════════════════════════════════════════════════════════════════════════════


def test_collect_meta_includes_all_fields(fake_ffmpeg):
    from datetime import datetime

    embedder = FFmpegMetadataEmbedder()
    yt = _yt(
        title="My Song",
        author="Artist",
        publish_date=datetime(2024, 3, 15),
        description="Some description",
    )
    meta = embedder._collect_meta(yt)
    assert meta["title"] == "My Song"
    assert meta["artist"] == "Artist"
    assert meta["date"] == "20240315"
    assert "Some description" in meta["comment"]


def test_collect_meta_skips_none_fields(fake_ffmpeg):
    embedder = FFmpegMetadataEmbedder()
    yt = mock.MagicMock()
    yt.title = None
    yt.author = "Author"
    yt.publish_date = None
    yt.description = None
    meta = embedder._collect_meta(yt)
    assert "title" not in meta
    assert "date" not in meta
    assert meta["artist"] == "Author"


def test_embed_ffmpeg_calls_codec_copy(fake_ffmpeg, tmp_path):
    embedder = FFmpegMetadataEmbedder()
    input_file = tmp_path / "video.mp4"
    input_file.write_bytes(b"fake")
    tmp_out = str(tmp_path / "tmp.mp4")

    captured_args = []

    def fake_run(cmd, **kwargs):
        captured_args.extend(cmd)
        r = mock.MagicMock()
        r.returncode = 0
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch.object(embedder, "_temp_path", return_value=tmp_out):
            with mock.patch.object(
                embedder, "_replace_file", return_value=str(input_file)
            ):
                embedder._embed_ffmpeg(str(input_file), _yt(title="T", author="A"))

    assert "-metadata" in captured_args
    assert "title=T" in captured_args
    assert "-codec" in captured_args
    assert "copy" in captured_args


def test_run_uses_mutagen_for_mp3_when_available(fake_ffmpeg, tmp_path):
    embedder = FFmpegMetadataEmbedder()
    mp3_file = tmp_path / "audio.mp3"
    mp3_file.write_bytes(b"fake")

    with mock.patch.object(embedder, "_try_mutagen", return_value=True) as m:
        result = embedder.run(str(mp3_file), _stream(subtype="mp3"), _yt())
    m.assert_called_once()
    assert result == str(mp3_file)


def test_run_falls_back_to_ffmpeg_for_mp3_when_mutagen_absent(fake_ffmpeg, tmp_path):
    embedder = FFmpegMetadataEmbedder()
    mp3_file = tmp_path / "audio.mp3"
    mp3_file.write_bytes(b"fake")

    with mock.patch.object(embedder, "_try_mutagen", return_value=False):
        with mock.patch.object(
            embedder, "_embed_ffmpeg", return_value=str(mp3_file)
        ) as m:
            result = embedder.run(str(mp3_file), _stream(subtype="mp3"), _yt())
    m.assert_called_once()


def test_run_skips_mutagen_for_non_mp3(fake_ffmpeg, tmp_path):
    embedder = FFmpegMetadataEmbedder()
    m4a_file = tmp_path / "audio.m4a"
    m4a_file.write_bytes(b"fake")

    with mock.patch.object(embedder, "_try_mutagen") as try_mut:
        with mock.patch.object(embedder, "_embed_ffmpeg", return_value=str(m4a_file)):
            embedder.run(str(m4a_file), _stream(subtype="m4a"), _yt())
    try_mut.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# EmbedThumbnailPostProcessor
# ══════════════════════════════════════════════════════════════════════════════


def test_thumbnail_skips_when_no_url(fake_ffmpeg, tmp_path, caplog):
    pp = EmbedThumbnailPostProcessor()
    yt = _yt(thumbnail_url=None)
    yt.thumbnail_url = None
    input_file = str(tmp_path / "video.mp4")
    result = pp.run(input_file, _stream(), yt)
    assert result == input_file


def test_thumbnail_embed_mp4_calls_ffmpeg(fake_ffmpeg, tmp_path):
    pp = EmbedThumbnailPostProcessor()
    input_file = tmp_path / "video.mp4"
    input_file.write_bytes(b"fake")
    yt = _yt(thumbnail_url="http://img/thumb.jpg")

    captured_args = []

    def fake_run(cmd, **kwargs):
        captured_args.extend(cmd)
        r = mock.MagicMock()
        r.returncode = 0
        return r

    with mock.patch("urllib.request.urlretrieve"):
        with mock.patch("subprocess.run", side_effect=fake_run):
            with mock.patch.object(
                pp, "_temp_path", return_value=str(tmp_path / "tmp.mp4")
            ):
                with mock.patch.object(
                    pp, "_replace_file", return_value=str(input_file)
                ):
                    pp.run(str(input_file), _stream(subtype="mp4"), yt)

    assert "-disposition:v:1" in captured_args
    assert "attached_pic" in captured_args


def test_thumbnail_cleans_up_temp_thumb_on_success(fake_ffmpeg, tmp_path):
    pp = EmbedThumbnailPostProcessor()
    input_file = tmp_path / "video.mp4"
    input_file.write_bytes(b"fake")

    thumb_path = str(tmp_path / "thumb.jpg")
    removed = []

    def fake_run(cmd, **kwargs):
        r = mock.MagicMock()
        r.returncode = 0
        return r

    with mock.patch("urllib.request.urlretrieve"):
        with mock.patch("subprocess.run", side_effect=fake_run):
            with mock.patch.object(pp, "_temp_path", return_value=thumb_path):
                with mock.patch.object(
                    pp, "_replace_file", return_value=str(input_file)
                ):
                    with mock.patch("tempfile.mkstemp", return_value=(0, thumb_path)):
                        with mock.patch("os.close"):
                            with mock.patch("os.path.exists", return_value=True):
                                with mock.patch(
                                    "os.unlink", side_effect=lambda p: removed.append(p)
                                ):
                                    pp.run(
                                        str(input_file),
                                        _stream(subtype="mp4"),
                                        _yt(thumbnail_url="http://img/t.jpg"),
                                    )

    assert thumb_path in removed


def test_thumbnail_logs_warning_for_unsupported_ext(fake_ffmpeg, tmp_path, caplog):
    import logging

    pp = EmbedThumbnailPostProcessor()
    input_file = str(tmp_path / "video.avi")
    yt = _yt(thumbnail_url="http://img/t.jpg")

    with mock.patch("urllib.request.urlretrieve"):
        with mock.patch("tempfile.mkstemp", return_value=(0, str(tmp_path / "t.jpg"))):
            with mock.patch("os.close"):
                with mock.patch("os.path.exists", return_value=True):
                    with mock.patch("os.unlink"):
                        with caplog.at_level(logging.WARNING):
                            result = pp.run(input_file, _stream(subtype="avi"), yt)

    assert result == input_file
    assert "not supported" in caplog.text.lower()


# ══════════════════════════════════════════════════════════════════════════════
# EmbedSubtitlePostProcessor
# ══════════════════════════════════════════════════════════════════════════════


def test_subtitle_skips_unsupported_container(fake_ffmpeg, tmp_path, caplog):
    import logging

    pp = EmbedSubtitlePostProcessor(lang_code="en")
    input_file = str(tmp_path / "video.avi")
    with caplog.at_level(logging.WARNING):
        result = pp.run(input_file, _stream(subtype="avi"), _yt())
    assert result == input_file
    assert "not supported" in caplog.text.lower()


def test_subtitle_skips_when_no_caption(fake_ffmpeg, tmp_path, caplog):
    import logging

    pp = EmbedSubtitlePostProcessor(lang_code="en")
    input_file = str(tmp_path / "video.mp4")
    yt = _yt()
    yt.captions = {}  # no captions

    # captions.get should return None for both "en" and "a.en"
    captions_mock = mock.MagicMock()
    captions_mock.get.return_value = None
    yt.captions = captions_mock

    with caplog.at_level(logging.WARNING):
        result = pp.run(input_file, _stream(), yt)
    assert result == input_file
    assert "no caption" in caplog.text.lower()


def test_subtitle_uses_auto_generated_fallback(fake_ffmpeg, tmp_path):
    pp = EmbedSubtitlePostProcessor(lang_code="en")
    input_file = tmp_path / "video.mp4"
    input_file.write_bytes(b"fake")

    auto_caption = mock.MagicMock()
    auto_caption.generate_srt_captions.return_value = (
        "1\n00:00:01,000 --> 00:00:02,000\nHello\n"
    )

    captions_mock = mock.MagicMock()
    captions_mock.get.side_effect = lambda key: auto_caption if key == "a.en" else None
    yt = _yt()
    yt.captions = captions_mock

    def fake_run(cmd, **kwargs):
        r = mock.MagicMock()
        r.returncode = 0
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch.object(
            pp, "_temp_path", return_value=str(tmp_path / "tmp.mp4")
        ):
            with mock.patch.object(pp, "_replace_file", return_value=str(input_file)):
                with mock.patch("os.fdopen", mock.mock_open()):
                    with mock.patch(
                        "tempfile.mkstemp", return_value=(0, str(tmp_path / "srt.srt"))
                    ):
                        with mock.patch("os.close"):
                            with mock.patch("os.path.exists", return_value=True):
                                with mock.patch("os.unlink"):
                                    pp.run(str(input_file), _stream(), yt)

    captions_mock.get.assert_any_call("a.en")


def test_subtitle_mp4_uses_mov_text_codec(fake_ffmpeg, tmp_path):
    pp = EmbedSubtitlePostProcessor(lang_code="en")
    input_file = tmp_path / "video.mp4"
    input_file.write_bytes(b"fake")

    caption = mock.MagicMock()
    caption.generate_srt_captions.return_value = (
        "1\n00:00:01,000 --> 00:00:02,000\nHi\n"
    )
    captions_mock = mock.MagicMock()
    captions_mock.get.side_effect = lambda key: caption if key == "en" else None
    yt = _yt()
    yt.captions = captions_mock

    captured_args = []

    def fake_run(cmd, **kwargs):
        captured_args.extend(cmd)
        r = mock.MagicMock()
        r.returncode = 0
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch.object(
            pp, "_temp_path", return_value=str(tmp_path / "tmp.mp4")
        ):
            with mock.patch.object(pp, "_replace_file", return_value=str(input_file)):
                with mock.patch("os.fdopen", mock.mock_open()):
                    with mock.patch(
                        "tempfile.mkstemp", return_value=(0, str(tmp_path / "s.srt"))
                    ):
                        with mock.patch("os.close"):
                            with mock.patch("os.path.exists", return_value=True):
                                with mock.patch("os.unlink"):
                                    pp.run(str(input_file), _stream(), yt)

    assert "mov_text" in captured_args


# ══════════════════════════════════════════════════════════════════════════════
# SponsorBlockPP
# ══════════════════════════════════════════════════════════════════════════════


def test_all_categories_list_not_empty():
    assert len(ALL_CATEGORIES) >= 10


def test_sponsorblock_invalid_mode_raises(fake_ffmpeg):
    with pytest.raises(PostProcessorError, match="mode must be"):
        SponsorBlockPP(mode="delete")


def test_sponsorblock_default_category_is_sponsor(fake_ffmpeg):
    pp = SponsorBlockPP()
    assert pp._categories == ["sponsor"]


def test_sponsorblock_no_segments_returns_original(fake_ffmpeg, tmp_path):
    pp = SponsorBlockPP(mode="mark")
    input_file = str(tmp_path / "video.mp4")
    yt = _yt()

    with mock.patch("pyt.postprocessors.sponsorblock._fetch_segments", return_value=[]):
        result = pp.run(input_file, _stream(), yt)
    assert result == input_file


def test_segments_to_chapters_converts_ms():
    segments = [
        {"segment": [10.5, 30.0], "category": "sponsor"},
        {"segment": [60.0, 90.0], "category": "intro"},
    ]
    chapters = SponsorBlockPP._segments_to_chapters(segments)
    assert chapters[0]["start_ms"] == 10_500
    assert chapters[0]["end_ms"] == 30_000
    assert chapters[0]["title"] == "[SponsorBlock] Sponsor"
    assert chapters[1]["title"] == "[SponsorBlock] Intro"


def test_segments_to_chapters_sorted_by_start():
    segments = [
        {"segment": [60.0, 90.0], "category": "outro"},
        {"segment": [5.0, 15.0], "category": "intro"},
    ]
    chapters = SponsorBlockPP._segments_to_chapters(segments)
    assert chapters[0]["start_ms"] < chapters[1]["start_ms"]


def test_segments_to_chapters_unknown_category_title_cased():
    segments = [{"segment": [0.0, 1.0], "category": "mystery_cat"}]
    chapters = SponsorBlockPP._segments_to_chapters(segments)
    assert "Mystery_Cat" in chapters[0]["title"]


def test_mark_mode_writes_ffmetadata_and_calls_ffmpeg(fake_ffmpeg, tmp_path):
    pp = SponsorBlockPP(mode="mark")
    input_file = tmp_path / "video.mp4"
    input_file.write_bytes(b"fake")

    segments = [{"segment": [10.0, 30.0], "category": "sponsor"}]
    captured_args = []

    def fake_run(cmd, **kwargs):
        captured_args.extend(cmd)
        r = mock.MagicMock()
        r.returncode = 0
        return r

    with mock.patch(
        "pyt.postprocessors.sponsorblock._fetch_segments", return_value=segments
    ):
        with mock.patch("subprocess.run", side_effect=fake_run):
            with mock.patch.object(
                pp, "_temp_path", return_value=str(tmp_path / "tmp.mp4")
            ):
                with mock.patch.object(
                    pp, "_replace_file", return_value=str(input_file)
                ):
                    pp.run(str(input_file), _stream(), _yt())

    assert "-map_chapters" in captured_args
    assert "-codec" in captured_args
    assert "copy" in captured_args


def test_remove_mode_builds_select_filter(fake_ffmpeg, tmp_path):
    pp = SponsorBlockPP(mode="remove", categories=["sponsor"])
    input_file = tmp_path / "video.mp4"
    input_file.write_bytes(b"fake")

    segments = [{"segment": [10.0, 30.0], "category": "sponsor"}]
    captured_args = []

    def fake_run(cmd, **kwargs):
        captured_args.extend(cmd)
        r = mock.MagicMock()
        r.returncode = 0
        return r

    with mock.patch(
        "pyt.postprocessors.sponsorblock._fetch_segments", return_value=segments
    ):
        with mock.patch("subprocess.run", side_effect=fake_run):
            with mock.patch.object(
                pp, "_temp_path", return_value=str(tmp_path / "tmp.mp4")
            ):
                with mock.patch.object(
                    pp, "_replace_file", return_value=str(input_file)
                ):
                    pp.run(str(input_file), _stream(), _yt())

    # -vf and -af should be present for the select filter
    assert "-vf" in captured_args
    assert "-af" in captured_args
    filter_str = captured_args[captured_args.index("-vf") + 1]
    assert "between(t,10.0,30.0)" in filter_str


def test_remove_mode_multiple_segments_combined(fake_ffmpeg, tmp_path):
    pp = SponsorBlockPP(mode="remove")
    input_file = tmp_path / "video.mp4"
    input_file.write_bytes(b"fake")

    segments = [
        {"segment": [10.0, 30.0], "category": "sponsor"},
        {"segment": [60.0, 90.0], "category": "intro"},
    ]
    captured_args = []

    def fake_run(cmd, **kwargs):
        captured_args.extend(cmd)
        r = mock.MagicMock()
        r.returncode = 0
        return r

    with mock.patch(
        "pyt.postprocessors.sponsorblock._fetch_segments", return_value=segments
    ):
        with mock.patch("subprocess.run", side_effect=fake_run):
            with mock.patch.object(
                pp, "_temp_path", return_value=str(tmp_path / "tmp.mp4")
            ):
                with mock.patch.object(
                    pp, "_replace_file", return_value=str(input_file)
                ):
                    pp.run(str(input_file), _stream(), _yt())

    filter_str = captured_args[captured_args.index("-vf") + 1]
    assert "between(t,10.0,30.0)" in filter_str
    assert "between(t,60.0,90.0)" in filter_str


# ── _fetch_segments ───────────────────────────────────────────────────────────


def test_fetch_segments_returns_empty_on_404():
    resp = mock.MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = mock.MagicMock(return_value=False)
    resp.status = 404

    with mock.patch("urllib.request.urlopen", return_value=resp):
        result = _fetch_segments("vid123", ["sponsor"])
    assert result == []


def test_fetch_segments_returns_empty_on_exception():
    with mock.patch("urllib.request.urlopen", side_effect=Exception("network error")):
        result = _fetch_segments("vid123", ["sponsor"])
    assert result == []


def test_fetch_segments_parses_json():
    import json

    payload = [{"segment": [10.0, 30.0], "category": "sponsor"}]
    resp = mock.MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = mock.MagicMock(return_value=False)
    resp.status = 200
    resp.read.return_value = json.dumps(payload).encode("utf-8")

    with mock.patch("urllib.request.urlopen", return_value=resp):
        result = _fetch_segments("vid123", ["sponsor"])
    assert result == payload

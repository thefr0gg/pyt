"""Tests for pyt.template.OutputTemplate."""

import os
from datetime import datetime
from unittest import mock

import pytest

from pyt.template import OutputTemplate, DEFAULT_TEMPLATE

# ── helpers ──────────────────────────────────────────────────────────────────


def _yt(
    title="My Video",
    author="Chan",
    video_id="abc123",
    channel_id="UCxyz",
    publish_date=None,
):
    yt = mock.MagicMock()
    yt.title = title
    yt.author = author
    yt.video_id = video_id
    yt.channel_id = channel_id
    yt.publish_date = publish_date or datetime(2024, 3, 15)
    return yt


def _stream(
    subtype="mp4", resolution="1080p", fps=30, abr="128kbps", filesize_approx=10_000_000
):
    s = mock.MagicMock()
    s.subtype = subtype
    s.resolution = resolution
    s.fps = fps
    s.abr = abr
    s.filesize_approx = filesize_approx
    return s


# ── default template ──────────────────────────────────────────────────────────


def test_default_template_constant():
    assert DEFAULT_TEMPLATE == "{title}.{ext}"


def test_default_template_renders_title_and_ext():
    t = OutputTemplate()
    result = t.render(_yt(title="Hello World"), _stream(subtype="mp4"))
    assert result == "Hello World.mp4"


# ── individual field substitution ────────────────────────────────────────────


@pytest.mark.parametrize(
    "field,expected",
    [
        ("{title}.{ext}", "My Title.mp4"),
        ("{author}.{ext}", "Artist.mp4"),
        ("{id}.{ext}", "vid001.mp4"),
        ("{channel_id}.{ext}", "UC123.mp4"),
        ("{resolution}.{ext}", "720p.mp4"),
        ("{fps}.{ext}", "25.mp4"),
        ("{abr}.{ext}", "192kbps.mp4"),
    ],
)
def test_single_field(field, expected):
    yt = _yt(title="My Title", author="Artist", video_id="vid001", channel_id="UC123")
    stream = _stream(subtype="mp4", resolution="720p", fps=25, abr="192kbps")
    assert OutputTemplate(field).render(yt, stream) == expected


def test_upload_date_default_format():
    t = OutputTemplate("{upload_date}.{ext}")
    result = t.render(_yt(publish_date=datetime(2024, 3, 15)), _stream())
    assert result == "20240315.mp4"


def test_upload_date_custom_strftime():
    t = OutputTemplate("{upload_date:%Y-%m-%d}.{ext}")
    result = t.render(_yt(publish_date=datetime(2024, 3, 15)), _stream())
    assert result == "2024-03-15.mp4"


def test_upload_date_year_month_format():
    t = OutputTemplate("{upload_date:%Y/%m} - {title}.{ext}")
    result = t.render(_yt(title="Song", publish_date=datetime(2024, 3, 15)), _stream())
    assert result == os.path.join("2024", "03 - Song.mp4")


def test_filesize_field():
    t = OutputTemplate("{filesize}")
    result = t.render(_yt(), _stream(filesize_approx=52_428_800))
    assert result == "52428800"


# ── unknown fields left as-is ─────────────────────────────────────────────────


def test_unknown_field_preserved():
    t = OutputTemplate("{nonexistent}.{ext}")
    result = t.render(_yt(), _stream())
    assert result == "{nonexistent}.mp4"


# ── unsafe character sanitization ────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_title,safe",
    [
        ("Hello: World", "Hello_ World"),
        ("Test*File", "Test_File"),
        ("  leading", "leading"),  # leading space stripped
    ],
)
def test_unsafe_chars_sanitized(bad_title, safe):
    t = OutputTemplate("{title}.{ext}")
    result = t.render(_yt(title=bad_title), _stream(subtype="mp4"))
    assert result == f"{safe}.mp4"


def test_slash_in_title_creates_subdirectory():
    # A literal / in a field value becomes a path separator, not underscore.
    t = OutputTemplate("{title}.{ext}")
    result = t.render(_yt(title="A/B"), _stream(subtype="mp4"))
    assert result == os.path.join("A", "B.mp4")


def test_trailing_dot_in_title_preserved_mid_string():
    # "trailing." + ".mp4" → "trailing..mp4"; the dot is not at the end of
    # the path component, so strip(" .") leaves it untouched.
    t = OutputTemplate("{title}.{ext}")
    result = t.render(_yt(title="trailing."), _stream(subtype="mp4"))
    assert result == "trailing..mp4"


def test_all_dots_title_strips_to_extension_only():
    # "..." becomes the prefix; strip(" .") removes all leading dots from
    # "....mp4" → "mp4".  The component is non-empty so no "_" fallback.
    t = OutputTemplate("{title}.{ext}")
    result = t.render(_yt(title="..."), _stream(subtype="mp4"))
    assert result == "mp4"


# ── directory separators create sub-paths ────────────────────────────────────


def test_directory_separator_creates_subpath():
    t = OutputTemplate("{author}/{title}.{ext}")
    result = t.render(_yt(title="Song", author="Artist"), _stream())
    assert result == os.path.join("Artist", "Song.mp4")


def test_nested_directories():
    t = OutputTemplate("{upload_date:%Y}/{upload_date:%m}/{title}.{ext}")
    result = t.render(_yt(title="T", publish_date=datetime(2024, 3, 15)), _stream())
    assert result == os.path.join("2024", "03", "T.mp4")


# ── playlist fields ───────────────────────────────────────────────────────────


def test_playlist_index_zero_padded():
    t = OutputTemplate("{playlist_index}. {title}.{ext}")
    result = t.render(_yt(title="EP"), _stream(), playlist_index=7)
    assert result == "07. EP.mp4"


def test_playlist_title_field():
    t = OutputTemplate("{playlist_title}/{title}.{ext}")
    result = t.render(_yt(title="EP"), _stream(), playlist_title="My Playlist")
    assert result == os.path.join("My Playlist", "EP.mp4")


# ── missing youtube attributes don't crash ───────────────────────────────────


def test_missing_youtube_attrs_skip_gracefully():
    yt = mock.MagicMock()
    yt.title = "OK"
    yt.author = None
    yt.video_id = None
    yt.channel_id = None
    yt.publish_date = None
    stream = _stream()
    # Should not raise; unknown/None fields are simply omitted or preserved
    result = OutputTemplate("{title} - {author}.{ext}").render(yt, stream)
    assert "OK" in result

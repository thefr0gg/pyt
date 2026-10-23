"""Tests for pyt.config — load_config and apply_config."""

import argparse
import os

import pytest

from pyt.config import load_config, apply_config, _LOCATIONS

# ── load_config ───────────────────────────────────────────────────────────────


def test_load_config_returns_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Patch all search locations to non-existent paths inside tmp_path
    monkeypatch.setattr(
        "pyt.config._LOCATIONS",
        [
            str(tmp_path / "pyt.conf"),
            str(tmp_path / "xdg" / "pyt.conf"),
            str(tmp_path / ".pyt.conf"),
        ],
    )
    result = load_config()
    assert result == {}


def test_load_config_reads_default_section(tmp_path, monkeypatch):
    conf = tmp_path / "pyt.conf"
    conf.write_text(
        "[default]\ntarget = ~/Downloads\nembed_metadata = true\n", encoding="utf-8"
    )
    monkeypatch.setattr("pyt.config._LOCATIONS", [str(conf)])
    result = load_config()
    assert result["target"] == "~/Downloads"
    assert result["embed_metadata"] == "true"


def test_load_config_uses_first_found_file(tmp_path, monkeypatch):
    first = tmp_path / "first.conf"
    second = tmp_path / "second.conf"
    first.write_text("[default]\ntarget = first\n", encoding="utf-8")
    second.write_text("[default]\ntarget = second\n", encoding="utf-8")
    monkeypatch.setattr("pyt.config._LOCATIONS", [str(first), str(second)])
    result = load_config()
    assert result["target"] == "first"


def test_load_config_skips_missing_and_uses_second(tmp_path, monkeypatch):
    missing = tmp_path / "missing.conf"
    present = tmp_path / "present.conf"
    present.write_text("[default]\ntarget = here\n", encoding="utf-8")
    monkeypatch.setattr("pyt.config._LOCATIONS", [str(missing), str(present)])
    result = load_config()
    assert result["target"] == "here"


def test_load_config_no_default_section_returns_empty(tmp_path, monkeypatch):
    conf = tmp_path / "pyt.conf"
    conf.write_text("[other]\nfoo = bar\n", encoding="utf-8")
    monkeypatch.setattr("pyt.config._LOCATIONS", [str(conf)])
    result = load_config()
    assert result == {}


# ── apply_config ──────────────────────────────────────────────────────────────


def _args(**kwargs):
    ns = argparse.Namespace(
        target=None,
        output=None,
        verbose=False,
        embed_metadata=False,
        embed_thumbnail=False,
        embed_subs=False,
        extract_audio=False,
        audio_format=None,
        proxy=None,
        cookies=None,
        cookies_from_browser=None,
        download_archive=None,
        batch_file=None,
        caption_code=None,
        resolution=None,
        audio=None,
        ffmpeg=None,
        sponsorblock_mark=None,
        sponsorblock_remove=None,
        sleep_interval=None,
        max_sleep_interval=None,
        geo_bypass=False,
        geo_bypass_country=None,
        itag=None,
    )
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def test_apply_config_sets_string_value():
    args = _args()
    apply_config(args, {"target": "~/Downloads"})
    assert args.target == "~/Downloads"


def test_apply_config_does_not_override_explicit_cli_value():
    args = _args(target="/cli/target")
    apply_config(args, {"target": "~/Downloads"})
    assert args.target == "/cli/target"


def test_apply_config_bool_true_values():
    for value in ("true", "yes", "1"):
        args = _args()
        apply_config(args, {"embed_metadata": value})
        assert args.embed_metadata is True, f"Failed for value={value!r}"


def test_apply_config_bool_false_values():
    for value in ("false", "no", "0"):
        args = _args()
        apply_config(args, {"embed_metadata": value})
        assert args.embed_metadata is False, f"Failed for value={value!r}"


def test_apply_config_does_not_override_truthy_bool():
    args = _args(embed_metadata=True)
    apply_config(args, {"embed_metadata": "false"})
    assert args.embed_metadata is True


def test_apply_config_multiple_keys():
    args = _args()
    apply_config(
        args,
        {
            "target": "~/Music",
            "audio_format": "mp3",
            "embed_metadata": "true",
            "sleep_interval": "2",
        },
    )
    assert args.target == "~/Music"
    assert args.audio_format == "mp3"
    assert args.embed_metadata is True
    assert args.sleep_interval == "2"


def test_apply_config_hyphen_key_normalised():
    args = _args()
    apply_config(args, {"geo-bypass-country": "US"})
    assert args.geo_bypass_country == "US"


def test_apply_config_unknown_key_ignored():
    args = _args()
    apply_config(args, {"totally_unknown_key": "value"})
    # Should not raise; simply skip the unknown key

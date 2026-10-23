"""Configuration file support for pyt."""

from __future__ import annotations

import configparser
import os

_LOCATIONS = [
    "./pyt.conf",
    os.path.expanduser("~/.config/pyt/pyt.conf"),
    os.path.expanduser("~/.pyt.conf"),
]

# CLI flag names that are boolean toggles
_BOOL_FLAGS = {
    "verbose",
    "list",
    "list_captions",
    "build_playback_report",
    "extract_audio",
    "embed_metadata",
    "embed_thumbnail",
    "embed_subs",
    "geo_bypass",
}

# CLI flag names that are string values
_STR_FLAGS = {
    "target",
    "output",
    "audio_format",
    "proxy",
    "cookies",
    "cookies_from_browser",
    "download_archive",
    "batch_file",
    "caption_code",
    "resolution",
    "audio",
    "ffmpeg",
    "sponsorblock_mark",
    "sponsorblock_remove",
    "sleep_interval",
    "max_sleep_interval",
    "geo_bypass_country",
    "itag",
}


def load_config() -> dict:
    """Load pyt settings from the first found config file.

    Config files use INI format with a ``[default]`` section::

        [default]
        target = ~/Downloads
        embed_metadata = true
        sponsorblock_mark = sponsor,intro,outro

    Returns an empty dict if no config file exists.
    """
    cfg = configparser.ConfigParser()
    for path in _LOCATIONS:
        if os.path.isfile(path):
            cfg.read(path, encoding="utf-8")
            break
    return dict(cfg["default"]) if "default" in cfg else {}


def apply_config(args, config: dict) -> None:
    """Merge *config* into *args*, letting explicit CLI values take precedence.

    A flag is only overridden from config when its current value is falsy
    (``None``, ``False``, or empty string).
    """
    for raw_key, value in config.items():
        attr = raw_key.replace("-", "_")
        current = getattr(args, attr, None)
        if current:
            continue  # explicit CLI value wins
        if attr in _BOOL_FLAGS:
            setattr(args, attr, value.lower() in ("true", "yes", "1"))
        elif attr in _STR_FLAGS:
            setattr(args, attr, value)

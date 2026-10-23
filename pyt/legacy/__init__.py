"""pyt.legacy — v2 compatibility shims.

These re-export the legacy pytube-era classes from their original modules.
They are preserved here through v3; plan to remove in v4.

Migration guide: https://github.com/ramsy0dev/pyt/docs/migration-v3.md

Usage::

    # Old (top-level) — no longer works in v3:
    from pyt import YouTube, Stream, Playlist

    # Shim (works through v3, removed in v4):
    from pyt.legacy import YouTube, Stream, Caption, Playlist, Channel, Search

    # New (permanent):
    from pyt import Client
    video = Client().video(url)
"""

import warnings as _warnings


def _warn(name: str) -> None:
    _warnings.warn(
        f"pyt.legacy.{name} is a v2 compatibility shim and will be removed in v4. "
        "Migrate to pyt.Client — see https://github.com/ramsy0dev/pyt/docs/migration-v3.md",
        DeprecationWarning,
        stacklevel=3,
    )


def __getattr__(name: str):
    if name == "YouTube":
        _warn(name)
        from pyt.__main__ import YouTube

        return YouTube
    if name == "Stream":
        _warn(name)
        from pyt.streams import Stream

        return Stream
    if name == "Caption":
        _warn(name)
        from pyt.captions import Caption

        return Caption
    if name in ("StreamQuery", "CaptionQuery"):
        _warn(name)
        from pyt.query import StreamQuery, CaptionQuery

        return StreamQuery if name == "StreamQuery" else CaptionQuery
    if name == "Monostate":
        _warn(name)
        from pyt.monostate import Monostate

        return Monostate
    if name == "Playlist":
        _warn(name)
        from pyt.contrib.playlist import Playlist

        return Playlist
    if name == "Channel":
        _warn(name)
        from pyt.contrib.channel import Channel

        return Channel
    if name == "Search":
        _warn(name)
        from pyt.contrib.search import Search

        return Search
    raise AttributeError(f"module 'pyt.legacy' has no attribute {name!r}")

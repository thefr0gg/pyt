"""Modern :class:`Playlist`, :class:`ChannelFeed`, and :class:`SearchResults`.

:class:`Playlist` and :class:`ChannelFeed` fetch natively via InnerTube's
``browse`` endpoint (see :mod:`pyt.api._browse`) — they no longer
delegate to the legacy ``pyt.contrib.{playlist,channel}`` HTML scrapers,
which targeted renderer shapes (``gridRenderer``/``playlistVideoRenderer``)
YouTube has since replaced with ``lockupViewModel`` items. That legacy
code silently returned empty results against current responses rather
than raising, which is the bug this rewrite fixes.

:class:`SearchResults` still wraps the legacy ``pyt.contrib.search``
scraper; it wasn't part of this pass and may have the same kind of
staleness — not yet confirmed.

Iterating ``ChannelFeed``/``Playlist`` yields modern :class:`Video`
objects lazily: one HTTP fetch per video at iteration time, same as
before — only how the list of ids is obtained changed.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Iterator, List, Optional

from pyt.api._browse import (
    iter_channel_videos,
    iter_playlist_videos,
    playlist_video_count,
    resolve_channel_id,
)
from pyt.innertube import InnerTube

if TYPE_CHECKING:
    from pyt.api.client import Client
    from pyt.api.video import Video


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


class Playlist:
    """A YouTube playlist. Construct via :meth:`Client.playlist`."""

    def __init__(self, *, playlist_id: str, client: "Client"):
        self._playlist_id = playlist_id
        self._client = client
        self._innertube: Optional[InnerTube] = None
        self._page: Optional[dict] = None

    @classmethod
    def _from_url(cls, url: str, *, client: "Client") -> "Playlist":
        from pyt import extract

        playlist_id = extract.playlist_id(url)
        return cls(playlist_id=playlist_id, client=client)

    # ── internals ──────────────────────────────────────────────────────────

    def _it(self) -> InnerTube:
        if self._innertube is None:
            self._innertube = InnerTube(client="WEB")
        return self._innertube

    def _browse_id(self) -> str:
        pid = self._playlist_id
        return pid if pid.startswith("VL") else f"VL{pid}"

    def _fetch_page(self) -> dict:
        if self._page is None:
            self._page = self._it().browse(self._browse_id())
        return self._page

    # ── metadata ────────────────────────────────────────────────────────────

    @property
    def playlist_id(self) -> str:
        return self._playlist_id

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/playlist?list={self._playlist_id}"

    @property
    def title(self) -> Optional[str]:
        meta = (
            self._fetch_page().get("metadata", {}).get("playlistMetadataRenderer", {})
        )
        return meta.get("title")

    @property
    def description(self) -> str:
        meta = (
            self._fetch_page().get("metadata", {}).get("playlistMetadataRenderer", {})
        )
        return meta.get("description", "")

    @property
    def length(self) -> Optional[int]:
        """Number of videos in the playlist, from the sidebar's stats
        line. ``None`` if unavailable."""
        return playlist_video_count(self._playlist_id, innertube=self._it())

    @property
    def owner(self) -> Optional[str]:
        items = (
            self._fetch_page()
            .get("sidebar", {})
            .get("playlistSidebarRenderer", {})
            .get("items", [])
        )
        for item in items:
            secondary = item.get("playlistSidebarSecondaryInfoRenderer")
            if secondary is None:
                continue
            runs = (
                secondary.get("videoOwner", {})
                .get("videoOwnerRenderer", {})
                .get("title", {})
                .get("runs", [])
            )
            if runs:
                return runs[0].get("text")
        return None

    @property
    def owner_url(self) -> Optional[str]:
        items = (
            self._fetch_page()
            .get("sidebar", {})
            .get("playlistSidebarRenderer", {})
            .get("items", [])
        )
        for item in items:
            secondary = item.get("playlistSidebarSecondaryInfoRenderer")
            if secondary is None:
                continue
            runs = (
                secondary.get("videoOwner", {})
                .get("videoOwnerRenderer", {})
                .get("title", {})
                .get("runs", [])
            )
            if runs:
                browse_id = (
                    runs[0]
                    .get("navigationEndpoint", {})
                    .get("browseEndpoint", {})
                    .get("browseId")
                )
                canonical = (
                    runs[0]
                    .get("navigationEndpoint", {})
                    .get("browseEndpoint", {})
                    .get("canonicalBaseUrl")
                )
                if canonical:
                    return f"https://www.youtube.com{canonical}"
                if browse_id:
                    return f"https://www.youtube.com/channel/{browse_id}"
        return None

    @property
    def video_urls(self) -> List[str]:
        return [
            _watch_url(v["video_id"])
            for v in iter_playlist_videos(self._playlist_id, innertube=self._it())
        ]

    # ── iteration ───────────────────────────────────────────────────────────

    def __iter__(self) -> Iterator["Video"]:
        return self.videos()

    def videos(self) -> Iterator["Video"]:
        """Yield each :class:`Video` lazily; one HTTP fetch per iteration."""
        for v in iter_playlist_videos(self._playlist_id, innertube=self._it()):
            yield self._client.video(_watch_url(v["video_id"]))

    def __len__(self) -> int:
        length = self.length
        if length is None:
            raise TypeError(f"{self!r} has no known length")
        return length

    def __repr__(self) -> str:
        return f"<Playlist id={self.playlist_id!r} title={self.title!r}>"

    # ── legacy escape hatch ─────────────────────────────────────────────────

    @property
    def legacy(self):
        """The pytube-era :class:`pyt.contrib.playlist.Playlist`, built
        lazily on first access. Not used by any property above; kept
        only for callers that relied on scraper-specific attributes."""
        from pyt.contrib.playlist import Playlist as _LegacyPlaylist

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return _LegacyPlaylist(self.url, proxies=self._client._proxies_dict())


class ChannelFeed(Playlist):
    """A channel's "Videos" feed. Behaves like a :class:`Playlist`."""

    def __init__(self, *, channel_id: str, client: "Client"):
        super().__init__(playlist_id=channel_id, client=client)
        self._channel_id = channel_id
        self._home: Optional[dict] = None

    @classmethod
    def _from_url(cls, url: str, *, client: "Client") -> "ChannelFeed":
        channel_id = resolve_channel_id(url)
        return cls(channel_id=channel_id, client=client)

    def _fetch_home(self) -> dict:
        if self._home is None:
            self._home = self._it().browse(self._channel_id)
        return self._home

    @property
    def channel_id(self) -> Optional[str]:
        return self._channel_id

    @property
    def name(self) -> Optional[str]:
        meta = self._fetch_home().get("metadata", {}).get("channelMetadataRenderer", {})
        return meta.get("title")

    @property
    def title(self) -> Optional[str]:
        return self.name

    @property
    def description(self) -> str:
        meta = self._fetch_home().get("metadata", {}).get("channelMetadataRenderer", {})
        return meta.get("description", "")

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/channel/{self._channel_id}"

    @property
    def length(self) -> Optional[int]:
        """Number of uploaded videos is not exposed on a channel's home
        page the way it is on a playlist's sidebar; always ``None``."""
        return None

    @property
    def owner(self) -> Optional[str]:
        return self.name

    @property
    def owner_url(self) -> Optional[str]:
        return self.url

    @property
    def video_urls(self) -> List[str]:
        return [
            _watch_url(v["video_id"])
            for v in iter_channel_videos(self._channel_id, innertube=self._it())
        ]

    def videos(self) -> Iterator["Video"]:
        for v in iter_channel_videos(self._channel_id, innertube=self._it()):
            yield self._client.video(_watch_url(v["video_id"]))

    def __repr__(self) -> str:
        return f"<ChannelFeed id={self.channel_id!r} name={self.name!r}>"

    @property
    def legacy(self):
        """The pytube-era :class:`pyt.contrib.channel.Channel`, built
        lazily on first access. Its ``.videos``/``.video_urls`` are
        unreliable against current YouTube (see module docstring) —
        kept only for callers that relied on other scraper-specific
        attributes."""
        from pyt.contrib.channel import Channel as _LegacyChannel

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return _LegacyChannel(self.url, proxies=self._client._proxies_dict())


class SearchResults:
    """A YouTube search result set.

    :class:`SearchResults` is *not* a :class:`Playlist` — search results
    can mix videos, channels, and playlists, and YouTube returns them
    paged via continuation tokens. We expose the first page as
    :attr:`videos`; advanced paging is on the legacy object.
    """

    def __init__(self, *, legacy, client: "Client"):
        self._legacy = legacy
        self._client = client

    @classmethod
    def _from_query(cls, query: str, *, client: "Client") -> "SearchResults":
        from pyt.contrib.search import Search as _LegacySearch

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy = _LegacySearch(query)
        return cls(legacy=legacy, client=client)

    @property
    def query(self) -> str:
        return self._legacy.query

    @property
    def videos(self) -> List["Video"]:
        """First page of video results, materialized as :class:`Video`."""
        out: List["Video"] = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            raw = list(self._legacy.results or [])
        for legacy_yt in raw:
            try:
                out.append(self._client.video(legacy_yt.watch_url))
            except Exception:
                continue
        return out

    @property
    def legacy(self):
        return self._legacy

    def __repr__(self) -> str:
        return f"<SearchResults query={self.query!r}>"

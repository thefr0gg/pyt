"""Native InnerTube channel/playlist listing.

Replaces the legacy pyt.contrib.channel / pyt.contrib.playlist HTML
scrapers, which target renderer shapes (gridRenderer/gridVideoRenderer,
playlistVideoListRenderer/playlistVideoRenderer) YouTube has since moved
off of. Current responses for both a channel's Videos tab and a
playlist page use ``lockupViewModel`` items instead, with two different
continuation-item shapes depending on the page:

- Channel Videos tab: ``richGridRenderer`` contents, each item a
  ``richItemRenderer.content.lockupViewModel``; continuation as a
  trailing ``continuationItemRenderer.continuationEndpoint
  .continuationCommand.token``.
- Playlist page: ``sectionListRenderer`` -> ``itemSectionRenderer``
  contents, each item a bare ``lockupViewModel``; continuation as a
  trailing ``continuationItemViewModel.continuationCommand
  .innertubeCommand.continuationCommand.token``.

Both shapes were confirmed against live traffic, not derived from
documentation — YouTube's internal renderers aren't versioned or
publicly specified, so this will need updating again if they change.

The ``browse`` continuation response envelope is the same for both:
``onResponseReceivedActions[0].appendContinuationItemsAction
.continuationItems``.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Iterator, List, Optional

from pyt.innertube import InnerTube

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^\d{1,2}(:\d{2}){1,2}$")


def resolve_channel_id(url: str, *, innertube: Optional[InnerTube] = None) -> str:
    """Resolve any channel URL form (``/@handle``, ``/channel/UC...``,
    ``/c/name``, ``/user/name``) to the canonical ``UC...`` channel id."""
    it = innertube or InnerTube(client="WEB")
    resolved = it.resolve_url(url)
    endpoint = resolved.get("endpoint", {})
    browse_id = endpoint.get("browseEndpoint", {}).get("browseId")
    if not browse_id:
        raise ValueError(f"could not resolve channel id from {url!r}")
    return browse_id


def _videos_tab_params(channel_id: str, innertube: InnerTube) -> Optional[str]:
    """Find the Videos tab's opaque ``params`` from the channel's home
    page tab list. ``None`` if the channel has no Videos tab."""
    home = innertube.browse(channel_id)
    tabs = (
        home.get("contents", {})
        .get("twoColumnBrowseResultsRenderer", {})
        .get("tabs", [])
    )
    for tab in tabs:
        renderer = tab.get("tabRenderer", {})
        if renderer.get("title") == "Videos":
            endpoint = renderer.get("endpoint", {})
            return endpoint.get("browseEndpoint", {}).get("params")
    return None


def _text_from(obj) -> Optional[str]:
    if obj is None:
        return None
    if "content" in obj:
        return obj["content"]
    if "simpleText" in obj:
        return obj["simpleText"]
    runs = obj.get("runs")
    if runs:
        return "".join(r.get("text", "") for r in runs)
    return None


def _duration_from_lockup(lockup: dict) -> Optional[str]:
    """Pull the "mm:ss"/"h:mm:ss" duration text from a lockup's
    thumbnail overlay badge, if present. Live/premiere items have no
    duration badge."""
    overlays = (
        lockup.get("contentImage", {}).get("thumbnailViewModel", {}).get("overlays", [])
    )
    for overlay in overlays:
        badges = overlay.get("thumbnailBottomOverlayViewModel", {}).get("badges", [])
        for badge in badges:
            text = badge.get("thumbnailBadgeViewModel", {}).get("text")
            if text and _DURATION_RE.match(text):
                return text
    return None


def _video_from_lockup(lockup: dict) -> Optional[dict]:
    """Extract ``{video_id, title, duration_text}`` from a
    ``lockupViewModel``, or ``None`` if it isn't a video (a lockup can
    also represent a playlist or channel in mixed listings)."""
    if lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
        return None
    video_id = lockup.get("contentId")
    if not video_id:
        return None
    meta = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = _text_from(meta.get("title"))
    return {
        "video_id": video_id,
        "title": title,
        "duration_text": _duration_from_lockup(lockup),
    }


def _walk_items(items: List[dict]) -> Iterator[dict]:
    """Yield video dicts from a list of richItemRenderer/lockupViewModel
    items, skipping continuation markers and non-video lockups."""
    for item in items:
        lockup = None
        if "richItemRenderer" in item:
            lockup = item["richItemRenderer"].get("content", {}).get("lockupViewModel")
        elif "lockupViewModel" in item:
            lockup = item["lockupViewModel"]
        if lockup is None:
            continue
        video = _video_from_lockup(lockup)
        if video is not None:
            yield video


def _continuation_token(items: List[dict]) -> Optional[str]:
    """Pull a continuation token from the trailing item of a content
    list, trying both known continuation-item shapes."""
    if not items:
        return None
    last = items[-1]
    renderer = last.get("continuationItemRenderer")
    if renderer is not None:
        return (
            renderer.get("continuationEndpoint", {})
            .get("continuationCommand", {})
            .get("token")
        )
    view_model = last.get("continuationItemViewModel")
    if view_model is not None:
        return (
            view_model.get("continuationCommand", {})
            .get("innertubeCommand", {})
            .get("continuationCommand", {})
            .get("token")
        )
    return None


def _continuation_items(response: dict) -> List[dict]:
    actions = response.get("onResponseReceivedActions", [])
    if not actions:
        return []
    return (
        actions[0].get("appendContinuationItemsAction", {}).get("continuationItems", [])
    )


def iter_channel_videos(
    channel_id: str, *, innertube: Optional[InnerTube] = None
) -> Iterator[dict]:
    """Yield ``{video_id, title, duration_text}`` dicts for every video
    on a channel's Videos tab, paging via continuation tokens."""
    it = innertube or InnerTube(client="WEB")
    params = _videos_tab_params(channel_id, it)
    if params is None:
        logger.debug("_browse: channel %s has no Videos tab", channel_id)
        return

    page = it.browse(channel_id, params=params)
    items = (
        page.get("contents", {})
        .get("twoColumnBrowseResultsRenderer", {})
        .get("tabs", [{}])[1]
        .get("tabRenderer", {})
        .get("content", {})
        .get("richGridRenderer", {})
        .get("contents", [])
    )
    yield from _walk_items(items)
    token = _continuation_token(items)

    while token:
        page = it.browse(continuation=token)
        items = _continuation_items(page)
        yield from _walk_items(items)
        token = _continuation_token(items)


def iter_playlist_videos(
    playlist_id: str, *, innertube: Optional[InnerTube] = None
) -> Iterator[dict]:
    """Yield ``{video_id, title, duration_text}`` dicts for every video
    in a playlist, paging via continuation tokens.

    ``playlist_id`` is the bare id (no ``VL`` prefix) as it appears in
    a ``?list=`` URL parameter or a playlist's own id field.
    """
    it = innertube or InnerTube(client="WEB")
    browse_id = playlist_id if playlist_id.startswith("VL") else f"VL{playlist_id}"

    page = it.browse(browse_id)
    sections = (
        page.get("contents", {})
        .get("twoColumnBrowseResultsRenderer", {})
        .get("tabs", [{}])[0]
        .get("tabRenderer", {})
        .get("content", {})
        .get("sectionListRenderer", {})
        .get("contents", [])
    )
    items: List[dict] = []
    for section in sections:
        items.extend(section.get("itemSectionRenderer", {}).get("contents", []))
    yield from _walk_items(items)
    token = _continuation_token(items)

    while token:
        page = it.browse(continuation=token)
        items = _continuation_items(page)
        yield from _walk_items(items)
        token = _continuation_token(items)


def playlist_video_count(
    playlist_id: str, *, innertube: Optional[InnerTube] = None
) -> Optional[int]:
    """Best-effort video count from a playlist's sidebar stats.
    ``None`` if unavailable (e.g. a channel's uploads feed with no
    sidebar, or an unexpected stats format)."""
    it = innertube or InnerTube(client="WEB")
    browse_id = playlist_id if playlist_id.startswith("VL") else f"VL{playlist_id}"
    page = it.browse(browse_id)
    items = page.get("sidebar", {}).get("playlistSidebarRenderer", {}).get("items", [])
    if not items:
        return None
    stats = items[0].get("playlistSidebarPrimaryInfoRenderer", {}).get("stats", [])
    if not stats:
        return None
    text = _text_from(stats[0])
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None

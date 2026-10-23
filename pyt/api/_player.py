"""Internal: fetch a YouTube player response without the legacy YouTube class."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

from pyt import extract, request
from pyt.innertube import InnerTube, _default_clients

logger = logging.getLogger(__name__)

_PLAYER_CLIENT_PRIORITY = ["ANDROID_VR", "ANDROID", "IOS", "TV_EMBED"]
_WEB_CLIENTS = {"WEB", "WEB_EMBED", "WEB_MUSIC", "WEB_CREATOR", "MWEB", "TV_EMBED"}


def build_client_info(
    client: str, cfg: dict, visitor_data: Optional[str] = None
) -> dict:
    ctx = (cfg.get("context") or {}).get("client") or {}
    header = cfg.get("header") or {}
    client_name_int = header.get("X-Youtube-Client-Name")
    try:
        client_name_int = int(client_name_int) if client_name_int else None
    except (TypeError, ValueError):
        client_name_int = None
    return {
        "hl": ctx.get("hl"),
        "gl": ctx.get("gl"),
        "visitor_data": ctx.get("visitorData") or visitor_data,
        "client_name": client_name_int,
        "client_version": ctx.get("clientVersion"),
        "device_make": ctx.get("deviceMake"),
        "device_model": ctx.get("deviceModel"),
        "os_name": ctx.get("osName"),
        "os_version": ctx.get("osVersion"),
        "user_agent": ctx.get("userAgent") or header.get("User-Agent"),
    }


def _visitor_data_from_html(watch_html: str) -> Optional[str]:
    try:
        m = re.search(r'"visitorData"\s*:\s*"([^"]+)"', watch_html)
        return m.group(1) if m else None
    except Exception:
        return None


def _check_playability(
    player_response: dict,
    video_id: str,
    url: Optional[str] = None,
) -> None:
    """Raise a typed modern exception if *player_response* is not playable."""
    from pyt.api.errors import VideoUnavailable

    playability = player_response.get("playabilityStatus", {}) or {}
    status = playability.get("status", "")

    if status in ("OK", ""):
        return

    # Live-stream upcoming is detected later in _hydrate_meta — skip here.
    if "liveStreamability" in playability and status != "LIVE_STREAM":
        return

    reason = (playability.get("reason") or "").lower()
    messages = [m for m in (playability.get("messages") or []) if m]
    msg_blob = " ".join(str(m) for m in messages).lower()

    if status == "UNPLAYABLE":
        if "join this channel" in msg_blob or "members-only" in msg_blob:
            raise VideoUnavailable(video_id=video_id, reason="members-only", url=url)
        if "live stream recording is not available" in msg_blob:
            raise VideoUnavailable(
                video_id=video_id, reason="recording unavailable", url=url
            )
        if "region" in msg_blob or "country" in msg_blob:
            raise VideoUnavailable(video_id=video_id, reason="region-blocked", url=url)
        raise VideoUnavailable(video_id=video_id, reason="unavailable", url=url)

    if status == "LOGIN_REQUIRED":
        if "private video" in msg_blob or "private video" in reason:
            raise VideoUnavailable(video_id=video_id, reason="private", url=url)
        raise VideoUnavailable(video_id=video_id, reason="private", url=url)

    if status == "ERROR":
        raise VideoUnavailable(video_id=video_id, reason="unavailable", url=url)

    if status == "LIVE_STREAM":
        # Translated later from the streaming context — fall through.
        return


def fetch_player_response(
    video_id: str,
    url: str,
    *,
    use_oauth: bool = False,
    allow_oauth_cache: bool = True,
) -> Tuple[dict, str, Dict[str, Any], bool]:
    """Fetch the InnerTube player response for *video_id*.

    Returns ``(player_response, client_name, client_cfg, is_age_restricted)``.
    Raises :class:`pyt.api.errors.VideoUnavailable` when no client returns a
    playable response.
    """
    from pyt.api.errors import VideoUnavailable

    watch_url = f"https://youtube.com/watch?v={video_id}"
    watch_html = request.get(watch_url)
    is_age_restricted = extract.is_age_restricted(watch_html)

    if is_age_restricted:
        client = "ANDROID_EMBED"
        innertube = InnerTube(
            client, use_oauth=use_oauth, allow_cache=allow_oauth_cache
        )
        response = innertube.player(video_id)
        return response, client, _default_clients[client], True

    visitor_data = _visitor_data_from_html(watch_html)
    web_version = extract.web_client_version(watch_html)
    first_response: Optional[dict] = None

    for client in _PLAYER_CLIENT_PRIORITY:
        try:
            innertube = InnerTube(
                client,
                use_oauth=use_oauth,
                allow_cache=allow_oauth_cache,
                client_version=web_version if client in _WEB_CLIENTS else None,
            )
            response = innertube.player(video_id, visitor_data=visitor_data)
            if first_response is None:
                first_response = response
            status = (response.get("playabilityStatus") or {}).get("status")
            if status == "OK" and "streamingData" in response:
                cfg = _default_clients.get(client, {})
                logger.debug("player data from %s client", client)
                return response, client, cfg, False
        except Exception:
            pass

    # Fallback: ytInitialPlayerResponse embedded in the watch page HTML.
    try:
        pr = extract.initial_player_response(watch_html)
        status = (pr.get("playabilityStatus") or {}).get("status")
        if status == "OK" and "streamingData" in pr:
            return pr, "WEB", _default_clients.get("WEB", {}), False
    except Exception:
        pass

    # Nothing worked — raise a typed error from the best response we have.
    response = first_response or {}
    _check_playability(response, video_id=video_id, url=url)
    # If _check_playability didn't raise (status was empty / live-stream edge
    # case), fall back to a generic VideoUnavailable.
    raise VideoUnavailable(video_id=video_id, reason="unavailable", url=url)

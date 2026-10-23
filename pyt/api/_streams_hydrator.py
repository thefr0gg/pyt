"""Internal: hydrate typed stream objects from a raw player response."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from pyt import extract, request
from pyt.itags import get_format_profile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RawStream:
    """Pre-parsed fields from one entry in the player response's streamingData.

    Produced by :func:`hydrate_streams`; consumed by :class:`StreamRef`.
    """

    itag: int
    url: str
    mime_type: str
    kind: str
    subtype: str
    video_codec: Optional[str]
    audio_codec: Optional[str]
    is_adaptive: bool
    is_progressive: bool
    is_otf: bool
    bitrate: Optional[int]
    filesize: int
    filesize_approx: int
    resolution: Optional[str]
    fps: Optional[int]
    abr: Optional[str]
    default_filename: str


def _decode_ustreamer_config(player_response: dict) -> Optional[bytes]:
    try:
        raw = (
            player_response.get("playerConfig", {})
            .get("mediaCommonConfig", {})
            .get("mediaUstreamerRequestConfig", {})
            .get("videoPlaybackUstreamerConfig")
        )
        if not raw:
            return None
        padded = raw + "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(padded)
    except Exception:
        return None


def _parse_one(
    stream_dict: dict, duration: Optional[float], title: str
) -> Optional[_RawStream]:
    url = stream_dict.get("url")
    if not url:
        return None

    itag = int(stream_dict.get("itag", 0))
    mime_raw = stream_dict.get("mimeType", "")

    try:
        mime_type, codecs = extract.mime_type_codec(mime_raw)
    except Exception:
        return None

    kind, subtype = mime_type.split("/", 1) if "/" in mime_type else (mime_type, "")
    is_adaptive = bool(len(codecs) % 2)
    is_progressive = not is_adaptive

    if not is_adaptive:
        video_codec = codecs[0] if len(codecs) > 0 else None
        audio_codec = codecs[1] if len(codecs) > 1 else None
    elif kind == "video":
        video_codec = codecs[0] if codecs else None
        audio_codec = None
    else:
        video_codec = None
        audio_codec = codecs[0] if codecs else None

    itag_profile = get_format_profile(itag)
    resolution: Optional[str] = itag_profile.get("resolution")
    abr: Optional[str] = itag_profile.get("abr")

    fps_raw = stream_dict.get("fps")
    fps = int(fps_raw) if fps_raw else None

    bitrate_raw = stream_dict.get("bitrate")
    bitrate = int(bitrate_raw) if bitrate_raw else None

    try:
        filesize = int(stream_dict.get("contentLength", 0) or 0)
    except (TypeError, ValueError):
        filesize = 0

    if filesize == 0 and duration and bitrate:
        filesize_approx = int(duration * bitrate / 8)
    else:
        filesize_approx = filesize

    from pyt.helpers import safe_filename as _safe

    default_filename = f"{_safe(title or 'video')}.{subtype}"

    return _RawStream(
        itag=itag,
        url=url,
        mime_type=mime_type,
        kind=kind,
        subtype=subtype,
        video_codec=video_codec,
        audio_codec=audio_codec,
        is_adaptive=is_adaptive,
        is_progressive=is_progressive,
        is_otf=bool(stream_dict.get("is_otf", False)),
        bitrate=bitrate,
        filesize=filesize,
        filesize_approx=filesize_approx,
        resolution=resolution,
        fps=fps,
        abr=abr,
        default_filename=default_filename,
    )


def hydrate_streams(
    player_response: dict,
    *,
    client_name: str,
    client_cfg: Dict[str, Any],
    visitor_data: Optional[str] = None,
    po_token: Optional[str] = None,
    on_progress: Optional[Callable] = None,
    on_complete: Optional[Callable] = None,
    duration: Optional[float] = None,
    video_id: Optional[str] = None,
    title: str = "",
) -> Tuple[List[_RawStream], "_SabrConfig"]:
    """Parse streaming data from *player_response* into typed stream objects.

    Returns ``(raw_streams, sabr_config)``.
    """
    from pyt.api._sabr_config import _SabrConfig
    from pyt.api._player import build_client_info
    from pyt.innertube import _default_clients

    streaming_data = player_response.get("streamingData") or {}
    stream_manifest = extract.apply_descrambler(streaming_data) or []

    # Fetch the player JS and apply cipher only if any stream needs it.
    needs_cipher = any("s" in s for s in stream_manifest)
    if needs_cipher and video_id:
        try:
            watch_url = f"https://youtube.com/watch?v={video_id}"
            watch_html = request.get(watch_url)
            js_url = extract.js_url(watch_html)
            js = request.get(js_url)
            extract.apply_signature(stream_manifest, player_response, js)
        except Exception as exc:
            logger.warning(
                "apply_signature failed for %s: %s; some URLs may not work",
                video_id,
                exc,
            )

    sabr_url = streaming_data.get("serverAbrStreamingUrl")
    ustreamer_config = _decode_ustreamer_config(player_response)

    cfg = client_cfg or _default_clients.get(client_name, {})
    client_info = build_client_info(client_name, cfg, visitor_data)
    stream_headers = dict(cfg.get("header") or {})

    sabr_config = _SabrConfig(
        sabr_url=sabr_url,
        ustreamer_config=ustreamer_config,
        po_token=po_token,
        client_info=client_info,
        stream_headers=stream_headers,
        duration=duration,
        on_progress=on_progress,
        on_complete=on_complete,
    )

    # Wire up a refresh callback that re-fetches the player response and
    # updates sabr_url / ustreamer_config in-place on the _SabrConfig.
    if video_id:

        def _refresh() -> Tuple[Optional[str], Optional[bytes]]:
            from pyt.api._player import fetch_player_response

            try:
                fresh, _, _, _ = fetch_player_response(
                    video_id,
                    url=f"https://youtube.com/watch?v={video_id}",
                )
                new_sd = fresh.get("streamingData") or {}
                sabr_config.sabr_url = new_sd.get("serverAbrStreamingUrl")
                sabr_config.ustreamer_config = _decode_ustreamer_config(fresh)
                return sabr_config.sabr_url, sabr_config.ustreamer_config
            except Exception as exc:
                # Returning the stale url/config here would look like a
                # successful refresh to SabrSession (it only checks
                # `if not new_url`), sending it right back into another
                # round against the same dead URL. Report failure instead
                # so the caller raises SabrError and the fallback path runs.
                logger.warning("SABR refresh failed: %s", exc)
                return None, None

        sabr_config.refresh_sabr_config = _refresh

    raw_streams = []
    for s in stream_manifest:
        parsed = _parse_one(s, duration, title)
        if parsed is not None:
            raw_streams.append(parsed)

    return raw_streams, sabr_config

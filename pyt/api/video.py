"""The :class:`Video` — a typed facade backed by a raw InnerTube player response."""

from __future__ import annotations

import logging
import shutil
import subprocess  # nosec
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

from pyt.api.errors import (
    AgeRestricted,
    LiveStreamNotSupported,
    LiveStreamUpcoming,
    PostProcessError,
    PytError,
    VideoUnavailable,
)
from pyt.api.models import Channel, Thumbnail, VideoMeta
from pyt.api.streams import StreamSet
from pyt.api._sabr_config import _SabrConfig
from pyt.api._streams_hydrator import hydrate_streams


class Video:
    """A YouTube video and its attached :class:`StreamSet`.

    Construct via :meth:`Client.video`. Direct construction is not part
    of the public API.
    """

    def __init__(
        self,
        *,
        player_response: dict,
        meta: VideoMeta,
        client_name: str,
        client_cfg: dict,
        visitor_data: Optional[str] = None,
        is_age_restricted: bool = False,
    ):
        self._player_response = player_response
        self._meta = meta
        self._client_name = client_name
        self._client_cfg = client_cfg
        self._visitor_data = visitor_data
        self._is_age_restricted = is_age_restricted
        self._streams: Optional[StreamSet] = None
        self._sabr_config: Optional[_SabrConfig] = None
        # Filled in by Client.video() so the download path can ask the
        # client to refresh the PO token on AttestationRequired.
        self._client = None  # type: Optional[Any]

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def _from_url(
        cls,
        url: str,
        *,
        po_token: Optional[str],
        use_oauth: bool,
        allow_oauth_cache: bool,
        proxies: Optional[Dict[str, str]],
        on_progress: Optional[Callable[[Any, bytes, int], None]],
        on_complete: Optional[Callable[[Any, Optional[str]], None]],
    ) -> "Video":
        from pyt.api._player import fetch_player_response
        from pyt.extract import video_id as _extract_video_id

        video_id = _extract_video_id(url)

        player_response, client_name, client_cfg, is_age_restricted = (
            fetch_player_response(
                video_id,
                url,
                use_oauth=use_oauth,
                allow_oauth_cache=allow_oauth_cache,
            )
        )

        logger.debug(
            "Video._from_url: player response OK video_id=%s client=%s",
            video_id,
            client_name,
        )

        meta = _hydrate_meta(
            player_response,
            url=url,
            is_age_restricted=is_age_restricted,
        )

        return cls(
            player_response=player_response,
            meta=meta,
            client_name=client_name,
            client_cfg=client_cfg,
            is_age_restricted=is_age_restricted,
        )

    # ── public surface ──────────────────────────────────────────────────────

    @property
    def metadata(self) -> VideoMeta:
        return self._meta

    @property
    def video_id(self) -> str:
        return self._meta.video_id

    @property
    def url(self) -> str:
        return self._meta.url

    @property
    def title(self) -> str:
        return self._meta.title

    @property
    def author(self) -> Channel:
        return self._meta.author

    @property
    def length(self) -> timedelta:
        return self._meta.length

    @property
    def description(self) -> Optional[str]:
        return self._meta.description

    @property
    def published_at(self) -> Optional[datetime]:
        return self._meta.published_at

    @property
    def views(self) -> Optional[int]:
        return self._meta.views

    @property
    def thumbnails(self) -> List[Thumbnail]:
        return list(self._meta.thumbnails)

    @property
    def is_live(self) -> bool:
        """``True`` while the broadcast is actively streaming."""
        return self._meta.is_live

    @property
    def is_live_content(self) -> bool:
        """``True`` for VODs that were originally broadcast as live streams."""
        return self._meta.is_live_content

    @property
    def hls_manifest_url(self) -> Optional[str]:
        """HLS manifest URL for active live streams; ``None`` otherwise."""
        return self._meta.hls_manifest_url

    @property
    def is_age_restricted(self) -> bool:
        """``True`` when YouTube requires age-verification to access this video.

        If ``True``, :attr:`streams` will raise :class:`AgeRestricted` unless
        the :class:`Client` was constructed with browser cookies from a
        signed-in, age-verified account::

            client = Client(cookies_from_browser="chrome")
        """
        return self._meta.is_age_restricted

    @property
    def streams(self) -> StreamSet:
        """The video's stream catalog. Lazily fetched once, then cached.

        Raises :class:`AgeRestricted` immediately (without extra network
        calls) when :attr:`is_age_restricted` is ``True`` and the client
        has not supplied cookies from a signed-in browser session.
        """
        if self._streams is None:
            if self.is_age_restricted:
                client = self._client
                has_auth = client is not None and (
                    getattr(client, "_cookies_path", None)
                    or getattr(client, "_cookies_browser", None)
                    or getattr(client, "_use_oauth", False)
                )
                if not has_auth:
                    raise AgeRestricted(self.video_id, url=self.url)

            logger.debug(
                "video.streams: hydrating stream catalog for %s", self.video_id
            )

            po_token = None
            on_progress = None
            on_complete = None
            client = self._client
            if client is not None:
                po_token = getattr(client, "_current_po_token", lambda: None)()
                on_progress = getattr(client, "_on_progress", None)
                on_complete = getattr(client, "_on_complete", None)

            raw_streams, sabr_config = hydrate_streams(
                self._player_response,
                client_name=self._client_name,
                client_cfg=self._client_cfg,
                visitor_data=self._visitor_data,
                po_token=po_token,
                on_progress=on_progress,
                on_complete=on_complete,
                duration=(
                    self._meta.length.total_seconds() if self._meta.length else None
                ),
                video_id=self.video_id,
                title=self.title,
            )
            self._sabr_config = sabr_config
            self._streams = StreamSet._from_raw(raw_streams, video=self)
            logger.info(
                "video.streams: %s catalog has %d streams",
                self.video_id,
                len(self._streams),
            )
        return self._streams

    # ── combined download ──────────────────────────────────────────────────

    def download_best(
        self,
        output_path: Optional[str] = None,
        *,
        prefer_resolution: Optional[str] = None,
        filename: Optional[str] = None,
        container: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        """Pick the best adaptive video + audio pair and run them through
        a single multiplexed SABR session, then merge with ffmpeg.
        """
        from pyt.api.combined import CombinedDownload

        video_stream, audio_stream = self.streams.best_pair(
            prefer_resolution=prefer_resolution,
        )
        return CombinedDownload(
            video=self,
            video_stream=video_stream,
            audio_stream=audio_stream,
            output_path=output_path,
            filename=filename,
            container=container,
            timeout=timeout,
        )

    def download_live(
        self,
        output_path: Optional[str] = None,
        *,
        prefer_resolution: Optional[str] = None,
        filename: Optional[str] = None,
        container: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        """Capture an active live broadcast via a native SABR session,
        chasing the live edge until stopped, then merge with ffmpeg.

        Call :meth:`LiveDownload.stop` (e.g. from a signal handler) to end
        the capture cleanly. Unlike :meth:`record_live`, this uses pyt's
        own SABR/UMP implementation instead of shelling out to ffmpeg for
        the capture itself — ffmpeg is still used for the final mux.
        :meth:`record_live` remains the more broadly compatible fallback.
        """
        from pyt.api.live_download import LiveDownload

        if not self.is_live:
            raise LiveStreamNotSupported(self.video_id, url=self.url)

        video_stream, audio_stream = self.streams.best_pair(
            prefer_resolution=prefer_resolution,
        )
        return LiveDownload(
            video=self,
            video_stream=video_stream,
            audio_stream=audio_stream,
            output_path=output_path,
            filename=filename,
            container=container,
            timeout=timeout,
        )

    # ── live stream capture ─────────────────────────────────────────────────

    def record_live(
        self,
        output_path: Union[str, Path, None] = None,
        *,
        filename: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Path:
        """Record an active live stream by consuming its HLS manifest.

        Uses ``ffmpeg -i <hls_url> -c copy`` to capture the stream in
        real time. The call **blocks** until the broadcast ends or the
        process is interrupted (Ctrl-C / SIGINT is propagated cleanly).
        """
        if not self.is_live:
            raise LiveStreamNotSupported(self.video_id, url=self.url)

        hls_url = self.hls_manifest_url
        if not hls_url:
            raise LiveStreamNotSupported(self.video_id, url=self.url)

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError(
                "ffmpeg not found on PATH — required to record live streams. "
                "Install with `pyt --doctor --install ffmpeg`."
            )

        safe_title = (
            "".join(
                c if c.isalnum() or c in " ._-" else "_"
                for c in (self.title or self.video_id)
            ).strip()
            or self.video_id
        )
        out_filename = filename or f"{safe_title}.ts"

        if output_path is None:
            dest = Path.cwd() / out_filename
        else:
            p = Path(output_path)
            dest = (p / out_filename) if p.is_dir() else p

        cmd = [ffmpeg, "-y", "-i", hls_url, "-c", "copy"]
        if timeout is not None:
            cmd += ["-t", str(timeout)]
        cmd.append(str(dest))

        logger.info(
            "Video.record_live: recording %s -> %s (timeout=%s)",
            self.video_id,
            dest,
            timeout,
        )
        try:
            subprocess.run(cmd, check=True)  # nosec
        except subprocess.CalledProcessError as exc:
            raise PostProcessError(
                f"ffmpeg exited {exc.returncode} while recording live stream",
                step="record_live",
                partial_output_path=str(dest),
                cause=exc,
            ) from exc
        return dest

    def __repr__(self) -> str:
        return f"<Video id={self.video_id!r} title={self.title!r}>"


def _hydrate_meta(
    player_response: dict,
    *,
    url: str,
    is_age_restricted: bool = False,
) -> VideoMeta:
    """Build a :class:`VideoMeta` from a raw InnerTube player response dict."""
    details = player_response.get("videoDetails", {}) or {}

    raw_thumbs = details.get("thumbnail", {}).get("thumbnails", []) or []
    thumbnails = [
        Thumbnail(
            url=t["url"],
            width=t.get("width"),
            height=t.get("height"),
        )
        for t in raw_thumbs
        if t.get("url")
    ]

    channel_id = details.get("channelId")
    author = Channel(
        id=channel_id,
        name=details.get("author", "unknown"),
        url=f"https://www.youtube.com/channel/{channel_id}" if channel_id else None,
    )

    try:
        length = timedelta(seconds=int(details.get("lengthSeconds", 0) or 0))
    except (TypeError, ValueError):
        length = timedelta(0)

    try:
        views = int(details.get("viewCount")) if details.get("viewCount") else None
    except (TypeError, ValueError):
        views = None

    rating = details.get("averageRating")

    # publish_date from microformat (present in InnerTube player responses)
    published_at: Optional[datetime] = None
    try:
        pub_str = (
            player_response.get("microformat", {})
            .get("playerMicroformatRenderer", {})
            .get("publishDate")
        )
        if pub_str:
            published_at = datetime.strptime(pub_str, "%Y-%m-%d")
    except (ValueError, AttributeError):
        pass

    is_live = bool(details.get("isLive"))
    is_live_content = bool(details.get("isLiveContent"))
    is_upcoming = bool(details.get("isUpcoming"))

    hls_manifest_url: Optional[str] = None
    scheduled_start: Optional[datetime] = None

    streaming_data = player_response.get("streamingData") or {}
    if is_live:
        hls_manifest_url = streaming_data.get("hlsManifestUrl") or None

    if is_upcoming:
        playability = player_response.get("playabilityStatus") or {}
        offline_slate = playability.get("offlineSlate") or {}
        ts_str = offline_slate.get("scheduledStartTime")
        if ts_str:
            try:
                scheduled_start = datetime.fromtimestamp(int(ts_str), tz=timezone.utc)
            except (TypeError, ValueError):
                pass
        video_id = details.get("videoId", "")
        raise LiveStreamUpcoming(
            video_id=video_id,
            url=url,
            scheduled_start=scheduled_start,
        )

    video_id = details.get("videoId", "")

    return VideoMeta(
        video_id=video_id,
        url=url,
        title=details.get("title") or "",
        author=author,
        length=length,
        description=details.get("shortDescription"),
        published_at=published_at,
        views=views,
        rating=float(rating) if rating is not None else None,
        keywords=list(details.get("keywords", []) or []),
        thumbnails=thumbnails,
        is_live=is_live,
        is_live_content=is_live_content,
        hls_manifest_url=hls_manifest_url,
        is_age_restricted=is_age_restricted,
    )

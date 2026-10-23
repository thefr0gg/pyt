"""
This module implements the core developer interface for pyt.

The problem domain of the :class:`YouTube <YouTube> class focuses almost
exclusively on the developer interface. Pytube offloads the heavy lifting to
smaller peripheral modules and functions.

This is the **legacy** developer interface. New code should use
:class:`pyt.Client` and :class:`pyt.Video` from :mod:`pyt.api`. The classes
here will be moved to :mod:`pyt.legacy` and removed from the top-level
namespace in a future major release.
"""

import logging
import re
import warnings
import pyt
import pyt.exceptions as exceptions

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

_DEPRECATION_MESSAGE = (
    "pyt.YouTube is deprecated and will be removed in a future major release. "
    "Use pyt.Client instead:\n"
    "    client = pyt.Client()\n"
    "    video = client.video(url)\n"
    "    video.streams.audio.best().download_to('downloads/').run()\n"
    "See the README for the full migration guide. To silence this warning "
    "while you migrate, import the class explicitly from pyt.legacy."
)

from pyt import extract, request
from pyt.streams import Stream
from pyt.query import StreamQuery
from pyt.helpers import install_proxy
from pyt.innertube import InnerTube, _default_clients
from pyt.metadata import YouTubeMetadata
from pyt.monostate import Monostate

logger = logging.getLogger(__name__)

# Clients tried in order when fetching player data for non-age-restricted videos.
# ANDROID_VR (version ≤1.65) returns direct signed URLs for all adaptive formats
# without SABR-only enforcement. Newer ANDROID versions require SABR for many streams.
_PLAYER_CLIENT_PRIORITY = ["ANDROID_VR", "ANDROID", "IOS", "TV_EMBED"]

# Web-based clients whose version string can be updated from the live page.
_WEB_CLIENTS = {"WEB", "WEB_EMBED", "WEB_MUSIC", "WEB_CREATOR", "MWEB", "TV_EMBED"}


class YouTube:
    """Core developer interface for pyt."""

    def __init__(
        self,
        url: str,
        on_progress_callback: Optional[Callable[[Any, bytes, int], None]] = None,
        on_complete_callback: Optional[Callable[[Any, Optional[str]], None]] = None,
        proxies: Dict[str, str] = None,
        use_oauth: bool = False,
        allow_oauth_cache: bool = True,
        po_token: Optional[str] = None,
    ):
        """Construct a :class:`YouTube <YouTube>`.

        :param str url:
            A valid YouTube watch URL.
        :param func on_progress_callback:
            (Optional) User defined callback function for stream download
            progress events.
        :param func on_complete_callback:
            (Optional) User defined callback function for stream download
            complete events.
        :param dict proxies:
            (Optional) A dict mapping protocol to proxy address which will be used by pyt.
        :param bool use_oauth:
            (Optional) Prompt the user to authenticate to YouTube.
            If allow_oauth_cache is set to True, the user should only be prompted once.
        :param bool allow_oauth_cache:
            (Optional) Cache OAuth tokens locally on the machine. Defaults to True.
            These tokens are only generated if use_oauth is set to True as well.
        """
        warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)

        self._js: Optional[str] = None  # js fetched by js_url
        self._js_url: Optional[str] = None  # the url to the js, parsed from watch html

        self._vid_info: Optional[Dict] = None  # content fetched from innertube/player

        self._watch_html: Optional[str] = None  # the html of /watch?v=<video_id>
        self._embed_html: Optional[str] = None
        self._player_config_args: Optional[Dict] = (
            None  # inline js in the html containing
        )
        self._age_restricted: Optional[bool] = None

        self._fmt_streams: Optional[List[Stream]] = None

        self._initial_data = None
        self._metadata: Optional[YouTubeMetadata] = None

        # video_id part of /watch?v=<video_id>
        self.video_id = extract.video_id(url)

        self.watch_url = f"https://youtube.com/watch?v={self.video_id}"
        self.embed_url = f"https://www.youtube.com/embed/{self.video_id}"

        # Shared between all instances of `Stream` (Borg pattern).
        self.stream_monostate = Monostate(
            on_progress=on_progress_callback, on_complete=on_complete_callback
        )

        if proxies:
            install_proxy(proxies)

        self._author = None
        self._title = None
        self._publish_date = None

        self.use_oauth = use_oauth
        self.allow_oauth_cache = allow_oauth_cache
        self._po_token = po_token

    def __repr__(self):
        return f"<pyt.__main__.YouTube object: videoId={self.video_id}>"

    def __eq__(self, o: object) -> bool:
        # Compare types and urls, if they're same return true, else return false.
        return type(o) == type(self) and o.watch_url == self.watch_url

    @property
    def watch_html(self):
        if self._watch_html:
            return self._watch_html
        self._watch_html = request.get(url=self.watch_url)
        return self._watch_html

    @property
    def embed_html(self):
        if self._embed_html:
            return self._embed_html
        self._embed_html = request.get(url=self.embed_url)
        return self._embed_html

    @property
    def age_restricted(self):
        if self._age_restricted:
            return self._age_restricted
        self._age_restricted = extract.is_age_restricted(self.watch_html)
        return self._age_restricted

    @property
    def js_url(self):
        if self._js_url:
            return self._js_url

        if self.age_restricted:
            self._js_url = extract.js_url(self.embed_html)
        else:
            self._js_url = extract.js_url(self.watch_html)

        return self._js_url

    @property
    def js(self):
        if self._js:
            return self._js

        # If the js_url doesn't match the cached url, fetch the new js and update
        #  the cache; otherwise, load the cache.
        if pyt.__js_url__ != self.js_url:
            self._js = request.get(self.js_url)
            pyt.__js__ = self._js
            pyt.__js_url__ = self.js_url
        else:
            self._js = pyt.__js__

        return self._js

    @property
    def initial_data(self):
        if self._initial_data:
            return self._initial_data
        self._initial_data = extract.initial_data(self.watch_html)
        return self._initial_data

    @property
    def streaming_data(self):
        """Return streamingData from video info."""
        if "streamingData" in self.vid_info:
            return self.vid_info["streamingData"]
        self.bypass_age_gate()
        if "streamingData" not in self.vid_info:
            raise exceptions.VideoUnavailable(self.video_id)
        return self.vid_info["streamingData"]

    @property
    def sabr_url(self) -> Optional[str]:
        """SABR streaming endpoint URL, if present in the player response."""
        return self.streaming_data.get("serverAbrStreamingUrl")

    @property
    def ustreamer_config(self) -> Optional[bytes]:
        """Raw videoPlaybackUstreamerConfig bytes for SABR requests.

        Located at vid_info['playerConfig']['mediaCommonConfig']
        ['mediaUstreamerRequestConfig']['videoPlaybackUstreamerConfig']
        as a base64url-encoded string.
        """
        try:
            raw = (
                self.vid_info.get("playerConfig", {})
                .get("mediaCommonConfig", {})
                .get("mediaUstreamerRequestConfig", {})
                .get("videoPlaybackUstreamerConfig")
            )
            if not raw:
                return None
            import base64

            padded = raw + "=" * (-len(raw) % 4)
            return base64.urlsafe_b64decode(padded)
        except Exception:
            return None

    @property
    def fmt_streams(self):
        """Returns a list of streams if they have been initialized.

        If the streams have not been initialized, finds all relevant
        streams and initializes them.
        """
        self.check_availability()
        if self._fmt_streams:
            return self._fmt_streams

        self._fmt_streams = []

        stream_manifest = extract.apply_descrambler(self.streaming_data)

        # If the cached js doesn't work, try fetching a new js file
        # https://github.com/pyt/pyt/issues/1054
        try:
            extract.apply_signature(stream_manifest, self.vid_info, self.js)
        except exceptions.ExtractError:
            # To force an update to the js file, we clear the cache and retry
            self._js = None
            self._js_url = None
            pyt.__js__ = None
            pyt.__js_url__ = None
            extract.apply_signature(stream_manifest, self.vid_info, self.js)

        # build instances of :class:`Stream <Stream>`
        # OTF (on-the-fly) streams have no direct URL and are skipped.
        for stream in stream_manifest:
            if "url" not in stream:
                continue
            video = Stream(
                stream=stream,
                monostate=self.stream_monostate,
            )
            self._fmt_streams.append(video)

        self.stream_monostate.title = self.title
        self.stream_monostate.duration = self.length
        self.stream_monostate.sabr_url = self.sabr_url
        self.stream_monostate.ustreamer_config = self.ustreamer_config
        self.stream_monostate.po_token = self._po_token

        _ms = self.stream_monostate
        _yt = self

        def _refresh_sabr_config():
            """Force a fresh player response and return the new (sabr_url, ustreamer_config).

            Used by both the legacy code path (Stream.download fallback) and by
            SabrSession's refresh_callback when a single-use SABR token expires.
            """
            _yt._vid_info = None
            _ms.sabr_url = _yt.sabr_url
            _ms.ustreamer_config = _yt.ustreamer_config
            return _ms.sabr_url, _ms.ustreamer_config

        self.stream_monostate.refresh_sabr_config = _refresh_sabr_config

        return self._fmt_streams

    def check_availability(self):
        """Check whether the video is available.

        Raises different exceptions based on why the video is unavailable,
        otherwise does nothing.
        """
        status, messages = extract.playability_status(self.watch_html)

        for reason in messages:
            if status == "UNPLAYABLE":
                if reason == (
                    "Join this channel to get access to members-only content "
                    "like this video, and other exclusive perks."
                ):
                    raise exceptions.MembersOnly(video_id=self.video_id)
                elif reason == "This live stream recording is not available.":
                    raise exceptions.RecordingUnavailable(video_id=self.video_id)
                else:
                    raise exceptions.VideoUnavailable(video_id=self.video_id)
            elif status == "LOGIN_REQUIRED":
                if reason == (
                    "This is a private video. "
                    "Please sign in to verify that you may see it."
                ):
                    raise exceptions.VideoPrivate(video_id=self.video_id)
            elif status == "ERROR":
                if reason == "Video unavailable":
                    raise exceptions.VideoUnavailable(video_id=self.video_id)
            elif status == "LIVE_STREAM":
                raise exceptions.LiveStreamError(video_id=self.video_id)

    @staticmethod
    def _build_client_info(
        client: str, cfg: dict, visitor_data: Optional[str] = None
    ) -> dict:
        """Translate an InnerTube client config into the SABR StreamerContext.ClientInfo dict.

        Field mapping mirrors yt-dlp's innertube/client_info.py (ClientName enum at 16,
        device_make at 12, device_model at 13, visitor_data at 14, user_agent at 15,
        client_version at 17, os_name at 18, os_version at 19).
        """
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

    @property
    def _visitor_data(self) -> Optional[str]:
        """Extract visitorData from the watch page for use in innertube requests."""
        try:
            match = re.search(r'"visitorData"\s*:\s*"([^"]+)"', self.watch_html)
            return match.group(1) if match else None
        except Exception:
            return None

    @property
    def vid_info(self):
        """Parse the raw vid info and return the parsed result.

        Iterates through _PLAYER_CLIENT_PRIORITY (ANDROID → IOS → TV_EMBED),
        returning the first response that carries usable streaming data. Falls
        back to the ytInitialPlayerResponse embedded in the watch page HTML,
        then finally to whatever the first client returned. Age-restricted
        videos skip straight to the ANDROID_EMBED client.

        :rtype: Dict[Any, Any]
        """
        if self._vid_info:
            return self._vid_info

        if self.age_restricted:
            innertube = InnerTube(
                "ANDROID_EMBED",
                use_oauth=self.use_oauth,
                allow_cache=self.allow_oauth_cache,
            )
            self._vid_info = innertube.player(self.video_id)
            return self._vid_info

        visitor_data = self._visitor_data
        # Read the live WEB client version once; passed to web-type clients so
        # they use the current deploy version instead of the hardcoded one.
        web_version = extract.web_client_version(self.watch_html)
        first_response = None

        for client in _PLAYER_CLIENT_PRIORITY:
            try:
                innertube = InnerTube(
                    client,
                    use_oauth=self.use_oauth,
                    allow_cache=self.allow_oauth_cache,
                    client_version=web_version if client in _WEB_CLIENTS else None,
                )
                response = innertube.player(
                    self.video_id,
                    visitor_data=visitor_data,
                )
                if first_response is None:
                    first_response = response
                status = response.get("playabilityStatus", {}).get("status")
                if status == "OK" and "streamingData" in response:
                    logger.debug("player data from %s client", client)
                    self._vid_info = response
                    cfg = _default_clients.get(client, {})
                    self.stream_monostate.stream_headers = cfg.get("header", {})
                    self.stream_monostate.client_info = self._build_client_info(
                        client, cfg, visitor_data
                    )
                    return self._vid_info
            except Exception:
                pass

        # Fallback: ytInitialPlayerResponse from the watch page HTML.
        try:
            player_response = extract.initial_player_response(self.watch_html)
            status = player_response.get("playabilityStatus", {}).get("status")
            if status == "OK" and "streamingData" in player_response:
                self._vid_info = player_response
                return self._vid_info
        except exceptions.RegexMatchError:
            pass

        self._vid_info = first_response or {}
        return self._vid_info

    def bypass_age_gate(self):
        """Attempt to update the vid_info by bypassing the age gate."""
        for client in ("ANDROID_EMBED", "IOS_EMBED", "TV_EMBED"):
            try:
                innertube = InnerTube(
                    client,
                    use_oauth=self.use_oauth,
                    allow_cache=self.allow_oauth_cache,
                )
                response = innertube.player(self.video_id)
                status = response.get("playabilityStatus", {}).get("status")
                if status not in (None, "UNPLAYABLE"):
                    self._vid_info = response
                    return
            except Exception:
                pass

        raise exceptions.AgeRestrictedError(self.video_id)

    @property
    def caption_tracks(self):
        """Get a list of :class:`Caption <Caption>`."""
        from pyt.captions import Caption

        raw_tracks = (
            self.vid_info.get("captions", {})
            .get("playerCaptionsTracklistRenderer", {})
            .get("captionTracks", [])
        )
        return [Caption(track) for track in raw_tracks]

    @property
    def captions(self):
        """Interface to query caption tracks."""
        from pyt.query import CaptionQuery

        return CaptionQuery(self.caption_tracks)

    @property
    def streams(self) -> StreamQuery:
        """Interface to query both adaptive (DASH) and progressive streams.

        :rtype: :class:`StreamQuery <StreamQuery>`.
        """
        self.check_availability()
        return StreamQuery(self.fmt_streams)

    @property
    def thumbnail_url(self) -> str:
        """Get the thumbnail url image.

        :rtype: str
        """
        thumbnail_details = (
            self.vid_info.get("videoDetails", {}).get("thumbnail", {}).get("thumbnails")
        )
        if thumbnail_details:
            thumbnail_details = thumbnail_details[-1]  # last item has max size
            return thumbnail_details["url"]

        return f"https://img.youtube.com/vi/{self.video_id}/maxresdefault.jpg"

    @property
    def publish_date(self):
        """Get the publish date.

        :rtype: datetime
        """
        if self._publish_date:
            return self._publish_date
        self._publish_date = extract.publish_date(self.watch_html)
        return self._publish_date

    @publish_date.setter
    def publish_date(self, value):
        """Sets the publish date."""
        self._publish_date = value

    @property
    def title(self) -> str:
        """Get the video title.

        :rtype: str
        """
        if self._title:
            return self._title

        try:
            self._title = self.vid_info["videoDetails"]["title"]
        except KeyError:
            # Check_availability will raise the correct exception in most cases
            #  if it doesn't, ask for a report.
            self.check_availability()
            raise exceptions.PytError(
                (
                    f"Exception while accessing title of {self.watch_url}. "
                    "Please file a bug report at https://github.com/pyt/pyt"
                )
            )

        return self._title

    @title.setter
    def title(self, value):
        """Sets the title value."""
        self._title = value

    @property
    def description(self) -> str:
        """Get the video description.

        :rtype: str
        """
        return self.vid_info.get("videoDetails", {}).get("shortDescription")

    @property
    def rating(self) -> float:
        """Get the video average rating.

        :rtype: float

        """
        return self.vid_info.get("videoDetails", {}).get("averageRating")

    @property
    def length(self) -> int:
        """Get the video length in seconds.

        :rtype: int
        """
        return int(self.vid_info.get("videoDetails", {}).get("lengthSeconds"))

    @property
    def views(self) -> int:
        """Get the number of the times the video has been viewed.

        :rtype: int
        """
        return int(self.vid_info.get("videoDetails", {}).get("viewCount"))

    @property
    def author(self) -> str:
        """Get the video author.
        :rtype: str
        """
        if self._author:
            return self._author
        self._author = self.vid_info.get("videoDetails", {}).get("author", "unknown")
        return self._author

    @author.setter
    def author(self, value):
        """Set the video author."""
        self._author = value

    @property
    def keywords(self) -> List[str]:
        """Get the video keywords.

        :rtype: List[str]
        """
        return self.vid_info.get("videoDetails", {}).get("keywords", [])

    @property
    def channel_id(self) -> str:
        """Get the video poster's channel id.

        :rtype: str
        """
        return self.vid_info.get("videoDetails", {}).get("channelId", None)

    @property
    def channel_url(self) -> str:
        """Construct the channel url for the video's poster from the channel id.

        :rtype: str
        """
        return f"https://www.youtube.com/channel/{self.channel_id}"

    @property
    def metadata(self) -> Optional[YouTubeMetadata]:
        """Get the metadata for the video.

        :rtype: YouTubeMetadata
        """
        if self._metadata:
            return self._metadata
        else:
            self._metadata = extract.metadata(self.initial_data)
            return self._metadata

    def register_on_progress_callback(self, func: Callable[[Any, bytes, int], None]):
        """Register a download progress callback function post initialization.

        :param callable func:
            A callback function that takes ``stream``, ``chunk``,
             and ``bytes_remaining`` as parameters.

        :rtype: None

        .. deprecated::
            Pass ``on_progress=`` to :class:`pyt.Client` instead.
        """
        warnings.warn(
            "register_on_progress_callback is deprecated. "
            "Pass on_progress= to pyt.Client(...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.stream_monostate.on_progress = func

    def register_on_complete_callback(self, func: Callable[[Any, Optional[str]], None]):
        """Register a download complete callback function post initialization.

        :param callable func:
            A callback function that takes ``stream`` and  ``file_path``.

        :rtype: None

        .. deprecated::
            Pass ``on_complete=`` to :class:`pyt.Client` instead.
        """
        warnings.warn(
            "register_on_complete_callback is deprecated. "
            "Pass on_complete= to pyt.Client(...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.stream_monostate.on_complete = func

    @staticmethod
    def from_id(video_id: str) -> "YouTube":
        """Construct a :class:`YouTube <YouTube>` object from a video id.

        :param str video_id:
            The video id of the YouTube video.

        :rtype: :class:`YouTube <YouTube>`

        .. deprecated::
            Use :meth:`pyt.Client.video` instead.
        """
        warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return YouTube(f"https://www.youtube.com/watch?v={video_id}")

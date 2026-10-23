from typing import Any, Callable, Optional


class Monostate:
    def __init__(
        self,
        on_progress: Optional[Callable[[Any, bytes, int], None]],
        on_complete: Optional[Callable[[Any, Optional[str]], None]],
        title: Optional[str] = None,
        duration: Optional[int] = None,
        sabr_url: Optional[str] = None,
        po_token: Optional[str] = None,
        ustreamer_config: Optional[bytes] = None,
    ):
        self.on_progress = on_progress
        self.on_complete = on_complete
        self.title = title
        self.duration = duration
        self.sabr_url = sabr_url
        self.po_token = po_token
        self.ustreamer_config = ustreamer_config
        # Optional callback that re-fetches the player response and updates
        # sabr_url / ustreamer_config in-place (for SABR token refresh).
        self.refresh_sabr_config: Optional[Callable[[], None]] = None
        # Extra headers to use for CDN stream requests (e.g. client User-Agent).
        self.stream_headers: dict = {}
        # ClientInfo subset embedded in SABR's StreamerContext message
        # (clientName int, clientVersion, deviceMake/Model, osName/Version, userAgent).
        self.client_info: dict = {}

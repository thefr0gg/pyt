"""The :class:`Client` — single entry point and owner of session state.

Construct one ``Client`` per "user" (process, FastAPI app, notebook). It
holds the proxy config, cookies, OAuth flag, and PO token, and produces
:class:`Video` instances. The legacy :class:`pyt.YouTube` global state
(``pyt.__js__``, ``pyt.__js_url__``, ``Monostate``) is still touched
internally — fully decoupling from it is a v3 task.
"""

from __future__ import annotations

import http.cookiejar
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Union

from pyt.api.errors import AttestationRequired, ConfigError
from pyt.api.po_token import PoTokenProvider, coerce_provider
from pyt.api.video import Video

logger = logging.getLogger(__name__)


_BROWSER_CHOICES = {"chrome", "chromium", "firefox", "brave", "edge", "safari", "opera"}

ProgressCallback = Callable[[Any, bytes, int], None]
CompleteCallback = Callable[[Any, Optional[str]], None]


class Client:
    """Holds the configuration shared by every video downloaded through it.

    Example::

        client = Client(proxy="socks5://127.0.0.1:9050", cookies_from_browser="firefox")
        video = client.video("https://youtu.be/dQw4w9WgXcQ")
        video.streams.audio.best().download_to("downloads/")
    """

    def __init__(
        self,
        *,
        proxy: Optional[str] = None,
        cookies: Optional[str] = None,
        cookies_from_browser: Optional[str] = None,
        po_token: Optional[str] = None,
        po_token_provider: Optional[Callable[[], str]] = None,
        po_token_cmd: Optional[Union[str, Sequence[str]]] = None,
        po_token_script: Optional[Union[str, Path]] = None,
        use_oauth: bool = False,
        allow_oauth_cache: bool = True,
        on_progress: Optional[ProgressCallback] = None,
        on_complete: Optional[CompleteCallback] = None,
    ):
        if cookies and cookies_from_browser:
            raise ConfigError(
                "pass either cookies=<file> or cookies_from_browser=<name>, not both"
            )
        if (
            cookies_from_browser
            and cookies_from_browser.lower() not in _BROWSER_CHOICES
        ):
            raise ConfigError(
                f"unknown browser '{cookies_from_browser}'. "
                f"choose from: {', '.join(sorted(_BROWSER_CHOICES))}"
            )

        self._proxy = proxy
        self._cookies_path = cookies
        self._cookies_browser = cookies_from_browser
        # ``coerce_provider`` enforces "at most one" across the four
        # po_token entry points. Result is None when the user didn't
        # configure any — Client.video then falls back to running
        # without a token, which is fine for most public videos.
        self._po_token_provider: Optional[PoTokenProvider] = coerce_provider(
            po_token=po_token,
            po_token_provider=po_token_provider,
            po_token_cmd=po_token_cmd,
            po_token_script=po_token_script,
        )
        self._use_oauth = use_oauth
        self._allow_oauth_cache = allow_oauth_cache
        self._on_progress = on_progress
        self._on_complete = on_complete

        self._cookies_installed = False

        logger.debug(
            "Client init: proxy=%s cookies=%s cookies_from_browser=%s "
            "po_token_provider=%s oauth=%s on_progress=%s on_complete=%s",
            bool(proxy),
            bool(cookies),
            cookies_from_browser,
            self._po_token_provider.name if self._po_token_provider else "unset",
            use_oauth,
            "set" if on_progress else "unset",
            "set" if on_complete else "unset",
        )

    # ── public ──────────────────────────────────────────────────────────────

    def video(self, url: str) -> Video:
        """Fetch a single video by URL or ID. This is the network boundary —
        all attribute access on the returned :class:`Video` is pure.

        Hydration uses the cached PO token (if any). If a provider is
        configured and the actual *download* later raises
        :class:`AttestationRequired`, the download path will refresh
        the token and retry once. The hydration step itself doesn't
        trigger ATTESTATION_REQUIRED — that comes from SABR, which
        only runs at byte-transfer time.
        """
        logger.info("client.video: fetching %s", url)
        t0 = time.monotonic()
        self._ensure_cookies_installed()
        proxies = self._proxies_dict()

        po_token = self._current_po_token()
        result = Video._from_url(
            url,
            po_token=po_token,
            use_oauth=self._use_oauth,
            allow_oauth_cache=self._allow_oauth_cache,
            proxies=proxies,
            on_progress=self._on_progress,
            on_complete=self._on_complete,
        )
        # Stash a back-reference so Download / CombinedDownload can ask
        # the client to refresh the PO token on AttestationRequired.
        result._client = self

        # Log post-hydration timing. Wrap the attribute access so test
        # mocks of _from_url that return a sentinel don't blow up — we
        # don't want our own logging to make the API less mockable.
        try:
            video_id = getattr(result, "video_id", "?")
            title = (getattr(result, "title", "") or "")[:60]
            length = getattr(result, "length", "?")
            logger.info(
                "client.video: %s hydrated in %.2fs (title=%r length=%s)",
                video_id,
                time.monotonic() - t0,
                title,
                length,
            )
        except Exception:  # noqa: BLE001 — logging must never raise
            pass
        return result

    def _current_po_token(self) -> Optional[str]:
        """The token to attach to the next outgoing request, or ``None``."""
        if self._po_token_provider is None:
            return None
        try:
            return self._po_token_provider.get()
        except ConfigError as exc:
            logger.warning(
                "client: po_token provider '%s' failed (%s); "
                "falling back to no token",
                self._po_token_provider.name,
                exc,
            )
            return None

    @property
    def po_token(self) -> Optional[str]:
        """The currently cached PO token, or ``None``."""
        return self._current_po_token()

    def refresh_po_token(self) -> Optional[str]:
        """Force the provider to regenerate. Returns the new token or
        ``None`` if no provider is configured. Called automatically by
        the download path on :class:`AttestationRequired`."""
        if self._po_token_provider is None:
            return None
        self._po_token_provider.invalidate()
        return self._po_token_provider.get(force_refresh=True)

    @property
    def has_po_token_provider(self) -> bool:
        return self._po_token_provider is not None

    def playlist(self, url: str):
        """Open a YouTube playlist."""
        from pyt.api.playlists import Playlist

        self._ensure_cookies_installed()
        return Playlist._from_url(url, client=self)

    def channel(self, url: str):
        """Open a channel's "Videos" feed."""
        from pyt.api.playlists import ChannelFeed

        self._ensure_cookies_installed()
        return ChannelFeed._from_url(url, client=self)

    def search(self, query: str):
        """Search YouTube. Returns :class:`SearchResults`."""
        from pyt.api.playlists import SearchResults

        self._ensure_cookies_installed()
        return SearchResults._from_query(query, client=self)

    # ── internals ───────────────────────────────────────────────────────────

    def _proxies_dict(self) -> Optional[Dict[str, str]]:
        if not self._proxy:
            return None
        return {"http": self._proxy, "https": self._proxy}

    def _ensure_cookies_installed(self) -> None:
        """Cookies are installed once per client. The legacy hook patches
        the module-level :func:`pyt.request._execute_request` — that's a
        global mutation we inherit from the old code path. v3 will lift
        this onto the client itself.
        """
        if self._cookies_installed:
            return
        if not (self._cookies_path or self._cookies_browser):
            self._cookies_installed = True
            return

        from pyt import cookies as cookie_loader

        if self._cookies_path:
            logger.debug("loading cookies from file %s", self._cookies_path)
            jar = cookie_loader.load_cookies_from_file(self._cookies_path)
        else:
            logger.debug("loading cookies from browser %s", self._cookies_browser)
            jar = cookie_loader.load_cookies_from_browser(self._cookies_browser)

        if not isinstance(jar, http.cookiejar.CookieJar):
            raise ConfigError("cookie loader did not return a CookieJar")

        cookie_count = sum(1 for _ in jar)
        logger.info(
            "installed %d cookies from %s",
            cookie_count,
            "file" if self._cookies_path else f"browser {self._cookies_browser}",
        )
        cookie_loader.install_cookies(jar)
        self._cookies_installed = True

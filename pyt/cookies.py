"""Cookie loading and installation for pyt."""

from __future__ import annotations

import http.cookiejar
import logging
import os

import browser_cookie3

logger = logging.getLogger(__name__)

_SUPPORTED_BROWSERS = (
    "chrome",
    "chromium",
    "firefox",
    "brave",
    "edge",
    "safari",
    "opera",
)


def load_cookies_from_file(path: str) -> http.cookiejar.CookieJar:
    """Load cookies from a Netscape/Mozilla format cookie file.

    Compatible with the format produced by browser extensions such as
    "Get cookies.txt LOCALLY" and with yt-dlp's ``--cookies`` flag.

    :param path: Path to the Netscape cookie file.
    :raises FileNotFoundError: If *path* does not exist.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Cookie file not found: {path}")
    jar = http.cookiejar.MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    logger.info("Loaded %d cookies from %s", len(list(jar)), path)
    return jar


def load_cookies_from_browser(browser: str) -> http.cookiejar.CookieJar:
    """Extract YouTube cookies from a locally installed browser.

    Supported browsers: chrome, chromium, firefox, brave, edge, safari, opera.

    :param browser: Browser name (case-insensitive).
    :raises ValueError: If *browser* is not supported.
    """
    browser = browser.lower()
    if browser not in _SUPPORTED_BROWSERS:
        raise ValueError(
            f"Unsupported browser '{browser}'. "
            f"Choose from: {', '.join(_SUPPORTED_BROWSERS)}"
        )

    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        raise ValueError(f"browser-cookie3 does not support '{browser}'")

    jar = loader(domain_name=".youtube.com")
    logger.info("Loaded cookies from %s browser", browser)
    return jar


def install_cookies(jar: http.cookiejar.CookieJar) -> None:
    """Inject *jar* into pyt's HTTP layer so all subsequent requests carry cookies.

    Patches :func:`pyt.request._execute_request` to use a cookie-aware opener.
    Must be called before any YouTube object is created.
    """
    import json
    import socket
    import urllib.request as ureq
    from pyt import request

    opener = ureq.build_opener(ureq.HTTPCookieProcessor(jar))

    def _patched(
        url,
        method=None,
        headers=None,
        data=None,
        timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    ):
        base = {"User-Agent": "Mozilla/5.0", "accept-language": "en-US,en"}
        if headers:
            base.update(headers)
        if data and not isinstance(data, bytes):
            data = bytes(json.dumps(data), encoding="utf-8")
        if not url.lower().startswith("http"):
            raise ValueError("Invalid URL")
        req = ureq.Request(url, headers=base, method=method, data=data)
        return opener.open(req, timeout=timeout)

    request._execute_request = _patched

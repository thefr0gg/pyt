"""Tests for pyt.cookies — load_cookies_from_file, load_cookies_from_browser, install_cookies."""

import http.cookiejar

import pytest
from unittest import mock

from pyt import cookies as cookies_mod
from pyt.cookies import (
    load_cookies_from_file,
    load_cookies_from_browser,
    install_cookies,
    _SUPPORTED_BROWSERS,
)

# ── load_cookies_from_file ────────────────────────────────────────────────────


def test_load_cookies_raises_for_missing_file():
    with pytest.raises(FileNotFoundError, match="Cookie file not found"):
        load_cookies_from_file("/no/such/file.txt")


def test_load_cookies_returns_cookie_jar(tmp_path):
    # Netscape cookie file format
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tFALSE\t0\tSID\tabc123\n",
        encoding="utf-8",
    )
    jar = load_cookies_from_file(str(cookie_file))
    assert isinstance(jar, http.cookiejar.CookieJar)
    cookies = list(jar)
    assert any(c.name == "SID" and c.value == "abc123" for c in cookies)


def test_load_cookies_empty_file_returns_empty_jar(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    jar = load_cookies_from_file(str(cookie_file))
    assert list(jar) == []


# ── load_cookies_from_browser ─────────────────────────────────────────────────


def test_load_cookies_browser_unsupported_raises():
    with pytest.raises(ValueError, match="Unsupported browser"):
        load_cookies_from_browser("lynx")


def test_load_cookies_browser_calls_correct_loader():
    fake_jar = http.cookiejar.CookieJar()
    mock_bc3 = mock.MagicMock()
    mock_bc3.chrome.return_value = fake_jar

    with mock.patch.object(cookies_mod, "browser_cookie3", mock_bc3):
        result = load_cookies_from_browser("chrome")

    mock_bc3.chrome.assert_called_once_with(domain_name=".youtube.com")
    assert result is fake_jar


def test_load_cookies_browser_case_insensitive():
    fake_jar = http.cookiejar.CookieJar()
    mock_bc3 = mock.MagicMock()
    mock_bc3.firefox.return_value = fake_jar

    with mock.patch.object(cookies_mod, "browser_cookie3", mock_bc3):
        result = load_cookies_from_browser("FIREFOX")

    mock_bc3.firefox.assert_called_once_with(domain_name=".youtube.com")
    assert result is fake_jar


# ── install_cookies ───────────────────────────────────────────────────────────


def test_install_cookies_patches_execute_request():
    from pyt import request as pyt_request

    original = pyt_request._execute_request
    jar = http.cookiejar.CookieJar()

    try:
        install_cookies(jar)
        assert pyt_request._execute_request is not original
    finally:
        pyt_request._execute_request = original


def test_install_cookies_rejects_non_http_url():
    from pyt import request as pyt_request

    original = pyt_request._execute_request
    jar = http.cookiejar.CookieJar()

    try:
        install_cookies(jar)
        with pytest.raises(ValueError, match="Invalid URL"):
            pyt_request._execute_request("ftp://evil.example.com")
    finally:
        pyt_request._execute_request = original

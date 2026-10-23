"""Implements a simple wrapper around urlopen."""

import re
import json
import logging
import socket
import http.client

from urllib import parse
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from functools import lru_cache

from pyt.exceptions import RegexMatchError, MaxRetriesExceeded
from pyt.helpers import regex_search

logger = logging.getLogger(__name__)
default_range_size = 9437184  # 9MB


def _execute_request(
    url, method=None, headers=None, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT
):
    base_headers = {"User-Agent": "Mozilla/5.0", "accept-language": "en-US,en"}
    if headers:
        base_headers.update(headers)
    if data:
        # encode data for request
        if not isinstance(data, bytes):
            data = bytes(json.dumps(data), encoding="utf-8")
    if url.lower().startswith("http"):
        request = Request(url, headers=base_headers, method=method, data=data)
    else:
        raise ValueError("Invalid URL")
    return urlopen(request, timeout=timeout)  # nosec


def get(url, extra_headers=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """Send an http GET request.

    :param str url:
        The URL to perform the GET request for.
    :param dict extra_headers:
        Extra headers to add to the request
    :rtype: str
    :returns:
        UTF-8 encoded string of response
    """
    if extra_headers is None:
        extra_headers = {}
    response = _execute_request(url, headers=extra_headers, timeout=timeout)
    return response.read().decode("utf-8")


def post(url, extra_headers=None, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """Send an http POST request.

    :param str url:
        The URL to perform the POST request for.
    :param dict extra_headers:
        Extra headers to add to the request
    :param dict data:
        The data to send on the POST request
    :rtype: str
    :returns:
        UTF-8 encoded string of response
    """
    # could technically be implemented in get,
    # but to avoid confusion implemented like this
    if extra_headers is None:
        extra_headers = {}
    if data is None:
        data = {}
    # required because the youtube servers are strict on content type
    # raises HTTPError [400]: Bad Request otherwise
    extra_headers.update({"Content-Type": "application/json"})
    response = _execute_request(url, headers=extra_headers, data=data, timeout=timeout)
    return response.read().decode("utf-8")


def seq_stream(url, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, max_retries=0):
    """Read the response in sequence.
    :param str url: The URL to perform the GET request for.
    :rtype: Iterable[bytes]
    """
    # YouTube expects a request sequence number as part of the parameters.
    split_url = parse.urlsplit(url)
    base_url = "%s://%s/%s?" % (split_url.scheme, split_url.netloc, split_url.path)

    querys = dict(parse.parse_qsl(split_url.query))

    # The 0th sequential request provides the file headers, which tell us
    #  information about how the file is segmented.
    querys["sq"] = 0
    url = base_url + parse.urlencode(querys)

    segment_data = b""
    for chunk in stream(url, timeout=timeout, max_retries=max_retries):
        yield chunk
        segment_data += chunk

    # We can then parse the header to find the number of segments
    stream_info = segment_data.split(b"\r\n")
    segment_count_pattern = re.compile(b"Segment-Count: (\\d+)")
    for line in stream_info:
        match = segment_count_pattern.search(line)
        if match:
            segment_count = int(match.group(1).decode("utf-8"))

    # We request these segments sequentially to build the file.
    seq_num = 1
    while seq_num <= segment_count:
        # Create sequential request URL
        querys["sq"] = seq_num
        url = base_url + parse.urlencode(querys)

        yield from stream(url, timeout=timeout, max_retries=max_retries)
        seq_num += 1
    return  # pylint: disable=R1711


def stream(
    url,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    max_retries=0,
    start_byte=0,
    extra_headers=None,
):
    """Read the response in chunks.
    :param str url: The URL to perform the GET request for.
    :param int start_byte: Resume offset — skip this many bytes from the start.
    :rtype: Iterable[bytes]
    """
    file_size: int = default_range_size  # fake filesize to start
    downloaded = start_byte
    cdn_url = url
    # YouTube CDN enforces per-request range size limits that vary by client
    # type and byte offset. Start at the default and halve on 403 to adapt.
    range_size = default_range_size

    while downloaded < file_size:
        stop_pos = min(downloaded + range_size, file_size) - 1
        tries = 0

        while True:
            if tries >= 1 + max_retries:
                raise MaxRetriesExceeded()

            try:
                req_headers = {"Range": f"bytes={downloaded}-{stop_pos}"}
                if extra_headers:
                    req_headers.update(extra_headers)
                response = _execute_request(
                    cdn_url, method="GET", headers=req_headers, timeout=timeout
                )
                if cdn_url == url:
                    cdn_url = response.geturl()
            except HTTPError as e:
                if e.code == 403 and range_size > 524288:
                    range_size //= 2
                    stop_pos = min(downloaded + range_size, file_size) - 1
                    continue  # retry with smaller chunk; don't count as a try
                raise
            except URLError as e:
                if isinstance(e.reason, socket.timeout):
                    pass
                else:
                    raise
            except http.client.IncompleteRead:
                pass
            else:
                break
            tries += 1

        if file_size == default_range_size:
            try:
                content_range = response.info().get("Content-Range", "")
                if content_range:
                    # "bytes 0-9437183/263700000" → 263700000
                    file_size = int(content_range.split("/")[-1])
            except (IndexError, ValueError, TypeError) as e:
                logger.error(e)

        while True:
            chunk = response.read()
            if not chunk:
                break
            downloaded += len(chunk)
            yield chunk
    return  # pylint: disable=R1711


def sabr_stream(
    sabr_url: str,
    itag: int,
    ustreamer_config=None,
    po_token=None,
    is_video: bool = True,
    filesize: int = 0,
    duration_ms: int = 0,
    already_downloaded: int = 0,
    client_info=None,
    refresh_callback=None,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
):
    """Stream media bytes via YouTube's SABR protocol.

    :param str sabr_url: serverAbrStreamingUrl from the InnerTube player response.
    :param int itag: Format itag to request.
    :param bytes ustreamer_config: Raw videoPlaybackUstreamerConfig bytes.
    :param str po_token: Optional proof-of-origin token (base64url).
    :param bool is_video: True for video streams, False for audio.
    :param int filesize: Total stream size in bytes.
    :param int duration_ms: Video duration in milliseconds.
    :param int already_downloaded: Bytes already on disk (resume offset).
    :param dict client_info: ClientInfo subset (clientName/Version/etc.) embedded in StreamerContext.
    :param callable refresh_callback: () -> (new_sabr_url, new_ustreamer_config) for token refresh.
    :rtype: Iterable[bytes]
    """
    from pyt.sabr.client import SabrClient

    yield from SabrClient(
        sabr_url=sabr_url,
        itag=itag,
        ustreamer_config=ustreamer_config,
        po_token=po_token,
        is_video=is_video,
        filesize=filesize,
        duration_ms=duration_ms,
        already_downloaded=already_downloaded,
        client_info=client_info,
        refresh_callback=refresh_callback,
    ).stream(timeout=timeout)


@lru_cache()
def filesize(url):
    """Fetch size in bytes of file at given URL

    :param str url: The URL to get the size of
    :returns: int: size in bytes of remote file
    """
    return int(head(url)["content-length"])


@lru_cache()
def seq_filesize(url):
    """Fetch size in bytes of file at given URL from sequential requests

    :param str url: The URL to get the size of
    :returns: int: size in bytes of remote file
    """
    total_filesize = 0
    # YouTube expects a request sequence number as part of the parameters.
    split_url = parse.urlsplit(url)
    base_url = "%s://%s/%s?" % (split_url.scheme, split_url.netloc, split_url.path)
    querys = dict(parse.parse_qsl(split_url.query))

    # The 0th sequential request provides the file headers, which tell us
    #  information about how the file is segmented.
    querys["sq"] = 0
    url = base_url + parse.urlencode(querys)
    response = _execute_request(url, method="GET")

    response_value = response.read()
    # The file header must be added to the total filesize
    total_filesize += len(response_value)

    # We can then parse the header to find the number of segments
    segment_count = 0
    stream_info = response_value.split(b"\r\n")
    segment_regex = b"Segment-Count: (\\d+)"
    for line in stream_info:
        # One of the lines should contain the segment count, but we don't know
        #  which, so we need to iterate through the lines to find it
        try:
            segment_count = int(regex_search(segment_regex, line, 1))
        except RegexMatchError:
            pass

    if segment_count == 0:
        raise RegexMatchError("seq_filesize", segment_regex)

    # We make HEAD requests to the segments sequentially to find the total filesize.
    seq_num = 1
    while seq_num <= segment_count:
        # Create sequential request URL
        querys["sq"] = seq_num
        url = base_url + parse.urlencode(querys)

        total_filesize += int(head(url)["content-length"])
        seq_num += 1
    return total_filesize


def head(url):
    """Fetch headers returned http GET request.

    :param str url:
        The URL to perform the GET request for.
    :rtype: dict
    :returns:
        dictionary of lowercase headers
    """
    response_headers = _execute_request(url, method="HEAD").info()
    return {k.lower(): v for k, v in response_headers.items()}

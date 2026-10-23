"""SabrSession: stateful SABR protocol driver.

Holds per-format state across the multi-request SABR exchange: header_id ->
itag map, segment indices, buffered ranges, init-segment flag, and bytes
downloaded. Drives the request loop, dispatches every UMP part type we need
(MEDIA_HEADER, MEDIA, MEDIA_END, NEXT_REQUEST_POLICY, FORMAT_INITIALIZATION_METADATA,
SABR_REDIRECT, SABR_ERROR, STREAM_PROTECTION_STATUS) and yields demuxed
(itag, chunk) pairs to the caller.

Replaces the empty-rounds heuristic from the previous SabrClient with explicit
termination signals from the server.

Pass live=True for a live broadcast: termination then comes from stop() or a
recognized SABR_ERROR broadcast-end signal instead of a fixed segment count,
and SABR_SEEK resets buffered-range state to the server-directed position.
LIVE_METADATA is parsed but not yet acted on - its field layout is unconfirmed.
"""

from __future__ import annotations

import gzip
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple
from urllib.error import HTTPError, URLError

from pyt import request
from pyt.sabr.client import build_video_playback_abr_request
from pyt.sabr.ump import (
    UmpParser,
    decode_ump_varint,
    parse_format_init_metadata,
    parse_live_metadata,
    parse_media_header,
    parse_next_request_policy,
    parse_reload_player_response,
    parse_sabr_context_sending_policy,
    parse_sabr_context_update,
    parse_sabr_error,
    parse_sabr_redirect,
    parse_sabr_seek,
    parse_stream_protection_status,
    UMP_FORMAT_INITIALIZATION_METADATA,
    UMP_LIVE_METADATA,
    UMP_MEDIA,
    UMP_MEDIA_END,
    UMP_MEDIA_HEADER,
    UMP_NEXT_REQUEST_POLICY,
    UMP_RELOAD_PLAYER_RESPONSE,
    UMP_SABR_CONTEXT_SENDING_POLICY,
    UMP_SABR_CONTEXT_UPDATE,
    UMP_SABR_ERROR,
    UMP_SABR_REDIRECT,
    UMP_SABR_SEEK,
    UMP_STREAM_PROTECTION_STATUS,
)

logger = logging.getLogger(__name__)


class SabrError(Exception):
    """Raised when the SABR server returns an unrecoverable error."""


class SabrAttestationRequired(SabrError):
    """STREAM_PROTECTION_STATUS=3 - PO token required and missing/invalid."""


@dataclass
class FormatState:
    """Per-itag bookkeeping for the SABR exchange."""

    itag: int
    is_video: bool
    expected_bytes: int = 0
    expected_duration_ms: int = 0

    downloaded_bytes: int = 0
    last_segment_index: int = -1
    end_segment_index: Optional[int] = None
    # Highest media-time end (start_ms + duration_ms) seen for this format.
    # Drives client_abr_state.player_time_ms — without this the server thinks
    # we're at t=0 and throttles delivery to ~real-time pace.
    buffered_end_ms: int = 0
    received_init: bool = False
    finished: bool = False
    # Tracks which header_ids in the current response stream apply to this format.
    active_header_ids: set = field(default_factory=set)
    # Live-only: no fixed end_segment_index/expected_bytes exists for an open
    # broadcast, so termination and buffered-range reporting take a different
    # path (see SabrSession._all_finished / _build_request_body).
    is_live: bool = False
    live_window_segments: int = 5


@dataclass
class _ResumeState:
    downloaded_bytes: int
    last_segment_index: int


# Tuned to mirror yt-dlp's defaults.
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_BACKOFF_BASE = 1.0
_DEFAULT_BACKOFF_CAP = 30.0

# After this many consecutive rounds that produced zero bytes AND came without
# a server-mandated NEXT_REQUEST_POLICY backoff, the session is considered
# stalled and raises SabrError. Server-requested ad pauses do NOT count toward
# this — they advance through the backoff path with the counter reset.
_MAX_STALL_ROUNDS = 8

# When NOT in an ad-enforcement window, cap the server's requested backoff to
# this many ms. Without this cap, "target_readahead reached" backoffs slow VOD
# downloads to real-time playback speed because the server enforces its
# readahead targets when we under-report player_time_ms. We still honor the
# full backoff when CONTENT_ADS scope context is active.
_NON_AD_BACKOFF_CAP_MS = 200

# Scope value for CONTENT_ADS in SabrContextUpdate.SabrContextScope.
_SABR_CTX_SCOPE_CONTENT_ADS = 4


class SabrSession:
    """Drive a SABR streaming exchange for one or more formats.

    Caller pattern:
        session = SabrSession(sabr_url, ustreamer_config, formats=[(140, False)],
                              po_token=..., client_info=..., duration_ms=...)
        for itag, chunk in session.iter_chunks():
            handle(itag, chunk)
    """

    def __init__(
        self,
        sabr_url: str,
        ustreamer_config: Optional[bytes],
        formats: List[Tuple[int, bool]],
        *,
        po_token: Optional[bytes] = None,
        client_info: Optional[dict] = None,
        duration_ms: int = 0,
        expected_sizes: Optional[Dict[int, int]] = None,
        resume: Optional[Dict[int, _ResumeState]] = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
        refresh_callback: Optional[
            Callable[[], Tuple[Optional[str], Optional[bytes]]]
        ] = None,
        live: bool = False,
    ):
        self._sabr_url = sabr_url
        self._ustreamer_config = ustreamer_config
        self._po_token = po_token
        self._client_info = client_info or {}
        self._duration_ms = duration_ms
        self._max_retries = max_retries
        self._timeout = timeout
        self._refresh_callback = refresh_callback
        self._live = live
        self._stop_requested = False
        self._broadcast_ended = False

        sizes = expected_sizes or {}
        resume = resume or {}
        self._formats: Dict[int, FormatState] = {}
        for itag, is_video in formats:
            r = resume.get(itag)
            self._formats[itag] = FormatState(
                itag=itag,
                is_video=is_video,
                expected_bytes=sizes.get(itag, 0),
                expected_duration_ms=duration_ms,
                downloaded_bytes=r.downloaded_bytes if r else 0,
                last_segment_index=r.last_segment_index if r else -1,
                is_live=live,
            )

        # Maps an in-flight header_id to the format it belongs to. Lifetime is
        # one HTTP response (yt-dlp resets between rounds).
        self._header_to_itag: Dict[int, int] = {}
        # Initialized format ids: server may skip the init segment for these.
        self._initialized_itags: set = set()
        # Server-suggested back-off between rounds (ad pause, throttling, etc.).
        self._next_backoff_ms = 0
        # Counter of consecutive empty rounds without a server backoff. A round
        # that DID receive a backoff signal does not increment this — that's an
        # ad-enforcement / playback-start delay, not a stall.
        self._stall_rounds = 0
        # Optional playback cookie returned by NEXT_REQUEST_POLICY; included on
        # subsequent requests so the server keeps the same logical session.
        self._playback_cookie: Optional[bytes] = None
        # SABR context updates from the server (type -> value). Sent back in
        # StreamerContext.sabr_contexts when present in `_contexts_to_send`.
        # CONTENT_ADS scope (4) updates are how the server enforces unskippable
        # ad pauses on subsequent rounds.
        self._sabr_contexts: Dict[int, bytes] = {}
        self._sabr_context_scopes: Dict[int, int] = {}
        self._contexts_to_send: set = set()

    # -- Public iteration ------------------------------------------------------

    def stop(self) -> None:
        """Request a clean stop of a live session after the in-flight round.

        No-op effect on VOD sessions beyond ending early — callers only need
        this for live downloads, which have no natural end-of-stream."""
        self._stop_requested = True

    def iter_chunks(self) -> Iterator[Tuple[int, bytes]]:
        """Yield (itag, chunk) pairs until every requested format is finished."""
        rn = 0
        url = self._sabr_url
        retries_remaining = self._max_retries

        while not (
            self._stop_requested
            or (self._live and self._broadcast_ended)
            or self._all_finished()
        ):
            if self._next_backoff_ms > 0:
                # Honor full backoff during ad enforcement; cap it otherwise.
                # The server applies readahead-based throttling that can request
                # multi-second waits between rounds — fine for a player, slow
                # for a downloader. The aggressive player_time_ms in the next
                # request body usually causes the server to skip the throttle
                # entirely on the following round.
                wait_ms = self._next_backoff_ms
                if not self._ad_active():
                    wait_ms = min(wait_ms, _NON_AD_BACKOFF_CAP_MS)
                if wait_ms > 0:
                    time.sleep(wait_ms / 1000.0)
                self._next_backoff_ms = 0

            body = self._build_request_body()
            try:
                yield from self._run_round(url, rn, body)
            except _Redirect as redir:
                url = redir.new_url
                logger.debug("SABR redirect -> %s", url)
                continue
            except _NeedRefresh:
                if not self._refresh_callback:
                    raise SabrError(
                        "SABR session expired and no refresh callback was provided"
                    )
                new_url, new_cfg = self._refresh_callback()
                if not new_url:
                    raise SabrError("Refresh callback returned no SABR URL")
                url = new_url
                self._sabr_url = new_url
                if new_cfg:
                    self._ustreamer_config = new_cfg
                continue
            except (HTTPError, URLError, ConnectionError, TimeoutError) as exc:
                if retries_remaining <= 0:
                    raise
                retries_remaining -= 1
                backoff = min(
                    _DEFAULT_BACKOFF_CAP,
                    _DEFAULT_BACKOFF_BASE
                    * (2 ** (self._max_retries - retries_remaining - 1)),
                )
                logger.warning(
                    "SABR transport error %s; retrying in %.1fs", exc, backoff
                )
                time.sleep(backoff)
                continue

            rn += 1
            # The header_id -> itag map is per-response.
            self._header_to_itag.clear()

    # -- Round execution -------------------------------------------------------

    def _build_request_body(self) -> bytes:
        video_itag: Optional[int] = None
        audio_itag: Optional[int] = None
        active_formats = []
        for fmt in self._formats.values():
            if fmt.finished:
                continue
            active_formats.append(fmt)
            if fmt.is_video and video_itag is None:
                video_itag = fmt.itag
            elif not fmt.is_video and audio_itag is None:
                audio_itag = fmt.itag

        # Buffered ranges advertise what we already have so the server skips it.
        # Live formats report a sliding window near the edge instead of a
        # from-zero range — there's no bounded total to advertise, and a
        # from-zero range would grow unboundedly over a multi-hour broadcast.
        buffered_ranges = []
        for fmt in self._formats.values():
            if fmt.last_segment_index < 0:
                continue
            start_segment_index = (
                max(0, fmt.last_segment_index - fmt.live_window_segments)
                if fmt.is_live
                else 0
            )
            buffered_ranges.append(
                {
                    "itag": fmt.itag,
                    "start_time_ms": 0,
                    "duration_ms": fmt.buffered_end_ms
                    or self._estimate_player_time(fmt),
                    "start_segment_index": start_segment_index,
                    "end_segment_index": fmt.last_segment_index,
                }
            )

        # Set player_time_ms aggressively forward: report the MIN buffered_end
        # across active formats so the server stops applying readahead-based
        # throttle and just sends us the next chunks. Falls back to the byte-
        # fraction estimate when the server hasn't sent media-time info yet.
        # Live formats never use the byte-fraction estimate - expected_bytes/
        # expected_duration_ms don't exist for an open-ended broadcast.
        if active_formats:
            ends = []
            for fmt in active_formats:
                if fmt.buffered_end_ms:
                    ends.append(fmt.buffered_end_ms)
                elif not fmt.is_live:
                    est = self._estimate_player_time(fmt)
                    if est:
                        ends.append(est)
            player_time_ms = min(ends) if ends else 0
        else:
            player_time_ms = 0

        # Build the (type, value) list of contexts the server asked us to send.
        sabr_contexts = [
            (t, self._sabr_contexts[t])
            for t in sorted(self._contexts_to_send)
            if t in self._sabr_contexts
        ]
        # Types currently registered but NOT being sent (server uses this to
        # detect dropped updates).
        unsent = sorted(set(self._sabr_contexts) - self._contexts_to_send)

        return build_video_playback_abr_request(
            video_itag=video_itag,
            audio_itag=audio_itag,
            ustreamer_config=self._ustreamer_config,
            po_token=self._po_token,
            client_info=self._client_info,
            initialized_itags=sorted(self._initialized_itags),
            buffered_ranges=buffered_ranges,
            player_time_ms=player_time_ms,
            playback_cookie=self._playback_cookie,
            sabr_contexts=sabr_contexts,
            unsent_sabr_contexts=unsent,
        )

    def _run_round(self, url: str, rn: int, body: bytes) -> Iterator[Tuple[int, bytes]]:
        sep = "&" if "?" in url else "?"
        full_url = f"{url}{sep}rn={rn}"
        response = request._execute_request(
            full_url,
            method="POST",
            headers={
                "Content-Type": "application/x-protobuf",
                "Accept": "application/vnd.yt-ump",
                "Accept-Encoding": "gzip",
            },
            data=body,
            timeout=self._timeout,
        )
        # urllib does not auto-decompress; wrap if needed.
        if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
            response = gzip.GzipFile(fileobj=response)

        parser = UmpParser()
        # Track formats that produced bytes this round - used as a liveness signal.
        produced_any = False

        for part_type, payload in parser.feed(response):
            if part_type == UMP_MEDIA_HEADER:
                self._handle_media_header(payload)

            elif part_type == UMP_MEDIA:
                if not payload:
                    continue
                header_id, media_pos = decode_ump_varint(payload, 0)
                itag = self._header_to_itag.get(header_id)
                if itag is None:
                    continue
                chunk = payload[media_pos:]
                if not chunk:
                    continue
                fmt = self._formats[itag]
                fmt.downloaded_bytes += len(chunk)
                produced_any = True
                yield itag, chunk

            elif part_type == UMP_MEDIA_END:
                if not payload:
                    continue
                header_id, _ = decode_ump_varint(payload, 0)
                itag = self._header_to_itag.get(header_id)
                if itag is None:
                    continue
                fmt = self._formats[itag]
                # MEDIA_END for a non-init segment of the final segment_index
                # means this format is done.
                if (
                    fmt.end_segment_index is not None
                    and fmt.last_segment_index >= fmt.end_segment_index
                ):
                    fmt.finished = True

            elif part_type == UMP_FORMAT_INITIALIZATION_METADATA:
                self._handle_format_init(payload)

            elif part_type == UMP_NEXT_REQUEST_POLICY:
                policy = parse_next_request_policy(payload)
                backoff = policy.get("backoff_time_ms") or 0
                if backoff:
                    # Ad enforcement / playback-start delay: server is asking us
                    # to wait before the next request. This is a normal SABR
                    # signal, not an error. Resets the stall counter.
                    self._next_backoff_ms = max(self._next_backoff_ms, int(backoff))
                    logger.debug("SABR backoff requested: %d ms", backoff)
                cookie = policy.get("playback_cookie")
                if cookie:
                    self._playback_cookie = (
                        bytes(cookie)
                        if isinstance(cookie, (bytes, bytearray))
                        else cookie
                    )

            elif part_type == UMP_STREAM_PROTECTION_STATUS:
                status = parse_stream_protection_status(payload)
                # Status enum (StreamProtectionStatus.Status):
                #   1 = OK
                #   2 = ATTESTATION_PENDING
                #   3 = ATTESTATION_REQUIRED  -> need a valid PO token
                code = status.get("status")
                if code == 3:
                    raise SabrAttestationRequired(
                        "YouTube requires a valid PO token (STREAM_PROTECTION_STATUS=ATTESTATION_REQUIRED)"
                    )
                if code == 2:
                    logger.debug("SABR attestation pending (status=2)")

            elif part_type == UMP_SABR_REDIRECT:
                new_url = parse_sabr_redirect(payload)
                if new_url:
                    raise _Redirect(new_url)

            elif part_type == UMP_SABR_ERROR:
                err = parse_sabr_error(payload)
                err_type = (err.get("type") or "").lower()
                # yt-dlp treats type containing 'lapsed' or status_code 3 as a
                # signal to fetch a fresh player response. Other errors abort.
                if "lapsed" in err_type or err.get("status_code") == 3:
                    raise _NeedRefresh()
                if self._live and "ended" in err_type:
                    # Broadcast-end signal for a live session - exact type
                    # string unconfirmed against real traffic (tracked as a
                    # follow-up); treat as clean termination, not a crash.
                    logger.debug("SABR_ERROR treated as broadcast end: %s", err)
                    self._broadcast_ended = True
                    return
                raise SabrError(
                    f"SABR_ERROR type={err.get('type')!r} action={err.get('action')} "
                    f"status_code={err.get('status_code')} error_type={err.get('error_type')}"
                )

            elif part_type == UMP_SABR_CONTEXT_UPDATE:
                self._handle_sabr_context_update(parse_sabr_context_update(payload))

            elif part_type == UMP_SABR_CONTEXT_SENDING_POLICY:
                self._handle_sabr_context_sending_policy(
                    parse_sabr_context_sending_policy(payload)
                )

            elif part_type == UMP_SABR_SEEK:
                seek = parse_sabr_seek(payload)
                logger.debug("SABR_SEEK %s", seek)
                self._handle_sabr_seek(seek)

            elif part_type == UMP_LIVE_METADATA:
                # Field layout unconfirmed - see parse_live_metadata. Inert
                # for now; just surfaces raw fields for future decoding.
                logger.debug("LIVE_METADATA %s", parse_live_metadata(payload))

            elif part_type == UMP_RELOAD_PLAYER_RESPONSE:
                # Server considers the current player response/URLs stale.
                # Observed on real traffic preceding a stall: continuing to
                # request against the same SABR/direct URLs after this
                # fires leads nowhere (and the byte-range fallback 403s on
                # the now-dead direct URL too). Treat it the same as a
                # "lapsed" SABR_ERROR - refetch via refresh_callback.
                logger.debug(
                    "RELOAD_PLAYER_RESPONSE %s", parse_reload_player_response(payload)
                )
                raise _NeedRefresh()

            else:
                logger.debug(
                    "SABR unhandled part type=%d len=%d", part_type, len(payload)
                )

        # Stall accounting: a round with no media is fine if the server told us
        # to back off (ad enforcement, throttling). Otherwise count it; raise
        # once we've burned _MAX_STALL_ROUNDS in a row with no progress and no
        # explicit pause signal.
        if produced_any:
            self._stall_rounds = 0
        elif self._next_backoff_ms > 0:
            self._stall_rounds = 0
        else:
            self._stall_rounds += 1
            if self._stall_rounds >= _MAX_STALL_ROUNDS:
                raise SabrError(
                    f"SABR stalled: {_MAX_STALL_ROUNDS} consecutive empty rounds with no server backoff"
                )

    # -- Helpers ---------------------------------------------------------------

    def _handle_media_header(self, payload: bytes) -> None:
        info = parse_media_header(payload)
        itag = info.get("itag")
        header_id = info.get("header_id")
        if itag is None or header_id is None:
            return
        fmt = self._formats.get(itag)
        if fmt is None:
            return  # server sent a format we didn't ask for; ignore

        # Per-MEDIA gzip is rare/unused in the wild; yt-dlp also refuses it.
        # We rely on HTTP-level Accept-Encoding: gzip for compression.
        compression = info.get("compression")
        if compression and compression != 1:  # 1 = NONE
            raise SabrError(
                f"MEDIA_HEADER compression={compression} not supported (itag={itag})"
            )

        self._header_to_itag[header_id] = itag
        self._initialized_itags.add(itag)

        if info.get("is_init_seg"):
            fmt.received_init = True
            return

        seq = info.get("sequence_number")
        if seq is not None and seq > fmt.last_segment_index:
            fmt.last_segment_index = int(seq)

        if info.get("content_length") and not fmt.expected_bytes:
            fmt.expected_bytes = int(info["content_length"])

        start_ms = info.get("start_ms") or 0
        dur_ms = info.get("duration_ms") or 0
        seg_end_ms = int(start_ms) + int(dur_ms)
        if seg_end_ms > fmt.buffered_end_ms:
            fmt.buffered_end_ms = seg_end_ms

    def _handle_format_init(self, payload: bytes) -> None:
        info = parse_format_init_metadata(payload)
        itag = info.get("itag")
        if itag is None:
            return
        fmt = self._formats.get(itag)
        if fmt is None:
            return
        # `total_segments` is the number of segments in the stream; the last
        # segment number is total_segments (1-indexed in yt-dlp's model).
        total = info.get("total_segments")
        if total is not None and not fmt.is_live:
            # A live format has no fixed total; ignore a provisional value
            # the server might still send so it can't falsely end the loop.
            fmt.end_segment_index = int(total)

    def _handle_sabr_seek(self, seek: dict) -> None:
        seek_ticks = seek.get("seek_time_ticks")
        timescale = seek.get("timescale")
        if seek_ticks is None or not timescale:
            return
        seek_ms = int(seek_ticks * 1000 / timescale)
        for fmt in self._formats.values():
            if not fmt.is_live:
                # VOD should not legitimately receive this; don't let an
                # unexpected seek corrupt an in-progress VOD download.
                continue
            fmt.buffered_end_ms = seek_ms
            fmt.last_segment_index = -1

    def _handle_sabr_context_update(self, ctx: dict) -> None:
        ctype = ctx.get("type")
        value = ctx.get("value")
        write_policy = ctx.get("write_policy")
        if ctype is None or value is None:
            return
        if write_policy == 2 and ctype in self._sabr_contexts:
            return
        self._sabr_contexts[ctype] = (
            bytes(value) if isinstance(value, (bytes, bytearray)) else value
        )
        scope = ctx.get("scope")
        if scope is not None:
            self._sabr_context_scopes[ctype] = int(scope)
        if ctx.get("send_by_default"):
            self._contexts_to_send.add(ctype)
        logger.debug(
            "SABR context registered type=%s scope=%s send_by_default=%s",
            ctype,
            scope,
            ctx.get("send_by_default"),
        )

    def _handle_sabr_context_sending_policy(self, policy: dict) -> None:
        for t in policy.get("start_policy", []):
            self._contexts_to_send.add(int(t))
        for t in policy.get("stop_policy", []):
            self._contexts_to_send.discard(int(t))
        for t in policy.get("discard_policy", []):
            self._sabr_contexts.pop(int(t), None)
            self._sabr_context_scopes.pop(int(t), None)
            self._contexts_to_send.discard(int(t))

    def _ad_active(self) -> bool:
        """True if any active context is scoped to CONTENT_ADS — i.e., the
        server is currently enforcing an ad. Backoffs during ad enforcement
        must be honored fully; outside ads they're throttling we cap."""
        for ctype in self._contexts_to_send:
            if self._sabr_context_scopes.get(ctype) == _SABR_CTX_SCOPE_CONTENT_ADS:
                return True
        return False

    def _all_finished(self) -> bool:
        for fmt in self._formats.values():
            if fmt.finished:
                continue
            if fmt.is_live:
                # No fixed end exists for an open broadcast; termination is
                # driven by stop()/_broadcast_ended in iter_chunks instead.
                return False
            # Reaching the announced end_segment is authoritative.
            if (
                fmt.end_segment_index is not None
                and fmt.last_segment_index >= fmt.end_segment_index
            ):
                fmt.finished = True
                continue
            # Reaching expected_bytes is authoritative ONLY when we got that
            # value from the server (MEDIA_HEADER.content_length), not from a
            # caller-supplied estimate. Without an end_segment we still trust
            # it because that's the only size truth we have.
            if fmt.expected_bytes and fmt.downloaded_bytes >= fmt.expected_bytes:
                fmt.finished = True
                continue
            return False
        return True

    def _estimate_player_time(self, fmt: FormatState) -> int:
        if not fmt.expected_bytes or not fmt.expected_duration_ms:
            return 0
        return int(fmt.downloaded_bytes * fmt.expected_duration_ms / fmt.expected_bytes)

    # -- Introspection ---------------------------------------------------------

    def state(self, itag: int) -> Optional[FormatState]:
        return self._formats.get(itag)


class _Redirect(Exception):
    def __init__(self, new_url: str):
        self.new_url = new_url


class _NeedRefresh(Exception):
    pass

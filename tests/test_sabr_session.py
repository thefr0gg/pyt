"""End-to-end tests for ``pyt.sabr.session.SabrSession`` driven by
hand-built UMP responses.

These tests make ``SabrSession.iter_chunks`` follow real-looking
exchanges without touching the network. The goal is regression
coverage on:

* Single-format completion (typical audio download)
* Multi-format multiplex (combined video+audio)
* Server-mandated backoffs, including the cap-outside-ads behavior
* SABR_REDIRECT mid-stream
* SABR_ERROR with type=="lapsed" → refresh callback
* STREAM_PROTECTION_STATUS=3 → SabrAttestationRequired
* Stall detection (consecutive empty rounds)
* gzip-encoded response bodies
* Player-time advancement to buffered_end_ms

We patch ``time.sleep`` everywhere — even when SabrSession honors a
backoff, we don't want the tests to actually sleep.
"""

from __future__ import annotations

from unittest import mock

import pytest

from pyt.sabr.session import (
    SabrAttestationRequired,
    SabrError,
    SabrSession,
)
from tests.sabr_fixtures import (
    MockResponse,
    build_multi_format_response,
    build_segment_response,
    concat_parts,
    gzip_wrap,
    make_format_init,
    make_media,
    make_media_end,
    make_media_header,
    make_next_request_policy,
    make_sabr_context_update,
    make_sabr_context_sending_policy,
    make_sabr_error,
    make_sabr_redirect,
    make_stream_protection_status,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _drive_session(
    session: SabrSession,
    responses: list,
) -> list:
    """Run ``session.iter_chunks()`` to completion, returning the list
    of yielded (itag, chunk) tuples.

    ``responses`` is consumed in order — one entry per round. Each entry
    is either bytes (interpreted as the UMP body, no Content-Encoding)
    or a tuple ``(body, headers)`` for Content-Encoding=gzip etc.
    """
    iter_responses = iter(responses)

    def fake_execute_request(url, *, method, headers, data, timeout):
        try:
            entry = next(iter_responses)
        except StopIteration:
            raise AssertionError(
                "SabrSession requested more rounds than the test fixture provided "
                "— check the response sequence"
            )
        if isinstance(entry, tuple):
            body, hdrs = entry
        else:
            body, hdrs = entry, {}
        return MockResponse(body, headers=hdrs)

    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=fake_execute_request,
    ), mock.patch("pyt.sabr.session.time.sleep"):
        return list(session.iter_chunks())


# ── Single-format happy path ────────────────────────────────────────────


def test_single_format_init_then_one_segment():
    """The simplest possible exchange: an init segment, a data segment,
    end_segment_index=1 → session terminates cleanly."""
    body = concat_parts(
        # Round 1, response 1: init segment + first data segment in one
        # response so the session sees both before deciding to stop.
        make_format_init(itag=140, total_segments=1),
        # Init seg
        make_media_header(header_id=1, itag=140, is_init_seg=True),
        make_media(header_id=1, data=b"INIT"),
        make_media_end(header_id=1),
        # Data seg
        make_media_header(
            header_id=2,
            itag=140,
            sequence_number=1,
            content_length=10,
            start_ms=0,
            duration_ms=5000,
        ),
        make_media(header_id=2, data=b"AUDIODATA0"),
        make_media_end(header_id=2),
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],  # itag, is_video
        duration_ms=5000,
    )
    chunks = _drive_session(session, [body])
    # Init segment + one data segment = two MEDIA chunks.
    assert chunks == [(140, b"INIT"), (140, b"AUDIODATA0")]
    state = session.state(140)
    assert state is not None
    assert state.finished
    assert state.last_segment_index == 1


def test_single_format_chunks_split_across_responses():
    """A real download often takes multiple HTTP requests to finish.
    Verify the session keeps state across rounds."""
    # Don't set content_length on the per-segment MEDIA_HEADERs — the
    # session reads the FIRST one's content_length as the *format*
    # total size, and would early-terminate when downloaded ≥ that
    # length. end_segment_index is the right termination signal here.
    r1 = concat_parts(
        make_format_init(itag=140, total_segments=2),
        make_media_header(header_id=1, itag=140, sequence_number=1),
        make_media(header_id=1, data=b"AAAA"),
        make_media_end(header_id=1),
    )
    r2 = concat_parts(
        make_media_header(header_id=2, itag=140, sequence_number=2),
        make_media(header_id=2, data=b"BBBB"),
        make_media_end(header_id=2),
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    chunks = _drive_session(session, [r1, r2])
    assert chunks == [(140, b"AAAA"), (140, b"BBBB")]


# ── Multi-format multiplex ──────────────────────────────────────────────


def test_multi_format_multiplexed_in_one_response():
    """Two formats delivered in a single SABR response. Bytes must
    demux to the right itag. This is the path the user's
    CombinedDownload exercises."""
    body = build_multi_format_response(
        formats=[
            (1, 137, b"VIDEO_BYTES"),  # video
            (2, 140, b"AUDIO_BYTES"),  # audio
        ],
        end_segment_index=1,
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(137, True), (140, False)],
    )
    chunks = _drive_session(session, [body])
    # Order is the order the server sent them; we just verify both
    # formats got their bytes.
    by_itag = {}
    for itag, data in chunks:
        by_itag.setdefault(itag, b"")
        by_itag[itag] += data
    assert by_itag[137] == b"VIDEO_BYTES"
    assert by_itag[140] == b"AUDIO_BYTES"


def test_multi_format_unrequested_format_is_dropped():
    """The server occasionally delivers bytes for a format we didn't
    request. The session must silently drop those rather than corrupt
    the bytes for a format we did request."""
    body = build_multi_format_response(
        formats=[
            (1, 137, b"VIDEO_BYTES"),  # we want this
            (2, 251, b"UNWANTED_AUDIO"),  # we did NOT request this itag
        ],
        end_segment_index=1,
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(137, True)],  # only video
    )
    chunks = _drive_session(session, [body])
    assert chunks == [(137, b"VIDEO_BYTES")]


# ── STREAM_PROTECTION_STATUS=3 → SabrAttestationRequired ────────────────


def test_attestation_required_raises():
    """status=3 is the trigger for the modern API's AttestationRequired
    error. Verify the protocol-level translation works."""
    body = make_stream_protection_status(3)
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    with pytest.raises(SabrAttestationRequired, match="ATTESTATION_REQUIRED"):
        _drive_session(session, [body])


def test_attestation_pending_does_not_raise():
    """status=2 is fine — server is just telling us a token would be
    nice but we can keep going without one. We follow up with a real
    delivery so the session has a way to terminate."""
    r1 = make_stream_protection_status(2)
    r2 = build_segment_response(
        header_id=1,
        itag=140,
        sequence_number=1,
        data=b"OK",
        end_segment_index=1,
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    chunks = _drive_session(session, [r1, r2])
    assert chunks == [(140, b"OK")]


# ── SABR_REDIRECT ────────────────────────────────────────────────────────


def test_redirect_sends_next_round_to_new_url():
    """A SABR_REDIRECT terminates the current round; the next round
    must hit the redirected URL. We capture the URL stream to verify."""
    redirect_url = "https://sabr.test/play-redirected"
    r1 = make_sabr_redirect(redirect_url)
    r2 = build_segment_response(
        header_id=1,
        itag=140,
        sequence_number=1,
        data=b"DATA",
        end_segment_index=1,
    )

    seen_urls = []
    iter_responses = iter([r1, r2])

    def fake_execute_request(url, *, method, headers, data, timeout):
        seen_urls.append(url)
        return MockResponse(next(iter_responses), headers={})

    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=fake_execute_request,
    ), mock.patch("pyt.sabr.session.time.sleep"):
        chunks = list(session.iter_chunks())

    assert chunks == [(140, b"DATA")]
    # The first request hit the original URL; the second hit the redirect.
    assert "play-redirected" in seen_urls[1]
    assert "play-redirected" not in seen_urls[0]


# ── SABR_ERROR + refresh callback ───────────────────────────────────────


def test_sabr_error_lapsed_triggers_refresh_callback():
    """A SABR_ERROR with type containing 'lapsed' (or status_code=3)
    means the session token expired. The refresh callback gets called
    and the session continues with the new URL/config."""
    refresh_called = []

    def refresh():
        refresh_called.append(True)
        return ("https://sabr.test/play-fresh", b"new-cfg-bytes")

    r1 = make_sabr_error(type="server.token_lapsed")
    r2 = build_segment_response(
        header_id=1,
        itag=140,
        sequence_number=1,
        data=b"OK",
        end_segment_index=1,
    )

    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=b"old-cfg",
        formats=[(140, False)],
        refresh_callback=refresh,
    )
    chunks = _drive_session(session, [r1, r2])
    assert chunks == [(140, b"OK")]
    assert refresh_called == [True]


def test_sabr_error_without_refresh_callback_raises():
    """If the session's configured without a refresh callback and the
    server sends a 'lapsed' SABR_ERROR, we have no way to recover —
    must raise."""
    r1 = make_sabr_error(type="server.token_lapsed")
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    with pytest.raises(SabrError, match="refresh callback"):
        _drive_session(session, [r1])


def test_sabr_error_unrelated_type_raises_without_refresh():
    """Errors that don't contain 'lapsed' and don't carry status_code=3
    abort the whole session — we have no recovery path for them."""
    r1 = make_sabr_error(type="server.bad_token", status_code=401)
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    with pytest.raises(SabrError, match="server.bad_token"):
        _drive_session(session, [r1])


# ── NEXT_REQUEST_POLICY backoff ─────────────────────────────────────────


def test_next_request_policy_backoff_capped_outside_ads():
    """The server requests a 5-second backoff (real-time playback
    pacing). Outside an ad window, the session should cap it at 200ms
    so VOD downloads don't crawl. We verify by inspecting the sleep
    argument: should be ≤ 0.2."""
    r1 = concat_parts(
        make_format_init(itag=140, total_segments=2),
        make_next_request_policy(backoff_time_ms=5000),
        make_media_header(header_id=1, itag=140, sequence_number=1),
        make_media(header_id=1, data=b"AB"),
        make_media_end(header_id=1),
    )
    r2 = concat_parts(
        make_media_header(header_id=2, itag=140, sequence_number=2),
        make_media(header_id=2, data=b"CD"),
        make_media_end(header_id=2),
    )

    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    iter_responses = iter([r1, r2])

    def fake_execute_request(url, *, method, headers, data, timeout):
        return MockResponse(next(iter_responses), headers={})

    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=fake_execute_request,
    ), mock.patch("pyt.sabr.session.time.sleep") as fake_sleep:
        list(session.iter_chunks())

    # Sleep was called once for the backoff between rounds; the requested
    # 5s was capped to ≤ 200ms.
    sleep_calls = [c.args[0] for c in fake_sleep.call_args_list if c.args[0] > 0]
    assert sleep_calls, "expected at least one backoff sleep"
    assert (
        max(sleep_calls) <= 0.2
    ), f"backoff was {max(sleep_calls)}s — should be capped at 0.2s outside ads"


def test_next_request_policy_backoff_full_during_ads():
    """When a CONTENT_ADS-scoped SABR context is active, the server
    backoff is honored in full (this is how YouTube enforces unskippable
    ads). Verify by checking the sleep duration matches the requested
    value, not the cap."""
    # Round 1: server registers a CONTENT_ADS context AND requests a
    # 1.5s backoff. The session should NOT cap this.
    r1 = concat_parts(
        make_format_init(itag=140, total_segments=2),
        make_sabr_context_update(
            type=42,
            scope=4,  # 4 = CONTENT_ADS
            value=b"\x01",
            send_by_default=True,
        ),
        make_next_request_policy(backoff_time_ms=1500),
        make_media_header(header_id=1, itag=140, sequence_number=1),
        make_media(header_id=1, data=b"X"),
        make_media_end(header_id=1),
    )
    r2 = concat_parts(
        make_media_header(header_id=2, itag=140, sequence_number=2),
        make_media(header_id=2, data=b"Y"),
        make_media_end(header_id=2),
    )

    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    iter_responses = iter([r1, r2])

    def fake_execute_request(url, *, method, headers, data, timeout):
        return MockResponse(next(iter_responses), headers={})

    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=fake_execute_request,
    ), mock.patch("pyt.sabr.session.time.sleep") as fake_sleep:
        list(session.iter_chunks())

    sleep_calls = [c.args[0] for c in fake_sleep.call_args_list if c.args[0] > 0]
    # During an ad, we honor the full 1.5s.
    assert any(
        0.9 < s < 2.0 for s in sleep_calls
    ), f"expected ad-scoped backoff to honor 1.5s; got {sleep_calls}"


def test_next_request_policy_records_playback_cookie():
    """Subsequent requests should carry the playback_cookie issued by
    the server. We verify by checking the request body of round 2
    contains the cookie bytes."""
    cookie_bytes = b"\xca\xfe\xba\xbe"
    r1 = concat_parts(
        make_format_init(itag=140, total_segments=2),
        make_next_request_policy(playback_cookie=cookie_bytes),
        make_media_header(header_id=1, itag=140, sequence_number=1),
        make_media(header_id=1, data=b"AB"),
        make_media_end(header_id=1),
    )
    r2 = concat_parts(
        make_media_header(header_id=2, itag=140, sequence_number=2),
        make_media(header_id=2, data=b"CD"),
        make_media_end(header_id=2),
    )

    captured_bodies = []
    iter_responses = iter([r1, r2])

    def fake_execute_request(url, *, method, headers, data, timeout):
        captured_bodies.append(data)
        return MockResponse(next(iter_responses), headers={})

    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=fake_execute_request,
    ), mock.patch("pyt.sabr.session.time.sleep"):
        list(session.iter_chunks())

    # The round-2 request body should contain the cookie bytes
    # somewhere inside the StreamerContext message.
    assert (
        cookie_bytes in captured_bodies[1]
    ), "expected the playback_cookie to appear in the second request's body"
    # But not in round 1 (we hadn't received the cookie yet).
    assert cookie_bytes not in captured_bodies[0]


# ── Stall detection ──────────────────────────────────────────────────────


def test_stall_detection_after_8_empty_rounds():
    """When the server keeps sending empty responses (no media, no
    backoff signal), we eventually give up. Going round and round on
    nothing forever is the worst possible failure mode."""
    # 9 empty responses — one more than _MAX_STALL_ROUNDS so we're sure
    # the limit gets hit. We keep them empty (no NEXT_REQUEST_POLICY)
    # so the stall counter actually increments.
    empty_rounds = [b""] * 9
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    with pytest.raises(SabrError, match="stalled"):
        _drive_session(session, empty_rounds)


def test_empty_round_with_backoff_does_not_count_toward_stall():
    """If the server signals a backoff, that's a legitimate pause —
    not a stall. Over many empty-but-backed-off rounds we should
    not raise."""
    # Many rounds that all have a backoff signal but no media. Then a
    # final round that delivers and ends.
    backoff_rounds = [make_next_request_policy(backoff_time_ms=10) for _ in range(20)]
    final = build_segment_response(
        header_id=1,
        itag=140,
        sequence_number=1,
        data=b"X",
        end_segment_index=1,
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    chunks = _drive_session(session, backoff_rounds + [final])
    assert chunks == [(140, b"X")]


# ── gzip-encoded responses ──────────────────────────────────────────────


def test_gzip_encoded_response_is_decoded():
    """SABR responses can come with Content-Encoding: gzip. The session
    must transparently decompress them; otherwise UMP parsing reads
    binary noise."""
    body = build_segment_response(
        header_id=1,
        itag=140,
        sequence_number=1,
        data=b"COMPRESSED",
        end_segment_index=1,
    )
    gzipped = gzip_wrap(body)

    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    chunks = _drive_session(session, [(gzipped, {"Content-Encoding": "gzip"})])
    assert chunks == [(140, b"COMPRESSED")]


# ── SABR_CONTEXT_SENDING_POLICY ─────────────────────────────────────────


def test_sabr_context_sending_policy_starts_then_stops_send():
    """The server can register a context with send_by_default=false,
    then start sending it via a sending policy, then stop. Verify the
    state transitions don't leak: after stop_policy, the context is
    NOT included in subsequent requests."""
    ctx_value = b"\xde\xad"

    # Round 1: register a context (not sent by default), tell us to
    # start sending it, deliver a segment so the loop continues.
    r1 = concat_parts(
        make_format_init(itag=140, total_segments=3),
        make_sabr_context_update(type=99, value=ctx_value, send_by_default=False),
        make_sabr_context_sending_policy(start=[99]),
        make_media_header(header_id=1, itag=140, sequence_number=1),
        make_media(header_id=1, data=b"a"),
        make_media_end(header_id=1),
    )
    # Round 2: stop sending it; one more segment.
    r2 = concat_parts(
        make_sabr_context_sending_policy(stop=[99]),
        make_media_header(header_id=2, itag=140, sequence_number=2),
        make_media(header_id=2, data=b"b"),
        make_media_end(header_id=2),
    )
    # Round 3: deliver the final segment to finish the format.
    r3 = concat_parts(
        make_media_header(header_id=3, itag=140, sequence_number=3),
        make_media(header_id=3, data=b"c"),
        make_media_end(header_id=3),
    )

    captured_bodies = []
    iter_responses = iter([r1, r2, r3])

    def fake_execute_request(url, *, method, headers, data, timeout):
        captured_bodies.append(data)
        return MockResponse(next(iter_responses), headers={})

    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=fake_execute_request,
    ), mock.patch("pyt.sabr.session.time.sleep"):
        list(session.iter_chunks())

    # Round 2 (after start_policy): the value should be in the request body.
    # Round 3 (after stop_policy): the value should NOT appear.
    assert (
        ctx_value in captured_bodies[1]
    ), "context bytes should be sent after start_policy"
    assert (
        ctx_value not in captured_bodies[2]
    ), "context bytes should NOT be sent after stop_policy"


# ── Player time advancement ─────────────────────────────────────────────


def test_player_time_advances_to_buffered_end_ms():
    """After receiving a MEDIA_HEADER with start_ms+duration_ms, the
    next request body's player_time_ms should equal that buffered
    edge — the trick that prevents the server from throttling us
    to real-time playback speed."""
    # Round 1: deliver a 5-second segment.
    r1 = concat_parts(
        make_format_init(itag=140, total_segments=3),
        make_media_header(
            header_id=1,
            itag=140,
            sequence_number=1,
            start_ms=0,
            duration_ms=5000,
        ),
        make_media(header_id=1, data=b"AAAA"),
        make_media_end(header_id=1),
    )
    # Round 2: deliver another 5-second segment, at start_ms=5000.
    r2 = concat_parts(
        make_media_header(
            header_id=2,
            itag=140,
            sequence_number=2,
            start_ms=5000,
            duration_ms=5000,
        ),
        make_media(header_id=2, data=b"BBBB"),
        make_media_end(header_id=2),
    )
    r3 = concat_parts(
        make_media_header(
            header_id=3,
            itag=140,
            sequence_number=3,
            start_ms=10000,
            duration_ms=5000,
        ),
        make_media(header_id=3, data=b"CCCC"),
        make_media_end(header_id=3),
    )

    captured_bodies = []
    iter_responses = iter([r1, r2, r3])

    def fake_execute_request(url, *, method, headers, data, timeout):
        captured_bodies.append(data)
        return MockResponse(next(iter_responses), headers={})

    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
        duration_ms=15_000,
    )
    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=fake_execute_request,
    ), mock.patch("pyt.sabr.session.time.sleep"):
        list(session.iter_chunks())

    # We can't easily decode the request body to extract the player_time
    # field, but we can sanity-check that round 2's body is *different*
    # from round 1's (because round 2 includes player_time_ms=5000 from
    # the buffered_end_ms update, plus a buffered range, and round 1
    # had neither).
    assert captured_bodies[0] != captured_bodies[1]
    # Round 2 is also longer because it carries the new buffered_range
    # message. Round 1 had no buffered ranges.
    assert len(captured_bodies[1]) > len(captured_bodies[0])


# ── Compression in MEDIA_HEADER (refused) ───────────────────────────────


def test_media_header_with_unsupported_compression_raises():
    """Per-MEDIA gzip is rare but if the server sends it, we refuse
    rather than emit garbage. yt-dlp does the same."""
    body = concat_parts(
        make_media_header(
            header_id=1,
            itag=140,
            sequence_number=1,
            content_length=4,
            compression=2,  # 2 = GZIP
        ),
        make_media(header_id=1, data=b"AAAA"),
        make_media_end(header_id=1),
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    with pytest.raises(SabrError, match="compression"):
        _drive_session(session, [body])


# ── Resume across runs ──────────────────────────────────────────────────


def test_resume_state_seeds_buffered_ranges():
    """Constructing SabrSession with resume= seeds last_segment_index
    so the server skips already-delivered segments. The data structures
    don't otherwise drive behavior in tests, but the seeding shouldn't
    break the session."""
    from pyt.sabr.session import _ResumeState

    r1 = build_segment_response(
        header_id=1,
        itag=140,
        sequence_number=3,
        data=b"NEW",
        end_segment_index=3,
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
        resume={140: _ResumeState(downloaded_bytes=2048, last_segment_index=2)},
    )
    chunks = _drive_session(session, [r1])
    assert chunks == [(140, b"NEW")]
    state = session.state(140)
    # downloaded_bytes preserves the resumed counter + any new bytes.
    assert state.downloaded_bytes == 2048 + 3

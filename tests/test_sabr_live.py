"""Tests for SabrSession's live-mode behavior (live=True).

Live has no fixed end-of-stream the way VOD does (_all_finished never
returns True for a live format), so these tests exercise the mechanisms
that take its place: stop(), SABR_SEEK resetting buffered state, and
LIVE_METADATA being inert. test_sabr_session.py's existing suite is the
regression gate that these changes don't affect live=False behavior.
"""

from __future__ import annotations

from unittest import mock

import pytest

from pyt.sabr.session import SabrError, SabrSession
from tests.sabr_fixtures import (
    MockResponse,
    concat_parts,
    make_live_metadata,
    make_media,
    make_media_end,
    make_media_header,
    make_sabr_error,
    make_sabr_seek,
)


def _drive_session(session: SabrSession, responses: list) -> list:
    iter_responses = iter(responses)

    def fake_execute_request(url, *, method, headers, data, timeout):
        try:
            entry = next(iter_responses)
        except StopIteration:
            raise AssertionError(
                "SabrSession requested more rounds than the test fixture provided"
            )
        return MockResponse(entry, headers={})

    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=fake_execute_request,
    ), mock.patch("pyt.sabr.session.time.sleep"):
        return list(session.iter_chunks())


def test_live_session_never_hits_all_finished():
    """A live format with no end_segment_index/expected_bytes must not
    cause _all_finished() to return True — otherwise a live download
    would stop as soon as the first segment lands."""
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
        live=True,
    )
    session.state(140).last_segment_index = 5
    assert session._all_finished() is False


def test_stop_ends_iteration_cleanly():
    """session.stop() ends iter_chunks() after the in-flight round,
    without raising and without requesting further rounds."""
    r1 = concat_parts(
        make_media_header(
            header_id=1, itag=140, sequence_number=1, start_ms=0, duration_ms=5000
        ),
        make_media(header_id=1, data=b"AAAA"),
        make_media_end(header_id=1),
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
        live=True,
    )

    iter_responses = iter([r1])

    def fake_execute_request(url, *, method, headers, data, timeout):
        session.stop()
        return MockResponse(next(iter_responses), headers={})

    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=fake_execute_request,
    ), mock.patch("pyt.sabr.session.time.sleep"):
        chunks = list(session.iter_chunks())

    assert chunks == [(140, b"AAAA")]


def test_sabr_seek_resets_buffered_state_for_live_format():
    """A SABR_SEEK on a live session resets buffered_end_ms to the seek
    target and clears last_segment_index, so the next request body
    reflects the server-directed position instead of stale state."""
    r1 = concat_parts(
        make_media_header(
            header_id=1, itag=140, sequence_number=5, start_ms=20000, duration_ms=5000
        ),
        make_media(header_id=1, data=b"AAAA"),
        make_media_end(header_id=1),
        make_sabr_seek(seek_time_ticks=1000, timescale=1),
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
        live=True,
    )

    iter_responses = iter([r1])
    calls = []

    def fake_execute_request(url, *, method, headers, data, timeout):
        calls.append(data)
        session.stop()  # stop after this round so the test terminates
        return MockResponse(next(iter_responses), headers={})

    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=fake_execute_request,
    ), mock.patch("pyt.sabr.session.time.sleep"):
        list(session.iter_chunks())

    state = session.state(140)
    assert state.buffered_end_ms == 1000 * 1000  # seek_time_ticks / timescale * 1000
    assert state.last_segment_index == -1


def test_sabr_seek_does_not_affect_vod_format():
    """A VOD (live=False) format should not have its state mutated by
    a SABR_SEEK - defensive, since it shouldn't legitimately occur."""
    r1 = concat_parts(
        make_media_header(
            header_id=1, itag=140, sequence_number=5, start_ms=20000, duration_ms=5000
        ),
        make_media(header_id=1, data=b"AAAA"),
        make_media_end(header_id=1),
        make_sabr_seek(seek_time_ticks=1000, timescale=1),
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )

    # Drive one round, then stop manually - a VOD session with no
    # end_segment_index/expected_bytes would otherwise stall forever.
    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=lambda *a, **k: MockResponse(r1, headers={}),
    ), mock.patch("pyt.sabr.session.time.sleep"):
        it = session.iter_chunks()
        next(it)
        session.stop()
        for _ in it:
            pass

    state = session.state(140)
    assert state.buffered_end_ms == 25000
    assert state.last_segment_index == 5


def test_live_metadata_is_inert():
    """LIVE_METADATA must not crash the session or affect termination -
    locks in the Phase 1 safe-no-op contract (field layout unconfirmed)."""
    r1 = concat_parts(
        make_media_header(
            header_id=1, itag=140, sequence_number=1, start_ms=0, duration_ms=5000
        ),
        make_media(header_id=1, data=b"AAAA"),
        make_media_end(header_id=1),
        make_live_metadata(raw_fields={1: 42, 2: 1000}),
    )
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
        live=True,
    )

    iter_responses = iter([r1])

    def fake_execute_request(url, *, method, headers, data, timeout):
        session.stop()
        return MockResponse(next(iter_responses), headers={})

    with mock.patch(
        "pyt.sabr.session.request._execute_request",
        side_effect=fake_execute_request,
    ), mock.patch("pyt.sabr.session.time.sleep"):
        chunks = list(session.iter_chunks())

    assert chunks == [(140, b"AAAA")]


def test_broadcast_end_error_terminates_cleanly():
    """Locks in the current heuristic (any error type containing 'ended'
    is treated as broadcast-end for a live session). The actual string
    YouTube sends on a real ended broadcast is unconfirmed against live
    traffic - tracked as a Phase 2 follow-up; this only pins today's
    code, not YouTube's real wire behavior."""
    body = make_sabr_error(type="ended")
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
        live=True,
    )
    chunks = _drive_session(session, [body])
    assert chunks == []
    assert session._broadcast_ended is True


def test_sabr_error_still_raises_for_vod():
    """Non-live sessions keep today's behavior: an unrecognized
    SABR_ERROR raises instead of being swallowed as a live signal."""
    body = make_sabr_error(type="ended")
    session = SabrSession(
        sabr_url="https://sabr.test/play",
        ustreamer_config=None,
        formats=[(140, False)],
    )
    with pytest.raises(SabrError):
        _drive_session(session, [body])

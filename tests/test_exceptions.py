import pytest
import pyt.exceptions as exceptions


def test_video_unavailable():
    try:
        raise exceptions.VideoUnavailable(video_id="YLnZklYFe7E")
    except exceptions.VideoUnavailable as e:
        assert e.video_id == "YLnZklYFe7E"  # noqa: PT017
        assert str(e) == "YLnZklYFe7E is unavailable"


def test_regex_match_error():
    try:
        raise exceptions.RegexMatchError(caller="hello", pattern="*")
    except exceptions.RegexMatchError as e:
        assert str(e) == "hello: could not find match for *"


def test_live_stream_error():
    # Ensure this can be caught as generic VideoUnavailable exception
    with pytest.raises(exceptions.VideoUnavailable):
        raise exceptions.LiveStreamError(video_id="YLnZklYFe7E")
    try:
        raise exceptions.LiveStreamError(video_id="YLnZklYFe7E")
    except exceptions.LiveStreamError as e:
        assert e.video_id == "YLnZklYFe7E"  # noqa: PT017
        assert str(e) == "YLnZklYFe7E is streaming live and cannot be loaded"

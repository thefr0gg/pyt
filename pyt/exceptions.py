"""Library specific exception definitions."""

from typing import Pattern, Union


class PytError(Exception):
    """Base pyt exception that all others inherit.

    This is done to not pollute the built-in exceptions, which *could* result
    in unintended errors being unexpectedly and incorrectly handled within
    implementers code.
    """


class MaxRetriesExceeded(PytError):
    """Maximum number of retries exceeded."""


class HTMLParseError(PytError):
    """HTML could not be parsed"""


class ExtractError(PytError):
    """Data extraction based exception."""


class RegexMatchError(ExtractError):
    """Regex pattern did not return any matches."""

    def __init__(self, caller: str, pattern: Union[str, Pattern]):
        """
        :param str caller:
            Calling function
        :param str pattern:
            Pattern that failed to match
        """
        super().__init__(f"{caller}: could not find match for {pattern}")
        self.caller = caller
        self.pattern = pattern


class VideoUnavailable(PytError):
    """Base video unavailable error."""

    def __init__(self, video_id: str):
        """
        :param str video_id:
            A YouTube video identifier.
        """
        self.video_id = video_id
        super().__init__(self.error_string)

    @property
    def error_string(self):
        return f"{self.video_id} is unavailable"


class LiveStreamError(VideoUnavailable):
    """Video is a live stream."""

    def __init__(self, video_id: str):
        """
        :param str video_id:
            A YouTube video identifier.
        """
        self.video_id = video_id
        super().__init__(self.video_id)

    @property
    def error_string(self):
        return f"{self.video_id} is streaming live and cannot be loaded"

"""Base class for pyt post-processors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyt.streams import Stream
    from pyt.__main__ import YouTube


class PostProcessorError(Exception):
    """Raised when a post-processor encounters an unrecoverable error."""


class BasePostProcessor(ABC):
    """Abstract base for all post-processors.

    Each processor receives the downloaded file path, the Stream, and the
    YouTube object, performs its work, and returns the (possibly new) file path.
    """

    @abstractmethod
    def run(self, file_path: str, stream: "Stream", youtube: "YouTube") -> str:
        """Process *file_path* and return the path to the result file."""

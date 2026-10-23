"""Internal: per-Video SABR session configuration.

Replaces the legacy Monostate Borg pattern. Each Video owns one instance;
CombinedDownload and Download read from it directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple


@dataclass
class _SabrConfig:
    sabr_url: Optional[str] = None
    ustreamer_config: Optional[bytes] = None
    po_token: Optional[str] = None
    client_info: Dict[str, Any] = field(default_factory=dict)
    stream_headers: Dict[str, str] = field(default_factory=dict)
    duration: Optional[float] = None
    on_progress: Optional[Callable] = None
    on_complete: Optional[Callable] = None
    refresh_sabr_config: Optional[
        Callable[[], Tuple[Optional[str], Optional[bytes]]]
    ] = None

"""
Shared types for ACP clients.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ACPResponse:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    finished: bool = False

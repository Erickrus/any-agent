"""
ACP clients for any-agent bridge.

Refactored into separate modules:
- acp_base.py — shared types
- acp_opencode.py — OpenCode ACP client (JSON-RPC 2.0 over stdio)
- acp_claude.py — Claude Code client (wraps claude CLI with session management)

This module provides backward-compatible imports.
"""

from .acp_base import ACPResponse
from .acp_claude import ClaudeCodeClient
from .acp_opencode import OpenCodeClient

# Backward compat aliases
ACPClient = OpenCodeClient
ClaudeClient = ClaudeCodeClient

__all__ = [
    "ACPResponse",
    "OpenCodeClient",
    "ClaudeCodeClient",
    "ACPClient",
    "ClaudeClient",
]

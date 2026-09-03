#!/usr/bin/env python3
"""
any-agent — WeChat bridge to local coding agents.

Thin shim so the repo checkout still runs with `python3 main.py`.
The real implementation lives in any_agent/app.py (also runnable as
`python3 -m any_agent` once installed).
"""

from any_agent.app import cli

if __name__ == "__main__":
    cli()

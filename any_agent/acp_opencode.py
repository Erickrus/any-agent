"""
OpenCode ACP Client — long-running ACP subprocess over JSON-RPC 2.0 ndJSON/stdio.
Handles session/update acks, permission auto-approve, filesystem requests,
and client-side slash commands.

Protocol reference: opencode_acp.py, opencode-dev/packages/opencode/src/acp/service.ts
"""

import asyncio
import json
import logging
import os
from typing import Any

from .acp_base import ACPResponse

logger = logging.getLogger("any_agent.acp_opencode")


class OpenCodeClient:
    """Async ACP client for OpenCode — spawns `opencode acp` subprocess."""

    def __init__(
        self,
        bin_path: str,
        args: list[str],
        cwd: str,
        name: str = "opencode",
        env_extra: dict[str, str] | None = None,
    ):
        self.bin_path = bin_path
        self.args = args
        self.cwd = cwd
        self.name = name
        self.env_extra = env_extra or {}

        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._session_id: str | None = None
        self._reader_task: asyncio.Task | None = None
        self._response_chunks: list[str] = []
        self._prompt_done: asyncio.Event = asyncio.Event()
        self._ready: asyncio.Event = asyncio.Event()
        self._config_options: list[dict] = []
        self._available_commands: list[dict] = []

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def start(self):
        env = os.environ.copy()
        env.update(self.env_extra)

        if not env.get("OPENCODE_PERMISSION"):
            env["OPENCODE_PERMISSION"] = json.dumps({"*": "allow"})

        cmd = [self.bin_path] + self.args
        if "--cwd" not in self.args:
            cmd.extend(["--cwd", self.cwd])
        if "--print-logs" not in self.args:
            cmd.append("--print-logs")

        logger.info(f"[{self.name}] Starting ACP: {' '.join(cmd)} in {self.cwd}")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=30)
            logger.info(f"[{self.name}] Internal server ready")
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Timeout waiting for ready signal, proceeding anyway")

    async def _drain_stderr(self):
        while self._proc and self._proc.stderr:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode().rstrip()
            logger.info(f"[{self.name} stderr] {text}")
            if "global event connected" in text:
                self._ready.set()

    async def _read_loop(self):
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            text = line.decode().strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                logger.warning(f"[{self.name}] Non-JSON: {text[:200]}")
                continue
            method = msg.get("method", "")
            has_id = "id" in msg
            if method:
                logger.info(f"[{self.name}] <<< {method} (id={msg.get('id', '-')})")
            elif has_id:
                err = msg.get("error")
                logger.info(f"[{self.name}] <<< response id={msg['id']} err={err}")
            self._handle_message(msg)

    def _handle_message(self, msg: dict):
        if "id" in msg and "method" not in msg:
            msg_id = msg["id"]
            logger.info(f"[{self.name}] RPC response id={msg_id} error={'error' in msg}")
            if msg_id in self._pending:
                fut = self._pending.pop(msg_id)
                if "error" in msg:
                    fut.set_exception(RuntimeError(
                        f"RPC error {msg['error'].get('code')}: {msg['error'].get('message')}"
                    ))
                else:
                    fut.set_result(msg.get("result", {}))
            return

        method = msg.get("method", "")

        if method == "session/update":
            params = msg.get("params", {})
            update = params.get("update", {})
            update_type = update.get("sessionUpdate", "")
            logger.info(f"[{self.name}] session/update type={update_type} has_id={'id' in msg}")

            if update_type == "agent_message_chunk":
                content = update.get("content", {})
                chunk = content.get("text", "")
                logger.info(f"[{self.name}] chunk: {len(chunk)} chars, preview={chunk[:120]!r}")
                if chunk:
                    self._response_chunks.append(chunk)

            elif update_type == "tool_call":
                title = update.get("title", "")
                status = update.get("status", "")
                logger.info(f"[{self.name}] Tool: {title} ({status})")

            elif update_type == "tool_call_update":
                pass

            elif update_type == "agent_thought_chunk":
                pass

            elif update_type == "available_commands_update":
                self._available_commands = update.get("availableCommands", [])
                logger.info(f"[{self.name}] Commands updated: {[c.get('name') for c in self._available_commands]}")

            if "id" in msg:
                logger.info(f"[{self.name}] >>> ack session/update id={msg['id']}")
                asyncio.ensure_future(self._send_raw({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {},
                }))

            return

        if method == "session/request_permission":
            msg_id = msg.get("id")
            if msg_id is not None:
                tool_title = msg.get("params", {}).get("toolCall", {}).get("title", "")
                logger.info(f"[{self.name}] Auto-approving: {tool_title}")
                asyncio.ensure_future(self._send_raw({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "outcome": {
                            "type": "selected",
                            "outcome": "selected",
                            "optionId": "allow_once",
                        }
                    },
                }))
            return

        if method in ("fs/read_text_file", "fs/write_text_file"):
            msg_id = msg.get("id")
            params = msg.get("params", {})
            file_path = params.get("path") or params.get("filePath") or ""
            try:
                if method == "fs/read_text_file":
                    with open(file_path, "r", encoding="utf-8") as f:
                        asyncio.ensure_future(self._send_raw({
                            "jsonrpc": "2.0", "id": msg_id,
                            "result": {"content": f.read()},
                        }))
                else:
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(params.get("content", ""))
                    asyncio.ensure_future(self._send_raw({
                        "jsonrpc": "2.0", "id": msg_id, "result": {},
                    }))
                logger.info(f"[{self.name}] {method}: {file_path}")
            except Exception as e:
                logger.error(f"[{self.name}] {method} failed: {e}")
                asyncio.ensure_future(self._send_raw({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": str(e)},
                }))
            return

        if "id" in msg and method:
            logger.warning(f"[{self.name}] Unhandled request: {method}")
            asyncio.ensure_future(self._send_raw({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }))
            return

        if method:
            logger.debug(f"[{self.name}] Unhandled notification: {method}")

    async def _send_raw(self, msg: dict):
        if self._proc and self._proc.stdin:
            method = msg.get("method", "")
            if method:
                logger.info(f"[{self.name}] >>> {method} id={msg.get('id', '-')}")
            data = json.dumps(msg) + "\n"
            self._proc.stdin.write(data.encode())
            await self._proc.stdin.drain()

    async def _send(
        self,
        method: str,
        params: dict | None = None,
        notification: bool = False,
        timeout: float = 30,
    ) -> dict:
        if notification:
            msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params:
                msg["params"] = params
            await self._send_raw(msg)
            return {}

        msg_id = self._next_id()
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params:
            msg["params"] = params

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        await self._send_raw(msg)
        return await asyncio.wait_for(fut, timeout=timeout)

    async def stop(self):
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                self._proc.kill()
            self._proc = None
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None

    async def initialize(self) -> dict:
        result = await self._send("initialize", {"protocolVersion": 1})
        info = result.get("agentInfo", {})
        logger.info(f"[{self.name}] Initialized: {info.get('name', 'unknown')} v{info.get('version', '?')}")
        return result

    async def new_session(self) -> str:
        result = await self._send("session/new", {"cwd": self.cwd, "mcpServers": []})
        self._session_id = result.get("sessionId", "")
        self._config_options = result.get("configOptions", [])
        logger.info(f"[{self.name}] Session: {self._session_id}")
        return self._session_id

    async def list_sessions(self) -> list[dict]:
        """Return sessions for this cwd as [{id, title, updatedAt}], newest first."""
        try:
            result = await self._send("session/list", {"cwd": self.cwd})
        except RuntimeError as e:
            logger.warning(f"[{self.name}] session/list failed: {e}")
            return []
        out = []
        for s in (result.get("sessions", []) if result else []):
            out.append({
                "id": s.get("sessionId", ""),
                "title": s.get("title", "") or "(untitled)",
                "updatedAt": s.get("updatedAt", ""),
            })
        return out

    async def load_session(self, session_id: str) -> str:
        result = await self._send("session/load", {
            "sessionId": session_id,
            "cwd": self.cwd,
            "mcpServers": [],
        })
        self._session_id = session_id
        if result:
            self._config_options = result.get("configOptions", self._config_options)
        logger.info(f"[{self.name}] Resumed session: {session_id}")
        return session_id

    async def prompt(self, text: str) -> ACPResponse:
        if not self._session_id:
            await self.new_session()

        self._response_chunks.clear()
        self._prompt_done.clear()

        try:
            result = await self._send(
                "session/prompt",
                {
                    "sessionId": self._session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=600,
            )
        except asyncio.TimeoutError:
            return ACPResponse(error="Agent timed out (600s)")
        except RuntimeError as e:
            return ACPResponse(error=str(e))

        full_text = "".join(self._response_chunks)
        stop = result.get("stopReason", "end_turn")
        logger.info(f"[{self.name}] Prompt done: {len(full_text)} chars, stop={stop}, chunks={len(self._response_chunks)}")
        logger.info(f"[{self.name}] RPC result keys: {list(result.keys())}")
        if not full_text:
            logger.warning(f"[{self.name}] Empty response! result={json.dumps(result, default=str)[:500]}")
        return ACPResponse(text=full_text, finished=True)

    async def cancel(self):
        if self._session_id:
            await self._send(
                "session/cancel",
                {"sessionId": self._session_id},
                notification=True,
            )

    def get_config_option(self, category: str) -> dict | None:
        for opt in self._config_options:
            if opt.get("category") == category or opt.get("id") == category:
                return opt
        return None

    def format_config_option(self, category: str, max_items: int = 0) -> str:
        opt = self.get_config_option(category)
        if not opt:
            return f"No {category} options available"
        current = opt.get("currentValue", "?")
        options = opt.get("options", [])
        if max_items <= 0 or len(options) <= max_items:
            lines = [f"{opt.get('name', category)} (current: {current})"]
            for o in options:
                marker = " *" if o.get("value") == current else ""
                lines.append(f"  {o.get('value', '?')} — {o.get('name', '')}{marker}")
            return "\n".join(lines)
        lines = [f"{opt.get('name', category)} (current: {current})", f"  ({len(options)} options available)"]
        return "\n".join(lines)

    async def set_model(self, model_id: str) -> dict:
        if not self._session_id:
            raise RuntimeError("No active session")
        result = await self._send("session/setModel", {
            "sessionId": self._session_id,
            "modelId": model_id,
        })
        self._config_options = result.get("configOptions", self._config_options)
        return result

    async def set_mode(self, mode_id: str) -> dict:
        if not self._session_id:
            raise RuntimeError("No active session")
        result = await self._send("session/setMode", {
            "sessionId": self._session_id,
            "modeId": mode_id,
        })
        self._config_options = result.get("configOptions", self._config_options)
        return result

    def handle_slash_command(self, text: str) -> str | None:
        """Handle slash commands client-side. Returns text to display, or None to forward to ACP."""
        text = text.strip()
        if not text.startswith("/"):
            return None

        parts = text[1:].split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "skills":
            if not self._available_commands:
                return "No skills available yet."
            lines = ["Available skills:"]
            for c in self._available_commands:
                lines.append(f"  /{c.get('name', '?')} -- {c.get('description', '')}")
            lines.append("\nUse /<name> to invoke a skill.")
            return "\n".join(lines)

        if cmd == "models":
            if args:
                return None
            return self.format_config_option("model", max_items=20)

        if cmd == "mcps":
            return (
                "MCP server status is not available via ACP.\n"
                "Check your opencode config for MCP server settings."
            )

        known_names = {c.get("name", "").lower() for c in self._available_commands}
        if cmd in known_names:
            return None

        return None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

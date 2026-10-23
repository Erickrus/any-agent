"""
Pi coding agent client — long-running `pi --mode rpc` subprocess over JSONL/stdio.

Pi's RPC protocol (docs/rpc.md, docs/rpc-commands.md):
  stdin  : {"id": "...", "type": "prompt", "message": "..."}          (one JSON per line)
  stdout : {"id":"...","type":"response","command":"prompt","success":true,"data":{...}}
           {"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"..."}}
           {"type":"agent_start"} / {"type":"agent_end"} / {"type":"agent_settled"}
           {"type":"response","command":"get_last_assistant_text","data":{"text":"..."}}

This client mirrors the OpenCodeClient/ClaudeCodeClient interface so the hub's
DeviceManager can drive it through the same `/use pi` path.
"""

import asyncio
import json
import logging
import os
from typing import Any

from .acp_base import ACPResponse

logger = logging.getLogger("any_agent.acp_pi")


class PiClient:
    """Async Pi RPC client — spawns `pi --mode rpc` subprocess."""

    def __init__(
        self,
        bin_path: str,
        cwd: str,
        name: str = "pi",
        env_extra: dict[str, str] | None = None,
        model: str | None = None,
        args: list[str] | None = None,
    ):
        self.bin_path = bin_path
        self.cwd = cwd
        self.name = name
        self.env_extra = env_extra or {}
        self.model = model or ""
        self.args = args or []

        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._settled: asyncio.Event = asyncio.Event()
        self._response_chunks: list[str] = []
        self._prompt_lock = asyncio.Lock()

        # Session / model state (read from pi via get_state / get_available_models).
        self._session_id: str | None = None
        self._session_file: str | None = None
        self._session_name: str | None = None
        self._current_model: str = ""
        self._models: list[dict] = []
        self._commands: list[dict] = []

    # ── Subprocess lifecycle ──────────────────────────────────────────

    async def start(self):
        env = os.environ.copy()
        env.update(self.env_extra)

        cmd = [self.bin_path, "--mode", "rpc"]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.args:
            cmd.extend(self.args)

        logger.info(f"[{self.name}] Starting: {' '.join(cmd)} in {self.cwd}")
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

        # Pi RPC has no handshake record; probe with get_state.
        try:
            state = await asyncio.wait_for(self.get_state(), timeout=30)
            self._session_id = state.get("sessionId")
            self._session_file = state.get("sessionFile")
            self._session_name = state.get("sessionName")
            model_obj = state.get("model") or {}
            self._current_model = model_obj.get("id", "") or model_obj.get("name", "")
            logger.info(f"[{self.name}] Ready: session={self._session_id} model={self._current_model or 'default'}")
        except Exception as e:
            logger.warning(f"[{self.name}] get_state probe failed: {e}")

        # Prefetch commands/skills for the client-side /skills handler.
        try:
            self._commands = await asyncio.wait_for(self._list_commands(), timeout=10)
        except Exception:
            self._commands = []

        # Prefetch the model list for the client-side /models handler.
        try:
            await asyncio.wait_for(self.get_available_models(), timeout=10)
        except Exception:
            pass

    async def _drain_stderr(self):
        while self._proc and self._proc.stderr:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                logger.info(f"[{self.name} stderr] {text[:300]}")

    async def _read_loop(self):
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                logger.warning(f"[{self.name}] Non-JSON stdout: {text[:200]}")
                continue
            self._handle_record(record)

    def _handle_record(self, record: dict):
        rtype = record.get("type", "")

        if rtype == "response":
            rid = record.get("id")
            if rid is not None and rid in self._pending:
                fut = self._pending.pop(rid)
                if record.get("success") is False:
                    fut.set_exception(RuntimeError(
                        f"{record.get('command', 'cmd')} failed: {record.get('error', 'unknown')}"
                    ))
                else:
                    fut.set_result(record.get("data", {}))
            return

        if rtype == "message_update":
            ev = record.get("assistantMessageEvent") or {}
            if ev.get("type") == "text_delta":
                delta = ev.get("delta", "")
                if delta:
                    self._response_chunks.append(delta)
            return

        if rtype == "agent_settled":
            self._settled.set()
            return

        # agent_start / agent_end / tool_execution_* / compaction / retry / queue
        # are informational; nothing to do here.

    async def _write(self, obj: dict):
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self._proc.stdin.drain()

    def _next_id(self) -> str:
        self._id += 1
        return f"pi-{self._id}"

    async def _send(self, command: dict, timeout: float = 60) -> dict:
        """Send one command and await its response record. Returns `data`."""
        msg_id = self._next_id()
        msg = {"id": msg_id, **command}
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        await self._write(msg)
        return await asyncio.wait_for(fut, timeout=timeout)

    async def stop(self):
        if self._proc:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                self._proc.kill()
            self._proc = None
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ── Agent interface (used by DeviceManager) ───────────────────────

    async def initialize(self) -> dict:
        """Pi RPC has no initialize handshake; just confirm readiness."""
        try:
            state = await self.get_state()
            self._session_id = state.get("sessionId")
            self._session_file = state.get("sessionFile")
            self._session_name = state.get("sessionName")
            return state
        except Exception as e:
            logger.warning(f"[{self.name}] initialize() get_state failed: {e}")
            return {}

    async def new_session(self) -> str:
        await self._send({"type": "new_session"}, timeout=30)
        state = await self.get_state()
        self._session_id = state.get("sessionId")
        self._session_file = state.get("sessionFile")
        self._session_name = state.get("sessionName")
        logger.info(f"[{self.name}] New session: {self._session_id}")
        return self._session_id or ""

    async def list_sessions(self) -> list[dict]:
        """Pi RPC has no session-list command; expose the current session."""
        try:
            state = await self.get_state()
            self._session_id = state.get("sessionId")
            self._session_file = state.get("sessionFile")
            self._session_name = state.get("sessionName")
        except Exception:
            pass
        if not self._session_file:
            return []
        return [{
            "id": self._session_file,
            "title": self._session_name or "(current session)",
            "updatedAt": "",
        }]

    async def load_session(self, session_id: str) -> str:
        # Pi's switch_session expects a session file path, which is what
        # list_sessions() stores in the "id" field.
        await self._send({"type": "switch_session", "sessionPath": session_id}, timeout=30)
        state = await self.get_state()
        self._session_id = state.get("sessionId")
        self._session_file = state.get("sessionFile")
        self._session_name = state.get("sessionName")
        logger.info(f"[{self.name}] Switched session: {session_id}")
        return self._session_id or session_id

    async def prompt(self, text: str) -> ACPResponse:
        async with self._prompt_lock:
            return await self._prompt_locked(text)

    async def _prompt_locked(self, text: str) -> ACPResponse:
        self._response_chunks.clear()
        self._settled.clear()

        try:
            data = await self._send({"type": "prompt", "message": text}, timeout=120)
        except asyncio.TimeoutError:
            return ACPResponse(error="Pi did not accept the prompt (120s)")
        except RuntimeError as e:
            return ACPResponse(error=str(e))

        disposition = data.get("disposition", "started")
        logger.info(f"[{self.name}] prompt disposition={disposition}")

        # Extension commands report "handled" and may not emit agent_settled.
        if disposition == "handled":
            await asyncio.sleep(0.2)
            final = await self._get_last_assistant_text()
            chunks = "".join(self._response_chunks).strip()
            return ACPResponse(text=final if final is not None else chunks, finished=True)

        try:
            await asyncio.wait_for(self._settled.wait(), timeout=600)
        except asyncio.TimeoutError:
            return ACPResponse(error="Pi timed out (600s)")

        final = await self._get_last_assistant_text()
        if final is None:
            final = "".join(self._response_chunks).strip()
        logger.info(f"[{self.name}] Prompt done: {len(final or '')} chars")
        return ACPResponse(text=final or "", finished=True)

    async def _get_last_assistant_text(self) -> str | None:
        try:
            data = await self._send({"type": "get_last_assistant_text"}, timeout=30)
            return data.get("text")
        except Exception as e:
            logger.warning(f"[{self.name}] get_last_assistant_text failed: {e}")
            return None

    async def cancel(self):
        try:
            await self._send({"type": "abort"}, timeout=30)
        except Exception as e:
            logger.warning(f"[{self.name}] abort failed: {e}")

    # ── Config / model ────────────────────────────────────────────────

    async def get_state(self) -> dict:
        return await self._send({"type": "get_state"}, timeout=30)

    async def get_available_models(self) -> list[dict]:
        try:
            data = await self._send({"type": "get_available_models"}, timeout=30)
            self._models = data.get("models", [])
        except Exception as e:
            logger.warning(f"[{self.name}] get_available_models failed: {e}")
            self._models = []
        return self._models

    def _format_model_list(self, max_items: int = 0) -> str:
        models = self._models
        current = self._current_model
        if not models:
            return f"Model: {current or '(default)'}"
        if max_items > 0 and len(models) > max_items:
            return f"Model: {current or '(default)'} ({len(models)} available)"
        lines = [f"Model (current: {current or '(default)'})"]
        for m in models:
            mid = m.get("id") or m.get("name") or "?"
            label = m.get("name") or mid
            marker = " *" if mid == current else ""
            lines.append(f"  {mid} — {label}{marker}")
        return "\n".join(lines)

    def format_config_option(self, category: str, max_items: int = 0) -> str:
        if category == "model":
            return self._format_model_list(max_items=max_items)
        if category == "mode":
            return "Pi has no mode setting (only model + thinking level)."
        return f"No {category} options available"

    async def set_model(self, model_id: str) -> dict:
        provider, _, model_part = model_id.partition("/")
        if not model_part:
            # No slash — send as-is; pi accepts bare ids in some providers.
            model_part = model_id
            provider = ""
        cmd: dict[str, Any] = {"type": "set_model", "modelId": model_part}
        if provider:
            cmd["provider"] = provider
        result = await self._send(cmd, timeout=30)
        self._current_model = model_id
        await self.get_available_models()
        return result

    async def set_mode(self, mode_id: str) -> dict:
        raise RuntimeError("Pi has no mode setting")

    # ── Slash commands handled client-side ────────────────────────────

    async def _list_commands(self) -> list[dict]:
        try:
            data = await self._send({"type": "get_commands"}, timeout=30)
            return data.get("commands", [])
        except Exception as e:
            logger.warning(f"[{self.name}] get_commands failed: {e}")
            return []

    def handle_slash_command(self, text: str) -> str | None:
        """Handle slash commands client-side. Returns text, or None to forward to pi."""
        text = text.strip()
        if not text.startswith("/"):
            return None

        parts = text[1:].split(None, 1)
        cmd = parts[0].lower()

        if cmd == "skills":
            if not self._commands:
                return "No skills/commands preloaded for pi. Ask pi directly what it can do."
            lines = ["Available commands/skills:"]
            for c in self._commands:
                name = c.get("name", "?")
                prefix = "skill:" if c.get("source") == "skill" else ""
                lines.append(f"  /{prefix}{name} — {c.get('description', '')}")
            return "\n".join(lines)

        if cmd == "models":
            return self.format_config_option("model", max_items=20)

        if cmd == "mcps":
            return "MCP status is not available via Pi RPC."

        return None

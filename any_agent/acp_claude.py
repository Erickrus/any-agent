"""
Claude Code SDK Client — long-running subprocess over NDJSON/stdio control protocol.
Spawns `claude --print --input-format stream-json --output-format stream-json`
and communicates via the SDK control protocol (control_request/control_response).

Protocol reference: claude-code/src/cli/structuredIO.ts, controlSchemas.ts, sessionRunner.ts
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from .acp_base import ACPResponse

logger = logging.getLogger("any_agent.acp_claude")


class ClaudeCodeClient:
    """Async Claude Code client using SDK control protocol (NDJSON over stdio)."""

    def __init__(
        self,
        bin_path: str,
        cwd: str,
        name: str = "claude",
        env_extra: dict[str, str] | None = None,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        skip_permissions: bool = True,
    ):
        self.bin_path = bin_path
        self.cwd = cwd
        self.name = name
        self.env_extra = env_extra or {}
        self.model = model or os.getenv("CLAUDE_ACP_MODEL")
        self.allowed_tools = allowed_tools
        self.skip_permissions = skip_permissions

        self._proc: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._response_chunks: list[str] = []
        self._tool_calls: list[dict[str, Any]] = []
        self._prompt_done: asyncio.Event = asyncio.Event()
        self._ready: asyncio.Event = asyncio.Event()
        self._pending_controls: dict[str, asyncio.Future] = {}

        self._available_commands: list[dict] = []
        self._models: list[dict] = []
        self._config_options: list[dict] = []
        self._mcp_servers: list[dict] = []
        self._current_model: str = ""

    # ── Subprocess lifecycle ──────────────────────────────────────────

    async def start(self, resume_id: str | None = None):
        env = self._build_env()

        args = [
            self.bin_path,
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if resume_id:
            # Resume an existing session by its uuid.
            self._session_id = resume_id
            args.extend(["--resume", resume_id])
        else:
            session_id = str(uuid.uuid4())
            self._session_id = session_id
            args.extend(["--session-id", session_id])
        if self.skip_permissions:
            args.extend(["--permission-mode", "bypasstool"])
        if self.model:
            args.extend(["--model", self.model])

        logger.info(f"[{self.name}] Starting: {' '.join(args[:8])}... cwd={self.cwd}")
        self._proc = await asyncio.create_subprocess_exec(
            *args,
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
            logger.info(f"[{self.name}] Process ready (pid={self._proc.pid})")
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Timeout waiting for ready, proceeding")

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.env_extra)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        return env

    async def _drain_stderr(self):
        while self._proc and self._proc.stderr:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode().rstrip()
            logger.info(f"[{self.name} stderr] {text}")

    # ── NDJSON read loop ──────────────────────────────────────────────

    async def _read_loop(self):
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                logger.info(f"[{self.name}] stdout EOF")
                self._prompt_done.set()
                break
            text = line.decode().strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                logger.warning(f"[{self.name}] Non-JSON: {text[:200]}")
                continue
            msg_type = msg.get("type", "")
            logger.info(f"[{self.name}] <<< type={msg_type} keys={list(msg.keys())[:6]}")
            self._handle_message(msg)

    def _handle_message(self, msg: dict):
        msg_type = msg.get("type", "")

        if msg_type == "system":
            subtype = msg.get("subtype", "")
            logger.info(f"[{self.name}] system/{subtype} keys={list(msg.keys())}")
            if subtype == "init":
                self._ready.set()
                self._ingest_system_init(msg)
            return

        if msg_type == "assistant":
            message = msg.get("message", {})
            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        chunk = block.get("text", "")
                        if chunk:
                            self._response_chunks.append(chunk)
                            logger.info(f"[{self.name}] text chunk: {len(chunk)} chars")
                    elif block.get("type") == "tool_use":
                        self._tool_calls.append({
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        })
                        logger.info(f"[{self.name}] tool_use: {block.get('name')}")
            return

        if msg_type == "result":
            subtype = msg.get("subtype", "")
            logger.info(f"[{self.name}] result/{subtype}")
            self._prompt_done.set()
            return

        if msg_type == "control_request":
            request = msg.get("request", {})
            request_id = msg.get("request_id", "")
            subtype = request.get("subtype", "")
            logger.info(f"[{self.name}] control_request/{subtype} id={request_id}")

            if subtype == "can_use_tool":
                asyncio.ensure_future(self._auto_approve_permission(request_id, request))
            return

        if msg_type == "control_response":
            response = msg.get("response", {})
            req_id = response.get("request_id", "")
            subtype = response.get("subtype", "")
            inner = response.get("response", {})
            logger.info(f"[{self.name}] control_response id={req_id} subtype={subtype} keys={list(inner.keys()) if isinstance(inner, dict) else type(inner)}")
            if req_id in self._pending_controls:
                fut = self._pending_controls.pop(req_id)
                if not fut.done():
                    if subtype == "error":
                        fut.set_exception(RuntimeError(response.get("error", "control error")))
                    else:
                        fut.set_result(inner if isinstance(inner, dict) else {})
            return

    # ── Send helpers ──────────────────────────────────────────────────

    async def _send_raw(self, msg: dict):
        if not self._proc or not self._proc.stdin:
            logger.warning(f"[{self.name}] Cannot send — no process")
            return
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()

    async def _send_user_message(self, text: str):
        msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
            "parent_tool_use_id": None,
            "session_id": "",
        }
        logger.info(f"[{self.name}] >>> user message: {len(text)} chars")
        await self._send_raw(msg)

    async def _send_control_request(self, subtype: str, **kwargs) -> dict:
        request_id = str(uuid.uuid4())
        msg = {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": subtype, **kwargs},
        }
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_controls[request_id] = fut
        logger.info(f"[{self.name}] >>> control_request/{subtype} id={request_id}")
        await self._send_raw(msg)
        try:
            result = await asyncio.wait_for(fut, timeout=30)
            return result
        except asyncio.TimeoutError:
            self._pending_controls.pop(request_id, None)
            logger.warning(f"[{self.name}] control_request/{subtype} timed out")
            return {}

    async def _auto_approve_permission(self, request_id: str, request: dict):
        tool_name = request.get("tool_name", "")
        logger.info(f"[{self.name}] Auto-approving permission for {tool_name}")
        await self._send_raw({
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "behavior": "allow",
                    "updatedInput": request.get("input", {}),
                },
            },
        })

    # ── Public interface ──────────────────────────────────────────────

    def _ingest_system_init(self, msg: dict):
        """Extract commands, skills, model info from system/init message."""
        slash_commands = msg.get("slash_commands", [])
        skills = msg.get("skills", [])
        model = msg.get("model", "")
        mcp_servers = msg.get("mcp_servers", [])

        cmds = []
        for name in slash_commands:
            cmds.append({"name": name, "description": ""})
        for name in skills:
            if not any(c.get("name") == name for c in cmds):
                cmds.append({"name": name, "description": f"Skill: {name}"})
        self._available_commands = cmds

        self._mcp_servers = mcp_servers

        if model:
            self._current_model = model
            if not self._models:
                self._models = [{"id": model, "name": model, "isDefault": True}]
                self._build_config_options()

        logger.info(
            f"[{self.name}] system/init: {len(cmds)} commands, "
            f"model={model}, {len(mcp_servers)} MCP servers, "
            f"{len(skills)} skills"
        )

    async def initialize(self) -> dict:
        """Send initialize control_request to get full model list and commands."""
        try:
            result = await self._send_control_request("initialize")
        except Exception as e:
            logger.warning(f"[{self.name}] initialize control_request failed: {e}, using system/init data")
            return {}

        if result.get("commands"):
            self._available_commands = result["commands"]
        if result.get("models"):
            self._models = result["models"]
        logger.info(
            f"[{self.name}] Initialized: {len(self._available_commands)} commands, "
            f"{len(self._models)} models"
        )
        self._build_config_options()
        return result

    def _build_config_options(self):
        if self._models:
            model_opt = {
                "id": "model",
                "category": "model",
                "displayName": "Model",
                "options": [],
            }
            for m in self._models:
                model_opt["options"].append({
                    "id": m.get("id", ""),
                    "label": m.get("name", m.get("id", "")),
                    "selected": m.get("isDefault", False),
                })
            self._config_options = [model_opt]

    async def new_session(self) -> str:
        logger.info(f"[{self.name}] Session: {self._session_id}")
        return self._session_id or ""

    # ── Session listing / resume (disk-based) ─────────────────────────

    def _project_dir(self) -> str:
        """Claude stores sessions at ~/.claude/projects/<cwd-slug>/<uuid>.jsonl."""
        slug = self.cwd.replace("/", "-").replace(".", "-")
        return os.path.join(os.path.expanduser("~/.claude/projects"), slug)

    async def list_sessions(self, scan_limit: int = 20) -> list[dict]:
        """
        List sessions for this cwd, newest first: [{id, title, updatedAt}].
        Scans the most-recent `scan_limit` jsonl files and derives a title from
        the first user message.
        """
        import glob

        proj = self._project_dir()
        files = glob.glob(os.path.join(proj, "*.jsonl"))
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        out = []
        for path in files[:scan_limit]:
            sid = os.path.splitext(os.path.basename(path))[0]
            title = self._extract_title(path)
            out.append({
                "id": sid,
                "title": title or "(untitled)",
                "updatedAt": os.path.getmtime(path),
            })
        return out

    @staticmethod
    def _extract_title(path: str) -> str:
        """First human user message text, truncated. Cheap: reads until found."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "summary" and obj.get("summary"):
                        return str(obj["summary"])[:60]
                    if obj.get("type") == "user":
                        msg = obj.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        content = (content or "").strip()
                        if content and not content.startswith("<"):
                            return content[:60]
        except OSError:
            pass
        return ""

    async def load_session(self, session_id: str) -> str:
        """Resume a session by restarting the subprocess with --resume."""
        await self.stop()
        await self.start(resume_id=session_id)
        logger.info(f"[{self.name}] Resumed session: {session_id}")
        return session_id

    async def prompt(self, text: str) -> ACPResponse:
        self._response_chunks.clear()
        self._tool_calls.clear()
        self._prompt_done.clear()

        await self._send_user_message(text)

        try:
            await asyncio.wait_for(self._prompt_done.wait(), timeout=600)
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Prompt timed out after 600s")
            return ACPResponse(error="Prompt timed out")

        full_text = "".join(self._response_chunks)
        logger.info(
            f"[{self.name}] Prompt done: {len(full_text)} chars, "
            f"{len(self._tool_calls)} tool calls"
        )
        return ACPResponse(
            text=full_text,
            tool_calls=list(self._tool_calls),
            finished=True,
        )

    async def cancel(self):
        await self._send_control_request("interrupt")

    async def stop(self):
        if self._proc:
            logger.info(f"[{self.name}] Stopping process")
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._proc.kill()
            self._proc = None
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()

    async def set_model(self, model_id: str) -> dict:
        result = await self._send_control_request("set_model", model=model_id)
        logger.info(f"[{self.name}] Model set to {model_id}")
        return result

    # ── Config option helpers (same interface as OpenCodeClient) ──────

    def get_config_option(self, category: str) -> dict | None:
        for opt in self._config_options:
            if opt.get("category") == category:
                return opt
        return None

    def format_config_option(self, category: str, max_items: int = 0) -> str:
        opt = self.get_config_option(category)
        if not opt:
            return f"No config options for '{category}'"

        display = opt.get("displayName", category)
        options = opt.get("options", [])
        current = next((o for o in options if o.get("selected")), None)
        current_label = current.get("label", "?") if current else "(none)"

        lines = [f"{display} (current: {current_label})"]
        shown = options[:max_items] if max_items > 0 else options
        for o in shown:
            marker = " *" if o.get("selected") else ""
            lines.append(f"  {o.get('id', '?')}{marker}")
        if max_items > 0 and len(options) > max_items:
            lines.append(f"  ... and {len(options) - max_items} more")
        return "\n".join(lines)

    # ── Client-side slash commands ────────────────────────────────────

    def handle_slash_command(self, text: str) -> str | None:
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
            servers = getattr(self, "_mcp_servers", [])
            if not servers:
                return "No MCP servers configured."
            lines = ["MCP servers:"]
            for s in servers:
                name = s.get("name", "?")
                status = s.get("status", "unknown")
                lines.append(f"  {name} ({status})")
            return "\n".join(lines)

        known_names = {c.get("name", "").lower() for c in self._available_commands}
        if cmd in known_names:
            return None

        return None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

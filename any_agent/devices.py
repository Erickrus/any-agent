"""
Device manager — manages multiple ACP agent devices and routes WeChat users.
Supports WeChat slash commands for choosing/switching agents and directories.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .acp import ACPClient, ACPResponse, ClaudeClient, OpenCodeClient, ClaudeCodeClient

logger = logging.getLogger("any_agent.devices")


def _fmt_ago(updated) -> str:
    """Human 'time ago' from an epoch float or ISO8601 string. Empty on failure."""
    import time
    from datetime import datetime, timezone

    if not updated:
        return ""
    try:
        if isinstance(updated, (int, float)):
            ts = float(updated)
        else:
            s = str(updated).replace("Z", "+00:00")
            ts = datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return ""
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


@dataclass
class DeviceConfig:
    name: str
    type: str  # "opencode", "claude", etc.
    bin: str
    cwd: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    model: str = ""


@dataclass
class UserBinding:
    device: str
    cwd: str
    # For remote agents: the remote device name (None = local hub agent).
    # When set, `device` holds the agent name on that remote device.
    remote_device: str | None = None


class DeviceManager:
    """Manages ACP agent instances and per-user routing."""

    def __init__(
        self,
        device_configs: list[DeviceConfig],
        default_agent: str | None = None,
        registry=None,
        device_client=None,
    ):
        self.configs = {d.name: d for d in device_configs}
        self._default_agent = default_agent if default_agent in self.configs else None
        # key: "device:cwd"
        self._clients: dict[str, OpenCodeClient | ClaudeCodeClient] = {}
        self._user_bindings: dict[str, UserBinding] = {}
        self._lock = asyncio.Lock()
        # Hub-only: registry of remote devices + client to reach them.
        self._registry = registry
        self._device_client = device_client
        # Per-user cache of the last /sessions listing: user_id -> [session_id,...]
        # (index n in the menu maps to _session_menu[user_id][n-1])
        self._session_menu: dict[str, list[str]] = {}

    def _client_key(self, device: str, cwd: str) -> str:
        return f"{device}:{cwd}"

    @property
    def device_names(self) -> list[str]:
        return list(self.configs.keys())

    def default_device(self) -> str | None:
        if self._default_agent:
            return self._default_agent
        names = self.device_names
        return names[0] if names else None

    def get_user_binding(self, user_id: str) -> UserBinding:
        if user_id in self._user_bindings:
            return self._user_bindings[user_id]
        dev = self.default_device() or ""
        cwd = self.configs[dev].cwd if dev and dev in self.configs else ""
        return UserBinding(device=dev, cwd=cwd)

    def set_user_binding(self, user_id: str, device: str | None = None, cwd: str | None = None) -> str | None:
        """
        Update user's device and/or cwd. Returns error message or None on success.
        `device` may be a local agent name, or '<remote-device>/<agent>' for a remote agent.
        """
        current = self.get_user_binding(user_id)

        # Remote agent: "<device>/<agent>"
        if device and "/" in device:
            remote_name, _, agent_name = device.partition("/")
            if self._registry is None:
                return "Remote agents are only available on the hub."
            found = self._registry.find(remote_name, agent_name)
            if not found:
                avail = ", ".join(self._registry.all_agent_paths()) or "(none online)"
                return f"Unknown remote agent: {device}\nOnline: {avail}"
            _, agent = found
            # cwd on the remote device: explicit arg, else the agent's configured cwd.
            remote_cwd = cwd or agent.get("cwd", "")
            self._user_bindings[user_id] = UserBinding(
                device=agent_name, cwd=remote_cwd, remote_device=remote_name,
            )
            return None

        new_device = (device or current.device).lower()
        if new_device not in self.configs:
            avail = list(self.device_names)
            if self._registry is not None:
                avail += self._registry.all_agent_paths()
            return f"Unknown agent: {new_device}\nAvailable: {', '.join(avail)}"
        new_cwd = cwd or (current.cwd if not current.remote_device else self.configs[new_device].cwd)
        expanded = os.path.expanduser(new_cwd)
        if not os.path.isdir(expanded):
            return f"Directory not found: {expanded}"
        self._user_bindings[user_id] = UserBinding(device=new_device, cwd=expanded)
        return None

    async def get_client(self, device_name: str, cwd: str) -> OpenCodeClient | ClaudeCodeClient:
        key = self._client_key(device_name, cwd)
        async with self._lock:
            if key in self._clients and self._clients[key].is_running:
                return self._clients[key]

            cfg = self.configs.get(device_name)
            if not cfg:
                raise ValueError(f"Unknown device: {device_name}")

            if cfg.type == "claude":
                client = ClaudeCodeClient(
                    bin_path=cfg.bin,
                    cwd=cwd,
                    name=cfg.name,
                    env_extra=cfg.env,
                    model=cfg.model or None,
                    allowed_tools=cfg.args if cfg.args else None,
                    skip_permissions=cfg.env.get("CLAUDE_ACP_SKIP_PERMISSIONS") == "true",
                )
            else:
                env_extra = dict(cfg.env)
                if cfg.model:
                    env_extra["OPENCODE_CONFIG_CONTENT"] = json.dumps({"model": cfg.model})
                client = OpenCodeClient(
                    bin_path=cfg.bin,
                    args=cfg.args,
                    cwd=cwd,
                    name=cfg.name,
                    env_extra=env_extra,
                )

            try:
                await client.start()
                logger.info(f"[{device_name}] start() OK")
                await client.initialize()
                logger.info(f"[{device_name}] initialize() OK")
                await client.new_session()
                logger.info(f"[{device_name}] new_session() OK")
            except Exception as e:
                logger.error(f"[{device_name}] failed at: {e}")
                await client.stop()
                raise
            self._clients[key] = client
            return client

    async def send_prompt(self, user_id: str, text: str) -> ACPResponse:
        binding = self.get_user_binding(user_id)
        if not binding.device:
            return ACPResponse(error="No device configured. Use /devices to see available agents.")

        # Remote agent: relay over HTTP to the owning device's tunnel.
        if binding.remote_device:
            return await self._remote_prompt(binding, text, user_id)

        try:
            client = await self.get_client(binding.device, binding.cwd)
        except Exception as e:
            logger.error(f"Failed to start device {binding.device}: {e}")
            return ACPResponse(error=f"Failed to start {binding.device}: {e}")

        return await client.prompt(text)

    async def _remote_prompt(self, binding: UserBinding, text: str, user_id: str) -> ACPResponse:
        if self._registry is None or self._device_client is None:
            return ACPResponse(error="Remote routing not available.")
        dev = self._registry.get(binding.remote_device)
        if not dev:
            return ACPResponse(
                error=f"Device '{binding.remote_device}' is offline. Use /devices to see what's online."
            )
        try:
            resp = await self._device_client.prompt(
                dev.tunnel_url, binding.device, binding.cwd, text, user_id,
            )
            return ACPResponse(text=resp.get("text", ""), error=resp.get("error", ""), finished=True)
        except Exception as e:
            logger.error(f"Remote prompt to {binding.remote_device}/{binding.device} failed: {e}")
            return ACPResponse(error=f"Remote agent error: {e}")

    BRIDGE_COMMANDS = {"/devices", "/agents", "/use", "/switch", "/cwd", "/status", "/help", "/new", "/model", "/models", "/mode", "/sessions", "/resume"}
    CLIENT_SIDE_COMMANDS = {"/skills", "/models", "/mcps"}
    # Commands that always run at the hub even when the user is on a remote agent.
    HUB_LOCAL_COMMANDS = {"/devices", "/agents", "/use", "/switch", "/status", "/help"}

    async def _remote_command(self, binding: "UserBinding", text: str) -> str | None:
        """Forward a slash command to the remote device that owns the bound agent."""
        if self._registry is None or self._device_client is None:
            return None
        dev = self._registry.get(binding.remote_device)
        if not dev:
            return f"Device '{binding.remote_device}' is offline."
        try:
            resp = await self._device_client.command(
                dev.tunnel_url, binding.device, binding.cwd, text,
            )
            return resp.get("result")
        except Exception as e:
            logger.error(f"Remote command to {binding.remote_device} failed: {e}")
            return f"Remote command error: {e}"

    async def handle_command(self, user_id: str, text: str) -> str | None:
        text = text.strip()
        if not text.startswith("/"):
            return None

        parts = text.split()
        cmd = parts[0].lower()

        # Hub-level commands always run locally, even when bound to a remote agent.
        # Everything else (e.g. /model, /skills, /mcps, skill invocations) forwards to
        # the remote device that owns the currently-bound agent.
        binding = self.get_user_binding(user_id)
        if binding.remote_device and cmd not in self.HUB_LOCAL_COMMANDS:
            return await self._remote_command(binding, text)

        # For Claude agents, some commands are aliases (Claude has no separate list command)
        #   /models -> /model, /mcp -> /mcps
        CLAUDE_ALIASES = {"/models": "/model", "/mcp": "/mcps"}
        if cmd in CLAUDE_ALIASES:
            binding = self.get_user_binding(user_id)
            cfg = self.configs.get(binding.device)
            if cfg and cfg.type == "claude":
                alias = CLAUDE_ALIASES[cmd]
                text = alias + text[len(cmd):]
                cmd = alias
                parts = text.split()

        if cmd not in self.BRIDGE_COMMANDS:
            binding = self.get_user_binding(user_id)
            if binding.device:
                key = self._client_key(binding.device, binding.cwd)
                client = self._clients.get(key)
                if client is None and cmd in self.CLIENT_SIDE_COMMANDS:
                    try:
                        client = await self.get_client(binding.device, binding.cwd)
                    except Exception as e:
                        logger.warning(f"Failed to start client for {cmd}: {e}")
                if isinstance(client, (OpenCodeClient, ClaudeCodeClient)):
                    result = client.handle_slash_command(text)
                    if result is not None:
                        return result
            if cmd in self.CLIENT_SIDE_COMMANDS:
                return f"No active session. Use /use <agent> first, then try {cmd}."
            return None

        args = parts[1:]

        if cmd in ("/devices", "/agents"):
            binding = self.get_user_binding(user_id)
            current_path = (
                f"{binding.remote_device}/{binding.device}" if binding.remote_device else binding.device
            )
            lines = ["Local agents:"]
            for name in self.device_names:
                cfg = self.configs[name]
                marker = " *" if (not binding.remote_device and name == binding.device) else ""
                lines.append(f"  {name} [{cfg.type}]{marker}")
            if self._registry is not None and self._registry.devices:
                lines.append("\nRemote agents:")
                for dev in self._registry.devices:
                    for a in dev.agents:
                        path = f"{dev.name}/{a.get('name')}"
                        marker = " *" if path == current_path else ""
                        lines.append(f"  {path} [{a.get('type','?')}]{marker}")
            lines.append(f"\nCurrent: {current_path} in {binding.cwd}")
            lines.append("Use /use <agent> or /use <device>/<agent> to switch.")
            return "\n".join(lines)

        # /use <agent> [cwd]
        if cmd in ("/use", "/switch"):
            if not args:
                return "Usage: /use <agent> [/path/to/project]"
            device = args[0]
            cwd = args[1] if len(args) > 1 else None
            err = self.set_user_binding(user_id, device=device, cwd=cwd)
            if err:
                return err
            b = self.get_user_binding(user_id)
            return f"Switched to {b.device} in {b.cwd}"

        # /cwd [path]
        if cmd == "/cwd":
            if not args:
                b = self.get_user_binding(user_id)
                return f"Current directory: {b.cwd}"
            cwd = args[0]
            err = self.set_user_binding(user_id, cwd=cwd)
            if err:
                return err
            b = self.get_user_binding(user_id)
            return f"{b.device} now working in {b.cwd}"

        if cmd == "/status":
            binding = self.get_user_binding(user_id)
            running = [key for key, c in self._clients.items() if c.is_running]
            current_path = (
                f"{binding.remote_device}/{binding.device}" if binding.remote_device else binding.device
            )
            lines = [
                f"Agent: {current_path}",
                f"Directory: {binding.cwd}",
                f"Local sessions: {len(running)}",
            ]
            if self._registry is not None:
                import time as _time
                now = _time.time()
                devs = self._registry.devices
                lines.append(f"Remote devices online: {len(devs)}")
                for dev in devs:
                    ago = int(now - dev.last_seen)
                    lines.append(f"  {dev.name} ({len(dev.agents)} agents, seen {ago}s ago)")
            return "\n".join(lines)

        if cmd == "/help":
            return (
                "Bridge commands:\n"
                "  /devices — list agents\n"
                "  /use <agent> [cwd] — switch agent/directory\n"
                "  /cwd [path] — show or change directory\n"
                "  /model [provider/model] — show or change model\n"
                "  /models — list available models\n"
                "  /mode [mode] — show or change mode (opencode)\n"
                "  /sessions — list past sessions (numbered)\n"
                "  /resume <n> — resume session number n\n"
                "  /new — start a new session\n"
                "  /status — current session info\n"
                "\nOther /commands are sent to the agent as text."
            )

        if cmd == "/model":
            binding = self.get_user_binding(user_id)
            cfg = self.configs.get(binding.device)
            if not cfg:
                return "No device"
            key = self._client_key(binding.device, binding.cwd)
            client = self._clients.get(key)
            logger.info(f"/model: key={key} client={type(client).__name__ if client else None} args={args}")
            if not args:
                if isinstance(client, OpenCodeClient):
                    return client.format_config_option("model")
                if isinstance(client, ClaudeCodeClient):
                    return f"Model: {client._current_model or cfg.model or '(default)'}"
                return f"[{binding.device}] model: {cfg.model or '(default)'}"
            new_model = args[0]
            cfg.model = new_model
            if isinstance(client, OpenCodeClient) and client._session_id:
                try:
                    await client.set_model(new_model)
                    return f"Model set to {new_model}"
                except Exception as e:
                    logger.warning(f"session/setModel failed: {e}")
                    return f"Model set to {new_model} (takes effect on next session)\nNote: {e}"
            if isinstance(client, ClaudeCodeClient):
                try:
                    await client.set_model(new_model)
                    client._current_model = new_model
                    return f"Model set to {new_model}"
                except Exception as e:
                    logger.warning(f"set_model failed: {e}")
                    return f"Model set to {new_model} (takes effect on next session)\nNote: {e}"
            return f"Model set to {new_model} (takes effect on next session)"

        if cmd == "/models":
            binding = self.get_user_binding(user_id)
            key = self._client_key(binding.device, binding.cwd)
            client = self._clients.get(key)
            logger.info(f"/models: key={key} client={type(client).__name__ if client else None} clients={list(self._clients.keys())}")
            if isinstance(client, (OpenCodeClient, ClaudeCodeClient)):
                result = client.format_config_option("model", max_items=20)
                logger.info(f"/models result: {result!r}")
                return result
            return "Model list only available for active sessions"

        if cmd == "/mode":
            binding = self.get_user_binding(user_id)
            key = self._client_key(binding.device, binding.cwd)
            client = self._clients.get(key)
            if not isinstance(client, OpenCodeClient):
                return "Mode switching only available for opencode sessions"
            if not args:
                return client.format_config_option("mode")
            try:
                await client.set_mode(args[0])
                return f"Mode set to {args[0]}"
            except Exception as e:
                return f"Failed to set mode: {e}"

        if cmd == "/new":
            binding = self.get_user_binding(user_id)
            key = self._client_key(binding.device, binding.cwd)
            client = self._clients.get(key)
            if client:
                await client.stop()
                del self._clients[key]
            return f"New session started for {binding.device}"

        if cmd == "/sessions":
            return await self._cmd_sessions(user_id)

        if cmd == "/resume":
            return await self._cmd_resume(user_id, args)

        return None

    async def _cmd_sessions(self, user_id: str, max_items: int = 8) -> str:
        """List sessions for the user's current agent as a numbered menu."""
        binding = self.get_user_binding(user_id)
        try:
            client = await self.get_client(binding.device, binding.cwd)
        except Exception as e:
            return f"Failed to start {binding.device}: {e}"
        if not hasattr(client, "list_sessions"):
            return "Session switching not supported for this agent."
        sessions = await client.list_sessions()
        if not sessions:
            return f"No sessions for {binding.device} in {binding.cwd}."

        self._session_menu[user_id] = [s["id"] for s in sessions]
        current = getattr(client, "_session_id", None)
        header = f"Sessions for {binding.device} ({os.path.basename(binding.cwd) or binding.cwd}):"
        lines = [header]
        for i, s in enumerate(sessions[:max_items], 1):
            marker = " *" if s["id"] == current else ""
            title = (s.get("title") or "(untitled)")[:40]
            when = _fmt_ago(s.get("updatedAt"))
            lines.append(f"  {i}. {title}   {when}{marker}")
        if len(sessions) > max_items:
            lines.append(f"  … {len(sessions) - max_items} older")
        lines.append("(reply /resume <n>)")
        return "\n".join(lines)

    async def _cmd_resume(self, user_id: str, args: list[str]) -> str:
        if not args or not args[0].isdigit():
            return "Usage: /resume <n>  (run /sessions first to see the list)"
        n = int(args[0])
        menu = self._session_menu.get(user_id)
        if not menu:
            # Build the list silently, then resume.
            await self._cmd_sessions(user_id)
            menu = self._session_menu.get(user_id) or []
        if n < 1 or n > len(menu):
            return f"No session #{n}. Run /sessions to see the list."
        session_id = menu[n - 1]
        binding = self.get_user_binding(user_id)
        try:
            client = await self.get_client(binding.device, binding.cwd)
            await client.load_session(session_id)
        except Exception as e:
            return f"Failed to resume: {e}"
        return f"Resumed session #{n}."

    def reassign_from_device(self, device_name: str) -> list[str]:
        """
        Move any users bound to agents on `device_name` back to the default agent.
        Returns the list of affected user_ids (for WeChat notification).
        """
        affected = []
        default = self.default_device() or ""
        default_cwd = self.configs[default].cwd if default in self.configs else ""
        for uid, binding in list(self._user_bindings.items()):
            if binding.remote_device == device_name:
                affected.append(uid)
                self._user_bindings[uid] = UserBinding(device=default, cwd=default_cwd)
        return affected

    async def stop_all(self):
        for key, client in self._clients.items():
            logger.info(f"Stopping {key}")
            await client.stop()
        self._clients.clear()

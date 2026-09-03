"""
HTTP wire between the hub and remote devices.

Both hub and device run an aiohttp server (exposed via their own cloudflared tunnel)
and use thin HTTP clients to reach the other side.

Hub server endpoints (called by devices):
  POST /register   {device_name, tunnel_url, agents:[{name,type,model,cwd}]}
  POST /heartbeat  {device_name}
  POST /log        {device_name, records:[{ts,level,name,msg}]}
  POST /media      {device_name, to_user_id, filename, caption, b64}

Device server endpoints (called by the hub):
  POST /prompt     {agent, cwd, text} -> {text, error}
  POST /command    {agent, cwd, text} -> {result}
"""

import asyncio
import base64
import logging
import os
import tempfile
from typing import Any, Callable, Awaitable

import aiohttp
from aiohttp import web

logger = logging.getLogger("any_agent.hublink")


# ── Clients ───────────────────────────────────────────────────────────

class HubClient:
    """Device-side client that talks to the hub."""

    def __init__(self, hub_url: str, device_name: str):
        self.hub_url = hub_url.rstrip("/")
        self.device_name = device_name
        self._session: aiohttp.ClientSession | None = None

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, path: str, body: dict, timeout: float = 15) -> dict:
        session = await self._ensure()
        url = f"{self.hub_url}{path}"
        to = aiohttp.ClientTimeout(total=timeout)
        async with session.post(url, json=body, timeout=to) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"hub {path} HTTP {resp.status}: {text[:200]}")
            return await resp.json() if text else {}

    async def register(self, tunnel_url: str, agents: list[dict]) -> dict:
        return await self._post("/register", {
            "device_name": self.device_name,
            "tunnel_url": tunnel_url,
            "agents": agents,
        })

    async def heartbeat(self) -> dict:
        return await self._post("/heartbeat", {"device_name": self.device_name})

    async def send_log(self, records: list[dict]) -> dict:
        return await self._post("/log", {"device_name": self.device_name, "records": records})

    async def send_media(self, to_user_id: str, path: str, caption: str = "") -> dict:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return await self._post("/media", {
            "device_name": self.device_name,
            "to_user_id": to_user_id,
            "filename": os.path.basename(path),
            "caption": caption,
            "b64": b64,
        }, timeout=120)

    async def heartbeat_loop(self, interval: float = 60.0):
        while True:
            try:
                await self.heartbeat()
            except Exception as e:
                logger.warning(f"heartbeat failed: {e}")
            await asyncio.sleep(interval)


class DeviceClient:
    """Hub-side client that reaches a remote device's tunnel URL."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, tunnel_url: str, path: str, body: dict, timeout: float = 600) -> dict:
        session = await self._ensure()
        url = f"{tunnel_url.rstrip('/')}{path}"
        to = aiohttp.ClientTimeout(total=timeout)
        async with session.post(url, json=body, timeout=to) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"device {path} HTTP {resp.status}: {text[:200]}")
            return await resp.json() if text else {}

    async def prompt(self, tunnel_url: str, agent: str, cwd: str, text: str, user_id: str = "") -> dict:
        return await self._post(tunnel_url, "/prompt", {
            "agent": agent, "cwd": cwd, "text": text, "user_id": user_id,
        })

    async def command(self, tunnel_url: str, agent: str, cwd: str, text: str) -> dict:
        return await self._post(tunnel_url, "/command", {"agent": agent, "cwd": cwd, "text": text}, timeout=60)


# ── Hub server ────────────────────────────────────────────────────────

def build_hub_app(
    registry,
    on_media: Callable[[str, str, str], Awaitable[None]],
    combined_log_path: str = "",
) -> web.Application:
    """
    Build the hub's HTTP app.
    - registry: RemoteRegistry
    - on_media(to_user_id, path, caption): coroutine that uploads+sends via WeChat
    - combined_log_path: file to append remote JSON log lines to
    """
    app = web.Application(client_max_size=64 * 1024 * 1024)

    async def register(request: web.Request) -> web.Response:
        data = await request.json()
        dev = registry.register(
            data["device_name"], data.get("tunnel_url", ""), data.get("agents", []),
        )
        logger.info(f"Device registered: {dev.name} ({len(dev.agents)} agents) @ {dev.tunnel_url}")
        return web.json_response({"ok": True})

    async def heartbeat(request: web.Request) -> web.Response:
        data = await request.json()
        ok = registry.touch(data["device_name"])
        return web.json_response({"ok": ok})

    async def log(request: web.Request) -> web.Response:
        data = await request.json()
        device = data.get("device_name", "?")
        records = data.get("records", [])
        if combined_log_path:
            try:
                import json
                with open(combined_log_path, "a") as f:
                    for r in records:
                        r.setdefault("device", device)
                        f.write(json.dumps(r) + "\n")
            except Exception as e:
                logger.warning(f"combined log write failed: {e}")
        for r in records:
            logger.info(f"[{device}] {r.get('level','')} {r.get('name','')}: {r.get('msg','')}")
        return web.json_response({"ok": True})

    async def media(request: web.Request) -> web.Response:
        data = await request.json()
        to_user_id = data["to_user_id"]
        filename = data.get("filename", "file.bin")
        caption = data.get("caption", "")
        raw = base64.b64decode(data["b64"])
        tmpdir = tempfile.mkdtemp(prefix="anyagent-relay-")
        path = os.path.join(tmpdir, filename)
        with open(path, "wb") as f:
            f.write(raw)
        try:
            await on_media(to_user_id, path, caption)
            return web.json_response({"ok": True})
        except Exception as e:
            logger.error(f"relay media send failed: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        finally:
            try:
                os.remove(path)
                os.rmdir(tmpdir)
            except OSError:
                pass

    app.router.add_post("/register", register)
    app.router.add_post("/heartbeat", heartbeat)
    app.router.add_post("/log", log)
    app.router.add_post("/media", media)
    return app


# ── Device server ─────────────────────────────────────────────────────

def build_device_app(
    on_prompt: Callable[[str, str, str, str], Awaitable[dict]],
    on_command: Callable[[str, str, str], Awaitable[dict]],
) -> web.Application:
    """
    Build a remote device's HTTP app.
    - on_prompt(agent, cwd, text, user_id) -> {text, error}
    - on_command(agent, cwd, text) -> {result}
    """
    app = web.Application(client_max_size=16 * 1024 * 1024)

    async def prompt(request: web.Request) -> web.Response:
        data = await request.json()
        result = await on_prompt(
            data["agent"], data.get("cwd", ""), data.get("text", ""), data.get("user_id", ""),
        )
        return web.json_response(result)

    async def command(request: web.Request) -> web.Response:
        data = await request.json()
        result = await on_command(data["agent"], data.get("cwd", ""), data.get("text", ""))
        return web.json_response(result)

    app.router.add_post("/prompt", prompt)
    app.router.add_post("/command", command)
    return app


async def run_app(app: web.Application, port: int) -> web.AppRunner:
    """Start an aiohttp app on localhost:port. Returns the runner (call .cleanup() to stop)."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info(f"HTTP server listening on 127.0.0.1:{port}")
    return runner

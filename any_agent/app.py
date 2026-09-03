#!/usr/bin/env python3
"""
any-agent — WeChat bridge to local coding agents via ACP.
First milestone: QR login + message loop + route to one agent.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

import aiohttp
import yaml

from .ilink import (
    ILinkClient,
    WeixinMessage,
    start_qr_login,
    wait_for_qr_login,
)
from .devices import DeviceConfig, DeviceManager
from .sendfile import parse_sendfile
from .tunnel import CloudflaredTunnel, CloudflaredError
from .registry import RemoteRegistry
from .hublink import (
    HubClient, DeviceClient, build_hub_app, build_device_app, run_app,
)
from .jsonlog import QueueLogHandler
from .bootstrap import install_skills

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("any_agent")

STATE_DIR = Path.home() / ".any_agent"
CRED_FILE = STATE_DIR / "credentials.json"


DEFAULT_CONFIG = """\
ilink:
  base_url: "https://ilinkai.weixin.qq.com"
  cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c"
  bot_type: "3"
  long_poll_timeout_ms: 35000
  api_timeout_ms: 15000

# Node mode: "hub" (owns the WeChat connection) or "device" (registers with a hub).
mode: hub
device_name: hub
hub_url: ""
http_port: 8787

# Default agent for new users / before any /use command.
default_agent: opencode

devices:
  - name: opencode
    type: opencode
    bin: opencode
    cwd: .
    args: ["acp"]
  - name: claude
    type: claude
    bin: claude
    cwd: .
    args: []

state_dir: ~/.any_agent
"""


def ensure_config(path: str) -> bool:
    """Write a starter config if none exists. Returns True if it was created."""
    if os.path.exists(path):
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(DEFAULT_CONFIG)
    logger.info(f"Wrote starter config: {path} (edit it, then re-run)")
    return True


def load_config(path: str = "any_agent_config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_device_configs(config: dict) -> list[DeviceConfig]:
    """Parse the `devices:` section of the config into DeviceConfig objects."""
    device_configs = []
    for d in config.get("devices", []):
        device_configs.append(DeviceConfig(
            name=d["name"],
            type=d.get("type", d["name"]),
            bin=d["bin"],
            cwd=d.get("cwd", os.getcwd()),
            args=d.get("args", []),
            env=d.get("env", {}),
            model=d.get("model", ""),
        ))
    return device_configs


def load_credentials() -> dict:
    if CRED_FILE.exists():
        return json.loads(CRED_FILE.read_text())
    return {}


def save_credentials(creds: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CRED_FILE.write_text(json.dumps(creds, indent=2))


async def do_qr_login() -> dict:
    """Run QR login flow and return credentials."""
    async with aiohttp.ClientSession() as session:
        logger.info("Starting QR login...")
        qr_data = await start_qr_login(session)

        qrcode = qr_data.get("qrcode", "")
        qrcode_img = qr_data.get("qrcode_img_content", "")

        if not qrcode:
            logger.error(f"Failed to get QR code: {qr_data}")
            sys.exit(1)

        if qrcode_img:
            try:
                import qrcode_terminal
                qrcode_terminal.draw(qrcode)
            except ImportError:
                pass

        print(f"\nScan this QR code with WeChat:")
        print(f"QR URL: {qrcode}\n")

        def on_status(s):
            logger.info(f"QR status: {s}")

        result = await wait_for_qr_login(session, qrcode, on_status=on_status)

        if not result.connected:
            logger.error(f"Login failed: {result.message}")
            sys.exit(1)

        logger.info(f"Login OK! account={result.account_id}")
        creds = {
            "bot_token": result.bot_token,
            "account_id": result.account_id,
            "base_url": result.base_url,
            "user_id": result.user_id,
        }
        save_credentials(creds)
        return creds


async def message_loop(client: ILinkClient, devices: DeviceManager):
    """Long-poll loop: getUpdates -> process -> reply."""
    context_tokens: dict[str, str] = {}
    consecutive_failures = 0

    logger.info("Starting message loop...")
    try:
        await client.notify_start()
    except Exception as e:
        logger.warning(f"notify_start failed: {e}")

    try:
        config = await client.get_config()
        typing_ticket = config.get("typing_ticket", "")
    except Exception:
        typing_ticket = ""

    while True:
        try:
            data = await client.get_updates()
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"getUpdates error ({consecutive_failures}): {e}")
            if consecutive_failures >= 3:
                logger.error("3 consecutive failures, backing off 30s")
                consecutive_failures = 0
                await asyncio.sleep(30)
            else:
                await asyncio.sleep(2)
            continue

        msgs = data.get("msgs", [])
        for raw_msg in msgs:
            msg = WeixinMessage.from_dict(raw_msg)
            # Only process user messages (type=1), skip bot messages
            if msg.message_type != 1:
                continue

            text = msg.text_body()
            from_user = msg.from_user_id
            if not text or not from_user:
                continue

            if msg.context_token:
                context_tokens[from_user] = msg.context_token

            logger.info(f"Message from {from_user}: {text[:80]}")

            # Send typing indicator
            try:
                await client.send_typing(from_user, typing_ticket)
            except Exception:
                pass

            # Check for slash commands
            cmd_response = await devices.handle_command(from_user, text)
            if cmd_response is not None:
                logger.info(f"Command response ({len(cmd_response)} chars): {cmd_response[:200]}")
                ctx = context_tokens.get(from_user, "")
                try:
                    await client.send_message(from_user, cmd_response, context_token=ctx)
                except Exception as e:
                    logger.error(f"sendMessage for command failed: {e}")
                continue

            # Route to agent
            try:
                response = await devices.send_prompt(from_user, text)
                reply = response.text or response.error or "(no response)"
            except Exception as e:
                logger.error(f"Agent error: {e}")
                reply = f"Agent error: {e}"

            # Extract sendfile:// media markers from the agent's reply
            reply, media = parse_sendfile(reply)

            ctx = context_tokens.get(from_user, "")

            # Send remaining text (split long messages)
            max_len = 4000
            if reply:
                for i in range(0, len(reply), max_len):
                    chunk = reply[i:i + max_len]
                    try:
                        await client.send_message(from_user, chunk, context_token=ctx)
                    except Exception as e:
                        logger.error(f"sendMessage failed: {e}")

            # Send any media files the agent requested
            if media:
                allowed_root = os.path.realpath(devices.get_user_binding(from_user).cwd or "")
                for path, caption in media:
                    try:
                        real = os.path.realpath(path)
                        if not os.path.isfile(real):
                            raise FileNotFoundError(path)
                        if allowed_root and not real.startswith(allowed_root + os.sep):
                            raise PermissionError(f"outside agent directory: {path}")
                        await client.send_media(from_user, real, caption=caption, context_token=ctx)
                        logger.info(f"Sent media to {from_user}: {real}")
                    except Exception as e:
                        logger.error(f"send_media failed for {path}: {e}")
                        try:
                            await client.send_message(
                                from_user, f"[could not send file: {os.path.basename(path)}]",
                                context_token=ctx,
                            )
                        except Exception:
                            pass


def _install_signal_handlers(shutdown_event: asyncio.Event):
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)


async def _registry_sweeper(registry, devices, client, interval: float = 30.0):
    """Periodically expire silent devices; notify WeChat + reassign affected users."""
    while True:
        await asyncio.sleep(interval)
        try:
            offline = registry.sweep()
        except Exception as e:
            logger.warning(f"registry sweep error: {e}")
            continue
        for dev in offline:
            logger.warning(f"Device offline: {dev.name}")
            affected = devices.reassign_from_device(dev.name)
            default = devices.default_device()
            for uid in affected:
                try:
                    await client.send_message(
                        uid,
                        f"Device '{dev.name}' went offline. Switched you to '{default}'.",
                        context_token=client.get_context_token(uid),
                    )
                except Exception as e:
                    logger.warning(f"offline notice to {uid} failed: {e}")


async def run_hub(config: dict, args):
    ilink_cfg = config.get("ilink", {})

    creds = load_credentials()
    if args.login or not creds.get("bot_token"):
        creds = await do_qr_login()

    base_url = creds.get("base_url") or ilink_cfg.get("base_url", "https://ilinkai.weixin.qq.com")
    client = ILinkClient(
        base_url=base_url,
        token=creds["bot_token"],
        cdn_base_url=ilink_cfg.get("cdn_base_url", ""),
    )

    registry = RemoteRegistry()
    device_client = DeviceClient()
    devices = DeviceManager(
        build_device_configs(config),
        default_agent=config.get("default_agent"),
        registry=registry,
        device_client=device_client,
    )

    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event)

    # HTTP server + tunnel so remote devices can relay register/heartbeat/log/media.
    http_port = int(config.get("http_port", 8787))
    state_dir = os.path.expanduser(config.get("state_dir", "~/.any_agent"))
    os.makedirs(state_dir, exist_ok=True)
    combined_log = os.path.join(state_dir, "combined.jsonl")

    async def on_media(to_user_id: str, path: str, caption: str):
        await client.send_media(to_user_id, path, caption=caption,
                                context_token=client.get_context_token(to_user_id))

    hub_app = build_hub_app(registry, on_media, combined_log_path=combined_log)
    runner = await run_app(hub_app, http_port)

    tunnel = CloudflaredTunnel(http_port)
    try:
        hub_url = await tunnel.start()
        logger.info(f"Hub reachable at {hub_url} (share this with devices)")
    except CloudflaredError as e:
        logger.warning(f"Tunnel unavailable ({e}); remote devices cannot connect this run.")

    sweeper = asyncio.create_task(_registry_sweeper(registry, devices, client))

    # Pre-start the default (local) device
    default = devices.default_device()
    if default and default in devices.configs:
        default_cwd = devices.configs[default].cwd
        logger.info(f"Pre-starting {default} in {default_cwd}...")
        try:
            await devices.get_client(default, default_cwd)
            logger.info(f"{default} ready.")
        except Exception as e:
            logger.error(f"Failed to pre-start {default}: {e}")

    loop_task = asyncio.create_task(message_loop(client, devices))
    shutdown_wait = asyncio.create_task(shutdown_event.wait())
    done, _ = await asyncio.wait(
        [loop_task, shutdown_wait], return_when=asyncio.FIRST_COMPLETED
    )
    if loop_task in done:
        loop_task.result()
    else:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    logger.info("Shutting down hub...")
    sweeper.cancel()
    await tunnel.stop()
    await runner.cleanup()
    await device_client.close()
    try:
        await client.notify_stop()
    except Exception:
        pass
    await client.close()
    await devices.stop_all()
    logger.info("Hub stopped.")


async def run_device(config: dict, args):
    device_name = args.device_name or config.get("device_name", "device")
    hub_url = args.hub_url or config.get("hub_url", "")
    if not hub_url:
        logger.error("device mode requires hub_url (config or --hub-url)")
        sys.exit(1)

    devices = DeviceManager(
        build_device_configs(config),
        default_agent=config.get("default_agent"),
    )

    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event)

    hub = HubClient(hub_url, device_name)

    # Ship this node's logs to the hub.
    qhandler = QueueLogHandler(device_name)
    qhandler.setLevel(logging.INFO)
    logging.getLogger().addHandler(qhandler)

    # Device HTTP handlers: run local agents on behalf of the hub.
    async def on_prompt(agent: str, cwd: str, text: str, user_id: str) -> dict:
        # Bind a synthetic per-request user so DeviceManager routes to the right agent.
        devices.set_user_binding(f"hub:{user_id}", device=agent, cwd=cwd or None)
        resp = await devices.send_prompt(f"hub:{user_id}", text)
        clean, media = parse_sendfile(resp.text or "")
        # Relay any media files back to the hub (which owns WeChat).
        for path, caption in media:
            try:
                real = os.path.realpath(path)
                if os.path.isfile(real):
                    await hub.send_media(user_id, real, caption=caption)
            except Exception as e:
                logger.error(f"relay media {path} failed: {e}")
        return {"text": clean, "error": resp.error}

    async def on_command(agent: str, cwd: str, text: str) -> dict:
        devices.set_user_binding(f"hub:cmd", device=agent, cwd=cwd or None)
        result = await devices.handle_command("hub:cmd", text)
        return {"result": result}

    device_app = build_device_app(on_prompt, on_command)
    http_port = int(config.get("http_port", 8787))
    runner = await run_app(device_app, http_port)

    tunnel = CloudflaredTunnel(http_port)
    tunnel_url = await tunnel.start()
    logger.info(f"Device '{device_name}' tunnel: {tunnel_url}")

    agents = [
        {"name": c.name, "type": c.type, "model": c.model, "cwd": c.cwd}
        for c in devices.configs.values()
    ]
    await hub.register(tunnel_url, agents)
    logger.info(f"Registered {len(agents)} agents with hub {hub_url}")

    hb_task = asyncio.create_task(hub.heartbeat_loop(60.0))
    log_task = asyncio.create_task(qhandler.drain(hub))

    await shutdown_event.wait()

    logger.info("Shutting down device...")
    hb_task.cancel()
    log_task.cancel()
    await tunnel.stop()
    await runner.cleanup()
    await hub.close()
    await devices.stop_all()
    logger.info("Device stopped.")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="any-agent: WeChat bridge to coding agents")
    parser.add_argument("--config", default="any_agent_config.yaml", help="config file path")
    parser.add_argument("--login", action="store_true", help="force QR re-login")
    parser.add_argument("--mode", choices=["hub", "device"], help="override config mode")
    parser.add_argument("--hub-url", help="device mode: hub's public URL")
    parser.add_argument("--device-name", help="override this node's device name")
    parser.add_argument("--no-bootstrap", action="store_true",
                        help="skip first-run skill install")
    args = parser.parse_args()

    # First-run bootstrap: install bundled skills into the working directory.
    if not args.no_bootstrap:
        try:
            install_skills()
        except Exception as e:
            logger.warning(f"skill bootstrap skipped: {e}")

    # First run with no config: write a starter and stop so the user can edit it.
    if ensure_config(args.config):
        print(f"\nCreated {args.config}. Edit it (agents, cwd), then run again with --login.")
        return

    config = load_config(args.config)
    mode = args.mode or config.get("mode", "hub")

    if mode == "device":
        await run_device(config, args)
    else:
        await run_hub(config, args)


def cli():
    """Console-script / module entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()

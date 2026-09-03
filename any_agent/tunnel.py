"""
Cloudflared tunnel lifecycle manager.

Spawns `cloudflared tunnel --url http://localhost:<port>` as a managed subprocess,
parses the public https://*.trycloudflare.com URL from its output, and tears the
process down on stop() / shutdown.
"""

import asyncio
import logging
import os
import platform
import re
import shutil
import stat
import urllib.request

logger = logging.getLogger("any_agent.tunnel")

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

_INSTALL_DOCS = (
    "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
)

# Direct download URLs by (system, machine) for the standalone binary fallback.
_RELEASE_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"
_DOWNLOAD_ASSETS = {
    ("darwin", "arm64"): "cloudflared-darwin-arm64.tgz",
    ("darwin", "x86_64"): "cloudflared-darwin-amd64.tgz",
    ("linux", "x86_64"): "cloudflared-linux-amd64",
    ("linux", "aarch64"): "cloudflared-linux-arm64",
    ("linux", "arm64"): "cloudflared-linux-arm64",
    ("linux", "armv7l"): "cloudflared-linux-arm",
}


class CloudflaredError(RuntimeError):
    pass


async def _run(cmd: list[str], timeout: float = 300) -> tuple[int, str]:
    """Run a command, capturing combined output. Returns (returncode, output)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode, out.decode(errors="replace")


async def ensure_cloudflared(bin_path: str = "cloudflared") -> str:
    """
    Ensure a cloudflared binary is available. If missing, try to install it:
      - macOS: `brew install cloudflared` (if brew present)
      - else: download the standalone binary from GitHub releases into ~/.any_agent/bin
    Returns the resolved path/name to invoke. Raises CloudflaredError if it can't.
    """
    found = shutil.which(bin_path)
    if found:
        return bin_path

    system = platform.system().lower()
    machine = platform.machine().lower()
    logger.info(f"cloudflared not found; attempting install (system={system} machine={machine})")

    # 1) Homebrew on macOS
    if system == "darwin" and shutil.which("brew"):
        logger.info("Installing cloudflared via Homebrew...")
        try:
            rc, out = await _run(["brew", "install", "cloudflared"])
            if rc == 0 and shutil.which(bin_path):
                logger.info("cloudflared installed via brew")
                return bin_path
            logger.warning(f"brew install failed (rc={rc}): {out[-300:]}")
        except Exception as e:
            logger.warning(f"brew install error: {e}")

    # 2) Standalone binary download from GitHub releases
    asset = _DOWNLOAD_ASSETS.get((system, machine))
    if asset:
        try:
            return await _download_binary(asset)
        except Exception as e:
            logger.warning(f"cloudflared download failed: {e}")

    raise CloudflaredError(
        f"Could not auto-install cloudflared for {system}/{machine}. "
        f"Install it manually: {_INSTALL_DOCS}"
    )


async def _download_binary(asset: str) -> str:
    """Download the cloudflared binary (or .tgz) into ~/.any_agent/bin and return its path."""
    dest_dir = os.path.expanduser("~/.any_agent/bin")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "cloudflared")
    url = f"{_RELEASE_BASE}/{asset}"
    logger.info(f"Downloading cloudflared from {url}")

    def _fetch():
        tmp = os.path.join(dest_dir, asset)
        urllib.request.urlretrieve(url, tmp)
        if asset.endswith(".tgz"):
            import tarfile
            with tarfile.open(tmp) as tf:
                member = next((m for m in tf.getmembers() if m.name.endswith("cloudflared")), None)
                if member is None:
                    raise CloudflaredError("cloudflared binary not found in archive")
                member.name = "cloudflared"
                tf.extract(member, dest_dir)
            os.remove(tmp)
        else:
            os.replace(tmp, dest)
        st = os.stat(dest)
        os.chmod(dest, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # urlretrieve/tar are blocking — run off the event loop.
    await asyncio.get_event_loop().run_in_executor(None, _fetch)
    logger.info(f"cloudflared installed at {dest}")
    return dest


class CloudflaredTunnel:
    """Manages a `cloudflared` quick-tunnel subprocess for one local port."""

    def __init__(self, port: int, bin_path: str = "cloudflared"):
        self.port = port
        self.bin_path = bin_path
        self.url: str = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._ready: asyncio.Event = asyncio.Event()
        self._reader_task: asyncio.Task | None = None

    async def start(self, timeout: float = 30, auto_install: bool = True) -> str:
        """Launch cloudflared and return the public URL once it's ready."""
        if not shutil.which(self.bin_path) and not os.path.isfile(self.bin_path):
            if auto_install:
                self.bin_path = await ensure_cloudflared(self.bin_path)
            else:
                raise CloudflaredError(
                    f"'{self.bin_path}' not found. Install cloudflared: {_INSTALL_DOCS}"
                )

        cmd = [self.bin_path, "tunnel", "--url", f"http://localhost:{self.port}"]
        logger.info(f"Starting tunnel: {' '.join(cmd)}")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._reader_task = asyncio.create_task(self._read_output())

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self.stop()
            raise CloudflaredError(f"Tunnel did not report a URL within {timeout}s")
        return self.url

    async def _read_output(self):
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            logger.debug(f"[cloudflared] {text}")
            if not self.url:
                m = _URL_RE.search(text)
                if m:
                    self.url = m.group(0)
                    logger.info(f"Tunnel URL: {self.url}")
                    self._ready.set()

    async def stop(self):
        if self._proc and self._proc.returncode is None:
            logger.info("Stopping tunnel")
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
        self._proc = None
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

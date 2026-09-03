"""
Remote device registry (hub side).

Tracks devices that have registered their local agents with the hub via cloudflared
tunnels. Each device heartbeats periodically; the sweeper expires devices that go silent.
"""

import time
from dataclasses import dataclass, field


@dataclass
class RemoteDevice:
    name: str
    tunnel_url: str
    # agents: list of {name, type, model, cwd}
    agents: list[dict] = field(default_factory=list)
    last_seen: float = field(default_factory=time.time)


class RemoteRegistry:
    """In-memory registry of connected remote devices."""

    def __init__(self, timeout: float = 90.0):
        self.timeout = timeout
        self._devices: dict[str, RemoteDevice] = {}

    def register(self, name: str, tunnel_url: str, agents: list[dict]) -> RemoteDevice:
        """Add or replace a device. Auto-rejoin: re-registering just refreshes the entry."""
        dev = RemoteDevice(
            name=name,
            tunnel_url=tunnel_url,
            agents=agents or [],
            last_seen=time.time(),
        )
        self._devices[name] = dev
        return dev

    def touch(self, name: str) -> bool:
        """Refresh a device's last-seen. Returns False if not registered."""
        dev = self._devices.get(name)
        if not dev:
            return False
        dev.last_seen = time.time()
        return True

    def sweep(self, now: float | None = None) -> list[RemoteDevice]:
        """Remove devices whose last_seen exceeds the timeout. Returns removed devices."""
        now = now if now is not None else time.time()
        offline = [d for d in self._devices.values() if now - d.last_seen > self.timeout]
        for d in offline:
            self._devices.pop(d.name, None)
        return offline

    def get(self, name: str) -> RemoteDevice | None:
        return self._devices.get(name)

    def find(self, device: str, agent: str) -> tuple[RemoteDevice, dict] | None:
        """Look up a specific agent on a device. Returns (device, agent_dict) or None."""
        dev = self._devices.get(device)
        if not dev:
            return None
        for a in dev.agents:
            if a.get("name") == agent:
                return dev, a
        return None

    def agents_for(self, device: str) -> list[dict]:
        dev = self._devices.get(device)
        return list(dev.agents) if dev else []

    @property
    def devices(self) -> list[RemoteDevice]:
        return list(self._devices.values())

    def all_agent_paths(self) -> list[str]:
        """All remote agents as '<device>/<agent>' strings."""
        paths = []
        for dev in self._devices.values():
            for a in dev.agents:
                paths.append(f"{dev.name}/{a.get('name')}")
        return paths

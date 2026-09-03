"""
JSON log formatting + shipping device logs to the hub.

- JsonFormatter: one JSON object per line ({ts, level, name, msg, device}).
- QueueLogHandler: non-blocking handler that enqueues records; a background task
  drains the queue and ships batches to the hub via HubClient.send_log.
"""

import asyncio
import json
import logging
import time


class JsonFormatter(logging.Formatter):
    def __init__(self, device: str = ""):
        super().__init__()
        self.device = device

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        if self.device:
            obj["device"] = self.device
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False)


class QueueLogHandler(logging.Handler):
    """Enqueues log records as dicts; a drainer task ships them to the hub."""

    def __init__(self, device: str):
        super().__init__()
        self.device = device
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    def emit(self, record: logging.LogRecord):
        try:
            item = {
                "ts": round(record.created, 3),
                "level": record.levelname,
                "name": record.name,
                "msg": record.getMessage(),
            }
            self.queue.put_nowait(item)
        except (asyncio.QueueFull, Exception):
            pass  # never let logging raise

    async def drain(self, hub_client, interval: float = 2.0, batch: int = 50):
        """Background loop: batch records and POST to the hub /log endpoint."""
        while True:
            await asyncio.sleep(interval)
            records = []
            while not self.queue.empty() and len(records) < batch:
                try:
                    records.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if records:
                try:
                    await hub_client.send_log(records)
                except Exception:
                    pass  # best-effort; don't crash the device on log-ship failure

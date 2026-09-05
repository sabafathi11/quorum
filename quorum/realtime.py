"""One websocket room per open capture: ops, presence, job events.

Threads (jobs, ingest) publish through `Hub.publish`, which hops onto the event
loop; the loop fans out to each connection's own queue so one slow client can
never stall a writer.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict


class Connection:
    def __init__(self, user: str, capture_id: int | None):
        self.user = user
        self.capture_id = capture_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.state: dict = {}          # presence payload: frame, tool, selection

    def offer(self, msg: dict) -> None:
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass                        # a client this far behind will re-sync on reconnect


class Hub:
    def __init__(self):
        self.rooms: dict[int | None, set[Connection]] = defaultdict(set)
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop) -> None:
        self.loop = loop

    def join(self, conn: Connection) -> None:
        self.rooms[conn.capture_id].add(conn)
        self.presence(conn.capture_id)

    def leave(self, conn: Connection) -> None:
        self.rooms[conn.capture_id].discard(conn)
        self.presence(conn.capture_id)

    def presence(self, capture_id) -> None:
        who = [{"user": c.user, **c.state} for c in self.rooms.get(capture_id, ())]
        self._fan(capture_id, {"t": "presence", "users": who})

    def _fan(self, capture_id, msg: dict) -> None:
        for c in list(self.rooms.get(capture_id, ())):
            c.offer(msg)
        if capture_id is not None:                      # lobby watches everything
            for c in list(self.rooms.get(None, ())):
                c.offer(msg)

    def publish(self, capture_id, msg: dict) -> None:
        """Safe from any thread."""
        if self.loop is None:
            self._fan(capture_id, msg)
            return
        try:
            self.loop.call_soon_threadsafe(self._fan, capture_id, msg)
        except RuntimeError:
            pass

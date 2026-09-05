"""The capture room."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..realtime import Connection
from .deps import ws_user

router = APIRouter()


@router.websocket("/ws/capture/{cid}")
async def capture_socket(ws: WebSocket, cid: int):
    host = ws.app.state.host
    try:
        user = await ws_user(ws)
    except Exception:
        await ws.close(code=4401)
        return
    await ws.accept()
    capture_id = None if cid == 0 else cid
    conn = Connection(user["name"], capture_id)
    host.hub.bind(asyncio.get_running_loop())
    host.hub.join(conn)
    await ws.send_json({"t": "hello", "user": user["name"], "capture": capture_id,
                        "lastOp": host.db.scalar(
                            "SELECT MAX(id) FROM ops WHERE capture_id=?", capture_id) or 0})

    async def pump():
        while True:
            msg = await conn.queue.get()
            await ws.send_json(msg)

    task = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("t")
            if t == "presence":
                conn.state = {k: msg.get(k) for k in ("frame", "tool", "stream")}
                host.hub.presence(capture_id)
            elif t == "ping":
                await ws.send_json({"t": "pong"})
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        task.cancel()
        host.hub.leave(conn)

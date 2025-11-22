# server.py
import json
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

app = FastAPI()
# serve the static frontend from ./static
app.mount("/static", StaticFiles(directory="static"), name="static")

# rooms: room_id -> set of websockets
rooms = {}

@app.websocket("/ws/{room_id}/{client_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, client_id: str):
    await websocket.accept()
    if room_id not in rooms:
        rooms[room_id] = {}
    rooms[room_id][client_id] = websocket

    try:
        # Inform existing peers about newcomer (optional)
        await notify_peers_join(room_id, client_id)

        while True:
            data = await websocket.receive_text()
            # Expect JSON: { "to": "<client_id>", "type": "...", "payload": {...} }
            msg = json.loads(data)
            target = msg.get("to")
            if target:
                # forward to target if present
                target_ws = rooms.get(room_id, {}).get(target)
                if target_ws:
                    await target_ws.send_text(json.dumps({
                        "from": client_id,
                        "type": msg.get("type"),
                        "payload": msg.get("payload")
                    }))
                else:
                    # optional: let caller know peer not found
                    await websocket.send_text(json.dumps({
                        "from": "server",
                        "type": "error",
                        "payload": {"message": "target-not-found", "target": target}
                    }))
            else:
                # broadcast to others if no explicit target (useful for simple discovery)
                await broadcast_in_room(room_id, client_id, msg)
    except WebSocketDisconnect:
        # cleanup
        await remove_client_from_room(room_id, client_id)

async def notify_peers_join(room_id: str, new_client_id: str):
    # notify other peers about new participant
    for cid, ws in list(rooms.get(room_id, {}).items()):
        if cid == new_client_id:
            continue
        try:
            await ws.send_text(json.dumps({
                "from": "server",
                "type": "peer-joined",
                "payload": {"client_id": new_client_id}
            }))
        except:
            pass

async def broadcast_in_room(room_id: str, sender_id: str, msg: dict):
    for cid, ws in list(rooms.get(room_id, {}).items()):
        if cid == sender_id:
            continue
        try:
            await ws.send_text(json.dumps({
                "from": sender_id,
                "type": msg.get("type"),
                "payload": msg.get("payload")
            }))
        except:
            pass

async def remove_client_from_room(room_id: str, client_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    room.pop(client_id, None)
    # notify remaining
    for cid, ws in list(room.items()):
        try:
            await ws.send_text(json.dumps({
                "from": "server",
                "type": "peer-left",
                "payload": {"client_id": client_id}
            }))
        except:
            pass
    if len(room) == 0:
        rooms.pop(room_id, None)

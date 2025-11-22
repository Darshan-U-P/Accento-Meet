# server.py
import json
import asyncio
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI()

# serve static directory
static_dir = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# serve index at root so / loads the UI
INDEX_PATH = static_dir / "index.html"

@app.get("/")
async def root():
    # return the static/index.html at root
    return FileResponse(str(INDEX_PATH))

# rooms: room_id -> dict client_id -> websocket
rooms: dict[str, dict[str, WebSocket]] = {}

@app.websocket("/ws/{room_id}/{client_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, client_id: str):
    await websocket.accept()
    if room_id not in rooms:
        rooms[room_id] = {}
    rooms[room_id][client_id] = websocket

    try:
        await notify_peers_join(room_id, client_id)

        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            target = msg.get("to")
            if target:
                target_ws = rooms.get(room_id, {}).get(target)
                if target_ws:
                    await target_ws.send_text(json.dumps({
                        "from": client_id,
                        "type": msg.get("type"),
                        "payload": msg.get("payload")
                    }))
                else:
                    await websocket.send_text(json.dumps({
                        "from": "server",
                        "type": "error",
                        "payload": {"message": "target-not-found", "target": target}
                    }))
            else:
                await broadcast_in_room(room_id, client_id, msg)
    except WebSocketDisconnect:
        await remove_client_from_room(room_id, client_id)
    except Exception:
        await remove_client_from_room(room_id, client_id)

async def notify_peers_join(room_id: str, new_client_id: str):
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

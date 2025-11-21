# server.py
import json
import uuid
from typing import Dict, Any
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI()

# Serve static files from ./static
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")

# rooms: room_id -> dict(client_id -> { "ws": WebSocket, "name": str })
rooms: Dict[str, Dict[str, Dict[str, Any]]] = {}


async def safe_send(ws: WebSocket, payload: dict):
    """Send JSON payload safely to a websocket (swallow errors)."""
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        # client may have disconnected; ignore here
        pass


def _participants_list(room_id: str):
    """Return list of participants (id, name) for the room."""
    return [{"id": cid, "name": rec.get("name", "")} for cid, rec in rooms.get(room_id, {}).items()]


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    # Accept connection
    await websocket.accept()

    # Make sure room exists
    if room_id not in rooms:
        rooms[room_id] = {}

    # assign id
    client_id = uuid.uuid4().hex[:8]
    # placeholder record (name set after join)
    rooms[room_id][client_id] = {"ws": websocket, "name": ""}

    try:
        # Expect initial 'join' message from client with {"type":"join","name":"Alice"}
        raw = await websocket.receive_text()
        try:
            msg = json.loads(raw)
        except Exception:
            msg = {}

        if msg.get("type") != "join":
            await safe_send(websocket, {"type": "error", "message": "First message must be type:'join' with a name"})
            await websocket.close()
            del rooms[room_id][client_id]
            return

        display_name = str(msg.get("name", f"User-{client_id}"))[:64]
        rooms[room_id][client_id]["name"] = display_name

        # Send welcome (your id + current participants)
        await safe_send(websocket, {
            "type": "welcome",
            "id": client_id,
            "participants": _participants_list(room_id),
        })

        # Notify others
        join_notice = {"type": "participant-joined", "id": client_id, "name": display_name}
        for cid, rec in list(rooms[room_id].items()):
            if cid == client_id:
                continue
            await safe_send(rec["ws"], join_notice)

        # Broadcast updated participants to all
        participants_update = {"type": "participants", "participants": _participants_list(room_id)}
        for cid, rec in list(rooms[room_id].items()):
            await safe_send(rec["ws"], participants_update)

        # Main receive loop
        while True:
            text = await websocket.receive_text()
            try:
                message = json.loads(text)
            except Exception:
                # ignore invalid JSON
                continue

            mtype = message.get("type")

            # chat message: broadcast to room
            if mtype == "chat":
                text_msg = str(message.get("text", ""))[:2000]
                payload = {"type": "chat", "from": client_id, "name": display_name, "text": text_msg}
                for cid, rec in list(rooms[room_id].items()):
                    await safe_send(rec["ws"], payload)
                continue

            # client wants to rename
            if mtype == "rename":
                new_name = str(message.get("name", "") )[:64]
                rooms[room_id][client_id]["name"] = new_name
                participants_update = {"type": "participants", "participants": _participants_list(room_id)}
                for cid, rec in list(rooms[room_id].items()):
                    await safe_send(rec["ws"], participants_update)
                continue

            # fallback: broadcast raw message to others (if needed)
            for cid, rec in list(rooms[room_id].items()):
                if cid == client_id:
                    continue
                await safe_send(rec["ws"], message)

    except WebSocketDisconnect:
        # remove participant
        if room_id in rooms and client_id in rooms[room_id]:
            left_name = rooms[room_id][client_id].get("name", client_id)
            del rooms[room_id][client_id]

            # notify remaining participants
            left_notice = {"type": "participant-left", "id": client_id, "name": left_name}
            for cid, rec in list(rooms.get(room_id, {}).items()):
                await safe_send(rec["ws"], left_notice)

            # participants update
            participants_update = {"type": "participants", "participants": _participants_list(room_id)}
            for cid, rec in list(rooms.get(room_id, {}).items()):
                await safe_send(rec["ws"], participants_update)

        # cleanup empty room
        if room_id in rooms and not rooms[room_id]:
            del rooms[room_id]


if __name__ == "__main__":
    # Run with: python server.py
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

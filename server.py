# server.py
import json
import uvicorn
import uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Dict, Any

app = FastAPI()

# Serve static files at /static
app.mount("/static", StaticFiles(directory="static"), name="static")

# Serve index at root — serves static/index.html
@app.get("/")
async def index():
    return FileResponse("static/index.html")

# rooms: room_id -> dict(client_id -> { ws, displayName, is_host })
rooms: Dict[str, Dict[str, Dict[str, Any]]] = {}

async def safe_send(ws: WebSocket, payload: dict):
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        # ignore send errors; cleanup happens on disconnect
        pass

def _make_participant_info(client_id: str, record: Dict[str, Any]):
    return {"id": client_id, "displayName": record.get("displayName", ""), "is_host": bool(record.get("is_host", False))}

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()

    if room_id not in rooms:
        rooms[room_id] = {}

    client_id = uuid.uuid4().hex[:8]
    # placeholder entry until client sends 'join' with displayName
    rooms[room_id][client_id] = {"ws": websocket, "displayName": "", "is_host": False}

    try:
        # wait for join message from client to set displayName and notify peers
        text = await websocket.receive_text()
        try:
            message = json.loads(text)
        except Exception:
            message = {}

        # Expect client to send {"type":"join", "displayName":"Alice"}
        if message.get("type") == "join":
            display_name = message.get("displayName", f"User-{client_id}")
            rooms[room_id][client_id]["displayName"] = display_name

            # If no host yet, assign this client as host
            existing_hosts = [cid for cid, rec in rooms[room_id].items() if rec.get("is_host")]
            if not existing_hosts:
                rooms[room_id][client_id]["is_host"] = True

            # send welcome: assigned id, participant list, host id
            participants = [_make_participant_info(cid, rec) for cid, rec in rooms[room_id].items()]
            host_id = next((cid for cid, rec in rooms[room_id].items() if rec.get("is_host")), None)

            await safe_send(websocket, {"type": "welcome", "client_id": client_id, "participants": participants, "host_id": host_id})

            # notify others a participant joined
            join_msg = {"type": "participant-joined", "participant": _make_participant_info(client_id, rooms[room_id][client_id])}
            for cid, rec in list(rooms[room_id].items()):
                if cid == client_id:
                    continue
                await safe_send(rec["ws"], join_msg)
        else:
            # if not well-formed join, send error and close
            await safe_send(websocket, {"type": "error", "message": "Expected initial join message with type='join' and displayName"})
            await websocket.close()
            del rooms[room_id][client_id]
            return

        # main loop: handle messages for signaling, chat, and host actions
        while True:
            text = await websocket.receive_text()
            try:
                message = json.loads(text)
            except Exception:
                # ignore malformed
                continue

            # Attach 'from' if missing
            message.setdefault("from", client_id)

            mtype = message.get("type")

            # Signaling messages (offer/answer/ice) — directed with 'to'
            if mtype in ("offer", "answer", "ice-candidate"):
                target = message.get("to")
                if target and target in rooms[room_id]:
                    await safe_send(rooms[room_id][target]["ws"], message)
                else:
                    # target absent => return error
                    await safe_send(websocket, {"type": "error", "message": f"target {target} not in room"})
                continue

            # Chat: broadcast to all participants
            if mtype == "chat":
                text_payload = {"type": "chat", "from": client_id, "text": message.get("text", "")}
                for cid, rec in list(rooms[room_id].items()):
                    if cid == client_id:
                        continue
                    await safe_send(rec["ws"], text_payload)
                continue

            # Host actions: 'action' with action details
            if mtype == "action":
                action = message.get("action")
                target = message.get("target")
                actor = client_id
                actor_rec = rooms[room_id].get(actor)
                if not actor_rec:
                    continue

                # Only allow host to perform privileged actions
                if action in ("mute", "kick", "make-host", "lock-room", "unlock-room"):
                    if not actor_rec.get("is_host", False):
                        await safe_send(websocket, {"type": "error", "message": "Only host may perform this action"})
                        continue

                # handle mute: send command to target to force-mute
                if action == "mute" and target:
                    if target in rooms[room_id]:
                        await safe_send(rooms[room_id][target]["ws"], {"type": "command", "cmd": "force-mute", "from": actor})
                        notify = {"type": "system", "message": f"{actor_rec['displayName']} asked {rooms[room_id][target]['displayName']} to mute"}
                        for cid, rec in list(rooms[room_id].items()):
                            await safe_send(rec["ws"], notify)
                    continue

                # handle kick: close target connection and remove them
                if action == "kick" and target:
                    if target in rooms[room_id]:
                        try:
                            await safe_send(rooms[room_id][target]["ws"], {"type": "command", "cmd": "you-are-kicked", "from": actor})
                            await rooms[room_id][target]["ws"].close()
                        except Exception:
                            pass
                    continue

                # make-host: transfer host role to target
                if action == "make-host" and target:
                    if target in rooms[room_id]:
                        for cid, rec in rooms[room_id].items():
                            rec["is_host"] = False
                        rooms[room_id][target]["is_host"] = True
                        participants = [_make_participant_info(cid, rec) for cid, rec in rooms[room_id].items()]
                        host_id = target
                        for cid, rec in list(rooms[room_id].items()):
                            await safe_send(rec["ws"], {"type": "participants-update", "participants": participants, "host_id": host_id})
                    continue

                # lock/unlock room (simple flag)
                if action in ("lock-room", "unlock-room"):
                    if action == "lock-room":
                        rooms[room_id].setdefault("_meta", {})["locked"] = True
                    else:
                        rooms[room_id].setdefault("_meta", {})["locked"] = False
                    for cid, rec in list(rooms[room_id].items()):
                        await safe_send(rec["ws"], {"type": "room-lock", "locked": rooms[room_id].get("_meta", {}).get("locked", False)})
                    continue

                # unknown action -> ignore
                await safe_send(websocket, {"type": "error", "message": f"Unknown action {action}"})
                continue

            # If client wants to update their displayName
            if mtype == "update-name":
                newname = message.get("displayName", "")
                rooms[room_id][client_id]["displayName"] = newname
                participants = [_make_participant_info(cid, rec) for cid, rec in rooms[room_id].items()]
                host_id = next((cid for cid, rec in rooms[room_id].items() if rec.get("is_host")), None)
                for cid, rec in list(rooms[room_id].items()):
                    await safe_send(rec["ws"], {"type": "participants-update", "participants": participants, "host_id": host_id})
                continue

            # fallback: broadcast unknown message types
            for cid, rec in list(rooms[room_id].items()):
                if cid == client_id:
                    continue
                await safe_send(rec["ws"], message)

    except WebSocketDisconnect:
        # remove participant
        if room_id in rooms and client_id in rooms[room_id]:
            left_display = rooms[room_id][client_id].get("displayName", client_id)
            was_host = rooms[room_id][client_id].get("is_host", False)
            del rooms[room_id][client_id]

            # If host left, promote another participant (first in dict) to host
            if was_host and rooms.get(room_id):
                first_cid = next(iter(rooms[room_id].keys()))
                rooms[room_id][first_cid]["is_host"] = True

            # notify remaining participants
            participants = [_make_participant_info(cid, rec) for cid, rec in rooms[room_id].items()]
            host_id = next((cid for cid, rec in rooms[room_id].items() if rec.get("is_host")), None)
            left_msg = {"type": "participant-left", "id": client_id, "displayName": left_display}
            for cid, rec in list(rooms[room_id].items()):
                await safe_send(rec["ws"], left_msg)
                await safe_send(rec["ws"], {"type": "participants-update", "participants": participants, "host_id": host_id})

        # if room empty remove it
        if room_id in rooms and not rooms[room_id]:
            del rooms[room_id]


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

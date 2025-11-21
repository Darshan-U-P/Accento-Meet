# server.py
import json
import uvicorn
import uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Dict, Any

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")

# rooms: room_id -> dict(client_id -> { ws, displayName, is_host, is_approved })
rooms: Dict[str, Dict[str, Dict[str, Any]]] = {}

async def safe_send(ws: WebSocket, payload: dict):
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass

def _make_participant_info(client_id: str, record: Dict[str, Any]):
    # only send info for approved participants
    return {"id": client_id, "displayName": record.get("displayName", ""), "is_host": bool(record.get("is_host", False))}

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()

    if room_id not in rooms:
        rooms[room_id] = {}

    client_id = uuid.uuid4().hex[:8]
    # We'll store is_approved flag; default True for now until we check room meta
    rooms[room_id][client_id] = {"ws": websocket, "displayName": "", "is_host": False, "is_approved": True}

    print(f"[CONNECT] room={room_id} assigned_id={client_id}")

    try:
        # Receive initial join
        text = await websocket.receive_text()
        try:
            message = json.loads(text)
        except Exception:
            message = {}

        print(f"[WS RX] room={room_id} id={client_id} initial_msg={message.get('type')}")

        if message.get("type") != "join":
            await safe_send(websocket, {"type": "error", "message": "Expected initial join message with type='join' and displayName"})
            await websocket.close()
            del rooms[room_id][client_id]
            print(f"[DISCONNECT] room={room_id} id={client_id} missing-join")
            return

        # Set display name
        display_name = message.get("displayName", f"User-{client_id}")
        rooms[room_id][client_id]["displayName"] = display_name

        # Discover host and meta
        meta = rooms[room_id].get("_meta", {})
        require_approval = bool(meta.get("require_approval", False))
        # If no host yet, first participant becomes host and is auto-approved
        existing_hosts = [cid for cid, rec in rooms[room_id].items() if rec.get("is_host")]
        if not existing_hosts:
            rooms[room_id][client_id]["is_host"] = True
            rooms[room_id][client_id]["is_approved"] = True
            print(f"[HOST] room={room_id} id={client_id} became_host")
        else:
            # if approval required and this joiner is not the host, mark as pending
            if require_approval:
                rooms[room_id][client_id]["is_approved"] = False
                print(f"[PENDING] room={room_id} id={client_id} waiting_for_host")
            else:
                rooms[room_id][client_id]["is_approved"] = True
                print(f"[APPROVED] room={room_id} id={client_id} auto_approved")

        # If the participant is not approved, notify host(s) and put client in waiting state
        if not rooms[room_id][client_id]["is_approved"]:
            # find host(s) - normally single host
            host_ids = [cid for cid, rec in rooms[room_id].items() if rec.get("is_host")]
            # send waiting message to the joiner
            await safe_send(websocket, {"type": "waiting", "message": "Waiting for host approval"})
            # notify hosts about the join request
            join_req = {"type": "join-request", "participant": {"id": client_id, "displayName": display_name}}
            for hid in host_ids:
                try:
                    await safe_send(rooms[room_id][hid]["ws"], join_req)
                    print(f"[JOIN-REQ] room={room_id} from={client_id} -> host={hid}")
                except Exception:
                    pass
            # now just loop waiting for host to accept/reject (host's action handler will set is_approved)
        else:
            # Approved immediately: build participants list of only approved participants
            participants = [_make_participant_info(cid, rec) for cid, rec in rooms[room_id].items() if rec.get("is_approved")]
            host_id = next((cid for cid, rec in rooms[room_id].items() if rec.get("is_host")), None)
            # send welcome to this client (only approved participants included)
            await safe_send(websocket, {"type": "welcome", "client_id": client_id, "participants": participants, "host_id": host_id})
            print(f"[WELCOME] room={room_id} to={client_id} participants_count={len(participants)}")
            # notify other approved participants that someone joined
            join_msg = {"type": "participant-joined", "participant": _make_participant_info(client_id, rooms[room_id][client_id])}
            for cid, rec in list(rooms[room_id].items()):
                if cid == client_id:
                    continue
                # only notify approved participants
                if rec.get("is_approved"):
                    await safe_send(rec["ws"], join_msg)

            # Tell all existing approved participants to start WebRTC offer to this new member
            offer_trigger = {"type": "need-offer", "target": client_id}
            for cid, rec in list(rooms[room_id].items()):
                if cid != client_id and rec.get("is_approved"):
                    try:
                        await safe_send(rec["ws"], offer_trigger)
                        print(f"[NEED-OFFER] room={room_id} ask={cid} -> create offer for {client_id}")
                    except Exception:
                        pass

        # main loop
        while True:
            text = await websocket.receive_text()
            try:
                message = json.loads(text)
            except Exception:
                continue

            message.setdefault("from", client_id)
            mtype = message.get("type")
            print(f"[WS RX] room={room_id} from={client_id} type={mtype} to={message.get('to')}")

            # Signaling: offer/answer/ice directed by 'to'
            if mtype in ("offer", "answer", "ice-candidate"):
                target = message.get("to")
                if target and target in rooms[room_id]:
                    # only forward if target is approved (or allow forwarded for pending? choose to allow only approved)
                    if rooms[room_id][target].get("is_approved", False):
                        await safe_send(rooms[room_id][target]["ws"], message)
                        print(f"[FORWARD] room={room_id} {mtype} from={client_id} -> {target}")
                    else:
                        await safe_send(websocket, {"type": "error", "message": f"target {target} not approved yet"})
                        print(f"[BLOCK] room={room_id} cannot forward {mtype} to {target} not approved")
                else:
                    await safe_send(websocket, {"type": "error", "message": f"target {target} not in room"})
                    print(f"[ERROR] room={room_id} {mtype} target missing: {target}")
                continue

            # Chat: broadcast to approved participants only
            if mtype == "chat":
                text_payload = {"type": "chat", "from": client_id, "text": message.get("text", "")}
                for cid, rec in list(rooms[room_id].items()):
                    if cid == client_id:
                        continue
                    if rec.get("is_approved"):
                        await safe_send(rec["ws"], text_payload)
                continue

            # Host actions
            if mtype == "action":
                action = message.get("action")
                target = message.get("target")
                actor = client_id
                actor_rec = rooms[room_id].get(actor)
                if not actor_rec:
                    continue

                # Privileged actions require host
                if action in ("mute", "kick", "make-host", "lock-room", "unlock-room", "accept", "reject", "set-approval"):
                    if not actor_rec.get("is_host", False):
                        await safe_send(websocket, {"type": "error", "message": "Only host may perform this action"})
                        print(f"[DENY] room={room_id} actor={actor} attempted privileged action {action}")
                        continue

                # Accept pending participant
                if action == "accept" and target:
                    if target in rooms[room_id] and not rooms[room_id][target].get("is_approved", False):
                        rooms[room_id][target]["is_approved"] = True
                        print(f"[ACCEPT] room={room_id} host={actor} accepted={target}")
                        # send welcome to the newly approved participant
                        participants = [_make_participant_info(cid, rec) for cid, rec in rooms[room_id].items() if rec.get("is_approved")]
                        host_id = next((cid for cid, rec in rooms[room_id].items() if rec.get("is_host")), None)
                        await safe_send(rooms[room_id][target]["ws"], {"type": "welcome", "client_id": target, "participants": participants, "host_id": host_id})
                        # notify other approved participants about the new participant
                        join_msg = {"type": "participant-joined", "participant": _make_participant_info(target, rooms[room_id][target])}
                        for cid, rec in list(rooms[room_id].items()):
                            if cid == target:
                                continue
                            if rec.get("is_approved"):
                                await safe_send(rec["ws"], join_msg)
                        # broadcast updated participants list to all approved participants
                        for cid, rec in list(rooms[room_id].items()):
                            if rec.get("is_approved"):
                                await safe_send(rec["ws"], {"type": "participants-update", "participants": participants, "host_id": host_id})

                        # Ask existing approved peers to create offers to the newly approved participant
                        offer_trigger = {"type": "need-offer", "target": target}
                        for cid, rec in list(rooms[room_id].items()):
                            if cid != target and rec.get("is_approved"):
                                try:
                                    await safe_send(rec["ws"], offer_trigger)
                                    print(f"[NEED-OFFER] room={room_id} ask={cid} -> create offer for {target}")
                                except Exception:
                                    pass
                    continue

                # Reject pending participant
                if action == "reject" and target:
                    if target in rooms[room_id] and not rooms[room_id][target].get("is_approved", False):
                        try:
                            await safe_send(rooms[room_id][target]["ws"], {"type": "command", "cmd": "you-are-rejected", "from": actor})
                            await rooms[room_id][target]["ws"].close()
                            print(f"[REJECT] room={room_id} host={actor} rejected={target}")
                        except Exception:
                            pass
                    continue

                # Handle mute (force-mute)
                if action == "mute" and target:
                    if target in rooms[room_id]:
                        await safe_send(rooms[room_id][target]["ws"], {"type": "command", "cmd": "force-mute", "from": actor})
                        notify = {"type": "system", "message": f"{actor_rec['displayName']} asked {rooms[room_id][target]['displayName']} to mute"}
                        for cid, rec in list(rooms[room_id].items()):
                            if rec.get("is_approved"):
                                await safe_send(rec["ws"], notify)
                        print(f"[MUTE] room={room_id} host={actor} -> mute {target}")
                    continue

                # Kick (close connection)
                if action == "kick" and target:
                    if target in rooms[room_id]:
                        try:
                            await safe_send(rooms[room_id][target]["ws"], {"type": "command", "cmd": "you-are-kicked", "from": actor})
                            await rooms[room_id][target]["ws"].close()
                            print(f"[KICK] room={room_id} host={actor} kicked={target}")
                        except Exception:
                            pass
                    continue

                # make-host
                if action == "make-host" and target:
                    if target in rooms[room_id]:
                        for cid, rec in rooms[room_id].items():
                            rec["is_host"] = False
                        rooms[room_id][target]["is_host"] = True
                        participants = [_make_participant_info(cid, rec) for cid, rec in rooms[room_id].items() if rec.get("is_approved")]
                        host_id = target
                        for cid, rec in list(rooms[room_id].items()):
                            if rec.get("is_approved"):
                                await safe_send(rec["ws"], {"type": "participants-update", "participants": participants, "host_id": host_id})
                        print(f"[MAKE-HOST] room={room_id} new_host={target}")
                    continue

                # lock/unlock room
                if action == "lock-room":
                    rooms[room_id].setdefault("_meta", {})["locked"] = True
                    for cid, rec in list(rooms[room_id].items()):
                        await safe_send(rec["ws"], {"type": "room-lock", "locked": True})
                    continue
                if action == "unlock-room":
                    rooms[room_id].setdefault("_meta", {})["locked"] = False
                    for cid, rec in list(rooms[room_id].items()):
                        await safe_send(rec["ws"], {"type": "room-lock", "locked": False})
                    continue

                # set-approval toggles require_approval meta
                if action == "set-approval":
                    val = bool(message.get("value", False))
                    rooms[room_id].setdefault("_meta", {})["require_approval"] = val
                    # notify all participants (approved ones) of new meta
                    for cid, rec in list(rooms[room_id].items()):
                        if rec.get("is_approved"):
                            await safe_send(rec["ws"], {"type": "room-meta", "meta": rooms[room_id].get("_meta", {})})
                    print(f"[META] room={room_id} require_approval set to {val} by {actor}")
                    continue

                # unknown privileged action
                await safe_send(websocket, {"type": "error", "message": f"Unknown action {action}"})
                print(f"[ERROR] room={room_id} unknown action {action} by {actor}")
                continue

            # update-name
            if mtype == "update-name":
                newname = message.get("displayName", "")
                rooms[room_id][client_id]["displayName"] = newname
                participants = [_make_participant_info(cid, rec) for cid, rec in rooms[room_id].items() if rec.get("is_approved")]
                host_id = next((cid for cid, rec in rooms[room_id].items() if rec.get("is_host")), None)
                for cid, rec in list(rooms[room_id].items()):
                    if rec.get("is_approved"):
                        await safe_send(rec["ws"], {"type": "participants-update", "participants": participants, "host_id": host_id})
                continue

            # fallback: broadcast unknown message types to approved participants
            for cid, rec in list(rooms[room_id].items()):
                if cid == client_id:
                    continue
                if rec.get("is_approved"):
                    await safe_send(rec["ws"], message)

    except WebSocketDisconnect:
        print(f"[DISCONNECT] room={room_id} id={client_id}")
        # cleanup on disconnect
        if room_id in rooms and client_id in rooms[room_id]:
            left_display = rooms[room_id][client_id].get("displayName", client_id)
            was_host = rooms[room_id][client_id].get("is_host", False)
            was_approved = rooms[room_id][client_id].get("is_approved", True)
            del rooms[room_id][client_id]

            # If host left, promote another approved participant to host
            if was_host and rooms.get(room_id):
                # pick first approved participant
                first_cid = None
                for cid, rec in rooms[room_id].items():
                    if rec.get("is_approved", False):
                        first_cid = cid
                        break
                if first_cid:
                    rooms[room_id][first_cid]["is_host"] = True
                    print(f"[PROMOTE] room={room_id} new_host={first_cid}")

            # notify remaining approved participants
            participants = [_make_participant_info(cid, rec) for cid, rec in rooms[room_id].items() if rec.get("is_approved")]
            host_id = next((cid for cid, rec in rooms[room_id].items() if rec.get("is_host")), None)
            left_msg = {"type": "participant-left", "id": client_id, "displayName": left_display}
            for cid, rec in list(rooms[room_id].items()):
                if rec.get("is_approved"):
                    await safe_send(rec["ws"], left_msg)
                    await safe_send(rec["ws"], {"type": "participants-update", "participants": participants, "host_id": host_id})

        if room_id in rooms and not rooms[room_id]:
            del rooms[room_id]


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

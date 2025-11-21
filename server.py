# server.py
import json
import uuid
from typing import Dict, Any
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI()

# Serve static files from ./static
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


# rooms: room_id -> dict(client_id -> { "ws": WebSocket, "name": str, "is_host": bool, "is_approved": bool })
rooms: Dict[str, Dict[str, Dict[str, Any]]] = {}


async def safe_send(ws: WebSocket, payload: dict):
    """Send JSON payload safely to a websocket (swallow errors)."""
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass


def _participants_list(room_id: str):
    """Return list of approved participants (id, name)."""
    return [
        {"id": cid, "name": rec.get("name", "")}
        for cid, rec in rooms.get(room_id, {}).items()
        if rec.get("is_approved", False)
    ]


def _pending_list(room_id: str):
    """Return list of pending (unapproved) participants."""
    return [
        {"id": cid, "name": rec.get("name", "")}
        for cid, rec in rooms.get(room_id, {}).items()
        if not rec.get("is_approved", True)
    ]


async def send_pending_to_hosts(room_id: str):
    """Send the current pending list to all hosts in the room."""
    if room_id not in rooms:
        return
    pending = _pending_list(room_id)
    pending_msg = {"type": "pending", "pending": pending}
    for cid, rec in list(rooms[room_id].items()):
        if rec.get("is_host"):
            try:
                await safe_send(rec["ws"], pending_msg)
            except Exception:
                pass


@app.get("/debug/room/{room_id}")
async def debug_room(room_id: str, request: Request):
    """
    Development-only debug endpoint returning room state.
    Useful to inspect server view while testing across devices.
    """
    # Simple ACL: only allow localhost requests by default (safety)
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "localhost", "::1"):
        # Still allow but mark as remote request (you can tighten this if desired)
        pass

    if room_id not in rooms:
        return JSONResponse({"room": room_id, "exists": False})
    participants = []
    pending = []
    meta = rooms[room_id].get("_meta", {})
    for cid, rec in rooms[room_id].items():
        if cid == "_meta":
            continue
        info = {"id": cid, "name": rec.get("name", ""), "is_host": bool(rec.get("is_host", False)), "is_approved": bool(rec.get("is_approved", False))}
        if rec.get("is_approved"):
            participants.append(info)
        else:
            pending.append(info)
    return JSONResponse({"room": room_id, "exists": True, "participants": participants, "pending": pending, "meta": meta})


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()

    if room_id not in rooms:
        rooms[room_id] = {}
        # default room meta
        rooms[room_id]["_meta"] = {"require_approval": False}

    client_id = uuid.uuid4().hex[:8]
    # placeholder record
    rooms[room_id][client_id] = {"ws": websocket, "name": "", "is_host": False, "is_approved": True}

    print(f"[CONNECT] room={room_id} assigned_id={client_id}")

    try:
        # Expect initial 'join' msg
        raw = await websocket.receive_text()
        try:
            msg = json.loads(raw)
        except Exception:
            msg = {}

        if msg.get("type") != "join":
            await safe_send(
                websocket,
                {"type": "error", "message": "First message must be type:'join' with a name"},
            )
            await websocket.close()
            del rooms[room_id][client_id]
            return

        display_name = str(msg.get("name", f"User-{client_id}"))[:64]
        rooms[room_id][client_id]["name"] = display_name

        # decide host & approval behavior
        meta = rooms[room_id].get("_meta", {})
        require_approval = bool(meta.get("require_approval", False))
        # if no host present, make this client host
        existing_hosts = [cid for cid, rec in rooms[room_id].items() if rec.get("is_host")]
        if not existing_hosts:
            rooms[room_id][client_id]["is_host"] = True
            rooms[room_id][client_id]["is_approved"] = True
            print(f"[HOST] room={room_id} id={client_id} became_host")
        else:
            # if room requires approval, mark pending
            if require_approval:
                rooms[room_id][client_id]["is_approved"] = False
                print(f"[PENDING] room={room_id} id={client_id} waiting_for_host")
            else:
                rooms[room_id][client_id]["is_approved"] = True
                print(f"[APPROVED] room={room_id} id={client_id} auto_approved")

        # If the new participant is pending: tell them and inform hosts (and send full pending list to hosts)
        if not rooms[room_id][client_id]["is_approved"]:
            await safe_send(websocket, {"type": "waiting", "message": "Waiting for host approval"})
            # notify all hosts (usually one) about the join request
            for cid, rec in list(rooms[room_id].items()):
                if rec.get("is_host"):
                    try:
                        await safe_send(
                            rec["ws"],
                            {
                                "type": "join-request",
                                "participant": {"id": client_id, "name": display_name},
                            },
                        )
                    except Exception:
                        pass
            # send the current pending list to hosts so UI is consistent
            await send_pending_to_hosts(room_id)
        else:
            # Approved: send welcome and participants
            welcome_payload = {
                "type": "welcome",
                "id": client_id,
                "participants": _participants_list(room_id),
                "host_id": next((cid for cid, rec in rooms[room_id].items() if rec.get("is_host")), None),
                "room_meta": rooms[room_id].get("_meta", {}),
            }
            # if the recipient is a host include pending list immediately
            if rooms[room_id][client_id].get("is_host"):
                welcome_payload["pending"] = _pending_list(room_id)
            await safe_send(websocket, welcome_payload)
            print(f"[WELCOME] room={room_id} to={client_id} participants_count={len(welcome_payload['participants'])}")

            # notify other approved participants about join
            join_notice = {"type": "participant-joined", "id": client_id, "name": display_name}
            for cid, rec in list(rooms[room_id].items()):
                if cid != client_id and rec.get("is_approved"):
                    await safe_send(rec["ws"], join_notice)

            # broadcast participants update to approved participants
            participants_update = {"type": "participants", "participants": _participants_list(room_id)}
            for cid, rec in list(rooms[room_id].items()):
                if rec.get("is_approved"):
                    await safe_send(rec["ws"], participants_update)

            # make sure hosts have the latest pending list too
            await send_pending_to_hosts(room_id)

        # main loop
        while True:
            text = await websocket.receive_text()
            try:
                message = json.loads(text)
            except Exception:
                continue

            message.setdefault("from", client_id)
            mtype = message.get("type")
            # LOG minimal
            # print(f"[WS RX] room={room_id} from={client_id} type={mtype} to={message.get('to')}")

            # Signaling passthrough (offer/answer/ice) - forward if target exists
            if mtype in ("offer", "answer", "ice-candidate"):
                target = message.get("to")
                if target and target in rooms[room_id]:
                    # only forward if target is approved (we keep this policy)
                    if rooms[room_id][target].get("is_approved", False):
                        await safe_send(rooms[room_id][target]["ws"], message)
                    else:
                        await safe_send(websocket, {"type": "error", "message": f"target {target} not approved yet"})
                else:
                    await safe_send(websocket, {"type": "error", "message": f"target {target} not in room"})
                continue

            # Chat: only approved participants receive chats
            if mtype == "chat":
                text_msg = str(message.get("text", ""))[:2000]
                payload = {"type": "chat", "from": client_id, "name": display_name, "text": text_msg}
                for cid, rec in list(rooms[room_id].items()):
                    if rec.get("is_approved"):
                        await safe_send(rec["ws"], payload)
                continue

            # Host actions: accept/reject/set-approval/refresh-pending
            if mtype == "action":
                action = message.get("action")
                target = message.get("target")
                actor = client_id
                actor_rec = rooms[room_id].get(actor)
                if action in ("accept", "reject", "set-approval", "refresh-pending"):
                    # only host can do these
                    if not actor_rec or not actor_rec.get("is_host", False):
                        await safe_send(websocket, {"type": "error", "message": "Only host may perform this action"})
                        continue

                # refresh pending (host requests current pending list resend)
                if action == "refresh-pending":
                    await send_pending_to_hosts(room_id)
                    continue

                # accept pending participant
                if action == "accept" and target:
                    if target in rooms[room_id] and not rooms[room_id][target].get("is_approved", False):
                        rooms[room_id][target]["is_approved"] = True
                        print(f"[ACCEPT] room={room_id} host={actor} accepted={target}")
                        # send welcome to the newly approved participant
                        await safe_send(
                            rooms[room_id][target]["ws"],
                            {
                                "type": "welcome",
                                "id": target,
                                "participants": _participants_list(room_id),
                                "host_id": next((cid for cid, rec in rooms[room_id].items() if rec.get("is_host")), None),
                                "room_meta": rooms[room_id].get("_meta", {}),
                            },
                        )
                        # notify approved participants about the new participant
                        join_msg = {"type": "participant-joined", "id": target, "name": rooms[room_id][target]["name"]}
                        for cid, rec in list(rooms[room_id].items()):
                            if cid != target and rec.get("is_approved"):
                                await safe_send(rec["ws"], join_msg)
                        # broadcast participants update
                        participants_update = {"type": "participants", "participants": _participants_list(room_id)}
                        for cid, rec in list(rooms[room_id].items()):
                            if rec.get("is_approved"):
                                await safe_send(rec["ws"], participants_update)
                        # after accept, update hosts with new pending list
                        await send_pending_to_hosts(room_id)
                    continue

                # reject pending participant
                if action == "reject" and target:
                    if target in rooms[room_id] and not rooms[room_id][target].get("is_approved", False):
                        try:
                            await safe_send(rooms[room_id][target]["ws"], {"type": "command", "cmd": "you-are-rejected"})
                            await rooms[room_id][target]["ws"].close()
                        except Exception:
                            pass
                        # after reject, update hosts with new pending list
                        await send_pending_to_hosts(room_id)
                    continue

                # set approval requirement (toggle)
                if action == "set-approval":
                    val = bool(message.get("value", False))
                    rooms[room_id].setdefault("_meta", {})["require_approval"] = val
                    # send room-meta update to all connected (approved ones)
                    meta_msg = {"type": "room-meta", "meta": rooms[room_id].get("_meta", {})}
                    for cid, rec in list(rooms[room_id].items()):
                        try:
                            await safe_send(rec["ws"], meta_msg)
                        except Exception:
                            pass
                    # send pending list to hosts (host UI may need to update)
                    await send_pending_to_hosts(room_id)
                    print(f"[META] room={room_id} require_approval set to {val} by {actor}")
                    continue

            # rename (allowed for pending/approved)
            if mtype == "rename":
                new_name = str(message.get("name", ""))[:64]
                rooms[room_id][client_id]["name"] = new_name
                # notify approved participants
                update_msg = {"type": "participants", "participants": _participants_list(room_id)}
                for cid, rec in list(rooms[room_id].items()):
                    if rec.get("is_approved"):
                        await safe_send(rec["ws"], update_msg)
                # update pending list for hosts
                await send_pending_to_hosts(room_id)
                continue

            # fallback: ignore / broadcast only permitted messages
            continue

    except WebSocketDisconnect:
        print(f"[DISCONNECT] room={room_id} id={client_id}")
        # cleanup
        if room_id in rooms and client_id in rooms[room_id]:
            left_name = rooms[room_id][client_id].get("name", client_id)
            was_host = rooms[room_id][client_id].get("is_host", False)
            was_approved = rooms[room_id][client_id].get("is_approved", True)
            del rooms[room_id][client_id]

            # if host left: promote first approved participant to host
            if was_host and rooms.get(room_id):
                new_host = None
                for cid, rec in rooms[room_id].items():
                    if rec.get("is_approved"):
                        new_host = cid
                        break
                if new_host:
                    rooms[room_id][new_host]["is_host"] = True
                    print(f"[PROMOTE] room={room_id} new_host={new_host}")
                    # notify all approved participants about host change via participants update
                    participants_update = {"type": "participants", "participants": _participants_list(room_id)}
                    for cid, rec in list(rooms[room_id].items()):
                        if rec.get("is_approved"):
                            await safe_send(rec["ws"], participants_update)

            # notify remaining approved participants about left
            left_notice = {"type": "participant-left", "id": client_id, "name": left_name}
            for cid, rec in list(rooms.get(room_id, {}).items()):
                if rec.get("is_approved"):
                    await safe_send(rec["ws"], left_notice)

            # also notify hosts about updated pending list
            await send_pending_to_hosts(room_id)

        # cleanup empty room
        if room_id in rooms and len([k for k in rooms[room_id].keys() if k != "_meta"]) == 0:
            del rooms[room_id]


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

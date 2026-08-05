from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_jwt_extended import decode_token
from flask import request
from bson import ObjectId
from datetime import datetime
from utils.time_utils import to_utc_iso
from models.chat_model import censor, is_muted, is_chat_banned, serialize_message

# sid -> {"user_id": str, "name": str, "role": str, "channel": str|None}
online_users = {}


def register_chat_events(socketio: SocketIO, mongo):

    def _broadcast_presence():
        socketio.emit("presence_update", {"count": len(online_users)})

    @socketio.on("connect")
    def handle_connect(auth):
        token = (auth or {}).get("token")
        if not token:
            return False  # reject connection

        try:
            decoded = decode_token(token)
        except Exception:
            return False  # invalid/expired token, reject connection

        user_id = decoded.get("sub")
        role = decoded.get("role", "user")

        user = mongo.db.users.find_one({"_id": ObjectId(user_id)})
        name = user.get("name") if user else "Unknown"

        online_users[request.sid] = {
            "user_id": user_id, "name": name, "role": role, "channel": None
        }

        emit("presence_update", {"count": len(online_users)}, broadcast=True)

    @socketio.on("disconnect")
    def handle_disconnect():
        online_users.pop(request.sid, None)
        emit("presence_update", {"count": len(online_users)}, broadcast=True)

    @socketio.on("join_channel")
    def handle_join_channel(data):
        sender = online_users.get(request.sid)
        if not sender:
            return

        channel = (data or {}).get("channel")
        if not channel:
            return

        # leave previous channel room if switching
        prev = sender.get("channel")
        if prev and prev != channel:
            leave_room(prev)

        join_room(channel)
        sender["channel"] = channel

    @socketio.on("leave_channel")
    def handle_leave_channel(data):
        sender = online_users.get(request.sid)
        channel = (data or {}).get("channel")
        if channel:
            leave_room(channel)
        if sender and sender.get("channel") == channel:
            sender["channel"] = None

    @socketio.on("send_message")
    def handle_send_message(data):
        sender = online_users.get(request.sid)
        if not sender:
            return  # not authenticated, ignore silently

        channel = (data or {}).get("channel")
        if not channel:
            return

        text = (data or {}).get("message", "")
        text = text.strip()
        if not text or len(text) > 500:
            return

        user = mongo.db.users.find_one({"_id": ObjectId(sender["user_id"])})
        if is_chat_banned(user) or is_muted(user):
            emit("chat_error", {"error": "You are not permitted to send messages right now."})
            return

        text = censor(text)

        doc = {
            "channel": channel,
            "user_id": sender["user_id"],
            "name": sender["name"],
            "role": sender["role"],
            "is_champion": bool(user.get("is_champion")) if user else False,
            "message": text,
            "image_url": (data or {}).get("image_url"),
            "reply_to": (data or {}).get("reply_to"),
            "mentions": (data or {}).get("mentions", []),
            "reactions": {},
            "lfg": (data or {}).get("lfg"),
            "pinned": False,
            "deleted": False,
            "edited_at": None,
            "created_at": datetime.utcnow()
        }
        result = mongo.db.chat_messages.insert_one(doc)
        doc["_id"] = result.inserted_id

        payload = serialize_message(doc)
        emit("new_message", payload, room=channel, broadcast=True)

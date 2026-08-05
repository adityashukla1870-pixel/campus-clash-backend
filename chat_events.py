from flask_socketio import SocketIO, emit
from flask_jwt_extended import decode_token
from flask import request
from bson import ObjectId
from datetime import datetime
from utils.time_utils import to_utc_iso

# sid -> {"user_id": str, "name": str, "role": str}
online_users = {}


def register_chat_events(socketio: SocketIO, mongo):

    def _broadcast_presence():
        socketio.emit("presence_update", {"count": len(online_users)})

    @socketio.on("connect")
    def handle_connect(auth):
        token = (auth or {}).get("token")
        if not token:
            return False  # reject connectioon

        try:
            decoded = decode_token(token)
        except Exception:
            return False  # invalid/expired token, reject connection

        user_id = decoded.get("sub")
        role = decoded.get("role", "user")

        user = mongo.db.users.find_one({"_id": ObjectId(user_id)})
        name = user.get("name") if user else "Unknown"

        online_users[request.sid] = {"user_id": user_id, "name": name, "role": role}

        emit("presence_update", {"count": len(online_users)}, broadcast=True)

    @socketio.on("disconnect")
    def handle_disconnect():
        online_users.pop(request.sid, None)
        emit("presence_update", {"count": len(online_users)}, broadcast=True)

    @socketio.on("send_message")
    def handle_send_message(data):
        sender = online_users.get(request.sid)
        if not sender:
            return  # not authenticated, ignore silently

        text = (data or {}).get("message", "")
        text = text.strip()
        if not text or len(text) > 500:
            return

        doc = {
            "user_id": sender["user_id"],
            "name": sender["name"],
            "role": sender["role"],
            "message": text,
            "created_at": datetime.utcnow()
        }
        result = mongo.db.chat_messages.insert_one(doc)

        payload = {
            "id": str(result.inserted_id),
            "user_id": sender["user_id"],
            "name": sender["name"],
            "role": sender["role"],
            "message": text,
            "created_at": to_utc_iso(doc["created_at"])
        }
        emit("new_message", payload, broadcast=True)

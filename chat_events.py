from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_jwt_extended import decode_token
from flask import request
from bson import ObjectId
from datetime import datetime, timedelta
from utils.time_utils import to_utc_iso
from models.chat_model import censor, is_muted, is_chat_banned, serialize_message, can_delete_message

# sid -> {"user_id": str, "name": str, "role": str, "channel": str|None}
online_users = {}


def register_chat_events(socketio: SocketIO, mongo):

    def _presence_payload():
        # A person can have several open tabs. The presence rail should show
        # people, not Socket.IO connections, so collapse entries by user ID.
        members = {}
        for user in online_users.values():
            members[user["user_id"]] = {
                "user_id": user["user_id"],
                "name": user["name"],
                "role": user["role"]
            }
        return {"count": len(members), "members": list(members.values())}

    def _broadcast_presence():
        socketio.emit("presence_update", _presence_payload())

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

        _broadcast_presence()

    @socketio.on("disconnect")
    def handle_disconnect():
        online_users.pop(request.sid, None)
        _broadcast_presence()

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

        text = (data or {}).get("message", "").strip()
        image_url = (data or {}).get("image_url")
        lfg = (data or {}).get("lfg")
        if (not text and not image_url and not lfg) or len(text) > 500:
            return

        channel_meta = mongo.db.chat_channels.find_one({"key": channel})
        if channel_meta and channel_meta.get("admin_only_post") and sender["role"] != "admin":
            emit("chat_error", {"error": "Only Campus Clash staff can post in this channel."})
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
            "image_url": image_url,
            "reply_to": (data or {}).get("reply_to"),
            "mentions": (data or {}).get("mentions", []),
            "reactions": {},
            "lfg": lfg,
            "pinned": False,
            "deleted": False,
            "edited_at": None,
            "created_at": datetime.utcnow()
        }
        result = mongo.db.chat_messages.insert_one(doc)
        doc["_id"] = result.inserted_id

        payload = serialize_message(doc)
        emit("new_message", payload, room=channel, broadcast=True)

    @socketio.on("edit_message")
    def handle_edit_message(data):
        sender = online_users.get(request.sid)
        if not sender:
            return

        message_id = (data or {}).get("message_id")
        updated = ((data or {}).get("message") or "").strip()
        if not message_id or not updated:
            return

        message = mongo.db.chat_messages.find_one({"_id": ObjectId(message_id)})
        if not message:
            return

        actor = mongo.db.users.find_one({"_id": ObjectId(sender["user_id"])})
        if not actor or not (sender["role"] == "admin" or str(actor.get("_id")) == str(message.get("user_id"))):
            return

        mongo.db.chat_messages.update_one(
            {"_id": ObjectId(message_id)},
            {"$set": {"message": censor(updated), "edited_at": datetime.utcnow()}}
        )

        updated_doc = mongo.db.chat_messages.find_one({"_id": ObjectId(message_id)})
        socketio.emit("message_edited", {
            "id": message_id,
            "message": updated_doc.get("message"),
            "edited_at": updated_doc.get("edited_at").isoformat() if updated_doc.get("edited_at") else None,
        }, room=updated_doc.get("channel"), broadcast=True)

    @socketio.on("delete_message")
    def handle_delete_message(data):
        sender = online_users.get(request.sid)
        if not sender:
            return

        message_id = (data or {}).get("message_id")
        if not message_id:
            return

        message = mongo.db.chat_messages.find_one({"_id": ObjectId(message_id)})
        if not message:
            return

        actor = mongo.db.users.find_one({"_id": ObjectId(sender["user_id"])})
        if not actor or not can_delete_message(actor, message):
            return

        mongo.db.chat_messages.update_one(
            {"_id": ObjectId(message_id)},
            {"$set": {"deleted": True, "message": "", "image_url": None, "edited_at": datetime.utcnow()}}
        )
        socketio.emit("message_deleted", {"id": message_id}, room=message.get("channel"), broadcast=True)

    @socketio.on("toggle_pin")
    def handle_toggle_pin(data):
        sender = online_users.get(request.sid)
        if not sender or sender.get("role") != "admin":
            return

        message_id = (data or {}).get("message_id")
        if not message_id:
            return

        message = mongo.db.chat_messages.find_one({"_id": ObjectId(message_id)})
        if not message:
            return

        next_pinned = not bool(message.get("pinned"))
        mongo.db.chat_messages.update_one({"_id": ObjectId(message_id)}, {"$set": {"pinned": next_pinned}})
        socketio.emit("message_pinned", {"id": message_id, "pinned": next_pinned}, room=message.get("channel"), broadcast=True)

    @socketio.on("react_message")
    def handle_react_message(data):
        sender = online_users.get(request.sid)
        if not sender:
            return

        message_id = (data or {}).get("message_id")
        emoji = (data or {}).get("emoji")
        if not message_id or not emoji:
            return

        message = mongo.db.chat_messages.find_one({"_id": ObjectId(message_id)})
        if not message:
            return

        reactions = message.get("reactions") or {}
        users = list(reactions.get(emoji, []))
        if sender["user_id"] in users:
            users.remove(sender["user_id"])
        else:
            users.append(sender["user_id"])
        reactions[emoji] = users
        mongo.db.chat_messages.update_one({"_id": ObjectId(message_id)}, {"$set": {"reactions": reactions}})
        socketio.emit("reaction_update", {"id": message_id, "reactions": reactions}, room=message.get("channel"), broadcast=True)

    @socketio.on("admin_mute_user")
    def handle_admin_mute_user(data):
        sender = online_users.get(request.sid)
        if not sender or sender.get("role") != "admin":
            return

        user_id = (data or {}).get("user_id")
        minutes = int((data or {}).get("minutes") or 10)
        if not user_id:
            return

        mongo.db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"muted_until": (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()}}
        )

    @socketio.on("admin_ban_user")
    def handle_admin_ban_user(data):
        sender = online_users.get(request.sid)
        if not sender or sender.get("role") != "admin":
            return

        user_id = (data or {}).get("user_id")
        if not user_id:
            return

        mongo.db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"chat_banned": True}})

from flask import request
from flask_socketio import join_room, leave_room
from flask_jwt_extended import decode_token
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime, timedelta

from models.chat_model import censor, serialize_message
from routes.notification_routes import create_notification

# sid -> {"user_id", "name", "role", "channel", "is_champion"}
online_users = {}
# user_id -> datetime of last sent message, used for slow-mode
_last_sent = {}

SLOW_MODE_SECONDS = 2
EDIT_WINDOW_MINUTES = 15
MAX_MESSAGE_LENGTH = 500


def register_chat_events(socketio, mongo):

    def _get_user_doc(user_id):
        try:
            return mongo.db.users.find_one({"_id": ObjectId(user_id)})
        except (InvalidId, TypeError):
            return None

    def _is_muted(user):
        until = user.get("muted_until") if user else None
        return bool(until and datetime.utcnow() < until)

    def _is_banned(user):
        return bool(user and user.get("chat_banned"))

    def _online_members():
        seen = {}
        for info in online_users.values():
            seen[info["user_id"]] = info["name"]
        return [{"user_id": uid, "name": name} for uid, name in seen.items()]

    def _broadcast_presence():
        socketio.emit("presence_update", {"count": len(online_users), "members": _online_members()})

    def _to_object_id(value):
        try:
            return ObjectId(value)
        except (InvalidId, TypeError):
            return None

    # ---------------- CONNECTION ----------------

    @socketio.on("connect")
    def handle_connect(auth):
        token = (auth or {}).get("token")
        if not token:
            return False

        try:
            decoded = decode_token(token)
        except Exception:
            return False

        user_id = decoded.get("sub")
        role = decoded.get("role", "user")
        user = _get_user_doc(user_id)
        if not user:
            return False

        is_champion = mongo.db.tournaments.count_documents({"winner_id": user_id}) > 0

        online_users[request.sid] = {
            "user_id": user_id,
            "name": user.get("name", "Unknown"),
            "role": role,
            "channel": None,
            "is_champion": is_champion
        }

        _broadcast_presence()

    @socketio.on("disconnect")
    def handle_disconnect():
        info = online_users.pop(request.sid, None)
        if info and info.get("channel"):
            leave_room(info["channel"])
        _broadcast_presence()

    # ---------------- CHANNELS ----------------

    @socketio.on("join_channel")
    def handle_join_channel(data):
        info = online_users.get(request.sid)
        if not info:
            return
        channel = (data or {}).get("channel")
        if not channel:
            return
        if info.get("channel"):
            leave_room(info["channel"])
        join_room(channel)
        info["channel"] = channel

    @socketio.on("typing")
    def handle_typing(data):
        info = online_users.get(request.sid)
        if not info:
            return
        channel = (data or {}).get("channel") or info.get("channel")
        if not channel:
            return
        socketio.emit(
            "user_typing",
            {"user_id": info["user_id"], "name": info["name"]},
            room=channel, include_self=False
        )

    # ---------------- MESSAGES ----------------

    @socketio.on("send_message")
    def handle_send_message(data):
        sender = online_users.get(request.sid)
        if not sender:
            return

        user = _get_user_doc(sender["user_id"])
        if _is_banned(user):
            socketio.emit("chat_error", {"error": "You are banned from chat"}, room=request.sid)
            return
        if _is_muted(user):
            socketio.emit("chat_error", {"error": "You are muted. Try again later."}, room=request.sid)
            return

        now = datetime.utcnow()
        last = _last_sent.get(sender["user_id"])
        if sender["role"] != "admin" and last and (now - last).total_seconds() < SLOW_MODE_SECONDS:
            socketio.emit("chat_error", {"error": "You're sending messages too fast — slow down."}, room=request.sid)
            return

        data = data or {}
        channel = data.get("channel", "general")
        text = (data.get("message") or "").strip()
        image_url = data.get("image_url")

        if not text and not image_url:
            return
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[:MAX_MESSAGE_LENGTH]

        channel_doc = mongo.db.chat_channels.find_one({"key": channel})
        if channel_doc and channel_doc.get("admin_only_post") and sender["role"] != "admin":
            socketio.emit("chat_error", {"error": "Only admins can post in this channel"}, room=request.sid)
            return

        text = censor(text)

        reply_to = data.get("reply_to")
        reply_to_clean = None
        if reply_to and reply_to.get("id"):
            reply_to_clean = {
                "id": reply_to.get("id"),
                "name": reply_to.get("name", ""),
                "message": (reply_to.get("message") or "")[:120]
            }

        mentions = [m for m in (data.get("mentions") or []) if isinstance(m, str)][:10]
        lfg = data.get("lfg") if channel == "lfg" and isinstance(data.get("lfg"), dict) else None

        doc = {
            "channel": channel,
            "user_id": sender["user_id"],
            "name": sender["name"],
            "role": sender["role"],
            "is_champion": sender.get("is_champion", False),
            "message": text,
            "image_url": image_url,
            "reply_to": reply_to_clean,
            "mentions": mentions,
            "reactions": {},
            "lfg": lfg,
            "pinned": False,
            "edited_at": None,
            "deleted": False,
            "created_at": now
        }
        result = mongo.db.chat_messages.insert_one(doc)
        doc["_id"] = result.inserted_id
        _last_sent[sender["user_id"]] = now

        socketio.emit("new_message", serialize_message(doc), room=channel)

        for uid in mentions:
            if uid and uid != sender["user_id"]:
                create_notification(mongo, uid, f"{sender['name']} mentioned you in #{channel}", "mention")

    @socketio.on("edit_message")
    def handle_edit_message(data):
        sender = online_users.get(request.sid)
        if not sender:
            return
        data = data or {}
        oid = _to_object_id(data.get("message_id"))
        new_text = (data.get("message") or "").strip()
        if not oid or not new_text:
            return
        new_text = new_text[:MAX_MESSAGE_LENGTH]

        msg = mongo.db.chat_messages.find_one({"_id": oid})
        if not msg or msg.get("user_id") != sender["user_id"] or msg.get("deleted"):
            return
        if (datetime.utcnow() - msg["created_at"]).total_seconds() > EDIT_WINDOW_MINUTES * 60:
            socketio.emit("chat_error", {"error": "Edit window has expired"}, room=request.sid)
            return

        new_text = censor(new_text)
        edited_at = datetime.utcnow()
        mongo.db.chat_messages.update_one({"_id": oid}, {"$set": {"message": new_text, "edited_at": edited_at}})

        socketio.emit("message_edited", {
            "id": str(oid), "channel": msg["channel"],
            "message": new_text, "edited_at": edited_at.isoformat()
        }, room=msg["channel"])

    @socketio.on("delete_message")
    def handle_delete_message(data):
        sender = online_users.get(request.sid)
        if not sender:
            return
        oid = _to_object_id((data or {}).get("message_id"))
        if not oid:
            return

        msg = mongo.db.chat_messages.find_one({"_id": oid})
        if not msg:
            return

        is_owner = msg.get("user_id") == sender["user_id"]
        is_admin = sender["role"] == "admin"
        if not (is_owner or is_admin):
            return

        mongo.db.chat_messages.update_one({"_id": oid}, {"$set": {
            "message": "", "deleted": True, "image_url": None, "pinned": False
        }})

        socketio.emit("message_deleted", {"id": str(oid), "channel": msg["channel"]}, room=msg["channel"])

    @socketio.on("react_message")
    def handle_react_message(data):
        sender = online_users.get(request.sid)
        if not sender:
            return
        data = data or {}
        oid = _to_object_id(data.get("message_id"))
        emoji = data.get("emoji")
        if not oid or not emoji or len(emoji) > 8:
            return

        msg = mongo.db.chat_messages.find_one({"_id": oid})
        if not msg or msg.get("deleted"):
            return

        already_reacted = sender["user_id"] in msg.get("reactions", {}).get(emoji, [])
        if already_reacted:
            mongo.db.chat_messages.update_one({"_id": oid}, {"$pull": {f"reactions.{emoji}": sender["user_id"]}})
        else:
            mongo.db.chat_messages.update_one({"_id": oid}, {"$addToSet": {f"reactions.{emoji}": sender["user_id"]}})

        updated = mongo.db.chat_messages.find_one({"_id": oid})
        socketio.emit("reaction_update", {
            "id": str(oid), "channel": msg["channel"], "reactions": updated.get("reactions", {})
        }, room=msg["channel"])

    # ---------------- MODERATION ----------------

    @socketio.on("toggle_pin")
    def handle_toggle_pin(data):
        sender = online_users.get(request.sid)
        if not sender or sender["role"] != "admin":
            return
        oid = _to_object_id((data or {}).get("message_id"))
        if not oid:
            return
        msg = mongo.db.chat_messages.find_one({"_id": oid})
        if not msg:
            return
        new_pinned = not msg.get("pinned", False)
        mongo.db.chat_messages.update_one({"_id": oid}, {"$set": {"pinned": new_pinned}})
        socketio.emit("message_pinned", {
            "id": str(oid), "channel": msg["channel"], "pinned": new_pinned
        }, room=msg["channel"])

    @socketio.on("admin_mute_user")
    def handle_admin_mute(data):
        sender = online_users.get(request.sid)
        if not sender or sender["role"] != "admin":
            return
        data = data or {}
        oid = _to_object_id(data.get("user_id"))
        if not oid:
            return
        minutes = int(data.get("minutes", 10))
        until = datetime.utcnow() + timedelta(minutes=minutes)
        mongo.db.users.update_one({"_id": oid}, {"$set": {"muted_until": until}})
        create_notification(mongo, str(oid), f"You've been muted in chat for {minutes} minutes", "moderation")
        socketio.emit("moderation_action", {"type": "mute", "user_id": str(oid), "minutes": minutes}, room=request.sid)

    @socketio.on("admin_unmute_user")
    def handle_admin_unmute(data):
        sender = online_users.get(request.sid)
        if not sender or sender["role"] != "admin":
            return
        oid = _to_object_id((data or {}).get("user_id"))
        if not oid:
            return
        mongo.db.users.update_one({"_id": oid}, {"$set": {"muted_until": None}})
        socketio.emit("moderation_action", {"type": "unmute", "user_id": str(oid)}, room=request.sid)

    @socketio.on("admin_ban_user")
    def handle_admin_ban(data):
        sender = online_users.get(request.sid)
        if not sender or sender["role"] != "admin":
            return
        oid = _to_object_id((data or {}).get("user_id"))
        if not oid:
            return
        mongo.db.users.update_one({"_id": oid}, {"$set": {"chat_banned": True}})
        create_notification(mongo, str(oid), "You've been banned from chat by an admin", "moderation")
        socketio.emit("moderation_action", {"type": "ban", "user_id": str(oid)}, room=request.sid)

    @socketio.on("admin_unban_user")
    def handle_admin_unban(data):
        sender = online_users.get(request.sid)
        if not sender or sender["role"] != "admin":
            return
        oid = _to_object_id((data or {}).get("user_id"))
        if not oid:
            return
        mongo.db.users.update_one({"_id": oid}, {"$set": {"chat_banned": False}})
        socketio.emit("moderation_action", {"type": "unban", "user_id": str(oid)}, room=request.sid)

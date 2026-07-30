import os
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from bson.errors import InvalidId

from models.chat_model import ensure_default_channels, serialize_channel, serialize_message
from routes.tournament_routes import admin_required, get_current_user, safe_object_id

chat = Blueprint("chat", __name__)
mongo = None

CHAT_UPLOAD_DIR = os.path.join("uploads", "chat")
ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB


def init_chat_routes(mongo_instance):
    global mongo
    mongo = mongo_instance
    ensure_default_channels(mongo)


# ---------------- CHANNELS ----------------

@chat.route("/channels", methods=["GET"])
@jwt_required()
def list_channels():
    channels = list(mongo.db.chat_channels.find())
    return jsonify([serialize_channel(c) for c in channels])


# ---------------- HISTORY (paginated, newest batch first, oldest-first within batch) ----------------

@chat.route("/history/<channel_key>", methods=["GET"])
@jwt_required()
def history(channel_key):
    try:
        limit = min(int(request.args.get("limit", 30)), 100)
    except (TypeError, ValueError):
        limit = 30

    query = {"channel": channel_key}
    before = request.args.get("before")
    if before:
        before_oid = safe_object_id(before)
        if before_oid:
            query["_id"] = {"$lt": before_oid}

    messages = list(mongo.db.chat_messages.find(query).sort("_id", -1).limit(limit))
    has_more = len(messages) == limit
    messages.reverse()

    return jsonify({
        "messages": [serialize_message(m) for m in messages],
        "has_more": has_more
    })


@chat.route("/pinned/<channel_key>", methods=["GET"])
@jwt_required()
def pinned_messages(channel_key):
    messages = list(
        mongo.db.chat_messages.find({"channel": channel_key, "pinned": True}).sort("created_at", -1).limit(20)
    )
    return jsonify([serialize_message(m) for m in messages])


# ---------------- IMAGE UPLOAD ----------------

@chat.route("/upload-image", methods=["POST"])
@jwt_required()
def upload_chat_image():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No image provided"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_IMAGE_EXT:
        return jsonify({"error": "Unsupported image type. Use png, jpg, gif or webp."}), 400

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_IMAGE_SIZE:
        return jsonify({"error": "Image too large (max 5MB)"}), 400

    if not os.path.exists(CHAT_UPLOAD_DIR):
        os.makedirs(CHAT_UPLOAD_DIR)

    filename = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(CHAT_UPLOAD_DIR, filename)
    file.save(path)

    return jsonify({"url": f"/uploads/chat/{filename}"})


# ---------------- REPORTS ----------------

@chat.route("/report", methods=["POST"])
@jwt_required()
def report_message():
    user_id = get_jwt_identity()
    data = request.json or {}
    message_id = data.get("message_id")
    reason = (data.get("reason") or "").strip()[:300]

    if not message_id:
        return jsonify({"error": "message_id is required"}), 400

    msg_oid = safe_object_id(message_id)
    msg = mongo.db.chat_messages.find_one({"_id": msg_oid}) if msg_oid else None
    if not msg:
        return jsonify({"error": "Message not found"}), 404

    reporter = get_current_user()

    mongo.db.chat_reports.insert_one({
        "message_id": str(msg["_id"]),
        "channel": msg.get("channel"),
        "message_snapshot": msg.get("message", ""),
        "message_author_id": msg.get("user_id"),
        "message_author_name": msg.get("name"),
        "reporter_id": user_id,
        "reporter_name": reporter.get("name") if reporter else "Unknown",
        "reason": reason or "No reason given",
        "status": "open",
        "created_at": datetime.utcnow()
    })

    return jsonify({"message": "Report submitted. Our admins will review it."})


# ---------------- ADMIN MODERATION ----------------

@chat.route("/admin/reports", methods=["GET"])
@admin_required
def list_reports():
    status = request.args.get("status", "open")
    query = {} if status == "all" else {"status": status}
    reports = list(mongo.db.chat_reports.find(query).sort("created_at", -1).limit(100))

    data = [{
        "id": str(r["_id"]),
        "message_id": r.get("message_id"),
        "channel": r.get("channel"),
        "message_snapshot": r.get("message_snapshot"),
        "message_author_name": r.get("message_author_name"),
        "message_author_id": r.get("message_author_id"),
        "reporter_name": r.get("reporter_name"),
        "reason": r.get("reason"),
        "status": r.get("status"),
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None
    } for r in reports]

    return jsonify(data)


@chat.route("/admin/reports/<report_id>/resolve", methods=["POST"])
@admin_required
def resolve_report(report_id):
    oid = safe_object_id(report_id)
    if not oid:
        return jsonify({"error": "Invalid report id"}), 400
    mongo.db.chat_reports.update_one({"_id": oid}, {"$set": {"status": "resolved"}})
    return jsonify({"message": "Report resolved"})


@chat.route("/admin/moderation-list", methods=["GET"])
@admin_required
def moderation_list():
    now = datetime.utcnow()
    muted = list(mongo.db.users.find({"muted_until": {"$gt": now}}))
    banned = list(mongo.db.users.find({"chat_banned": True}))

    def brief(u):
        return {"id": str(u["_id"]), "name": u.get("name"), "email": u.get("email")}

    return jsonify({
        "muted": [{**brief(u), "muted_until": u["muted_until"].isoformat()} for u in muted],
        "banned": [brief(u) for u in banned]
    })

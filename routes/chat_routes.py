from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt, get_jwt_identity
from bson import ObjectId
from models.chat_model import ensure_default_channels, serialize_channel, serialize_message

chat = Blueprint("chat", __name__)
mongo = None


def init_chat_routes(mongo_instance):
    global mongo
    mongo = mongo_instance
    # Seed the default channel list on startup so /channels never returns empty.
    ensure_default_channels(mongo)


@chat.route("/channels", methods=["GET"])
@jwt_required()
def get_channels():
    channels = list(mongo.db.chat_channels.find())
    if not channels:
        # safety net in case the DB was wiped after startup seeding
        ensure_default_channels(mongo)
        channels = list(mongo.db.chat_channels.find())
    return jsonify([serialize_channel(c) for c in channels])


@chat.route("/history/<channel>", methods=["GET"])
@jwt_required()
def history(channel):
    query = {"channel": channel, "deleted": {"$ne": True}}
    before = request.args.get("before")
    if before:
        try:
            query["_id"] = {"$lt": ObjectId(before)}
        except Exception:
            return jsonify({"error": "Invalid message cursor"}), 400

    messages = list(
        mongo.db.chat_messages.find(query)
        .sort("created_at", -1)
        .limit(51)
    )
    has_more = len(messages) > 50
    messages = messages[:50]
    messages.reverse()  # oldest first for rendering top-to-bottom

    return jsonify({"messages": [serialize_message(m) for m in messages], "has_more": has_more})


@chat.route("/report", methods=["POST"])
@jwt_required()
def submit_report():
    data = request.get_json(silent=True) or {}
    message_id = data.get("message_id")
    reason = (data.get("reason") or "").strip()[:300]
    if not message_id:
        return jsonify({"error": "Message id is required"}), 400

    try:
        message = mongo.db.chat_messages.find_one({"_id": ObjectId(message_id)})
    except Exception:
        return jsonify({"error": "Invalid message id"}), 400

    if not message:
        return jsonify({"error": "Message not found"}), 404

    reporter_id = get_jwt_identity()
    reporter = mongo.db.users.find_one({"_id": ObjectId(reporter_id)})
    report = {
        "message_id": str(message_id),
        "channel": message.get("channel"),
        "message_author_id": str(message.get("user_id")),
        "message_author_name": message.get("name", "Unknown"),
        "message_snapshot": (message.get("message") or "")[:180],
        "reporter_id": reporter_id,
        "reporter_name": reporter.get("name", "Unknown") if reporter else "Unknown",
        "reason": reason or "No reason provided",
        "created_at": __import__("datetime").datetime.utcnow(),
    }
    mongo.db.chat_reports.insert_one(report)
    return jsonify({"message": "Report submitted"})


@chat.route("/admin/reports", methods=["GET"])
@jwt_required()
def get_reports():
    if get_jwt().get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403

    reports = list(mongo.db.chat_reports.find().sort("created_at", -1).limit(50))
    payload = []
    for r in reports:
        payload.append({
            "id": str(r["_id"]),
            "channel": r.get("channel"),
            "message_author_id": r.get("message_author_id"),
            "message_author_name": r.get("message_author_name"),
            "message_snapshot": r.get("message_snapshot"),
            "reporter_name": r.get("reporter_name"),
            "reason": r.get("reason"),
        })
    return jsonify(payload)


@chat.route("/admin/reports/<report_id>/resolve", methods=["POST"])
@jwt_required()
def resolve_report(report_id):
    if get_jwt().get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403

    try:
        mongo.db.chat_reports.delete_one({"_id": ObjectId(report_id)})
    except Exception:
        return jsonify({"error": "Invalid report id"}), 400

    return jsonify({"message": "Report resolved"})


@chat.route("/pinned/<channel>", methods=["GET"])
@jwt_required()
def get_pinned(channel):
    messages = list(mongo.db.chat_messages.find({"channel": channel, "pinned": True, "deleted": {"$ne": True}}).sort("created_at", -1).limit(20))
    return jsonify([serialize_message(m) for m in messages])

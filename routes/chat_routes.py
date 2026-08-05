from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required
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
    messages = list(
        mongo.db.chat_messages.find({"channel": channel, "deleted": {"$ne": True}})
        .sort("created_at", -1)
        .limit(50)
    )
    messages.reverse()  # oldest first for rendering top-to-bottom

    return jsonify([serialize_message(m) for m in messages])

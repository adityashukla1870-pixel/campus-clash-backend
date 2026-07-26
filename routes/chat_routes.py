from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required

chat = Blueprint("chat", __name__)
mongo = None


def init_chat_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


@chat.route("/history", methods=["GET"])
@jwt_required()
def history():
    messages = list(
        mongo.db.chat_messages.find().sort("created_at", -1).limit(50)
    )
    messages.reverse()  # oldest first for rendering top-to-bottom

    data = [{
        "id": str(m["_id"]),
        "user_id": m.get("user_id"),
        "name": m.get("name"),
        "role": m.get("role", "user"),
        "message": m.get("message"),
        "created_at": m["created_at"].isoformat() if m.get("created_at") else None
    } for m in messages]

    return jsonify(data)

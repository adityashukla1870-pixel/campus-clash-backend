from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime

notification = Blueprint("notification", __name__)
mongo = None


def init_notification_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


def create_notification(user_id, message, ntype="info", tournament_id=None):
    """Helper other route files can import to push a notification to a user."""
    mongo.db.notifications.insert_one({
        "user_id": str(user_id),
        "message": message,
        "type": ntype,               # "payment" | "room" | "winner" | "info"
        "tournament_id": str(tournament_id) if tournament_id else None,
        "read": False,
        "created_at": datetime.utcnow()
    })


# ---------------- MY NOTIFICATIONS ----------------
@notification.route("/my", methods=["GET"])
@jwt_required()
def my_notifications():
    user_id = get_jwt_identity()

    items = list(mongo.db.notifications.find(
        {"user_id": user_id}
    ).sort("created_at", -1).limit(30))

    unread_count = mongo.db.notifications.count_documents({
        "user_id": user_id,
        "read": False
    })

    data = []
    for n in items:
        data.append({
            "id": str(n["_id"]),
            "message": n.get("message"),
            "type": n.get("type", "info"),
            "tournament_id": n.get("tournament_id"),
            "read": n.get("read", False),
            "created_at": n["created_at"].isoformat() if n.get("created_at") else None
        })

    return jsonify({"notifications": data, "unread_count": unread_count})


# ---------------- MARK ONE AS READ ----------------
@notification.route("/<notification_id>/read", methods=["POST"])
@jwt_required()
def mark_read(notification_id):
    user_id = get_jwt_identity()

    try:
        oid = ObjectId(notification_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid notification id"}), 400

    mongo.db.notifications.update_one(
        {"_id": oid, "user_id": user_id},
        {"$set": {"read": True}}
    )

    return jsonify({"message": "Marked as read"})


# ---------------- MARK ALL AS READ ----------------
@notification.route("/read-all", methods=["POST"])
@jwt_required()
def mark_all_read():
    user_id = get_jwt_identity()

    mongo.db.notifications.update_many(
        {"user_id": user_id, "read": False},
        {"$set": {"read": True}}
    )

    return jsonify({"message": "All marked as read"})

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from bson.errors import InvalidId
from functools import wraps
from datetime import datetime, timezone

feedbacks = Blueprint("feedbacks", __name__)
mongo = None


def init_feedback_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


def get_current_user():
    user_id = get_jwt_identity()
    try:
        return mongo.db.users.find_one({"_id": ObjectId(user_id)})
    except (InvalidId, TypeError):
        return None


def admin_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user or user.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper


def serialize_feedback(doc):
    if not doc:
        return None
    return {
        "id": str(doc["_id"]),
        "user_id": str(doc.get("user_id", "")),
        "username": doc.get("username", "Anonymous"),
        "tournament_id": str(doc.get("tournament_id", "")),
        "tournament_name": doc.get("tournament_name", ""),
        "rating": doc.get("rating", 5),
        "comment": doc.get("comment", ""),
        "status": doc.get("status", "pending"),
        "created_at": doc.get("created_at", ""),
        "reviewed_at": doc.get("reviewed_at"),
        "reviewed_by": str(doc.get("reviewed_by", "")) if doc.get("reviewed_by") else None,
    }


@feedbacks.route("", methods=["POST"])
@jwt_required()
def submit_feedback():
    user = get_current_user()
    if not user:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json(silent=True) or {}
    tournament_id = data.get("tournament_id", "").strip()
    rating = data.get("rating")
    comment = data.get("comment", "").strip()

    if not tournament_id:
        return jsonify({"error": "Tournament ID is required"}), 400

    try:
        tid = ObjectId(tournament_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid tournament ID"}), 400

    if rating is None or not isinstance(rating, (int, float)) or rating < 1 or rating > 5:
        return jsonify({"error": "Rating must be between 1 and 5"}), 400

    tournament = mongo.db.tournaments.find_one({"_id": tid})
    if not tournament:
        return jsonify({"error": "Tournament not found"}), 404

    existing = mongo.db.feedbacks.find_one({
        "user_id": user["_id"],
        "tournament_id": tid,
    })
    if existing:
        return jsonify({"error": "You have already submitted feedback for this tournament"}), 409

    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "user_id": user["_id"],
        "username": user.get("name") or user.get("username", "Anonymous"),
        "tournament_id": tid,
        "tournament_name": tournament.get("name", ""),
        "rating": int(rating),
        "comment": comment,
        "status": "pending",
        "created_at": now,
        "reviewed_at": None,
        "reviewed_by": None,
    }
    result = mongo.db.feedbacks.insert_one(doc)
    doc["_id"] = result.inserted_id

    return jsonify(serialize_feedback(doc)), 201


@feedbacks.route("/mine", methods=["GET"])
@jwt_required()
def my_feedbacks():
    user = get_current_user()
    if not user:
        return jsonify({"error": "User not found"}), 404

    cursor = mongo.db.feedbacks.find({"user_id": user["_id"]}).sort("created_at", -1)
    return jsonify([serialize_feedback(d) for d in cursor])


@feedbacks.route("/admin/all", methods=["GET"])
@admin_required
def admin_all_feedbacks():
    status_filter = request.args.get("status")
    query = {}
    if status_filter and status_filter in ("pending", "approved", "rejected"):
        query["status"] = status_filter

    cursor = mongo.db.feedbacks.find(query).sort("created_at", -1)
    return jsonify([serialize_feedback(d) for d in cursor])


@feedbacks.route("/admin/<feedback_id>/approve", methods=["PATCH"])
@admin_required
def approve_feedback(feedback_id):
    try:
        fid = ObjectId(feedback_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid feedback ID"}), 400

    user = get_current_user()
    now = datetime.now(timezone.utc).isoformat()

    result = mongo.db.feedbacks.update_one(
        {"_id": fid},
        {"$set": {"status": "approved", "reviewed_at": now, "reviewed_by": user["_id"]}}
    )
    if result.matched_count == 0:
        return jsonify({"error": "Feedback not found"}), 404

    return jsonify({"message": "Feedback approved"})


@feedbacks.route("/admin/<feedback_id>/reject", methods=["PATCH"])
@admin_required
def reject_feedback(feedback_id):
    try:
        fid = ObjectId(feedback_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid feedback ID"}), 400

    user = get_current_user()
    now = datetime.now(timezone.utc).isoformat()

    result = mongo.db.feedbacks.update_one(
        {"_id": fid},
        {"$set": {"status": "rejected", "reviewed_at": now, "reviewed_by": user["_id"]}}
    )
    if result.matched_count == 0:
        return jsonify({"error": "Feedback not found"}), 404

    return jsonify({"message": "Feedback rejected"})


@feedbacks.route("/community", methods=["GET"])
def community_reviews():
    cursor = mongo.db.feedbacks.find({"status": "approved"}).sort("created_at", -1).limit(20)
    return jsonify([serialize_feedback(d) for d in cursor])


@feedbacks.route("/pending-launcher", methods=["GET"])
@jwt_required()
def pending_launcher():
    user = get_current_user()
    if not user:
        return jsonify({"error": "User not found"}), 404

    user_id = user["_id"]

    registrations = list(mongo.db.registrations.find({
        "user_id": str(user_id),
        "payment_status": {"$in": ["approved", "teammate"]}
    }))

    tournament_ids = []
    reg_map = {}
    for r in registrations:
        tid = r.get("tournament_id")
        if tid:
            tournament_ids.append(tid)
            reg_map[str(tid)] = r

    if not tournament_ids:
        return jsonify({"pending": []})

    tournaments = list(mongo.db.tournaments.find({
        "_id": {"$in": tournament_ids},
        "status": "completed",
        "feedback_launched": True
    }))

    if not tournaments:
        return jsonify({"pending": []})

    existing_feedbacks = list(mongo.db.feedbacks.find({
        "user_id": user_id,
        "tournament_id": {"$in": tournament_ids}
    }))
    feedback_tournament_ids = {str(f.get("tournament_id")) for f in existing_feedbacks}

    pending = []
    for t in tournaments:
        tid_str = str(t["_id"])
        if tid_str not in feedback_tournament_ids:
            pending.append({
                "tournament_id": tid_str,
                "tournament_name": t.get("name", "Tournament"),
            })

    return jsonify({"pending": pending})


@feedbacks.route("/admin/launchable-tournaments", methods=["GET"])
@admin_required
def launchable_tournaments():
    tournaments = list(mongo.db.tournaments.find({"status": "completed"}))

    result = []
    for t in tournaments:
        tid = t["_id"]
        total_players = mongo.db.registrations.count_documents({
            "tournament_id": tid,
            "payment_status": {"$in": ["approved", "teammate"]}
        })
        feedback_count = mongo.db.feedbacks.count_documents({"tournament_id": tid})
        result.append({
            "id": str(tid),
            "name": t.get("name", "Tournament"),
            "feedback_launched": bool(t.get("feedback_launched", False)),
            "total_players": total_players,
            "feedback_count": feedback_count,
        })

    return jsonify(result)


@feedbacks.route("/admin/launch/<tournament_id>", methods=["POST"])
@admin_required
def launch_feedback(tournament_id):
    try:
        tid = ObjectId(tournament_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid tournament ID"}), 400

    tournament = mongo.db.tournaments.find_one({"_id": tid})
    if not tournament:
        return jsonify({"error": "Tournament not found"}), 404

    if tournament.get("status") != "completed":
        return jsonify({"error": "Tournament must be completed before launching feedback"}), 400

    mongo.db.tournaments.update_one(
        {"_id": tid},
        {"$set": {"feedback_launched": True}}
    )

    return jsonify({"message": "Feedback launched", "tournament_id": str(tid)})


@feedbacks.route("/admin/launch/<tournament_id>", methods=["DELETE"])
@admin_required
def unlaunch_feedback(tournament_id):
    try:
        tid = ObjectId(tournament_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid tournament ID"}), 400

    tournament = mongo.db.tournaments.find_one({"_id": tid})
    if not tournament:
        return jsonify({"error": "Tournament not found"}), 404

    mongo.db.tournaments.update_one(
        {"_id": tid},
        {"$set": {"feedback_launched": False}}
    )

    return jsonify({"message": "Feedback launch revoked", "tournament_id": str(tid)})

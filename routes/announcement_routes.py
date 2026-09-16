from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime
from functools import wraps
from utils.cloud_storage import upload_image

announcements = Blueprint("announcements", __name__)
mongo = None


def init_announcement_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


def admin_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        claims = get_jwt()
        if claims.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# ADMIN ENDPOINTS
# ---------------------------------------------------------------------------

@announcements.route("", methods=["POST"])
@admin_required
def create_announcement():
    data = request.form if request.content_type and "multipart" in request.content_type else (request.get_json() or {})

    title = (data.get("title") or "").strip()
    message = (data.get("message") or "").strip()
    if not title or not message:
        return jsonify({"error": "Title and message are required"}), 400

    ntype = data.get("type", "general")
    if ntype not in ("global_announcement", "specific_user", "tournament", "general"):
        return jsonify({"error": "Invalid notification type"}), 400

    image_url = None
    if "image" in request.files:
        img = request.files["image"]
        if img.filename:
            try:
                image_url = upload_image(img, "campus-clash/announcements")
            except RuntimeError as e:
                return jsonify({"error": str(e)}), 400

    target_users = []
    if ntype == "specific_user":
        raw = data.get("targetUsers", "")
        if isinstance(raw, str):
            target_users = [u.strip() for u in raw.split(",") if u.strip()]
        elif isinstance(raw, list):
            target_users = [str(u).strip() for u in raw if str(u).strip()]

    tournament_id = None
    if ntype == "tournament":
        tid = data.get("tournamentId", "")
        if tid:
            try:
                tournament_id = str(ObjectId(tid))
            except (InvalidId, TypeError):
                return jsonify({"error": "Invalid tournament ID"}), 400

    def parse_bool(val, default=True):
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        return str(val).lower() in ("true", "1", "yes")

    show_popup = parse_bool(data.get("showPopup"), True)
    show_in_center = parse_bool(data.get("showInCenter"), True)
    show_once = parse_bool(data.get("showOnce"), True)
    is_active = parse_bool(data.get("isActive"), True)

    action_label = (data.get("actionLabel") or "").strip() or None
    action_url = (data.get("actionUrl") or "").strip() or None

    expires_at = None
    if data.get("expiresAt"):
        try:
            expires_at = datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    scheduled_at = None
    if data.get("scheduledAt"):
        try:
            scheduled_at = datetime.fromisoformat(data["scheduledAt"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    now = datetime.utcnow()
    doc = {
        "title": title,
        "message": message,
        "imageUrl": image_url,
        "type": ntype,
        "targetUsers": target_users,
        "tournamentId": tournament_id,
        "actionLabel": action_label,
        "actionUrl": action_url,
        "showPopup": show_popup,
        "showInCenter": show_in_center,
        "showOnce": show_once,
        "isActive": is_active,
        "createdAt": now,
        "updatedAt": now,
        "expiresAt": expires_at,
        "scheduledAt": scheduled_at,
    }

    result = mongo.db.announcements.insert_one(doc)
    doc["_id"] = result.inserted_id
    return jsonify({"id": str(doc["_id"]), "message": "Announcement created"}), 201


@announcements.route("/admin", methods=["GET"])
@admin_required
def list_admin():
    page = max(int(request.args.get("page", 1)), 1)
    limit = min(int(request.args.get("limit", 50)), 100)
    skip = (page - 1) * limit

    query = {}
    status = request.args.get("status")
    if status == "active":
        query["isActive"] = True
    elif status == "inactive":
        query["isActive"] = False

    total = mongo.db.announcements.count_documents(query)
    items = list(
        mongo.db.announcements.find(query)
        .sort("createdAt", -1)
        .skip(skip)
        .limit(limit)
    )

    data = []
    for a in items:
        data.append({
            "id": str(a["_id"]),
            "title": a.get("title"),
            "message": a.get("message"),
            "imageUrl": a.get("imageUrl"),
            "type": a.get("type", "general"),
            "targetUsers": a.get("targetUsers", []),
            "tournamentId": a.get("tournamentId"),
            "actionLabel": a.get("actionLabel"),
            "actionUrl": a.get("actionUrl"),
            "showPopup": a.get("showPopup", True),
            "showInCenter": a.get("showInCenter", True),
            "showOnce": a.get("showOnce", True),
            "isActive": a.get("isActive", True),
            "createdAt": a.get("createdAt").isoformat() if a.get("createdAt") else None,
            "updatedAt": a.get("updatedAt").isoformat() if a.get("updatedAt") else None,
            "expiresAt": a.get("expiresAt").isoformat() if a.get("expiresAt") else None,
            "scheduledAt": a.get("scheduledAt").isoformat() if a.get("scheduledAt") else None,
        })

    return jsonify({"notifications": data, "total": total, "page": page, "pages": (total + limit - 1) // limit})


@announcements.route("/admin/<ann_id>", methods=["GET"])
@admin_required
def get_admin_one(ann_id):
    try:
        oid = ObjectId(ann_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid ID"}), 400

    a = mongo.db.announcements.find_one({"_id": oid})
    if not a:
        return jsonify({"error": "Not found"}), 404

    return jsonify({
        "id": str(a["_id"]),
        "title": a.get("title"),
        "message": a.get("message"),
        "imageUrl": a.get("imageUrl"),
        "type": a.get("type", "general"),
        "targetUsers": a.get("targetUsers", []),
        "tournamentId": a.get("tournamentId"),
        "actionLabel": a.get("actionLabel"),
        "actionUrl": a.get("actionUrl"),
        "showPopup": a.get("showPopup", True),
        "showInCenter": a.get("showInCenter", True),
        "showOnce": a.get("showOnce", True),
        "isActive": a.get("isActive", True),
        "createdAt": a.get("createdAt").isoformat() if a.get("createdAt") else None,
        "updatedAt": a.get("updatedAt").isoformat() if a.get("updatedAt") else None,
        "expiresAt": a.get("expiresAt").isoformat() if a.get("expiresAt") else None,
        "scheduledAt": a.get("scheduledAt").isoformat() if a.get("scheduledAt") else None,
    })


@announcements.route("/admin/<ann_id>", methods=["PUT"])
@admin_required
def update_announcement(ann_id):
    try:
        oid = ObjectId(ann_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid ID"}), 400

    a = mongo.db.announcements.find_one({"_id": oid})
    if not a:
        return jsonify({"error": "Not found"}), 404

    data = request.form if request.content_type and "multipart" in request.content_type else (request.get_json() or {})
    updates = {}

    if "title" in data:
        updates["title"] = data["title"].strip()
    if "message" in data:
        updates["message"] = data["message"].strip()
    if "type" in data:
        ntype = data["type"]
        if ntype in ("global_announcement", "specific_user", "tournament", "general"):
            updates["type"] = ntype
    if "targetUsers" in data:
        raw = data["targetUsers"]
        if isinstance(raw, str):
            updates["targetUsers"] = [u.strip() for u in raw.split(",") if u.strip()]
        elif isinstance(raw, list):
            updates["targetUsers"] = [str(u).strip() for u in raw if str(u).strip()]
    if "tournamentId" in data:
        tid = data["tournamentId"]
        if tid:
            try:
                updates["tournamentId"] = str(ObjectId(tid))
            except (InvalidId, TypeError):
                pass
        else:
            updates["tournamentId"] = None
    if "actionLabel" in data:
        updates["actionLabel"] = (data["actionLabel"] or "").strip() or None
    if "actionUrl" in data:
        updates["actionUrl"] = (data["actionUrl"] or "").strip() or None
    if "showPopup" in data:
        val = data["showPopup"]
        updates["showPopup"] = val if isinstance(val, bool) else str(val).lower() in ("true", "1", "yes")
    if "showInCenter" in data:
        val = data["showInCenter"]
        updates["showInCenter"] = val if isinstance(val, bool) else str(val).lower() in ("true", "1", "yes")
    if "showOnce" in data:
        val = data["showOnce"]
        updates["showOnce"] = val if isinstance(val, bool) else str(val).lower() in ("true", "1", "yes")
    if "isActive" in data:
        val = data["isActive"]
        updates["isActive"] = val if isinstance(val, bool) else str(val).lower() in ("true", "1", "yes")
    if "expiresAt" in data:
        if data["expiresAt"]:
            try:
                updates["expiresAt"] = datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
        else:
            updates["expiresAt"] = None
    if "scheduledAt" in data:
        if data["scheduledAt"]:
            try:
                updates["scheduledAt"] = datetime.fromisoformat(data["scheduledAt"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
        else:
            updates["scheduledAt"] = None

    if "image" in request.files:
        img = request.files["image"]
        if img.filename:
            try:
                updates["imageUrl"] = upload_image(img, "campus-clash/announcements")
            except RuntimeError as e:
                return jsonify({"error": str(e)}), 400

    if updates:
        updates["updatedAt"] = datetime.utcnow()
        mongo.db.announcements.update_one({"_id": oid}, {"$set": updates})

    return jsonify({"message": "Updated"})


@announcements.route("/admin/<ann_id>", methods=["DELETE"])
@admin_required
def delete_announcement(ann_id):
    try:
        oid = ObjectId(ann_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid ID"}), 400

    result = mongo.db.announcements.delete_one({"_id": oid})
    if result.deleted_count == 0:
        return jsonify({"error": "Not found"}), 404

    mongo.db.notification_states.delete_many({"announcementId": oid})
    return jsonify({"message": "Deleted"})


@announcements.route("/admin/<ann_id>/status", methods=["PATCH"])
@admin_required
def toggle_status(ann_id):
    try:
        oid = ObjectId(ann_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid ID"}), 400

    a = mongo.db.announcements.find_one({"_id": oid})
    if not a:
        return jsonify({"error": "Not found"}), 404

    new_status = not a.get("isActive", True)
    mongo.db.announcements.update_one(
        {"_id": oid},
        {"$set": {"isActive": new_status, "updatedAt": datetime.utcnow()}}
    )
    return jsonify({"isActive": new_status})


# ---------------------------------------------------------------------------
# USER ENDPOINTS
# ---------------------------------------------------------------------------

def _is_targeted(announcement, user_id):
    ntype = announcement.get("type", "general")
    if ntype == "global_announcement":
        return True
    if ntype == "specific_user":
        return user_id in (announcement.get("targetUsers") or [])
    if ntype == "tournament":
        tid = announcement.get("tournamentId")
        if not tid:
            return False
        count = mongo.db.registrations.count_documents({
            "user_id": user_id,
            "tournament_id": ObjectId(tid),
            "payment_status": {"$in": ["approved", "teammate"]}
        })
        return count > 0
    if ntype == "general":
        return True
    return False


@announcements.route("/user", methods=["GET"])
@jwt_required()
def user_announcements():
    user_id = get_jwt_identity()
    now = datetime.utcnow()

    query = {
        "isActive": True,
        "$and": [
            {"$or": [
                {"expiresAt": None},
                {"expiresAt": {"$gt": now}},
            ]},
            {"$or": [
                {"scheduledAt": None},
                {"scheduledAt": {"$lte": now}},
            ]}
        ]
    }
    items = list(mongo.db.announcements.find(query).sort("createdAt", -1).limit(50))

    targeted = [a for a in items if _is_targeted(a, user_id)]

    state_docs = list(mongo.db.notification_states.find({"userId": user_id}))
    state_map = {str(s["announcementId"]): s for s in state_docs}

    data = []
    for a in targeted:
        aid = str(a["_id"])
        state = state_map.get(aid)
        seen = state.get("seen", False) if state else False
        dismissed = state.get("dismissed", False) if state else False

        data.append({
            "id": aid,
            "title": a.get("title"),
            "message": a.get("message"),
            "imageUrl": a.get("imageUrl"),
            "type": a.get("type", "general"),
            "actionLabel": a.get("actionLabel"),
            "actionUrl": a.get("actionUrl"),
            "showPopup": a.get("showPopup", True),
            "showInCenter": a.get("showInCenter", True),
            "showOnce": a.get("showOnce", True),
            "seen": seen,
            "dismissed": dismissed,
            "createdAt": a.get("createdAt").isoformat() if a.get("createdAt") else None,
            "expiresAt": a.get("expiresAt").isoformat() if a.get("expiresAt") else None,
            "tournamentId": a.get("tournamentId"),
        })

    return jsonify({"announcements": data})


@announcements.route("/unread-count", methods=["GET"])
@jwt_required()
def unread_count():
    user_id = get_jwt_identity()
    now = datetime.utcnow()

    query = {
        "isActive": True,
        "$and": [
            {"$or": [
                {"expiresAt": None},
                {"expiresAt": {"$gt": now}},
            ]},
            {"$or": [
                {"scheduledAt": None},
                {"scheduledAt": {"$lte": now}},
            ]}
        ]
    }
    items = list(mongo.db.announcements.find(query).sort("createdAt", -1).limit(50))
    targeted = [a for a in items if _is_targeted(a, user_id)]

    state_docs = list(mongo.db.notification_states.find({"userId": user_id}))
    state_map = {str(s["announcementId"]): s for s in state_docs}

    count = 0
    for a in targeted:
        aid = str(a["_id"])
        state = state_map.get(aid)
        if not state or not state.get("seen", False):
            count += 1

    return jsonify({"unread_count": count})


@announcements.route("/<ann_id>/read", methods=["PATCH"])
@jwt_required()
def mark_read(ann_id):
    user_id = get_jwt_identity()
    try:
        oid = ObjectId(ann_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid ID"}), 400

    now = datetime.utcnow()
    mongo.db.notification_states.update_one(
        {"announcementId": oid, "userId": user_id},
        {"$set": {"seen": True, "readAt": now}, "$setOnInsert": {"createdAt": now}},
        upsert=True
    )
    return jsonify({"message": "Marked as read"})


@announcements.route("/<ann_id>/dismiss", methods=["PATCH"])
@jwt_required()
def dismiss_popup(ann_id):
    user_id = get_jwt_identity()
    try:
        oid = ObjectId(ann_id)
    except (InvalidId, TypeError):
        return jsonify({"error": "Invalid ID"}), 400

    now = datetime.utcnow()
    mongo.db.notification_states.update_one(
        {"announcementId": oid, "userId": user_id},
        {"$set": {"dismissed": True, "dismissedAt": now, "seen": True, "readAt": now},
         "$setOnInsert": {"createdAt": now}},
        upsert=True
    )
    return jsonify({"message": "Dismissed"})


@announcements.route("/read-all", methods=["PATCH"])
@jwt_required()
def mark_all_read():
    user_id = get_jwt_identity()
    now = datetime.utcnow()

    query = {
        "isActive": True,
        "$and": [
            {"$or": [
                {"expiresAt": None},
                {"expiresAt": {"$gt": now}},
            ]},
            {"$or": [
                {"scheduledAt": None},
                {"scheduledAt": {"$lte": now}},
            ]}
        ]
    }
    items = list(mongo.db.announcements.find(query).limit(200))
    targeted = [a for a in items if _is_targeted(a, user_id)]

    for a in targeted:
        oid = a["_id"]
        mongo.db.notification_states.update_one(
            {"announcementId": oid, "userId": user_id},
            {"$set": {"seen": True, "readAt": now}, "$setOnInsert": {"createdAt": now}},
            upsert=True
        )

    return jsonify({"message": "All marked as read"})

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from bson.errors import InvalidId
from utils.cloud_storage import upload_image
from functools import wraps
from datetime import datetime, timezone

avatars = Blueprint("avatars", __name__)
mongo = None


def init_avatar_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_BYTES = 5 * 1024 * 1024


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


def safe_object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def serialize_avatar(doc):
    if not doc:
        return None
    return {
        "id": str(doc["_id"]),
        "name": doc.get("name", ""),
        "imageUrl": doc.get("imageUrl", ""),
        "status": doc.get("status", "published"),
        "themeId": doc.get("themeId"),
    }


# ---------------------------------------------------------------------------
# Seed — ensure Cyber Boy & Girl exist so every user can select them
# ---------------------------------------------------------------------------

CYBER_BOY_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120">'
    "<defs><linearGradient id=\"g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\">"
    '<stop offset="0%" stop-color="#0077B6"/>'
    '<stop offset="100%" stop-color="#00B4D8"/>'
    "</linearGradient></defs>"
    '<circle cx="60" cy="60" r="60" fill="#03045e"/>'
    '<circle cx="60" cy="60" r="56" fill="none" stroke="url(#g)" stroke-width="3"/>'
    '<text x="60" y="68" text-anchor="middle" font-family="sans-serif" font-size="42" font-weight="800" fill="#90e0ef">CB</text>'
    "</svg>"
)

CYBER_GIRL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120">'
    "<defs><linearGradient id=\"g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\">"
    '<stop offset="0%" stop-color="#CDB4DB"/>'
    '<stop offset="100%" stop-color="#FFAFCC"/>'
    "</linearGradient></defs>"
    '<circle cx="60" cy="60" r="60" fill="#1a0f2e"/>'
    '<circle cx="60" cy="60" r="56" fill="none" stroke="url(#g)" stroke-width="3"/>'
    '<text x="60" y="68" text-anchor="middle" font-family="sans-serif" font-size="42" font-weight="800" fill="#ffc8dd">CG</text>'
    "</svg>"
)

SEED_AVATARS = [
    {"name": "Cyber Boy", "imageUrl": f"data:image/svg+xml,{CYBER_BOY_SVG}", "themeId": "cyber-boy", "status": "published"},
    {"name": "Cyber Girl", "imageUrl": f"data:image/svg+xml,{CYBER_GIRL_SVG}", "themeId": "cyber-girl", "status": "published"},
]


def ensure_seeded():
    if mongo.db.avatars.count_documents({}) == 0:
        for seed in SEED_AVATARS:
            seed["createdAt"] = datetime.now(timezone.utc).isoformat()
            seed["updatedAt"] = seed["createdAt"]
            mongo.db.avatars.insert_one(seed)


# ---------------------------------------------------------------------------
# GET /avatars — list all (any authenticated user)
# ---------------------------------------------------------------------------

@avatars.route("", methods=["GET"])
@jwt_required()
def list_avatars():
    ensure_seeded()
    docs = list(mongo.db.avatars.find())
    return jsonify([serialize_avatar(d) for d in docs])


# ---------------------------------------------------------------------------
# POST /avatars — create (admin only, multipart form)
# ---------------------------------------------------------------------------

@avatars.route("", methods=["POST"])
@admin_required
def create_avatar():
    name = request.form.get("name")
    theme_id = request.form.get("themeId")
    file = request.files.get("image")

    if not name or not file:
        return jsonify({"error": "name and image are required"}), 400
    if file.mimetype not in ALLOWED_TYPES:
        return jsonify({"error": "unsupported image type"}), 400
    if file.content_length and file.content_length > MAX_BYTES:
        return jsonify({"error": "image must be under 5 MB"}), 400

    try:
        image_url = upload_image(file, folder="campus-clash/avatars")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "name": name,
        "imageUrl": image_url,
        "themeId": theme_id or None,
        "status": "published",
        "createdAt": now,
        "updatedAt": now,
    }
    inserted = mongo.db.avatars.insert_one(doc)
    doc["id"] = str(inserted.inserted_id)
    return jsonify(serialize_avatar(doc)), 201


# ---------------------------------------------------------------------------
# PATCH /avatars/<id> — update (admin only)
# ---------------------------------------------------------------------------

@avatars.route("/<avatar_id>", methods=["PATCH"])
@admin_required
def update_avatar(avatar_id):
    oid = safe_object_id(avatar_id)
    if not oid:
        return jsonify({"error": "invalid avatar id"}), 400

    updates = {}
    if "name" in request.form:
        updates["name"] = request.form["name"]
    if "themeId" in request.form:
        updates["themeId"] = request.form["themeId"] or None
    if "status" in request.form:
        updates["status"] = request.form["status"]

    file = request.files.get("image")
    if file:
        if file.mimetype not in ALLOWED_TYPES:
            return jsonify({"error": "unsupported image type"}), 400
        try:
            updates["imageUrl"] = upload_image(file, folder="campus-clash/avatars")
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 502

    if not updates:
        return jsonify({"error": "no fields to update"}), 400

    updates["updatedAt"] = datetime.now(timezone.utc).isoformat()
    result = mongo.db.avatars.update_one({"_id": oid}, {"$set": updates})
    if result.matched_count == 0:
        return jsonify({"error": "avatar not found"}), 404

    doc = mongo.db.avatars.find_one({"_id": oid})
    return jsonify(serialize_avatar(doc))


# ---------------------------------------------------------------------------
# DELETE /avatars/<id> — delete (admin only)
# ---------------------------------------------------------------------------

@avatars.route("/<avatar_id>", methods=["DELETE"])
@admin_required
def delete_avatar(avatar_id):
    oid = safe_object_id(avatar_id)
    if not oid:
        return jsonify({"error": "invalid avatar id"}), 400
    result = mongo.db.avatars.delete_one({"_id": oid})
    if result.deleted_count == 0:
        return jsonify({"error": "avatar not found"}), 404
    return jsonify({"message": "deleted"})


# ---------------------------------------------------------------------------
# POST /avatars/select — save avatar selection on user document
# ---------------------------------------------------------------------------

@avatars.route("/select", methods=["POST"])
@jwt_required()
def select_avatar():
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    avatar_id = data.get("avatarId")

    oid = safe_object_id(user_id)
    if not oid:
        return jsonify({"error": "invalid user id"}), 400

    mongo.db.users.update_one(
        {"_id": oid},
        {"$set": {"avatarId": avatar_id or None}},
    )
    return jsonify({"ok": True})

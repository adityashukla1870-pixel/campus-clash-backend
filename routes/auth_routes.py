from flask import Blueprint, request, jsonify, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import create_access_token, create_refresh_token, jwt_required, get_jwt_identity
from bson import ObjectId
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import re

auth = Blueprint("auth", __name__)
mongo = None

USERNAME_RE = re.compile(r'^[a-z0-9._-]{3,20}$')

def _normalize_username(raw):
    return raw.strip().lower()

def _is_valid_username(username):
    return bool(USERNAME_RE.match(username))

def init_auth_routes(mongo_instance):
    global mongo
    mongo = mongo_instance
    _ensure_username_index()


def _ensure_username_index():
    try:
        mongo.db.users.create_index("username", unique=True, sparse=True)
    except Exception:
        pass


# GOOGLE AUTH
@auth.route("/google", methods=["POST"])
def google_login():
    import requests as http_requests

    data = request.json
    credential = data.get("credential")

    if not credential:
        return jsonify({"error": "Missing Google credential"}), 400

    client_id = current_app.config.get("GOOGLE_CLIENT_ID")

    try:
        # Verify token with Google's tokeninfo endpoint
        resp = http_requests.get(
            f"https://oauth2.googleapis.com/tokeninfo?id_token={credential}",
            timeout=10
        )

        if resp.status_code != 200:
            return jsonify({"error": "Invalid Google token"}), 401

        token_data = resp.json()

        # Verify audience
        if token_data.get("aud") != client_id:
            return jsonify({"error": "Invalid audience"}), 401

        email = token_data.get("email")
        name = token_data.get("name", "")
        picture = token_data.get("picture", "")

        if not email:
            return jsonify({"error": "No email in Google token"}), 401

    except Exception as e:
        return jsonify({"error": f"Google verification failed: {str(e)}"}), 401

    users = mongo.db.users
    user = users.find_one({"email": email})

    if not user:
        # Auto-create new user
        base_username = re.sub(r'[^a-z0-9._-]', '', email.split("@")[0].lower())
        if len(base_username) < 3:
            base_username = base_username + "0"
        username_candidate = base_username
        counter = 1
        while users.find_one({"username": username_candidate}):
            username_candidate = f"{base_username}{counter}"
            counter += 1

        user_data = {
            "name": name,
            "email": email,
            "password": generate_password_hash(secrets.token_hex(32)),
            "college": "",
            "game_uid": "",
            "role": "player",
            "auth_provider": "google",
            "avatar": picture,
            "username": username_candidate,
        }
        result = users.insert_one(user_data)
        user_id = str(result.inserted_id)
        role = "player"
    else:
        user_id = str(user["_id"])
        role = user.get("role", "player")
        # Update avatar if not set
        if not user.get("avatar") and picture:
            users.update_one({"_id": user["_id"]}, {"$set": {"avatar": picture}})

    token = create_access_token(
        identity=user_id,
        additional_claims={"role": role}
    )

    return jsonify({
        "message": "Login successful",
        "token": token
    })


# CHECK USERNAME AVAILABILITY
@auth.route("/check-username/<username>", methods=["GET"])
def check_username(username):
    normalized = _normalize_username(username)
    if not _is_valid_username(normalized):
        return jsonify({"available": False, "error": "Username must be 3-20 characters, lowercase letters, numbers, dots, hyphens, or underscores"}), 400
    exists = mongo.db.users.find_one({"username": normalized})
    return jsonify({"available": not exists, "username": normalized})


# REGISTER
@auth.route("/register", methods=["POST"])
def register():
    data = request.json
    users = mongo.db.users

    name = data.get("name")
    email = data.get("email")
    password = data.get("password")
    college = data.get("college")
    game_uid = data.get("game_uid", "")
    username_raw = data.get("username")

    if not name or not email or not password:
        return jsonify({"error": "Name, email, and password are required"}), 400

    if not username_raw:
        return jsonify({"error": "Username is required"}), 400

    username = _normalize_username(username_raw)
    if not _is_valid_username(username):
        return jsonify({"error": "Username must be 3-20 characters, lowercase letters, numbers, dots, hyphens, or underscores"}), 400

    if users.find_one({"email": email}):
        return jsonify({"error": "User already exists"}), 400

    if users.find_one({"username": username}):
        return jsonify({"error": "Username is already taken"}), 400

    users.insert_one({
        "name": name,
        "email": email,
        "password": generate_password_hash(password),
        "college": college,
        "game_uid": game_uid,
        "username": username,
        "role": "player"
    })

    return jsonify({"message": "Registration successful"})


# LOGIN
@auth.route("/login", methods=["POST"])
def login():
    data = request.json
    users = mongo.db.users

    user = users.find_one({"email": data["email"]})

    if not user:
        return jsonify({"error": "User not found"}), 404

    if not check_password_hash(user["password"], data["password"]):
        return jsonify({"error": "Wrong password"}), 401

    token = create_access_token(
identity=str(user["_id"]),
additional_claims={
"role": user.get("role","user")
}
)

    refresh = create_refresh_token(identity=str(user["_id"]))

    return jsonify({
        "message": "Login successful",
        "token": token,
        "refresh": refresh
    })


# PROFILE (Protected)
@auth.route("/profile", methods=["GET"])
@jwt_required()
def profile():

    user_id = get_jwt_identity()

    user = mongo.db.users.find_one({
        "_id": ObjectId(user_id)
    })

    if not user:
        return jsonify({"error": "User not found"}), 404

    registrations = list(mongo.db.registrations.find({"user_id": user_id}))
    tournaments_joined = sum(1 for r in registrations if r.get("payment_status") == "approved")

    wins = 0
    prize_won = 0
    for r in registrations:
        if r.get("payment_status") != "approved":
            continue
        t = mongo.db.tournaments.find_one({"_id": r.get("tournament_id")})
        if t and t.get("winner_id") == user_id:
            wins += 1
            prize_won += t.get("prize_pool", 0)

    return jsonify({
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "college": user.get("college", ""),
        "game_uid": user.get("game_uid", ""),
        "role": user.get("role", "user"),
        "username": user.get("username", ""),
        "has_username": bool(user.get("username")),
        "stats": {
            "tournaments_joined": tournaments_joined,
            "wins": wins,
            "prize_won": prize_won
        }
    })


# REFRESH ACCESS TOKEN
@auth.route("/refresh", methods=["POST"])
@jwt_required(refresh=True)
def refresh_token():
    identity = get_jwt_identity()
    claims = get_jwt()
    role = claims.get("role", "user")
    new_token = create_access_token(identity=identity, additional_claims={"role": role})
    return jsonify({"token": new_token})


# UPDATE PROFILE (Protected)
@auth.route("/profile", methods=["PUT"])
@jwt_required()
def update_profile():

    user_id = get_jwt_identity()
    data = request.json or {}

    allowed_fields = {"name", "college", "game_uid"}
    updates = {k: v for k, v in data.items() if k in allowed_fields and v}

    if not updates:
        return jsonify({"error": "Nothing to update"}), 400

    mongo.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": updates}
    )

    # If name changed, propagate to registrations and match results
    if "name" in updates:
        new_name = updates["name"]

        # Update in registrations.team_members
        mongo.db.registrations.update_many(
            {"team_members.user_id": user_id},
            {"$set": {"team_members.$.name": new_name}}
        )

        # Update in registrations.team_leader
        mongo.db.registrations.update_many(
            {"team_leader.user_id": user_id},
            {"$set": {"team_leader.name": new_name}}
        )

        # Update in stage_pods.participants
        mongo.db.stage_pods.update_many(
            {"participants.user_id": user_id},
            {"$set": {"participants.$.name": new_name}}
        )

        # Update in stage_matches.results (player names)
        mongo.db.stage_matches.update_many(
            {"results.players.user_id": user_id},
            {"$set": {"results.$.players.$[elem].name": new_name}},
            array_filters=[{"elem.user_id": user_id}]
        )

        # Update in cross_pod_matches.results (player names)
        mongo.db.cross_pod_matches.update_many(
            {"results.players.user_id": user_id},
            {"$set": {"results.$.players.$[elem].name": new_name}},
            array_filters=[{"elem.user_id": user_id}]
        )

        # Update player_stats username
        mongo.db.player_stats.update_many(
            {"user_id": user_id},
            {"$set": {"username": new_name}}
        )

    return jsonify({"message": "Profile updated successfully"})


# CLAIM USERNAME (for existing users who registered before the username system)
@auth.route("/claim-username", methods=["POST"])
@jwt_required()
def claim_username():
    user_id = get_jwt_identity()
    data = request.json or {}
    username_raw = data.get("username")

    if not username_raw:
        return jsonify({"error": "Username is required"}), 400

    username = _normalize_username(username_raw)
    if not _is_valid_username(username):
        return jsonify({"error": "Username must be 3-20 characters, lowercase letters, numbers, dots, hyphens, or underscores"}), 400

    user = mongo.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        return jsonify({"error": "User not found"}), 404

    if user.get("username"):
        return jsonify({"error": "You already have a username"}), 400

    if mongo.db.users.find_one({"username": username}):
        return jsonify({"error": "Username is already taken"}), 400

    mongo.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"username": username}}
    )

    return jsonify({"message": "Username claimed successfully", "username": username})


# ── CHANGE USERNAME (once per 5 months) ──────────────────────────
@auth.route("/change-username", methods=["POST"])
@jwt_required()
def change_username():
    try:
        user_id = get_jwt_identity()
        data = request.json or {}
        new_username_raw = data.get("username")

        if not new_username_raw:
            return jsonify({"error": "Username is required"}), 400

        new_username = _normalize_username(new_username_raw)
        if not _is_valid_username(new_username):
            return jsonify({"error": "Username must be 3-20 characters, lowercase letters, numbers, dots, hyphens, or underscores"}), 400

        user = mongo.db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            return jsonify({"error": "User not found"}), 404

        current_username = user.get("username", "")
        if new_username == current_username:
            return jsonify({"error": "This is already your username"}), 400

        # Check 5-month cooldown
        last_changed = user.get("username_changed_at")
        if last_changed:
            now = datetime.utcnow()
            if isinstance(last_changed, str):
                try:
                    last_changed = datetime.fromisoformat(last_changed.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    last_changed = None
            if last_changed and (now - last_changed) < timedelta(days=152):
                days_left = 152 - (now - last_changed).days
                return jsonify({"error": f"You can change your username again in {days_left} days"}), 400

        # Check if already taken by someone else
        existing = mongo.db.users.find_one({"username": new_username})
        if existing and str(existing["_id"]) != user_id:
            return jsonify({"error": "Username is already taken"}), 400

        # Update username
        mongo.db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"username": new_username, "username_changed_at": datetime.utcnow()}}
        )

        return jsonify({"message": "Username changed successfully", "username": new_username})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── CHECK USERNAME CHANGE AVAILABILITY ───────────────────────────
@auth.route("/username-change-status", methods=["GET"])
@jwt_required()
def username_change_status():
    user_id = get_jwt_identity()
    user = mongo.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        return jsonify({"error": "User not found"}), 404

    last_changed = user.get("username_changed_at")
    if not last_changed:
        return jsonify({"can_change": True, "next_change_at": None})

    from datetime import datetime, timedelta
    now = datetime.utcnow()
    if isinstance(last_changed, str):
        try:
            last_changed = datetime.fromisoformat(last_changed.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            last_changed = None

    if not last_changed:
        return jsonify({"can_change": True, "next_change_at": None})

    next_change = last_changed + timedelta(days=152)
    can_change = now >= next_change
    days_left = max(0, (next_change - now).days)

    return jsonify({
        "can_change": can_change,
        "next_change_at": next_change.isoformat(),
        "days_remaining": days_left,
    })


# ── FORGOT PASSWORD ─────────────────────────────────────────────
@auth.route("/forgot-password", methods=["POST"])
def forgot_password():
    """Send a password-reset email. Always returns 200 to prevent email enumeration."""
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()

    # Always return success so attackers can't enumerate accounts
    success_msg = {"message": "If an account with that email exists, a reset link has been sent."}

    if not email:
        return jsonify(success_msg)

    user = mongo.db.users.find_one({"email": email})
    if not user:
        return jsonify(success_msg)

    # Rate limit: max 3 requests per email per hour
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_count = mongo.db.password_resets.count_documents({
        "user_id": str(user["_id"]),
        "created_at": {"$gte": one_hour_ago}
    })
    if recent_count >= 3:
        return jsonify(success_msg)

    # Invalidate any existing unused tokens for this user
    mongo.db.password_resets.update_many(
        {"user_id": str(user["_id"]), "used": False},
        {"$set": {"used": True}}
    )

    # Generate token: SHA256(secrets.token + user_id + timestamp)
    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    mongo.db.password_resets.insert_one({
        "user_id": str(user["_id"]),
        "token_hash": token_hash,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
        "used": False,
    })

    # Ensure TTL index for auto-cleanup
    try:
        mongo.db.password_resets.create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        pass

    # Send email
    frontend_url = current_app.config.get("FRONTEND_URL", "https://campus-clash-og.vercel.app")
    reset_url = f"{frontend_url}/reset-password?token={raw_token}"

    from utils.email_sender import send_reset_email
    send_reset_email(email, reset_url)

    return jsonify(success_msg)


# ── RESET PASSWORD ──────────────────────────────────────────────
@auth.route("/reset-password", methods=["POST"])
def reset_password():
    """Reset a user's password using a valid, non-expired, non-used token."""
    data = request.json or {}
    token = data.get("token", "")
    new_password = data.get("password", "")

    if not token or not new_password:
        return jsonify({"error": "Token and new password are required"}), 400

    if len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    token_hash = hashlib.sha256(token.encode()).hexdigest()

    record = mongo.db.password_resets.find_one({
        "token_hash": token_hash,
        "used": False,
        "expires_at": {"$gt": datetime.now(timezone.utc)}
    })

    if not record:
        return jsonify({"error": "Invalid or expired reset token"}), 400

    # Mark token as used
    mongo.db.password_resets.update_one(
        {"_id": record["_id"]},
        {"$set": {"used": True}}
    )

    # Update password
    mongo.db.users.update_one(
        {"_id": ObjectId(record["user_id"])},
        {"$set": {"password": generate_password_hash(new_password)}}
    )

    return jsonify({"message": "Password reset successful. You can now log in."})


# ── ADMIN: SEARCH USERS ────────────────────────────────────────
@auth.route("/admin/users", methods=["GET"])
@jwt_required()
def admin_list_users():
    """Search users by name, email, or username. Admin only."""
    user_id = get_jwt_identity()
    admin = mongo.db.users.find_one({"_id": ObjectId(user_id)})
    if not admin or admin.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403

    q = (request.args.get("q") or "").strip()
    query = {}
    if q:
        query = {
            "$or": [
                {"name": {"$regex": q, "$options": "i"}},
                {"email": {"$regex": q, "$options": "i"}},
                {"username": {"$regex": q, "$options": "i"}},
            ]
        }

    users = list(mongo.db.users.find(query, {"password": 0}).sort("name", 1).limit(50))
    for u in users:
        u["_id"] = str(u["_id"])

    return jsonify(users)


# ── ADMIN: RESET USER PASSWORD ──────────────────────────────────
@auth.route("/admin/reset-user-password", methods=["POST"])
@jwt_required()
def admin_reset_password():
    """Admin can reset any user's password. Body: {user_id, new_password}"""
    admin_id = get_jwt_identity()
    admin = mongo.db.users.find_one({"_id": ObjectId(admin_id)})
    if not admin or admin.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403

    data = request.json or {}
    target_user_id = data.get("user_id")
    new_password = data.get("new_password", "")

    if not target_user_id or not new_password:
        return jsonify({"error": "user_id and new_password are required"}), 400

    if len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    target = mongo.db.users.find_one({"_id": ObjectId(target_user_id)})
    if not target:
        return jsonify({"error": "User not found"}), 404

    mongo.db.users.update_one(
        {"_id": ObjectId(target_user_id)},
        {"$set": {"password": generate_password_hash(new_password)}}
    )

    name = target.get("name") or target.get("email") or target_user_id
    return jsonify({"message": f"Password reset for {name}", "user_id": target_user_id})

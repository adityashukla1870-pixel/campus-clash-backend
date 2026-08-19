from flask import Blueprint, request, jsonify, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import create_access_token, create_refresh_token, jwt_required, get_jwt_identity
from bson import ObjectId
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

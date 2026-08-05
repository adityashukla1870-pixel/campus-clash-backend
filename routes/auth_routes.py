from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from bson import ObjectId

auth = Blueprint("auth", __name__)
mongo = None

def init_auth_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


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

    if not name or not email or not password:
        return jsonify({"error": "Name, email, and password are required"}), 400

    if users.find_one({"email": email}):
        return jsonify({"error": "User already exists"}), 400

    users.insert_one({
        "name": name,
        "email": email,
        "password": generate_password_hash(password),
        "college": college,
        "game_uid": game_uid,
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

    return jsonify({
        "message": "Login successful",
        "token": token
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
        "stats": {
            "tournaments_joined": tournaments_joined,
            "wins": wins,
            "prize_won": prize_won
        }
    })


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

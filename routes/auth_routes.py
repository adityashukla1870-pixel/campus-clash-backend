from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity

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

    if users.find_one({"email": data["email"]}):
        return jsonify({"error": "User already exists"}), 400

    users.insert_one({
        "name": data["name"],
        "email": data["email"],
        "password": generate_password_hash(data["password"]),
        "college": data["college"],
        "game_uid": data["game_uid"],
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
    email = get_jwt_identity()
    user = mongo.db.users.find_one({"email": email})

    if not user:
        return jsonify({"error": "User not found"}), 404

    return jsonify({
        "name": user["name"],
        "email": user["email"],
        "college": user["college"],
        "game_uid": user["game_uid"],
        "role": user["role"]
    })

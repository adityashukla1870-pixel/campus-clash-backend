from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from werkzeug.utils import secure_filename
from utils.code_generator import generate_payment_code
import os

tournament = Blueprint("tournament", __name__)
mongo = None


def init_tournament_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


# ---------------- PAYMENT CODE GENERATOR ----------------



# ---------------- CREATE TOURNAMENT ----------------
@tournament.route("/create", methods=["POST"])
@jwt_required()
def create_tournament():

    email = get_jwt_identity()
    user = mongo.db.users.find_one({"email": email})

    if not user or user.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403

    data = request.json

    mongo.db.tournaments.insert_one({
        "name": data["name"],
        "game": data["game"],
        "entry_fee": data["entry_fee"],
        "prize_pool": data["prize_pool"],
        "max_players": data.get("max_players", 100),
        "players": []
    })

    return jsonify({"message": "Tournament created"})


# ---------------- GET ALL TOURNAMENTS ----------------
@tournament.route("/all", methods=["GET"])
@jwt_required()
def get_tournaments():

    tournaments = []

    for t in mongo.db.tournaments.find():
        tournaments.append({
            "id": str(t["_id"]),
            "name": t.get("name"),
            "game": t.get("game"),
            "entry_fee": t.get("entry_fee"),
            "prize_pool": t.get("prize_pool"),
            "players": t.get("players", []),
            "max_players": t.get("max_players", 100)
        })

    return jsonify(tournaments)


# ---------------- SINGLE TOURNAMENT ----------------
@tournament.route("/<tournament_id>", methods=["GET"])
@jwt_required()
def get_tournament(tournament_id):

    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})

    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    return jsonify({
        "id": str(t["_id"]),
        "name": t.get("name"),
        "game": t.get("game"),
        "entry_fee": t.get("entry_fee"),
        "prize_pool": t.get("prize_pool"),
        "players": t.get("players", []),
        "max_players": t.get("max_players", 100)
    })


# ---------------- REGISTER (GENERATE PAYMENT CODE) ----------------
@tournament.route("/register/<tournament_id>", methods=["POST"])
@jwt_required()
def register_tournament(tournament_id):

    user_email = get_jwt_identity()

    existing = mongo.db.registrations.find_one({
        "user_id": user_email,
        "tournament_id": ObjectId(tournament_id)
    })

    # Return existing code if already registered
    if existing:
        return jsonify({
            "payment_code": existing["payment_code"],
            "registration_id": str(existing["_id"])
        })

    code = generate_payment_code()

    registration = {
        "user_id": user_email,
        "tournament_id": ObjectId(tournament_id),
        "payment_code": code,
        "payment_status": "pending",
        "utr": None,
        "screenshot": None
    }

    result = mongo.db.registrations.insert_one(registration)

    return jsonify({
        "payment_code": code,
        "registration_id": str(result.inserted_id)
    })


# ---------------- UPLOAD PAYMENT ----------------
@tournament.route("/upload-payment/<registration_id>", methods=["POST"])
@jwt_required()
def upload_payment(registration_id):

    file = request.files["file"]
    utr = request.form.get("utr")

    filename = secure_filename(file.filename)

    if not os.path.exists("uploads"):
        os.makedirs("uploads")

    path = f"uploads/{filename}"

    file.save(path)

    mongo.db.registrations.update_one(
        {"_id": ObjectId(registration_id)},
        {"$set": {
            "utr": utr,
            "screenshot": path
        }}
    )

    return jsonify({"message": "Payment proof uploaded"})


# ---------------- ADMIN - PENDING PAYMENTS ----------------
@tournament.route("/admin/pending-payments", methods=["GET"])
@jwt_required()
def pending_payments():

    email = get_jwt_identity()

    user = mongo.db.users.find_one({"email": email})

    if not user or user.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    pending = list(mongo.db.registrations.find({"payment_status": "pending"}))

    for p in pending:
        p["_id"] = str(p["_id"])
        p["tournament_id"] = str(p["tournament_id"])

    return jsonify(pending)


# ---------------- ADMIN - APPROVE PAYMENT ----------------
@tournament.route("/admin/approve/<registration_id>", methods=["POST"])
@jwt_required()
def approve_payment(registration_id):

    email = get_jwt_identity()

    user = mongo.db.users.find_one({"email": email})

    if not user or user.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    reg = mongo.db.registrations.find_one({"_id": ObjectId(registration_id)})

    if not reg:
        return jsonify({"error": "Registration not found"}), 404

    mongo.db.registrations.update_one(
        {"_id": ObjectId(registration_id)},
        {"$set": {"payment_status": "approved"}}
    )

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(reg["tournament_id"])},
        {"$push": {"players": reg["user_id"]}}
    )

    return jsonify({"message": "Payment Approved"})


# ---------------- ADMIN - REJECT PAYMENT ----------------
@tournament.route("/admin/reject/<registration_id>", methods=["POST"])
@jwt_required()
def reject_payment(registration_id):

    email = get_jwt_identity()

    user = mongo.db.users.find_one({"email": email})

    if not user or user.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    mongo.db.registrations.update_one(
        {"_id": ObjectId(registration_id)},
        {"$set": {"payment_status": "rejected"}}
    )

    return jsonify({"message": "Payment Rejected"})

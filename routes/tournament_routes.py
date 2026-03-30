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

    from datetime import datetime

    user_id = get_jwt_identity()

    user = mongo.db.users.find_one({
        "_id": ObjectId(user_id)
    })

    # 🔒 Admin check
    if not user or user.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403

    data = request.json

    name = data.get("name")
    game = data.get("game")
    entry_fee = data.get("entry_fee")
    prize_pool = data.get("prize_pool")
    max_players = data.get("max_players", 100)

    # ✅ Validation
    if not name or not entry_fee or not prize_pool:
        return jsonify({"error": "Missing required fields"}), 400

    try:
        entry_fee = int(entry_fee)
        prize_pool = int(prize_pool)
        max_players = int(max_players)
    except:
        return jsonify({"error": "Invalid number format"}), 400

    mongo.db.tournaments.insert_one({
        "name": name,
        "game": game,
        "entry_fee": entry_fee,
        "prize_pool": prize_pool,
        "max_players": max_players,
        "players": [],

        # room system
        "room_id": None,
        "room_password": None,
        "match_start_time": None,

        # metadata
        "created_at": datetime.utcnow()
    })

    return jsonify({"message": "Tournament created successfully"})


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

    # agar pehle se code hai
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
        {"$set":{
            "utr": utr,
            "screenshot": path
        }}
    )

    return jsonify({"message":"Payment proof uploaded"})


# ---------------- ADMIN - PENDING PAYMENTS ----------------
@tournament.route("/admin/pending-payments", methods=["GET"])
@jwt_required()
def pending_payments():

    user_id = get_jwt_identity()

    user = mongo.db.users.find_one({
        "_id": ObjectId(user_id)
    })

    if not user or user.get("role") != "admin":
        return jsonify({"error":"Unauthorized"}),403

    pending = list(mongo.db.registrations.find({
        "payment_status":"pending"
    }))

    for p in pending:
        p["_id"] = str(p["_id"])
        p["tournament_id"] = str(p["tournament_id"])

    return jsonify(pending)


# ---------------- ADMIN - APPROVE PAYMENT ----------------
@tournament.route("/admin/approve/<registration_id>", methods=["POST"])
@jwt_required()
def approve_payment(registration_id):

    user_id = get_jwt_identity()

    user = mongo.db.users.find_one({
    "_id": ObjectId(user_id)
})

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

    user_id = get_jwt_identity()

    user = mongo.db.users.find_one({
    "_id": ObjectId(user_id)
})

    if not user or user.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    mongo.db.registrations.update_one(
        {"_id": ObjectId(registration_id)},
        {"$set": {"payment_status": "rejected"}}
    )

    return jsonify({"message": "Payment Rejected"})


# ---------------- MY TOURNAMENTS ----------------
@tournament.route("/my-tournaments", methods=["GET"])
@jwt_required()
def my_tournaments():

    email = get_jwt_identity()

    registrations = list(mongo.db.registrations.find({
        "user_id": email
    }))

    data = []

    for r in registrations:

        tournament = mongo.db.tournaments.find_one({
            "_id": ObjectId(r["tournament_id"])
        })

        if tournament:

            data.append({
                "id": str(tournament["_id"]),
                "name": tournament["name"],
                "game": tournament["game"],
                "entry_fee": tournament["entry_fee"],
                "prize_pool": tournament["prize_pool"],
                "status": r["payment_status"]
            })

    return jsonify(data)

@tournament.route("/admin/release-room/<tournament_id>", methods=["POST"])
@jwt_required()
def release_room(tournament_id):

    user_id = get_jwt_identity()

    user = mongo.db.users.find_one({
        "_id": ObjectId(user_id)
    })

    if not user or user.get("role") != "admin":
        return jsonify({"error":"Unauthorized"}),403

    data = request.json

    room_id = data.get("room_id")
    password = data.get("password")
    start_time = data.get("start_time")

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {"$set":{
            "room_id": room_id,
            "room_password": password,
            "match_start_time": start_time
        }}
    )

    return jsonify({"message":"Room released successfully"})

@tournament.route("/room/<tournament_id>", methods=["GET"])
@jwt_required()
def get_tournament_room(tournament_id):

    user_id = get_jwt_identity()

    registration = mongo.db.registrations.find_one({
        "user_id": user_id,
        "tournament_id": ObjectId(tournament_id),
        "payment_status": "approved"
    })

    if not registration:
        return jsonify({"error":"Not approved"}),403

    tournament = mongo.db.tournaments.find_one({
        "_id": ObjectId(tournament_id)
    })

    match_time = tournament.get("match_start_time")

    return jsonify({
        "room_id": tournament.get("room_id"),
        "room_password": tournament.get("room_password"),
        "match_start_time": match_time.isoformat() if match_time else None
    })

@tournament.route("/admin/declare-winner", methods=["POST"])
@jwt_required()
def declare_winner():

    user_id = get_jwt_identity()

    user = mongo.db.users.find_one({
        "_id": ObjectId(user_id)
    })

    # 🔒 Admin check
    if not user or user.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json

    tournament_id = data.get("tournament_id")
    winner_id = data.get("winner_id")

    if not tournament_id or not winner_id:
        return jsonify({"error": "Missing fields"}), 400

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {
            "$set": {
                "winner_id": winner_id
            }
        }
    )

    return jsonify({"message": "Winner declared successfully"})

@tournament.route("/participants/<tournament_id>", methods=["GET"])
@jwt_required()
def get_participants(tournament_id):

    registrations = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": "approved"
    })

    participants = []

    for r in registrations:
        user = mongo.db.users.find_one({
            "_id": ObjectId(r["user_id"])
        })

        participants.append({
            "user_id": r["user_id"],
            "username": user.get("username")
        })

    return jsonify(participants)





print("LOADED TOURNAMENT_ROUTES FROM:", __file__)
from flask import Blueprint, request, jsonify
from models.tournament_model import create_tournament
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from datetime import datetime
from utils.code_generator import generate_payment_code
from werkzeug.utils import secure_filename
import os




tournament = Blueprint("tournament", __name__)

mongo = None


def init_tournament_routes(app, mongo_instance):
    global mongo
    mongo = mongo_instance


# -------- CREATE TOURNAMENT --------
@tournament.route("/create_tournament", methods=["POST", "OPTIONS"])
def create_tournament():
    data = request.json

    mongo.db.tournaments.insert_one({
        "game": data["game"],
        "mode": data["mode"],
        "entry_fee": data["entry_fee"],
        "prize_pool": data["prize_pool"],
        "slots": data["slots"],
        "date": data["date"],
        "players": []
    })

    return jsonify({"message": "Tournament created"}), 201


# -------- GET ALL TOURNAMENTS --------
@tournament.route("/tournaments", methods=["GET", "OPTIONS"])
@jwt_required()
def get_tournaments():
    all_tournaments = []

    for t in mongo.db.tournaments.find():
        all_tournaments.append({
            "id": str(t["_id"]),
            "game": t["game"],
            "mode": t["mode"],
            "entry_fee": t["entry_fee"],
            "prize_pool": t["prize_pool"],
            "slots": t["slots"],
            "date": t["date"],
            "players_joined": len(t.get("players", []))
        })

    return jsonify(all_tournaments)


# -------- JOIN TOURNAMENT --------
@tournament.route("/join_tournament/<tournament_id>", methods=["POST", "OPTIONS"])
@jwt_required()
def join_tournament(tournament_id):

    user_id = get_jwt_identity()
    tour = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})

    if not tour:
        return jsonify({"error": "Tournament not found"}), 404

    players = tour.get("players", [])

    if user_id in players:
        return jsonify({"error": "Already joined"}), 400

    if len(players) >= int(tour["slots"]):
        return jsonify({"error": "Slots full"}), 400

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {"$push": {"players": user_id}}
    )

    return jsonify({"message": "Joined successfully"})



    # UPLOAD PAYMENT PROOF
    @tournament.route("/upload-payment-proof/<registration_id>", methods=["POST", "OPTIONS"])
    @jwt_required()
    def upload_payment(registration_id):

        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files["file"]
        utr = request.form.get("utr")

        if not utr:
            return jsonify({"error": "UTR required"}), 400

        filename = secure_filename(file.filename)
        filepath = os.path.join("uploads", filename)
        file.save(filepath)

        mongo.db.registrations.update_one(
            {"_id": ObjectId(registration_id)},
            {"$set": {
                "screenshot": filepath,
                "utr": utr,
                "payment_status": "verification_pending"
            }}
        )

        return jsonify({"message": "Payment proof uploaded, waiting for verification"})

    # ADMIN - VIEW PENDING PAYMENTS
    @tournament.route("/admin/pending-payments", methods=["GET", "OPTIONS"])
    def pending_payments():
        pending = list(mongo.db.registrations.find({"payment_status": "verification_pending"}))

        for p in pending:
            p["_id"] = str(p["_id"])
            p["user_id"] = str(p["user_id"])
            p["tournament_id"] = str(p["tournament_id"])

        return jsonify(pending)

    # ADMIN - VERIFY PAYMENT
    @tournament.route("/admin/verify-payment/<registration_id>", methods=["POST", "OPTIONS"])
    def verify_payment(registration_id):

        registrations = mongo.db.registrations
        tournaments = mongo.db.tournaments

        reg = registrations.find_one({"_id": ObjectId(registration_id)})

        if not reg:
            return jsonify({"error": "Registration not found"}), 404

        registrations.update_one(
            {"_id": ObjectId(registration_id)},
            {"$set": {"payment_status": "approved"}}
        )

        tournaments.update_one(
            {"_id": ObjectId(reg["tournament_id"])},
            {"$inc": {"filled_slots": 1}}
        )

        return jsonify({"message": "Payment approved & slot confirmed"})

    # ADMIN - RELEASE ROOM
    @tournament.route("/admin/release-room/<tournament_id>", methods=["POST", "OPTIONS"])
    def release_room(tournament_id):

        data = request.json
        room_id = data.get("room_id")
        room_password = data.get("room_password")

        if not room_id or not room_password:
            return jsonify({"error": "Room details required"}), 400

        mongo.db.tournaments.update_one(
            {"_id": ObjectId(tournament_id)},
            {"$set": {
                "room_id": room_id,
                "room_password": room_password,
                "status": "live"
            }}
        )

        return jsonify({"message": "Room released successfully"})

    # PLAYER - VIEW MATCH DETAILS
    @tournament.route("/my-match/<tournament_id>", methods=["GET", "OPTIONS"])
    @jwt_required()
    def view_match(tournament_id):

        user_id = get_jwt_identity()

        registration = mongo.db.registrations.find_one({
            "user_id": user_id,
            "tournament_id": tournament_id,
            "payment_status": "approved"
        })

        if not registration:
            return jsonify({"error": "Not eligible or payment not approved"}), 403

        tournament_data = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})

        if not tournament_data or tournament_data["status"] != "live":
            return jsonify({"error": "Room not released yet"}), 400

        return jsonify({
            "room_id": tournament_data["room_id"],
            "room_password": tournament_data["room_password"]
        })

    app.register_blueprint(tournament)


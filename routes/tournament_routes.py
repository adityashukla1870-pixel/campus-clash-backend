from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from bson.errors import InvalidId
from werkzeug.utils import secure_filename
from utils.code_generator import generate_payment_code
from routes.notification_routes import create_notification
from functools import wraps
import os
import random

tournament = Blueprint("tournament", __name__)
mongo = None


def init_tournament_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


# ---------------- SHARED HELPERS ----------------

def get_current_user():
    """Fetch the logged-in user's document, or None if it can't be resolved."""
    user_id = get_jwt_identity()
    try:
        return mongo.db.users.find_one({"_id": ObjectId(user_id)})
    except (InvalidId, TypeError):
        return None


def admin_required(fn):
    """Decorator: blocks the route unless the caller is a logged-in admin."""
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


# ---------------- BRACKET HELPERS ----------------

def _build_bracket_rounds(entrants):
    """Builds a single-elimination bracket (list of rounds) from a list of entrants.
    Each entrant is a dict: {registration_id, name, user_id}. Byes are auto-resolved."""

    size = 1
    while size < len(entrants):
        size *= 2

    slots = list(entrants) + [None] * (size - len(entrants))
    random.shuffle(slots)

    rounds = []
    round1 = []
    for i in range(0, size, 2):
        round1.append({
            "match_id": f"r1m{i // 2 + 1}",
            "a": slots[i],
            "b": slots[i + 1],
            "winner": None
        })
    rounds.append(round1)

    num_matches = size // 2
    r = 2
    while num_matches > 1:
        num_matches //= 2
        rounds.append([
            {"match_id": f"r{r}m{j + 1}", "a": None, "b": None, "winner": None}
            for j in range(num_matches)
        ])
        r += 1

    _resolve_byes(rounds)
    return rounds


def _resolve_byes(rounds):
    """Auto-advances any entrant whose opponent slot is empty (a bye), propagating
    the result forward through the bracket until no more auto-advances are possible."""
    changed = True
    while changed:
        changed = False
        for ridx, rnd in enumerate(rounds):
            for midx, m in enumerate(rnd):
                if m["winner"] is None:
                    if m["a"] is not None and m["b"] is None:
                        m["winner"] = m["a"]
                        changed = True
                    elif m["b"] is not None and m["a"] is None:
                        m["winner"] = m["b"]
                        changed = True

                if m["winner"] is not None and ridx + 1 < len(rounds):
                    next_match = rounds[ridx + 1][midx // 2]
                    slot_key = "a" if midx % 2 == 0 else "b"
                    if next_match[slot_key] is None:
                        next_match[slot_key] = m["winner"]
                        changed = True


# ---------------- PAYMENT CODE GENERATOR ----------------



# ---------------- CREATE TOURNAMENT ----------------
@tournament.route("/create", methods=["POST"])
@admin_required
def create_tournament():

    from datetime import datetime

    data = request.json

    name = data.get("name")
    game = data.get("game")
    entry_fee = data.get("entry_fee")
    prize_pool = data.get("prize_pool")
    max_players = data.get("max_players", 100)
    mode = data.get("mode", "solo")          # "solo" or "squad"
    team_size = data.get("team_size", 1)     # only relevant for squad mode
    format_ = data.get("format", "quick")    # "quick" (single match) or "full" (multi-stage)
    points_table = data.get("points_table") or {"1": 10, "2": 6, "3": 5, "4": 4, "5": 3, "6": 2, "7": 2, "8": 1, "9": 1}
    kill_point_value = data.get("kill_point_value", 1)

    # ✅ Validation
    if not name or not entry_fee or not prize_pool:
        return jsonify({"error": "Missing required fields"}), 400

    if mode not in ("solo", "squad"):
        return jsonify({"error": "Invalid mode"}), 400

    if format_ not in ("quick", "full"):
        return jsonify({"error": "Invalid format"}), 400

    try:
        entry_fee = int(entry_fee)
        prize_pool = int(prize_pool)
        max_players = int(max_players)
        team_size = int(team_size) if mode == "squad" else 1
    except:
        return jsonify({"error": "Invalid number format"}), 400

    if mode == "squad" and team_size < 2:
        return jsonify({"error": "Squad team size must be at least 2"}), 400

    mongo.db.tournaments.insert_one({
        "name": name,
        "game": game,
        "entry_fee": entry_fee,
        "prize_pool": prize_pool,
        "max_players": max_players,
        "players": [],
        "mode": mode,
        "team_size": team_size,
        "format": format_,
        "points_table": points_table,
        "kill_point_value": kill_point_value,

        # room system
        "room_id": None,
        "room_password": None,
        "match_start_time": None,

        # bracket / results
        "bracket": None,
        "winner_id": None,
        "status": "upcoming",

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
            "max_players": t.get("max_players", 100),
            "mode": t.get("mode", "solo"),
            "team_size": t.get("team_size", 1),
            "status": t.get("status", "upcoming"),
            "format": t.get("format", "quick"),
            "has_bracket": bool(t.get("bracket"))
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
        "max_players": t.get("max_players", 100),
        "mode": t.get("mode", "solo"),
        "team_size": t.get("team_size", 1),
        "status": t.get("status", "upcoming"),
        "format": t.get("format", "quick"),
        "points_table": t.get("points_table"),
        "kill_point_value": t.get("kill_point_value", 1),
        "has_bracket": bool(t.get("bracket"))
    })


# ---------------- REGISTER (GENERATE PAYMENT CODE / TEAM INFO) ----------------
@tournament.route("/register/<tournament_id>", methods=["POST"])
@jwt_required()

def register_tournament(tournament_id):

    user_id = get_jwt_identity()

    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    data = request.get_json(silent=True) or {}
    team_name = data.get("team_name")
    team_members = data.get("team_members", [])

    if t.get("mode") == "squad" and not team_name:
        return jsonify({"error": "Team name is required for this tournament"}), 400

    existing = mongo.db.registrations.find_one({
        "user_id": user_id,
        "tournament_id": ObjectId(tournament_id)
    })

    # agar pehle se code hai aur reject nahi hua, wahi return karo
    if existing and existing.get("payment_status") != "rejected":
        return jsonify({
            "payment_code": existing["payment_code"],
            "registration_id": str(existing["_id"])
        })

    code = generate_payment_code()

    registration = {
        "user_id": user_id,
        "tournament_id": ObjectId(tournament_id),
        "payment_code": code,
        "payment_status": "pending",
        "utr": None,
        "screenshot": None,
        "team_name": team_name,
        "team_members": team_members
    }

    if existing:
        # re-register after a rejected payment
        mongo.db.registrations.update_one(
            {"_id": existing["_id"]},
            {"$set": registration}
        )
        registration_id = str(existing["_id"])
    else:
        result = mongo.db.registrations.insert_one(registration)
        registration_id = str(result.inserted_id)

    return jsonify({
        "payment_code": code,
        "registration_id": registration_id
    })

# ---------------- UPLOAD PAYMENT ----------------
@tournament.route("/upload-payment/<registration_id>", methods=["POST"])
@jwt_required()
def upload_payment(registration_id):

    file = request.files.get("file")
    utr = request.form.get("utr")

    if not file or not file.filename:
        return jsonify({"error": "Payment screenshot is required"}), 400

    if not utr:
        return jsonify({"error": "UTR / reference number is required"}), 400

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
@admin_required
def pending_payments():

    # Only show registrations where the user has actually submitted proof
    # (UTR + screenshot). A registration that only reserved a payment code
    # but never paid has nothing for the admin to verify yet.
    pending = list(mongo.db.registrations.find({
        "payment_status": "pending",
        "utr": {"$ne": None},
        "screenshot": {"$ne": None}
    }))

    for p in pending:
        p["_id"] = str(p["_id"])
        p["tournament_id_raw"] = p["tournament_id"]
        p["tournament_id"] = str(p["tournament_id"])

        user = mongo.db.users.find_one({"_id": safe_object_id(p.get("user_id"))})
        p["player_name"] = user.get("name") if user else "Unknown"
        p["player_email"] = user.get("email") if user else None

        t = mongo.db.tournaments.find_one({"_id": p["tournament_id_raw"]})
        p["tournament_name"] = t.get("name") if t else "Unknown Tournament"
        p["entry_fee"] = t.get("entry_fee") if t else None
        del p["tournament_id_raw"]

    return jsonify(pending)


# ---------------- ADMIN - APPROVE PAYMENT ----------------
@tournament.route("/admin/approve/<registration_id>", methods=["POST"])
@admin_required
def approve_payment(registration_id):

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

    t = mongo.db.tournaments.find_one({"_id": ObjectId(reg["tournament_id"])})
    create_notification(
        mongo,
        reg["user_id"],
        f"Your payment for \"{t.get('name') if t else 'a tournament'}\" was approved. You're in!",
        ntype="payment",
        tournament_id=reg["tournament_id"]
    )

    return jsonify({"message": "Payment Approved"})


# ---------------- ADMIN - REJECT PAYMENT ----------------
@tournament.route("/admin/reject/<registration_id>", methods=["POST"])
@admin_required
def reject_payment(registration_id):

    reg = mongo.db.registrations.find_one({"_id": ObjectId(registration_id)})

    mongo.db.registrations.update_one(
        {"_id": ObjectId(registration_id)},
        {"$set": {"payment_status": "rejected"}}
    )

    if reg:
        t = mongo.db.tournaments.find_one({"_id": safe_object_id(reg.get("tournament_id"))})
        create_notification(
            mongo,
            reg["user_id"],
            f"Your payment for \"{t.get('name') if t else 'a tournament'}\" was rejected. Please re-register with valid proof.",
            ntype="payment",
            tournament_id=reg.get("tournament_id")
        )

    return jsonify({"message": "Payment Rejected"})


# ---------------- MY TOURNAMENTS ----------------
@tournament.route("/my-tournaments", methods=["GET"])
@jwt_required()
def my_tournaments():

    user_id = get_jwt_identity()

    registrations = list(mongo.db.registrations.find({
        "user_id": user_id
    }))

    data = []

    for r in registrations:

        t = mongo.db.tournaments.find_one({
            "_id": ObjectId(r["tournament_id"])
        })

        if t:

            status = r["payment_status"]
            is_winner = False
            winner_name = None

            if t.get("winner_id"):
                status = "completed"
                is_winner = (t["winner_id"] == user_id)
                winner_user = mongo.db.users.find_one({"_id": safe_object_id(t["winner_id"])})
                winner_name = winner_user.get("name") if winner_user else "Unknown"

            data.append({
                "id": str(t["_id"]),
                "name": t["name"],
                "game": t["game"],
                "entry_fee": t["entry_fee"],
                "prize_pool": t["prize_pool"],
                "status": status,
                "is_winner": is_winner,
                "winner": winner_name,
                "format": t.get("format", "quick"),
                "has_bracket": bool(t.get("bracket")),
                "team_name": r.get("team_name")
            })

    return jsonify(data)

@tournament.route("/admin/release-room/<tournament_id>", methods=["POST"])
@admin_required
def release_room(tournament_id):

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

    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    approved = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": "approved"
    })
    for r in approved:
        create_notification(
            mongo,
            r["user_id"],
            f"Room details for \"{t.get('name') if t else 'your tournament'}\" are live. Check the room page!",
            ntype="room",
            tournament_id=tournament_id
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
@admin_required
def declare_winner():

    data = request.json

    tournament_id = data.get("tournament_id")
    winner_id = data.get("winner_id")

    if not tournament_id or not winner_id:
        return jsonify({"error": "Missing fields"}), 400

    approved = list(mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": "approved"
    }))

    if len(approved) < 2:
        return jsonify({"error": "Need at least 2 approved participants before a winner can be declared"}), 400

    if not any(r["user_id"] == winner_id for r in approved):
        return jsonify({"error": "winner_id must be an approved participant of this tournament"}), 400

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {
            "$set": {
                "winner_id": winner_id,
                "status": "completed"
            }
        }
    )

    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    tname = t.get("name") if t else "the tournament"

    create_notification(
        mongo,
        winner_id,
        f"🏆 Congratulations! You won \"{tname}\"!",
        ntype="winner",
        tournament_id=tournament_id
    )

    other_participants = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": "approved",
        "user_id": {"$ne": winner_id}
    })
    for r in other_participants:
        create_notification(
            mongo,
            r["user_id"],
            f"\"{tname}\" has ended. Check the bracket to see how it played out.",
            ntype="winner",
            tournament_id=tournament_id
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
            "_id": safe_object_id(r["user_id"])
        })

        display_name = r.get("team_name") or (user.get("name") if user else "Unknown")

        participants.append({
            "user_id": r["user_id"],
            "registration_id": str(r["_id"]),
            "name": display_name
        })

    return jsonify(participants)


# ---------------- ADMIN - GENERATE BRACKET ----------------
@tournament.route("/admin/generate-bracket/<tournament_id>", methods=["POST"])
@admin_required
def generate_bracket(tournament_id):

    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    if t.get("format") == "full":
        return jsonify({"error": "This tournament uses the multi-stage format — manage it from \"Manage Stages\" instead of the bracket tool."}), 400

    registrations = list(mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": "approved"
    }))

    if len(registrations) < 2:
        return jsonify({"error": "Need at least 2 approved participants to generate a bracket"}), 400

    entrants = []
    for r in registrations:
        user = mongo.db.users.find_one({"_id": safe_object_id(r["user_id"])})
        display_name = r.get("team_name") or (user.get("name") if user else "Unknown")
        entrants.append({
            "registration_id": str(r["_id"]),
            "user_id": r["user_id"],
            "name": display_name
        })

    rounds = _build_bracket_rounds(entrants)

    update_fields = {"bracket": {"rounds": rounds}, "status": "in_progress"}

    # if the whole bracket resolved instantly (e.g. only byes), mark tournament complete
    if len(rounds[-1]) == 1 and rounds[-1][0]["winner"]:
        update_fields["winner_id"] = rounds[-1][0]["winner"]["user_id"]
        update_fields["status"] = "completed"

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {"$set": update_fields}
    )

    return jsonify({"message": "Bracket generated successfully", "rounds": rounds})


# ---------------- GET BRACKET ----------------
@tournament.route("/bracket/<tournament_id>", methods=["GET"])
@jwt_required()
def get_bracket(tournament_id):

    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    return jsonify({
        "tournament_name": t.get("name"),
        "status": t.get("status", "upcoming"),
        "bracket": t.get("bracket"),
        "winner_id": t.get("winner_id")
    })


# ---------------- ADMIN - REPORT MATCH RESULT ----------------
@tournament.route("/admin/report-match/<tournament_id>", methods=["POST"])
@admin_required
def report_match(tournament_id):

    data = request.json
    round_index = data.get("round_index")
    match_index = data.get("match_index")
    winner_slot = data.get("winner_slot")  # "a" or "b"

    if round_index is None or match_index is None or winner_slot not in ("a", "b"):
        return jsonify({"error": "Missing or invalid fields"}), 400

    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    if not t or not t.get("bracket"):
        return jsonify({"error": "Bracket not found for this tournament"}), 404

    rounds = t["bracket"]["rounds"]

    try:
        match = rounds[round_index][match_index]
    except (IndexError, TypeError):
        return jsonify({"error": "Invalid round/match index"}), 400

    winner_entrant = match.get(winner_slot)
    if not winner_entrant:
        return jsonify({"error": "That slot has no entrant to declare as winner"}), 400

    match["winner"] = winner_entrant
    _resolve_byes(rounds)

    update_fields = {"bracket": {"rounds": rounds}}

    final_round = rounds[-1]
    if len(final_round) == 1 and final_round[0]["winner"]:
        update_fields["winner_id"] = final_round[0]["winner"]["user_id"]
        update_fields["status"] = "completed"

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {"$set": update_fields}
    )

    return jsonify({"message": "Match result recorded", "rounds": rounds})


# ---------------- LEADERBOARD ----------------
@tournament.route("/leaderboard", methods=["GET"])
@jwt_required()
def leaderboard():

    completed = mongo.db.tournaments.find({"winner_id": {"$ne": None}})

    stats = {}
    for t in completed:
        winner_id = t["winner_id"]
        entry = stats.setdefault(winner_id, {"wins": 0, "prize_won": 0, "tournaments": []})
        entry["wins"] += 1
        entry["prize_won"] += t.get("prize_pool", 0)
        entry["tournaments"].append(t.get("name"))

    leaderboard_data = []
    for user_id, entry in stats.items():
        user = mongo.db.users.find_one({"_id": safe_object_id(user_id)})
        leaderboard_data.append({
            "user_id": user_id,
            "name": user.get("name") if user else "Unknown",
            "college": user.get("college") if user else None,
            "wins": entry["wins"],
            "prize_won": entry["prize_won"],
            "tournaments": entry["tournaments"]
        })

    leaderboard_data.sort(key=lambda x: (-x["wins"], -x["prize_won"]))

    return jsonify(leaderboard_data)





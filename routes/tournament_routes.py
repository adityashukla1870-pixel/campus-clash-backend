from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from bson.errors import InvalidId
from werkzeug.utils import secure_filename
from utils.code_generator import generate_payment_code
from utils.cloud_storage import upload_image
from utils.time_utils import to_utc_iso
from routes.notification_routes import create_notification
from routes.player_stats_routes import upsert_player_stats, upsert_global_wins
from utils.tournament_lifecycle import build_winner_update, normalize_tournament_payload, is_registration_open
from datetime import datetime as _dt
from functools import wraps
import os
import random


def _format_deadline_iso(deadline):
    """Ensure deadline ISO string has UTC Z suffix for frontend compatibility."""
    if not deadline:
        return None
    if deadline.endswith("Z") or "+00:00" in deadline:
        return deadline
    if "T" in deadline:
        return deadline + "Z"
    return deadline


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
    import json

    # Form-data (not JSON) so we can also accept an optional banner image
    # file alongside the regular fields.
    data = request.form

    name = data.get("name")
    game = data.get("game")
    entry_fee = data.get("entry_fee")
    prize_pool = data.get("prize_pool")
    max_players = data.get("max_players", 100)
    mode = data.get("mode", "solo")          # "solo" or "squad"
    team_size = data.get("team_size", 1)     # only relevant for squad mode
    format_ = data.get("format", "quick")    # "quick" (single match) or "full" (multi-stage)
    structure = data.get("structure", "group_playoff")
    seed_strategy = data.get("seed_strategy", "random")

    # Two-timing launch architecture: participation deadline vs. the
    # tournament's own scheduled date/time.
    registration_end_time = data.get("registration_end_time") or None
    scheduled_time = data.get("scheduled_time") or None

    points_table_raw = data.get("points_table")
    if points_table_raw:
        try:
            points_table = json.loads(points_table_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid points_table format"}), 400
    else:
        points_table = {"1": 10, "2": 6, "3": 5, "4": 4, "5": 3, "6": 2, "7": 2, "8": 1, "9": 1}

    kill_point_value = data.get("kill_point_value", 1)

    # Prize split — {rank: percent}. Optional; falls back to a 50/30/20
    # top-3 split inside normalize_tournament_payload if not provided.
    prize_distribution_raw = data.get("prize_distribution")
    if prize_distribution_raw:
        try:
            prize_distribution = json.loads(prize_distribution_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid prize_distribution format"}), 400

        if not prize_distribution:
            return jsonify({"error": "prize_distribution cannot be empty"}), 400

        try:
            total_percent = sum(float(v) for v in prize_distribution.values())
        except (TypeError, ValueError):
            return jsonify({"error": "prize_distribution values must be numbers"}), 400

        if round(total_percent, 2) != 100:
            return jsonify({"error": f"prize_distribution percentages must add up to 100 (got {total_percent})"}), 400
    else:
        prize_distribution = None

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
        kill_point_value = int(kill_point_value)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid number format"}), 400

    if mode == "squad" and team_size < 2:
        return jsonify({"error": "Squad team size must be at least 2"}), 400

    # Optional banner image — uploaded to Cloudinary (not local disk) so it
    # survives Render's ephemeral filesystem across restarts/redeploys.
    banner_image = None
    banner_file = request.files.get("banner_image")
    if banner_file and banner_file.filename:
        try:
            banner_image = upload_image(banner_file, folder="campus-clash/banners")
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 500

    payload = normalize_tournament_payload({
        "name": name,
        "game": game,
        "entry_fee": entry_fee,
        "prize_pool": prize_pool,
        "prize_distribution": prize_distribution,
        "max_players": max_players,
        "mode": mode,
        "team_size": team_size,
        "format": format_,
        "points_table": points_table,
        "kill_point_value": kill_point_value,
        "structure": structure,
        "seed_strategy": seed_strategy,
        "registration_end_time": registration_end_time,
        "scheduled_time": scheduled_time,
    })
    payload.update({
        "players": [],
        "banner_image": banner_image,
        "created_at": datetime.utcnow(),
        "structure": structure,
        "seed_strategy": seed_strategy,
    })

    mongo.db.tournaments.insert_one(payload)

    return jsonify({"message": "Tournament created successfully"})


# ---------------- GET ALL TOURNAMENTS ----------------
@tournament.route("/all", methods=["GET"])
def get_tournaments():

    tournaments = []

    for t in mongo.db.tournaments.find():
        tournaments.append({
            "id": str(t["_id"]),
            "name": t.get("name"),
            "game": t.get("game"),
            "entry_fee": t.get("entry_fee"),
            "prize_pool": t.get("prize_pool"),
            "prize_distribution": t.get("prize_distribution"),
            "prize_breakdown": t.get("prize_breakdown"),
            "players": t.get("players", []),
            "max_players": t.get("max_players", 100),
            "mode": t.get("mode", "solo"),
            "team_size": t.get("team_size", 1),
            "status": t.get("status", "upcoming"),
            "format": t.get("format", "quick"),
            "structure": t.get("structure", "group_playoff"),
            "seed_strategy": t.get("seed_strategy", "random"),
            "registration_end_time": _format_deadline_iso(t.get("registration_end_time")),
            "scheduled_time": _format_deadline_iso(t.get("scheduled_time")),
            "registration_open": is_registration_open(t),
            "grouping_status": t.get("grouping_status", "pending"),
            "banner_image": t.get("banner_image"),
            "has_bracket": bool(t.get("bracket")),
            "winner_name": t.get("winner_name"),
            "winner_source": t.get("winner_source")
        })

    return jsonify(tournaments)


# ---------------- SINGLE TOURNAMENT ----------------
@tournament.route("/<tournament_id>", methods=["GET"])
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
        "prize_distribution": t.get("prize_distribution"),
        "prize_breakdown": t.get("prize_breakdown"),
        "players": t.get("players", []),
        "max_players": t.get("max_players", 100),
        "mode": t.get("mode", "solo"),
        "team_size": t.get("team_size", 1),
        "status": t.get("status", "upcoming"),
        "format": t.get("format", "quick"),
        "structure": t.get("structure", "group_playoff"),
        "seed_strategy": t.get("seed_strategy", "random"),
        "registration_end_time": _format_deadline_iso(t.get("registration_end_time")),
        "scheduled_time": _format_deadline_iso(t.get("scheduled_time")),
        "registration_open": is_registration_open(t),
        "grouping_status": t.get("grouping_status", "pending"),
        "banner_image": t.get("banner_image"),
        "points_table": t.get("points_table"),
        "kill_point_value": t.get("kill_point_value", 1),
        "has_bracket": bool(t.get("bracket")),
        "winner_name": t.get("winner_name"),
        "winner_registration_id": t.get("winner_registration_id"),
        "winner_source": t.get("winner_source")
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
    team_leader = data.get("team_leader") or {}
    if not isinstance(team_leader, dict):
        team_leader = {}

    if t.get("mode") == "squad":
        if not team_name:
            # fallback: use first team member's name as team name
            if team_members and isinstance(team_members, list) and len(team_members) > 0:
                if isinstance(team_members[0], dict):
                    team_name = team_members[0].get("name")
                else:
                    team_name = str(team_members[0])
            else:
                return jsonify({"error": "Team name is required for this tournament"}), 400

        if not team_leader.get("name") or not team_leader.get("contact") or not team_leader.get("game_uid"):
            return jsonify({"error": "Team leader name, game UID and contact number are required"}), 400

        if not isinstance(team_members, list) or len(team_members) == 0:
            return jsonify({"error": "Teammate details are required"}), 400
        for m in team_members:
            if not isinstance(m, dict) or not m.get("name") or not m.get("game_uid"):
                return jsonify({"error": "Name and game UID are required for every teammate"}), 400

    existing = mongo.db.registrations.find_one({
        "user_id": user_id,
        "tournament_id": ObjectId(tournament_id)
    })

    # Check if this user is already a teammate in someone else's registration
    if not existing and t.get("mode") == "squad":
        already_teammate = mongo.db.registrations.find_one({
            "tournament_id": ObjectId(tournament_id),
            "team_members.user_id": user_id
        })
        if already_teammate:
            return jsonify({"error": "You are already registered as a teammate in another team for this tournament"}), 400

    # Registration deadline only blocks brand-new sign-ups — someone who
    # already has a pending/approved registration can still come back to
    # this endpoint to fetch their payment code.
    if not existing and not is_registration_open(t):
        return jsonify({"error": "Registration for this tournament has closed"}), 400

    # agar pehle se code hai aur reject nahi hua, wahi return karo
    if existing and existing.get("payment_status") != "rejected":
        return jsonify({
            "payment_code": existing["payment_code"],
            "registration_id": str(existing["_id"])
        })

    # Validate usernames for squad mode
    if t.get("mode") == "squad" and team_members:
        leader = mongo.db.users.find_one({"_id": safe_object_id(user_id)})
        if not leader:
            return jsonify({"error": "Leader user not found"}), 400

        seen_user_ids = {user_id}
        validated_members = []

        for member in team_members:
            if not isinstance(member, dict):
                return jsonify({"error": "Team members must be objects with username, name, and game_uid"}), 400

            member_username = (member.get("username") or "").strip().lower()
            if not member_username:
                return jsonify({"error": f"Username is required for teammate: {member.get('name', 'Unknown')}"}), 400

            member_user = mongo.db.users.find_one({"username": member_username})
            if not member_user:
                return jsonify({"error": f"No user found with username '{member_username}'"}), 400

            member_user_id = str(member_user["_id"])
            if member_user_id in seen_user_ids:
                return jsonify({"error": f"Duplicate player: {member_username}"}), 400
            seen_user_ids.add(member_user_id)

            validated_members.append({
                "user_id": member_user_id,
                "username": member_username,
                "name": member.get("name") or member_user.get("name", ""),
                "game_uid": member.get("game_uid") or member_user.get("game_uid", "")
            })

        team_members = validated_members

    code = generate_payment_code()

    registration = {
        "user_id": user_id,
        "tournament_id": ObjectId(tournament_id),
        "payment_code": code,
        "payment_status": "pending",
        "utr": None,
        "screenshot": None,
        "team_name": team_name,
        "team_members": team_members,
        "team_leader": {
            "name": team_leader.get("name"),
            "game_uid": team_leader.get("game_uid"),
            "contact": team_leader.get("contact")
        } if t.get("mode") == "squad" else None
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

    # --- Create individual registrations for each teammate + send notifications ---
    if t.get("mode") == "squad" and team_members:
        for member in team_members:
            member_user_id = member.get("user_id")
            if not member_user_id or member_user_id == user_id:
                continue

            # Check if teammate already has a registration for this tournament
            existing_member_reg = mongo.db.registrations.find_one({
                "user_id": member_user_id,
                "tournament_id": ObjectId(tournament_id)
            })
            if existing_member_reg:
                continue

            teammate_registration = {
                "user_id": member_user_id,
                "tournament_id": ObjectId(tournament_id),
                "payment_code": code,
                "payment_status": "teammate",
                "utr": None,
                "screenshot": None,
                "team_name": team_name,
                "team_members": team_members,
                "team_leader": {
                    "name": team_leader.get("name"),
                    "game_uid": team_leader.get("game_uid"),
                    "contact": team_leader.get("contact")
                }
            }
            mongo.db.registrations.insert_one(teammate_registration)

            leader_name = team_leader.get("name") or "Your team leader"
            create_notification(
                mongo,
                member_user_id,
                f'You\'ve been added to team "{team_name}" for "{t.get("name", "a tournament")}" by {leader_name}. Check My Matches!',
                ntype="info",
                tournament_id=str(tournament_id)
            )

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

    try:
        screenshot_url = upload_image(file, folder="campus-clash/payments")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    mongo.db.registrations.update_one(
        {"_id": ObjectId(registration_id)},
        {"$set":{
            "utr": utr,
            "screenshot": screenshot_url
        }}
    )

    return jsonify({"message":"Payment proof uploaded"})


# ---------------- UPLOAD INSTAGRAM FOLLOW PROOF ----------------
@tournament.route("/upload-ig-proof/<registration_id>", methods=["POST"])
@jwt_required()
def upload_ig_proof(registration_id):

    files = request.files.getlist("files")
    if not files or len(files) == 0:
        return jsonify({"error": "At least one screenshot is required"}), 400

    if len(files) > 4:
        return jsonify({"error": "Maximum 4 screenshots allowed"}), 400

    screenshot_urls = []
    for f in files:
        if not f or not f.filename:
            continue
        try:
            url = upload_image(f, folder="campus-clash/ig-proof")
            screenshot_urls.append(url)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 500

    if not screenshot_urls:
        return jsonify({"error": "No valid screenshots uploaded"}), 400

    mongo.db.registrations.update_one(
        {"_id": ObjectId(registration_id)},
        {"$set": {
            "ig_screenshots": screenshot_urls,
            "payment_status": "pending"
        }}
    )

    return jsonify({"message": "Instagram proof uploaded", "count": len(screenshot_urls)})


# ---------------- ADMIN - FINAL PARTICIPANT LIST (after registration closes) ----------------
@tournament.route("/admin/<tournament_id>/final-participants", methods=["GET"])
@admin_required
def final_participants(tournament_id):
    """Approved roster for a tournament — used by the admin to review the
    locked-in field before grouping, and to download it as CSV."""
    t = mongo.db.tournaments.find_one({"_id": safe_object_id(tournament_id)})
    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    registrations = list(mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": "approved"
    }))

    roster = []
    for r in registrations:
        user = mongo.db.users.find_one({"_id": safe_object_id(r.get("user_id"))})
        roster.append({
            "registration_id": str(r["_id"]),
            "user_id": r.get("user_id"),
            "player_name": user.get("name") if user else "Unknown",
            "player_email": user.get("email") if user else None,
            "team_name": r.get("team_name"),
            "team_leader": r.get("team_leader"),
            "team_members": r.get("team_members", []),
        })

    return jsonify({
        "tournament_name": t.get("name"),
        "registration_open": is_registration_open(t),
        "registration_end_time": t.get("registration_end_time"),
        "grouping_status": t.get("grouping_status", "pending"),
        "count": len(roster),
        "participants": roster
    })


# ---------------- ADMIN - APPROVED TEAMS FOR SLOT ASSIGNMENT ----------------
@tournament.route("/admin/<tournament_id>/approved-teams", methods=["GET"])
@admin_required
def approved_teams(tournament_id):
    """Get approved teams for slot assignment in custom lobby."""
    t = mongo.db.tournaments.find_one({"_id": safe_object_id(tournament_id)})
    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    registrations = list(mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": "approved"
    }))

    teams = []
    for r in registrations:
        user = mongo.db.users.find_one({"_id": safe_object_id(r.get("user_id"))})
        teams.append({
            "registration_id": str(r["_id"]),
            "user_id": r.get("user_id"),
            "team_name": r.get("team_name") or (user.get("name") if user else "Unknown"),
            "team_leader": r.get("team_leader"),
            "team_members": r.get("team_members", []),
        })

    return jsonify({
        "tournament_name": t.get("name"),
        "teams": teams
    })


@tournament.route("/admin/<tournament_id>/final-participants.csv", methods=["GET"])
@admin_required
def final_participants_csv(tournament_id):
    import csv
    import io
    from flask import Response

    t = mongo.db.tournaments.find_one({"_id": safe_object_id(tournament_id)})
    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    registrations = list(mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": "approved"
    }))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Registration ID", "Player Name", "Email", "Team Name",
        "Team Leader Name", "Team Leader UID", "Team Leader Contact",
        "Team Members"
    ])

    for r in registrations:
        user = mongo.db.users.find_one({"_id": safe_object_id(r.get("user_id"))})
        members = ", ".join(m.get("name", "") for m in r.get("team_members", []))
        leader = r.get("team_leader") or {}
        writer.writerow([
            str(r["_id"]),
            user.get("name") if user else "Unknown",
            user.get("email") if user else "",
            r.get("team_name") or "",
            leader.get("name") or "",
            leader.get("game_uid") or "",
            leader.get("contact") or "",
            members
        ])

    filename = f"{(t.get('name') or 'tournament').replace(' ', '_')}_final_list.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ---------------- ADMIN - PENDING PAYMENTS ----------------
@tournament.route("/admin/pending-payments", methods=["GET"])
@admin_required
def pending_payments():

    # Show registrations where the user has submitted proof:
    # - Payment-based: UTR + screenshot (paid tournaments)
    # - IG-proof: ig_screenshots array (free tournaments)
    pending = list(mongo.db.registrations.find({
        "payment_status": "pending",
        "$or": [
            {"utr": {"$ne": None}, "screenshot": {"$ne": None}},
            {"ig_screenshots": {"$ne": None, "$not": {"$size": 0}}}
        ]
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


# ---------------- ADMIN - APPROVED PAYMENTS (persistent record) ----------------
@tournament.route("/admin/approved-payments", methods=["GET"])
@admin_required
def approved_payments():
    """Everything the admin has already approved, across all tournaments.
    Kept separate from pending-payments so approved registrations (and all
    the team/leader data submitted at registration) stay visible and don't
    just vanish from the admin panel once acted on."""

    approved = list(mongo.db.registrations.find({
        "payment_status": "approved"
    }).sort("_id", -1))

    for p in approved:
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

    return jsonify(approved)


# ---------------- ADMIN - ALL REGISTRATIONS ----------------
@tournament.route("/admin/all-registrations", methods=["GET"])
@admin_required
def all_registrations():
    """Every registration across all tournaments, regardless of proof status."""

    regs = list(mongo.db.registrations.find().sort("_id", -1))

    for p in regs:
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

    return jsonify(regs)


# ---------------- ADMIN - INCOMPLETE REGISTRATIONS ----------------
@tournament.route("/admin/incomplete-registrations", methods=["GET"])
@admin_required
def incomplete_registrations():
    """Registrations where user registered but never uploaded proof.
    These are leaders with payment_status=pending and no screenshot/ig_screenshots.
    Admin can call them to remind about completing registration."""

    regs = list(mongo.db.registrations.find({
        "payment_status": "pending",
        "$or": [
            {"screenshot": None, "ig_screenshots": None},
            {"screenshot": {"$exists": False}, "ig_screenshots": {"$exists": False}},
        ]
    }).sort("_id", -1))

    result = []
    for p in regs:
        # Only include leader registrations (not teammate copies)
        if p.get("team_members") and p.get("user_id"):
            # Check if this is the leader's registration
            leader_uid = p.get("user_id")
            # Get leader's contact from team_leader object
            team_leader = p.get("team_leader") or {}
            contact = team_leader.get("contact")
            leader_name = team_leader.get("name")

            # Fetch user for email
            user = mongo.db.users.find_one({"_id": safe_object_id(leader_uid)})
            player_name = user.get("name") if user else leader_name or "Unknown"
            player_email = user.get("email") if user else None
            username = user.get("username") if user else None

            t = mongo.db.tournaments.find_one({"_id": p["tournament_id"]})
            result.append({
                "_id": str(p["_id"]),
                "tournament_name": t.get("name") if t else "Unknown",
                "game": t.get("game") if t else "",
                "entry_fee": t.get("entry_fee") if t else 0,
                "team_name": p.get("team_name"),
                "player_name": player_name,
                "player_email": player_email,
                "username": username,
                "contact": contact,
                "payment_code": p.get("payment_code"),
                "registered_at": str(p.get("_id").generation_time) if p.get("_id") else None,
            })
        else:
            # Solo registration
            user = mongo.db.users.find_one({"_id": safe_object_id(p.get("user_id"))})
            player_name = user.get("name") if user else "Unknown"
            player_email = user.get("email") if user else None
            username = user.get("username") if user else None

            t = mongo.db.tournaments.find_one({"_id": p["tournament_id"]})
            result.append({
                "_id": str(p["_id"]),
                "tournament_name": t.get("name") if t else "Unknown",
                "game": t.get("game") if t else "",
                "entry_fee": t.get("entry_fee") if t else 0,
                "team_name": None,
                "player_name": player_name,
                "player_email": player_email,
                "username": username,
                "contact": None,
                "payment_code": p.get("payment_code"),
                "registered_at": str(p.get("_id").generation_time) if p.get("_id") else None,
            })

    return jsonify(result)


# ---------------- ADMIN - APPROVE PAYMENT ----------------
@tournament.route("/admin/approve/<registration_id>", methods=["POST"])
@admin_required
def approve_payment(registration_id):

    reg = mongo.db.registrations.find_one({"_id": ObjectId(registration_id)})

    if not reg:
        return jsonify({"error": "Registration not found"}), 404

    # --- Double-approve prevention ---
    if reg.get("payment_status") == "approved":
        return jsonify({"message": "Already approved"})

    mongo.db.registrations.update_one(
        {"_id": ObjectId(registration_id)},
        {"$set": {"payment_status": "approved"}}
    )

    t = mongo.db.tournaments.find_one({"_id": ObjectId(reg["tournament_id"])})

    # Add leader to players
    mongo.db.tournaments.update_one(
        {"_id": ObjectId(reg["tournament_id"])},
        {"$addToSet": {"players": reg["user_id"]}}
    )

    # Add all team members to players array
    team_members = reg.get("team_members", [])
    for member in team_members:
        member_uid = member.get("user_id")
        if member_uid:
            mongo.db.tournaments.update_one(
                {"_id": ObjectId(reg["tournament_id"])},
                {"$addToSet": {"players": member_uid}}
            )

    # Notify leader
    create_notification(
        mongo,
        reg["user_id"],
        f"Your payment for \"{t.get('name') if t else 'a tournament'}\" was approved. You're in!",
        ntype="payment",
        tournament_id=reg["tournament_id"]
    )

    # Notify all team members
    for member in team_members:
        member_uid = member.get("user_id")
        if member_uid and member_uid != reg["user_id"]:
            create_notification(
                mongo,
                member_uid,
                f"Your team's payment for \"{t.get('name') if t else 'a tournament'}\" was approved. You're in!",
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
        tname = t.get('name') if t else 'a tournament'

        # Notify leader
        create_notification(
            mongo,
            reg["user_id"],
            f"Your payment for \"{tname}\" was rejected. Please re-register with valid proof.",
            ntype="payment",
            tournament_id=reg.get("tournament_id")
        )

        # Notify all team members
        for member in reg.get("team_members", []):
            member_uid = member.get("user_id")
            if member_uid and member_uid != reg["user_id"]:
                create_notification(
                    mongo,
                    member_uid,
                    f"Your team's payment for \"{tname}\" was rejected. The leader needs to re-register.",
                    ntype="payment",
                    tournament_id=reg.get("tournament_id")
                )

    return jsonify({"message": "Payment Rejected"})


# ---------------- ADMIN - DISQUALIFY TEAM ----------------
@tournament.route("/admin/disqualify/<registration_id>", methods=["POST"])
@admin_required
def disqualify_team(registration_id):
    reg = mongo.db.registrations.find_one({"_id": ObjectId(registration_id)})
    if not reg:
        return jsonify({"error": "Registration not found"}), 404

    if reg.get("payment_status") == "disqualified":
        return jsonify({"message": "Already disqualified"})

    if reg.get("payment_status") != "approved":
        return jsonify({"error": "Can only disqualify approved teams"}), 400

    mongo.db.registrations.update_one(
        {"_id": ObjectId(registration_id)},
        {"$set": {"payment_status": "disqualified"}}
    )

    # Remove from tournament players array
    mongo.db.tournaments.update_one(
        {"_id": ObjectId(reg["tournament_id"])},
        {"$pull": {"players": reg["user_id"]}}
    )

    t = mongo.db.tournaments.find_one({"_id": ObjectId(reg["tournament_id"])})
    tname = t.get("name") if t else "a tournament"

    # Notify leader
    create_notification(
        mongo,
        reg["user_id"],
        f"Your team \"{reg.get('team_name', 'Unknown')}\" has been disqualified from \"{tname}\".",
        ntype="info",
        tournament_id=str(reg["tournament_id"])
    )

    # Notify team members
    for member in reg.get("team_members", []):
        member_uid = member.get("user_id")
        if member_uid and member_uid != reg["user_id"]:
            create_notification(
                mongo,
                member_uid,
                f"Your team \"{reg.get('team_name', 'Unknown')}\" has been disqualified from \"{tname}\".",
                ntype="info",
                tournament_id=str(reg["tournament_id"])
            )

    return jsonify({"message": "Team disqualified successfully"})


# ---------------- ADMIN - RE-QUALIFY TEAM ----------------
@tournament.route("/admin/re-qualify/<registration_id>", methods=["POST"])
@admin_required
def requalify_team(registration_id):
    reg = mongo.db.registrations.find_one({"_id": ObjectId(registration_id)})
    if not reg:
        return jsonify({"error": "Registration not found"}), 404

    if reg.get("payment_status") != "disqualified":
        return jsonify({"error": "Team is not disqualified"}), 400

    mongo.db.registrations.update_one(
        {"_id": ObjectId(registration_id)},
        {"$set": {"payment_status": "approved"}}
    )

    # Re-add to tournament players array
    mongo.db.tournaments.update_one(
        {"_id": ObjectId(reg["tournament_id"])},
        {"$addToSet": {"players": reg["user_id"]}}
    )

    t = mongo.db.tournaments.find_one({"_id": ObjectId(reg["tournament_id"])})
    tname = t.get("name") if t else "a tournament"

    # Notify leader
    create_notification(
        mongo,
        reg["user_id"],
        f"Your team \"{reg.get('team_name', 'Unknown')}\" has been re-qualified for \"{tname}\".",
        ntype="info",
        tournament_id=str(reg["tournament_id"])
    )

    # Notify team members
    for member in reg.get("team_members", []):
        member_uid = member.get("user_id")
        if member_uid and member_uid != reg["user_id"]:
            create_notification(
                mongo,
                member_uid,
                f"Your team \"{reg.get('team_name', 'Unknown')}\" has been re-qualified for \"{tname}\".",
                ntype="info",
                tournament_id=str(reg["tournament_id"])
            )

    return jsonify({"message": "Team re-qualified successfully"})


# ---------------- MY TOURNAMENTS ----------------
@tournament.route("/my-tournaments", methods=["GET"])
@jwt_required()
def my_tournaments():

    user_id = get_jwt_identity()

    # Find registrations where user is leader OR teammate
    registrations = list(mongo.db.registrations.find({
        "$or": [
            {"user_id": user_id},
            {"team_members.user_id": user_id}
        ]
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

            # Determine role: leader or teammate
            role = "leader"
            if r.get("user_id") != user_id:
                role = "teammate"

            # Skip incomplete registrations: leader with no proof uploaded
            has_proof = bool(r.get("screenshot")) or bool(r.get("ig_screenshots"))
            if role == "leader" and status == "pending" and not has_proof:
                continue

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
                "banner_image": t.get("banner_image"),
                "has_bracket": bool(t.get("bracket")),
                "team_name": r.get("team_name"),
                "role": role
            })

    return jsonify(data)

@tournament.route("/admin/update-slots/<tournament_id>", methods=["POST"])
@admin_required
def update_slots(tournament_id):
    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    data = request.json or {}
    slot_assignments = data.get("slot_assignments", {})

    if not isinstance(slot_assignments, dict):
        return jsonify({"error": "slot_assignments must be an object"}), 400
    for slot, reg_id in slot_assignments.items():
        try:
            slot_num = int(slot)
            if slot_num < 1 or slot_num > 10:
                return jsonify({"error": f"Invalid slot: {slot}. Must be 1-10"}), 400
        except ValueError:
            return jsonify({"error": f"Invalid slot key: {slot}"}), 400

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {"$set": {"slot_assignments": slot_assignments}}
    )

    return jsonify({"message": "Slots updated successfully"})


@tournament.route("/admin/release-room/<tournament_id>", methods=["POST"])
@admin_required
def release_room(tournament_id):

    from datetime import datetime

    data = request.json

    room_id = data.get("room_id")
    password = data.get("password")
    start_time_raw = data.get("start_time")
    slot_assignments = data.get("slot_assignments", {})

    start_time = None
    if start_time_raw:
        try:
            # Frontend sends an ISO string (new Date().toISOString()), which
            # ends in "Z" — Python's fromisoformat wants "+00:00" instead.
            start_time = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return jsonify({"error": "Invalid start_time format"}), 400

    # Validate slot assignments (10 slots max)
    if slot_assignments:
        if not isinstance(slot_assignments, dict):
            return jsonify({"error": "slot_assignments must be an object"}), 400
        for slot, reg_id in slot_assignments.items():
            try:
                slot_num = int(slot)
                if slot_num < 1 or slot_num > 10:
                    return jsonify({"error": f"Invalid slot number: {slot}. Must be 1-10"}), 400
            except ValueError:
                return jsonify({"error": f"Invalid slot key: {slot}"}), 400

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {"$set":{
            "room_id": room_id,
            "room_password": password,
            "match_start_time": start_time,
            "slot_assignments": slot_assignments
        }}
    )

    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    participants = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": {"$in": ["approved", "teammate"]}
    })
    notified_user_ids = set()
    for r in participants:
        uid = r["user_id"]
        if uid in notified_user_ids:
            continue
        notified_user_ids.add(uid)
        create_notification(
            mongo,
            uid,
            f"Room details for \"{t.get('name') if t else 'your tournament'}\" are live. Check the room page!",
            ntype="room",
            tournament_id=tournament_id
        )
        # Also notify team members
        for member in r.get("team_members", []):
            member_uid = member.get("user_id")
            if member_uid and member_uid not in notified_user_ids:
                notified_user_ids.add(member_uid)
                create_notification(
                    mongo,
                    member_uid,
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
        "payment_status": {"$in": ["approved", "teammate"]}
    })

    if not registration:
        return jsonify({"error":"Not approved"}),403

    tournament = mongo.db.tournaments.find_one({
        "_id": ObjectId(tournament_id)
    })

    match_time = tournament.get("match_start_time")
    match_time_str = to_utc_iso(match_time) if hasattr(match_time, "isoformat") else (match_time if isinstance(match_time, str) else None)

    # Build slot assignments with team names
    slot_assignments = tournament.get("slot_assignments", {})
    slots_with_teams = {}
    if slot_assignments:
        # Fetch team names for assigned registration IDs
        reg_ids = list(slot_assignments.values())
        registrations = list(mongo.db.registrations.find({
            "_id": {"$in": [ObjectId(rid) for rid in reg_ids if safe_object_id(rid)]}
        }))
        reg_name_map = {str(r["_id"]): r.get("team_name") for r in registrations}
        
        for slot, reg_id in slot_assignments.items():
            slots_with_teams[slot] = {
                "registration_id": reg_id,
                "team_name": reg_name_map.get(reg_id, "Unknown Team")
            }

    return jsonify({
        "room_id": tournament.get("room_id"),
        "room_password": tournament.get("room_password"),
        "match_start_time": match_time_str,
        "slot_assignments": slots_with_teams
    })

@tournament.route("/admin/declare-winner", methods=["POST"])
@admin_required
def declare_winner():

    data = request.json

    tournament_id = data.get("tournament_id")
    winner_id = data.get("winner_id")

    if not tournament_id or not winner_id:
        return jsonify({"error": "Missing fields"}), 400

    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    old_winner_id = t.get("winner_id")

    # Idempotent: if same winner, no-op
    if old_winner_id == winner_id:
        return jsonify({"message": "Winner already set to this player"})

    winner_entry = {
        "registration_id": None,
        "user_id": winner_id,
        "name": None,
    }

    participant = mongo.db.registrations.find_one({
        "tournament_id": ObjectId(tournament_id),
        "user_id": winner_id,
        "payment_status": "approved"
    })
    if participant:
        winner_entry["registration_id"] = str(participant["_id"])
        winner_entry["name"] = participant.get("team_name") or participant.get("name")

    update_fields = build_winner_update(winner_entry, stage_name="Manual declaration", source="admin_override")
    update_fields["winner_id"] = winner_id

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {"$set": update_fields}
    )

    # Update player stats: decrement old winner, increment new winner
    game = t.get("game", "")
    if old_winner_id:
        upsert_global_wins(old_winner_id, -1)
        if game:
            upsert_player_stats(old_winner_id, game, tournaments_won_delta=-1)

    upsert_global_wins(winner_id, 1)
    if game:
        upsert_player_stats(winner_id, game, tournaments_won_delta=1)

    # Also increment tournaments_played for the winner if first time
    # (only if they don't already have a stats record for this game)
    from routes.player_stats_routes import get_player_stats as _get_stats
    existing_stats = _get_stats(winner_id, game)
    if existing_stats.get("tournaments_played", 0) == 0:
        upsert_player_stats(winner_id, game, tournaments_played_delta=1)

    # Increment tournaments_played for all other approved participants
    other_participants = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": "approved",
        "user_id": {"$ne": winner_id}
    })
    for r in other_participants:
        user_id = r.get("user_id")
        if user_id:
            upsert_player_stats(user_id, game, tournaments_played_delta=1)

    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    tname = t.get("name") if t else "the tournament"

    create_notification(
        mongo,
        winner_id,
        f"Congratulations! You won \"{tname}\"!",
        ntype="winner",
        tournament_id=tournament_id
    )

    other_participants = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": {"$in": ["approved", "teammate"]},
        "user_id": {"$ne": winner_id}
    })
    notified_user_ids = {winner_id}
    for r in other_participants:
        uid = r.get("user_id")
        if uid and uid not in notified_user_ids:
            notified_user_ids.add(uid)
            create_notification(
                mongo,
                uid,
                f"\"{tname}\" has ended. Check the bracket to see how it played out.",
                ntype="winner",
                tournament_id=tournament_id
            )
        # Also notify team members
        for member in r.get("team_members", []):
            member_uid = member.get("user_id")
            if member_uid and member_uid not in notified_user_ids:
                notified_user_ids.add(member_uid)
                create_notification(
                    mongo,
                    member_uid,
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
        new_winner_id = rounds[-1][0]["winner"]["user_id"]
        update_fields["winner_id"] = new_winner_id
        update_fields["status"] = "completed"

        game = t.get("game", "")
        upsert_global_wins(new_winner_id, 1)
        if game:
            upsert_player_stats(new_winner_id, game, tournaments_won_delta=1)

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
        new_winner_id = final_round[0]["winner"]["user_id"]
        old_winner_id = t.get("winner_id")

        if old_winner_id != new_winner_id:
            game = t.get("game", "")
            if old_winner_id:
                upsert_global_wins(old_winner_id, -1)
                if game:
                    upsert_player_stats(old_winner_id, game, tournaments_won_delta=-1)
            upsert_global_wins(new_winner_id, 1)
            if game:
                upsert_player_stats(new_winner_id, game, tournaments_won_delta=1)

        update_fields["winner_id"] = new_winner_id
        update_fields["status"] = "completed"

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {"$set": update_fields}
    )

    return jsonify({"message": "Match result recorded", "rounds": rounds})


# ---------------- LEADERBOARD ----------------
@tournament.route("/leaderboard", methods=["GET"])
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
            "avatarId": user.get("avatarId") if user else None,
            "wins": entry["wins"],
            "prize_won": entry["prize_won"],
            "tournaments": entry["tournaments"]
        })

    leaderboard_data.sort(key=lambda x: (-x["wins"], -x["prize_won"]))

    return jsonify(leaderboard_data)

# ---------------- UPDATE TOURNAMENT ----------------
@tournament.route("/<tournament_id>", methods=["PATCH"])
@admin_required
def update_tournament(tournament_id):

    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    data = request.get_json(silent=True) or request.form

    update_fields = {}

    if data:
        if "name" in data and data["name"]:
            update_fields["name"] = data["name"]
        if "game" in data and data["game"]:
            update_fields["game"] = data["game"]
        if "registration_end_time" in data:
            update_fields["registration_end_time"] = data["registration_end_time"]
        if "scheduled_time" in data:
            update_fields["scheduled_time"] = data["scheduled_time"]

    if not update_fields:
        return jsonify({"error": "No fields to update"}), 400

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {"$set": update_fields}
    )

    # Return updated tournament data
    updated = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    return jsonify({
        "message": "Tournament updated successfully",
        "registration_end_time": _format_deadline_iso(updated.get("registration_end_time")),
        "scheduled_time": _format_deadline_iso(updated.get("scheduled_time")),
        "registration_open": is_registration_open(updated)
    })





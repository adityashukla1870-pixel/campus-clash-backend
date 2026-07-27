from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime
from functools import wraps

from routes.notification_routes import create_notification

stage = Blueprint("stage", __name__)
mongo = None


def init_stage_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


# ---------------- SHARED HELPERS (self-contained, no cross-import) ----------------

def safe_object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def admin_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        user_id = get_jwt_identity()
        user = mongo.db.users.find_one({"_id": safe_object_id(user_id)})
        if not user or user.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper


# Standard BR (BGMI/Free Fire-style) placement points table.
PLACEMENT_POINTS = {1: 10, 2: 6, 3: 5, 4: 4, 5: 3, 6: 2, 7: 2, 8: 1, 9: 1}
KILL_POINT_VALUE = 1


def calc_points(placement, kills):
    return PLACEMENT_POINTS.get(placement, 0) + (kills or 0) * KILL_POINT_VALUE


def get_approved_roster(tournament_id):
    """Every approved registration for a tournament, shaped as a team/player entry."""
    registrations = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": "approved"
    })
    roster = []
    for r in registrations:
        user = mongo.db.users.find_one({"_id": safe_object_id(r.get("user_id"))})
        display_name = r.get("team_name") or (user.get("name") if user else "Unknown")
        roster.append({
            "registration_id": str(r["_id"]),
            "user_id": r.get("user_id"),
            "name": display_name
        })
    return roster


def compute_standings(stage_doc):
    """Aggregate points/kills across every completed match in a stage."""
    matches = list(mongo.db.stage_matches.find({
        "stage_id": stage_doc["_id"],
        "status": "completed"
    }))

    totals = {}
    for p in stage_doc.get("participants", []):
        totals[p["registration_id"]] = {
            "registration_id": p["registration_id"],
            "user_id": p.get("user_id"),
            "name": p["name"],
            "matches_played": 0,
            "total_kills": 0,
            "total_points": 0
        }

    for m in matches:
        for res in m.get("results", []):
            rid = res.get("registration_id")
            if rid not in totals:
                continue
            totals[rid]["matches_played"] += 1
            totals[rid]["total_kills"] += res.get("kills", 0)
            totals[rid]["total_points"] += res.get("points", 0)

    standings = list(totals.values())
    standings.sort(key=lambda x: (-x["total_points"], -x["total_kills"]))
    for i, s in enumerate(standings):
        s["rank"] = i + 1

    return standings


def serialize_stage(s, include_matches=False):
    out = {
        "id": str(s["_id"]),
        "tournament_id": str(s["tournament_id"]),
        "stage_index": s["stage_index"],
        "name": s["name"],
        "is_final": s.get("is_final", False),
        "advance_count": s.get("advance_count"),
        "status": s.get("status", "active"),
        "participants": s.get("participants", []),
        "final_standings": s.get("final_standings")
    }
    if include_matches:
        matches = list(mongo.db.stage_matches.find({"stage_id": s["_id"]}).sort("match_number", 1))
        out["matches"] = [serialize_match(m) for m in matches]
    return out


def serialize_match(m):
    return {
        "id": str(m["_id"]),
        "stage_id": str(m["stage_id"]),
        "match_number": m["match_number"],
        "map": m.get("map"),
        "room_id": m.get("room_id"),
        "room_password": m.get("room_password"),
        "match_start_time": m.get("match_start_time"),
        "status": m.get("status", "scheduled"),
        "results": m.get("results", [])
    }


# ---------------- CREATE A STAGE ----------------
@stage.route("/<tournament_id>/create", methods=["POST"])
@admin_required
def create_stage(tournament_id):

    t = mongo.db.tournaments.find_one({"_id": safe_object_id(tournament_id)})
    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    is_final = bool(data.get("is_final", False))
    advance_count = data.get("advance_count")

    if not name:
        return jsonify({"error": "Stage name is required"}), 400

    existing = list(mongo.db.tournament_stages.find({"tournament_id": ObjectId(tournament_id)}))

    if not existing:
        participants = get_approved_roster(tournament_id)
        if len(participants) < 2:
            return jsonify({"error": "Need at least 2 approved participants to start a stage"}), 400
        stage_index = 0
    else:
        prev = max(existing, key=lambda s: s["stage_index"])
        if prev.get("status") != "completed":
            return jsonify({"error": f"Finalize '{prev['name']}' before starting the next stage"}), 400
        participants = prev.get("final_standings") or compute_standings(prev)
        cutoff = prev.get("advance_count") or len(participants)
        participants = [
            {"registration_id": p["registration_id"], "user_id": p.get("user_id"), "name": p["name"]}
            for p in participants[:cutoff]
        ]
        if len(participants) < 2:
            return jsonify({"error": "Not enough teams advanced to start this stage"}), 400
        stage_index = prev["stage_index"] + 1

    doc = {
        "tournament_id": ObjectId(tournament_id),
        "stage_index": stage_index,
        "name": name,
        "is_final": is_final,
        "advance_count": int(advance_count) if advance_count else None,
        "participants": participants,
        "status": "active",
        "final_standings": None,
        "created_at": datetime.utcnow()
    }
    result = mongo.db.tournament_stages.insert_one(doc)
    doc["_id"] = result.inserted_id

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {"$set": {"status": "in_progress"}}
    )

    return jsonify(serialize_stage(doc))


# ---------------- LIST STAGES FOR A TOURNAMENT ----------------
@stage.route("/tournament/<tournament_id>", methods=["GET"])
@jwt_required()
def list_stages(tournament_id):
    stages = list(mongo.db.tournament_stages.find(
        {"tournament_id": safe_object_id(tournament_id)}
    ).sort("stage_index", 1))
    return jsonify([serialize_stage(s) for s in stages])


# ---------------- STAGE DETAIL (incl. matches) ----------------
@stage.route("/<stage_id>", methods=["GET"])
@jwt_required()
def get_stage(stage_id):
    s = mongo.db.tournament_stages.find_one({"_id": safe_object_id(stage_id)})
    if not s:
        return jsonify({"error": "Stage not found"}), 404
    return jsonify(serialize_stage(s, include_matches=True))


# ---------------- STANDINGS ----------------
@stage.route("/<stage_id>/standings", methods=["GET"])
@jwt_required()
def get_standings(stage_id):
    s = mongo.db.tournament_stages.find_one({"_id": safe_object_id(stage_id)})
    if not s:
        return jsonify({"error": "Stage not found"}), 404

    if s.get("status") == "completed" and s.get("final_standings"):
        return jsonify(s["final_standings"])

    return jsonify(compute_standings(s))


# ---------------- ADD A MATCH TO A STAGE ----------------
@stage.route("/<stage_id>/matches", methods=["POST"])
@admin_required
def add_match(stage_id):
    s = mongo.db.tournament_stages.find_one({"_id": safe_object_id(stage_id)})
    if not s:
        return jsonify({"error": "Stage not found"}), 404
    if s.get("status") == "completed":
        return jsonify({"error": "Stage already finalized"}), 400

    data = request.get_json(silent=True) or {}
    match_count = mongo.db.stage_matches.count_documents({"stage_id": s["_id"]})

    doc = {
        "stage_id": s["_id"],
        "tournament_id": s["tournament_id"],
        "match_number": match_count + 1,
        "map": data.get("map"),
        "room_id": None,
        "room_password": None,
        "match_start_time": None,
        "status": "scheduled",
        "results": [],
        "created_at": datetime.utcnow()
    }
    result = mongo.db.stage_matches.insert_one(doc)
    doc["_id"] = result.inserted_id
    return jsonify(serialize_match(doc))


# ---------------- RELEASE ROOM FOR A MATCH ----------------
@stage.route("/matches/<match_id>/room", methods=["POST"])
@admin_required
def release_match_room(match_id):
    m = mongo.db.stage_matches.find_one({"_id": safe_object_id(match_id)})
    if not m:
        return jsonify({"error": "Match not found"}), 404

    data = request.get_json(silent=True) or {}
    room_id = data.get("room_id")
    password = data.get("password")
    start_time = data.get("start_time")

    if not room_id or not password:
        return jsonify({"error": "Room ID and password are required"}), 400

    mongo.db.stage_matches.update_one(
        {"_id": m["_id"]},
        {"$set": {"room_id": room_id, "room_password": password, "match_start_time": start_time}}
    )

    s = mongo.db.tournament_stages.find_one({"_id": m["stage_id"]})
    if s:
        for p in s.get("participants", []):
            if p.get("user_id"):
                create_notification(
                    mongo,
                    p["user_id"],
                    f"Room is live for {s['name']} — Match {m['match_number']}. Check the standings page!",
                    ntype="room",
                    tournament_id=str(m["tournament_id"])
                )

    return jsonify({"message": "Room released"})


# ---------------- SUBMIT MATCH RESULTS ----------------
@stage.route("/matches/<match_id>/results", methods=["POST"])
@admin_required
def submit_results(match_id):
    m = mongo.db.stage_matches.find_one({"_id": safe_object_id(match_id)})
    if not m:
        return jsonify({"error": "Match not found"}), 404

    s = mongo.db.tournament_stages.find_one({"_id": m["stage_id"]})
    if not s:
        return jsonify({"error": "Stage not found"}), 404

    data = request.get_json(silent=True) or {}
    raw_results = data.get("results", [])
    if not raw_results:
        return jsonify({"error": "No results submitted"}), 400

    name_lookup = {p["registration_id"]: p for p in s.get("participants", [])}

    results = []
    for r in raw_results:
        rid = r.get("registration_id")
        if rid not in name_lookup:
            continue
        placement = int(r.get("placement", 0))
        kills = int(r.get("kills", 0))
        results.append({
            "registration_id": rid,
            "name": name_lookup[rid]["name"],
            "placement": placement,
            "kills": kills,
            "points": calc_points(placement, kills)
        })

    mongo.db.stage_matches.update_one(
        {"_id": m["_id"]},
        {"$set": {"results": results, "status": "completed"}}
    )

    for r in results:
        p = name_lookup.get(r["registration_id"])
        if p and p.get("user_id"):
            create_notification(
                mongo,
                p["user_id"],
                f"Results are out for {s['name']} — Match {m['match_number']}: #{r['placement']} place, {r['kills']} kills ({r['points']} pts).",
                ntype="winner",
                tournament_id=str(m["tournament_id"])
            )

    return jsonify({"message": "Results submitted", "results": results})


# ---------------- FINALIZE A STAGE ----------------
@stage.route("/<stage_id>/finalize", methods=["POST"])
@admin_required
def finalize_stage(stage_id):
    s = mongo.db.tournament_stages.find_one({"_id": safe_object_id(stage_id)})
    if not s:
        return jsonify({"error": "Stage not found"}), 404
    if s.get("status") == "completed":
        return jsonify({"error": "Stage already finalized"}), 400

    standings = compute_standings(s)
    mongo.db.tournament_stages.update_one(
        {"_id": s["_id"]},
        {"$set": {"status": "completed", "final_standings": standings}}
    )

    if s.get("is_final") and standings:
        champion = standings[0]
        update_fields = {
            "status": "completed",
            "winner_registration_id": champion["registration_id"],
            "winner_name": champion["name"]
        }
        if champion.get("user_id"):
            update_fields["winner_id"] = champion["user_id"]

        mongo.db.tournaments.update_one(
            {"_id": s["tournament_id"]},
            {"$set": update_fields}
        )

        for p in s.get("participants", []):
            if p.get("user_id"):
                is_champ = p["registration_id"] == champion["registration_id"]
                msg = (f"🏆 Congratulations! Your team won {s['name']}!" if is_champ
                       else f"{s['name']} has concluded. Check the final standings!")
                create_notification(mongo, p["user_id"], msg, ntype="winner", tournament_id=str(s["tournament_id"]))

    return jsonify({"message": "Stage finalized", "standings": standings})

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime
from functools import wraps

from routes.notification_routes import create_notification
from routes.player_stats_routes import upsert_player_stats
from utils.player_stats import increment_tournaments_played

bgmi_league = Blueprint("bgmi_league", __name__)
mongo = None


def init_bgmi_league_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


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


def get_roster(tournament_id):
    registrations = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": {"$in": ["approved", "teammate"]}
    })
    roster = {}
    for r in registrations:
        rid = str(r["_id"])
        if r.get("payment_status") == "teammate":
            continue
        team_name = r.get("team_name") or r.get("player_name", "Unknown")
        members = r.get("team_members", [])
        leader = r.get("team_leader", {})
        roster[rid] = {
            "registration_id": rid,
            "user_id": r["user_id"],
            "name": team_name,
            "team_leader": leader,
            "team_members": members,
        }
    return roster


def serialize_match(m):
    return {
        "id": str(m["_id"]),
        "league_id": str(m.get("league_id", "")),
        "tournament_id": str(m.get("tournament_id", "")),
        "match_number": m.get("match_number", 0),
        "day": m.get("day", 1),
        "map": m.get("map"),
        "room_id": m.get("room_id"),
        "room_password": m.get("room_password"),
        "match_start_time": m.get("match_start_time").isoformat() if m.get("match_start_time") else None,
        "status": m.get("status", "scheduled"),
        "results": m.get("results", []),
        "mvp": m.get("mvp"),
        "slot_assignments": m.get("slot_assignments", {}),
        "slot_limit": m.get("slot_limit", 11),
        "participants": m.get("participants", []),
        "created_at": m.get("created_at").isoformat() if m.get("created_at") else None,
    }


def serialize_league(rr, include_matches=False):
    data = {
        "id": str(rr["_id"]),
        "tournament_id": str(rr.get("tournament_id", "")),
        "name": rr.get("name", ""),
        "status": rr.get("status", "active"),
        "created_at": rr.get("created_at").isoformat() if rr.get("created_at") else None,
    }
    if include_matches:
        matches = list(mongo.db.bgmi_league_matches.find(
            {"league_id": rr["_id"]}
        ).sort("day").sort("match_number"))
        data["matches"] = [serialize_match(m) for m in matches]
    return data


# ---------------- CREATE LEAGUE ----------------
@bgmi_league.route("/<tournament_id>/create", methods=["POST"])
@admin_required
def create_league(tournament_id):
    """Create a BGMI league with full-lobby matches (all teams in every match).
    Expects JSON: { name, match_count_per_day: [3, 3, 3] }
    """
    try:
        t = mongo.db.tournaments.find_one({"_id": safe_object_id(tournament_id)})
        if not t:
            return jsonify({"error": "Tournament not found"}), 404

        # Check if league already exists
        existing = mongo.db.bgmi_league.find_one({"tournament_id": ObjectId(tournament_id)})
        if existing:
            return jsonify({"error": "League already exists for this tournament"}), 400

        data = request.get_json(silent=True) or {}
        name = data.get("name") or f"{t['name']} - League"
        matches_per_day = data.get("matches_per_day") or [3, 3, 3]

        # Get all approved participants
        roster = get_roster(tournament_id)
        participants = list(roster.values())

        if len(participants) < 2:
            return jsonify({"error": "Need at least 2 approved participants"}), 400

        slot_limit = max(len(participants), 10)

        # Create league document
        league_doc = {
            "tournament_id": ObjectId(tournament_id),
            "name": name,
            "status": "active",
            "created_at": datetime.utcnow(),
        }
        result = mongo.db.bgmi_league.insert_one(league_doc)
        league_id = result.inserted_id

        # Create matches
        maps = ["Bermuda", "Pulgatory", "Kalahari", "Livik", "Sanhok"]
        match_counter = 0
        created_matches = []

        for day_num, count in enumerate(matches_per_day, 1):
            for i in range(count):
                match_counter += 1
                map_name = maps[(match_counter - 1) % len(maps)]

                match_doc = {
                    "league_id": league_id,
                    "tournament_id": ObjectId(tournament_id),
                    "match_number": match_counter,
                    "day": day_num,
                    "map": map_name,
                    "room_id": None,
                    "room_password": None,
                    "match_start_time": None,
                    "status": "scheduled",
                    "results": [],
                    "mvp": None,
                    "slot_assignments": {},
                    "slot_limit": slot_limit,
                    "participants": participants,
                    "created_at": datetime.utcnow(),
                }
                res = mongo.db.bgmi_league_matches.insert_one(match_doc)
                match_doc["_id"] = res.inserted_id
                created_matches.append(match_doc)

        league = mongo.db.bgmi_league.find_one({"_id": league_id})
        return jsonify({
            "message": f"League created with {len(created_matches)} matches across {len(matches_per_day)} days",
            "league": serialize_league(league, include_matches=True)
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ---------------- GET LEAGUE ----------------
@bgmi_league.route("/tournament/<tournament_id>", methods=["GET"])
@jwt_required()
def get_league(tournament_id):
    rr = mongo.db.bgmi_league.find_one({"tournament_id": safe_object_id(tournament_id)})
    if not rr:
        return jsonify(None)
    return jsonify(serialize_league(rr, include_matches=True))


# ---------------- RELEASE ROOM ----------------
@bgmi_league.route("/matches/<match_id>/room", methods=["POST"])
@admin_required
def release_room(match_id):
    """Release room for a specific league match."""
    match = mongo.db.bgmi_league_matches.find_one({"_id": safe_object_id(match_id)})
    if not match:
        return jsonify({"error": "Match not found"}), 404

    data = request.json
    room_id = data.get("room_id")
    password = data.get("password")
    start_time_raw = data.get("start_time")
    slot_assignments = data.get("slot_assignments", {})

    if not room_id or not password:
        return jsonify({"error": "Room ID and password required"}), 400

    start_time = None
    if start_time_raw:
        try:
            start_time = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return jsonify({"error": "Invalid start_time format"}), 400

    # Validate slot assignments
    slot_limit = match.get("slot_limit", 11)
    if slot_assignments:
        for slot, reg_id in slot_assignments.items():
            try:
                slot_num = int(slot)
                if slot_num < 1 or slot_num > slot_limit:
                    return jsonify({"error": f"Invalid slot: {slot}. Must be 1-{slot_limit}"}), 400
            except ValueError:
                return jsonify({"error": f"Invalid slot key: {slot}"}), 400

    mongo.db.bgmi_league_matches.update_one(
        {"_id": match["_id"]},
        {"$set": {
            "room_id": room_id,
            "room_password": password,
            "match_start_time": start_time,
            "slot_assignments": slot_assignments,
        }}
    )

    # Notify participants
    tournament_id = str(match["tournament_id"])
    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    participants = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": {"$in": ["approved", "teammate"]}
    })
    notified = set()
    for r in participants:
        uid = r["user_id"]
        if uid in notified:
            continue
        notified.add(uid)
        create_notification(
            mongo, uid,
            f"Room details for Match {match['match_number']} of \"{t.get('name') if t else 'tournament'}\" are live!",
            ntype="room",
            tournament_id=tournament_id
        )
        for member in r.get("team_members", []):
            member_uid = member.get("user_id")
            if member_uid and member_uid not in notified:
                notified.add(member_uid)
                create_notification(
                    mongo, member_uid,
                    f"Room details for Match {match['match_number']} of \"{t.get('name') if t else 'tournament'}\" are live!",
                    ntype="room",
                    tournament_id=tournament_id
                )

    return jsonify({"message": "Room released successfully"})


# ---------------- UPDATE SLOTS ----------------
@bgmi_league.route("/matches/<match_id>/slots", methods=["POST"])
@admin_required
def update_slots(match_id):
    match = mongo.db.bgmi_league_matches.find_one({"_id": safe_object_id(match_id)})
    if not match:
        return jsonify({"error": "Match not found"}), 404

    data = request.json
    slot_assignments = data.get("slot_assignments", {})

    mongo.db.bgmi_league_matches.update_one(
        {"_id": match["_id"]},
        {"$set": {"slot_assignments": slot_assignments}}
    )
    return jsonify({"message": "Slots updated"})


# ---------------- SUBMIT RESULTS ----------------
@bgmi_league.route("/matches/<match_id>/results", methods=["POST"])
@admin_required
def submit_results(match_id):
    """Submit results for a league match."""
    match = mongo.db.bgmi_league_matches.find_one({"_id": safe_object_id(match_id)})
    if not match:
        return jsonify({"error": "Match not found"}), 404

    data = request.json
    results = data.get("results", [])

    if not results:
        return jsonify({"error": "No results provided"}), 400

    tournament_id = str(match["tournament_id"])
    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    points_table = t.get("points_table", {"1": 10, "2": 6, "3": 5, "4": 4, "5": 3, "6": 2, "7": 2, "8": 1, "9": 1})
    kill_point_value = t.get("kill_point_value", 1)

    processed_results = []
    for r in results:
        reg_id = r.get("registration_id")
        placement = r.get("placement", 0)
        kills = r.get("kills", 0)
        players = r.get("players", [])

        reg = mongo.db.registrations.find_one({"_id": safe_object_id(reg_id)})
        name = "Unknown"
        if reg:
            name = reg.get("team_name") or reg.get("player_name", "Unknown")

        pts = points_table.get(str(placement), 0) + (kills or 0) * kill_point_value

        processed_results.append({
            "registration_id": reg_id,
            "name": name,
            "placement": placement,
            "kills": kills,
            "points": pts,
            "players": players,
        })

    # Calculate MVP
    mvp = None
    if processed_results:
        best_score = -1
        for r in processed_results:
            player_kills = sum(p.get("kills", 0) for p in r.get("players", []))
            score = player_kills * 10 + r.get("points", 0)
            if score > best_score:
                best_score = score
                mvp = {
                    "name": r["name"],
                    "registration_id": r["registration_id"],
                    "kills": r.get("kills", 0),
                    "placement": r.get("placement", 0),
                    "score": score,
                }

    # Save results
    processed_results.sort(key=lambda x: x.get("placement", 999))
    mongo.db.bgmi_league_matches.update_one(
        {"_id": match["_id"]},
        {"$set": {
            "results": processed_results,
            "mvp": mvp,
            "status": "completed",
        }}
    )

    # Update player stats
    for r in processed_results:
        reg = mongo.db.registrations.find_one({"_id": safe_object_id(r["registration_id"])})
        if reg:
            all_uids = []
            if reg.get("user_id"):
                all_uids.append(reg["user_id"])
            for m in reg.get("team_members", []):
                if m.get("user_id"):
                    all_uids.append(m["user_id"])
            for uid in all_uids:
                upsert_player_stats(mongo, uid, tournament_id, r.get("kills", 0), r.get("points", 0))
                increment_tournaments_played(mongo, uid)

    # Notify participants
    registrations = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": {"$in": ["approved", "teammate"]}
    })
    notified = set()
    for reg in registrations:
        uid = reg["user_id"]
        if uid in notified:
            continue
        notified.add(uid)
        create_notification(
            mongo, uid,
            f"Results for Match {match['match_number']} are out! Check the standings.",
            ntype="results",
            tournament_id=tournament_id
        )
        for member in reg.get("team_members", []):
            member_uid = member.get("user_id")
            if member_uid and member_uid not in notified:
                notified.add(member_uid)
                create_notification(
                    mongo, member_uid,
                    f"Results for Match {match['match_number']} are out! Check the standings.",
                    ntype="results",
                    tournament_id=tournament_id
                )

    return jsonify({"message": "Results submitted successfully"})


# ---------------- GET MATCH DETAIL ----------------
@bgmi_league.route("/matches/<match_id>", methods=["GET"])
@jwt_required()
def get_match(match_id):
    match = mongo.db.bgmi_league_matches.find_one({"_id": safe_object_id(match_id)})
    if not match:
        return jsonify({"error": "Match not found"}), 404
    return jsonify(serialize_match(match))


# ---------------- STANDINGS ----------------
@bgmi_league.route("/tournament/<tournament_id>/standings", methods=["GET"])
@jwt_required()
def get_standings(tournament_id):
    """Calculate overall standings from all completed league matches."""
    league = mongo.db.bgmi_league.find_one({"tournament_id": safe_object_id(tournament_id)})
    if not league:
        return jsonify([])

    matches = list(mongo.db.bgmi_league_matches.find({
        "league_id": league["_id"],
        "status": "completed"
    }))

    # Aggregate stats per team
    team_stats = {}
    for m in matches:
        for r in m.get("results", []):
            reg_id = r.get("registration_id")
            if not reg_id:
                continue
            if reg_id not in team_stats:
                team_stats[reg_id] = {
                    "registration_id": reg_id,
                    "name": r.get("name", "Unknown"),
                    "matches_played": 0,
                    "total_kills": 0,
                    "total_points": 0,
                    "chicken_dinners": 0,
                    "best_placement": 999,
                }
            ts = team_stats[reg_id]
            ts["matches_played"] += 1
            ts["total_kills"] += r.get("kills", 0)
            ts["total_points"] += r.get("points", 0)
            if r.get("placement") == 1:
                ts["chicken_dinners"] += 1
            if r.get("placement", 999) < ts["best_placement"]:
                ts["best_placement"] = r["placement"]

    # Sort by total_points, then total_kills
    standings = sorted(team_stats.values(), key=lambda x: (-x["total_points"], -x["total_kills"]))
    for i, s in enumerate(standings):
        s["rank"] = i + 1

    return jsonify(standings)


# ---------------- FINALIZE LEAGUE ----------------
@bgmi_league.route("/<league_id>/finalize", methods=["POST"])
@admin_required
def finalize_league(league_id):
    """Finalize the league and declare winner."""
    league = mongo.db.bgmi_league.find_one({"_id": safe_object_id(league_id)})
    if not league:
        return jsonify({"error": "League not found"}), 404

    # Check all matches completed
    matches = list(mongo.db.bgmi_league_matches.find({"league_id": league["_id"]}))
    incomplete = [m for m in matches if m.get("status") != "completed"]
    if incomplete:
        return jsonify({"error": f"{len(incomplete)} matches still incomplete"}), 400

    # Calculate final standings
    tournament_id = str(league["tournament_id"])
    team_stats = {}
    for m in matches:
        for r in m.get("results", []):
            reg_id = r.get("registration_id")
            if not reg_id:
                continue
            if reg_id not in team_stats:
                team_stats[reg_id] = {
                    "registration_id": reg_id,
                    "name": r.get("name", "Unknown"),
                    "total_kills": 0,
                    "total_points": 0,
                }
            ts = team_stats[reg_id]
            ts["total_kills"] += r.get("kills", 0)
            ts["total_points"] += r.get("points", 0)

    standings = sorted(team_stats.values(), key=lambda x: (-x["total_points"], -x["total_kills"]))

    if standings:
        winner = standings[0]
        winner_reg = mongo.db.registrations.find_one({"_id": safe_object_id(winner["registration_id"])})
        winner_user_id = winner_reg["user_id"] if winner_reg else None

        mongo.db.tournaments.update_one(
            {"_id": ObjectId(tournament_id)},
            {"$set": {
                "winner_id": winner_user_id,
                "winner_registration_id": winner["registration_id"],
                "winner_name": winner["name"],
                "winner_source": "bgmi_league",
                "status": "completed",
            }}
        )

        # Notify all participants
        registrations = mongo.db.registrations.find({
            "tournament_id": ObjectId(tournament_id),
            "payment_status": {"$in": ["approved", "teammate"]}
        })
        notified = set()
        for reg in registrations:
            uid = reg["user_id"]
            if uid in notified:
                continue
            notified.add(uid)
            create_notification(
                mongo, uid,
                f"League finalized! Winner: {winner['name']}. Congratulations!",
                ntype="winner",
                tournament_id=tournament_id
            )
            for member in reg.get("team_members", []):
                member_uid = member.get("user_id")
                if member_uid and member_uid not in notified:
                    notified.add(member_uid)
                    create_notification(
                        mongo, member_uid,
                        f"League finalized! Winner: {winner['name']}. Congratulations!",
                        ntype="winner",
                        tournament_id=tournament_id
                    )

    mongo.db.bgmi_league.update_one(
        {"_id": league["_id"]},
        {"$set": {"status": "completed"}}
    )

    return jsonify({"message": "League finalized", "winner": standings[0]["name"] if standings else None})


# ---------------- DELETE LEAGUE ----------------
@bgmi_league.route("/<league_id>", methods=["DELETE"])
@admin_required
def delete_league(league_id):
    league = mongo.db.bgmi_league.find_one({"_id": safe_object_id(league_id)})
    if not league:
        return jsonify({"error": "League not found"}), 404

    mongo.db.bgmi_league_matches.delete_many({"league_id": league["_id"]})
    mongo.db.bgmi_league.delete_one({"_id": league["_id"]})
    return jsonify({"message": "League deleted"})

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime
from functools import wraps

from routes.notification_routes import create_notification
from routes.player_stats_routes import upsert_player_stats
from utils.player_stats import increment_tournaments_played
from utils.tournament_lifecycle import generate_round_robin_pairings, build_winner_update

cross_pod = Blueprint("cross_pod", __name__)
mongo = None


def init_cross_pod_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


# ---------------- HELPERS ----------------

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


DEFAULT_POINTS_TABLE = {"1": 10, "2": 6, "3": 5, "4": 4, "5": 3, "6": 2, "7": 2, "8": 1, "9": 1}
DEFAULT_KILL_POINT = 1


def calc_points(placement, kills, points_table, kill_point_value):
    pts = points_table.get(str(placement), 0)
    return pts + (kills or 0) * kill_point_value


def get_tournament_scoring(tournament_id):
    t = mongo.db.tournaments.find_one({"_id": ObjectId(tournament_id)})
    if not t:
        return None, None, None
    points_table = t.get("points_table") or DEFAULT_POINTS_TABLE
    kill_point_value = t.get("kill_point_value", DEFAULT_KILL_POINT)
    return t, points_table, kill_point_value


def get_roster_by_id(tournament_id):
    registrations = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": {"$nin": ["rejected", "disqualified"]}
    })
    roster = {}
    seen_keys = set()
    for r in registrations:
        team_name = r.get("team_name")
        team_members = r.get("team_members", []) or []

        # Build dedup key: sorted member IDs catch duplicate squad registrations
        member_ids = sorted([str(mid) for mid in team_members])
        dedup_key = "|".join(member_ids) if member_ids else str(r.get("user_id", ""))

        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        user = mongo.db.users.find_one({"_id": safe_object_id(r.get("user_id"))})
        display_name = team_name or (user.get("name") if user else "Unknown")
        roster[str(r["_id"])] = {
            "registration_id": str(r["_id"]),
            "user_id": r.get("user_id"),
            "name": display_name,
            "team_members": team_members,
            "team_leader": r.get("team_leader")
        }
    return roster


def compute_match_mvp(results):
    best = None
    best_score = -1
    for r in results:
        placement = r.get("placement", 0)
        team_points = r.get("points", 0)
        kills = r.get("kills", 0)
        players = r.get("players") or [{"name": r["name"], "kills": kills}]
        for p in players:
            p_kills = p.get("kills", 0)
            score = p_kills * 10 + team_points
            if best is None or score > best_score:
                best_score = score
                best = {
                    "name": p["name"],
                    "team_name": r["name"],
                    "registration_id": r["registration_id"],
                    "kills": p_kills,
                    "placement": placement,
                    "score": score,
                }
    return best


def serialize_cross_pod_match(m):
    raw_slots = m.get("slot_assignments", {})
    resolved_slots = {}
    if raw_slots:
        reg_ids = [ObjectId(rid) for rid in raw_slots.values() if rid]
        regs = {}
        if reg_ids:
            for r in mongo.db.registrations.find({"_id": {"$in": reg_ids}}):
                regs[str(r["_id"])] = r
        for slot, reg_id in raw_slots.items():
            reg = regs.get(reg_id, {})
            resolved_slots[str(slot)] = {
                "registration_id": reg_id,
                "team_name": reg.get("team_name", "Unknown")
            }

    out = {
        "id": str(m["_id"]),
        "round_robin_id": str(m["round_robin_id"]),
        "pod_a_id": str(m["pod_a_id"]) if m.get("pod_a_id") else None,
        "pod_b_id": str(m["pod_b_id"]) if m.get("pod_b_id") else None,
        "pod_a_name": m.get("pod_a_name", ""),
        "pod_b_name": m.get("pod_b_name", ""),
        "match_number": m["match_number"],
        "map": m.get("map"),
        "room_id": m.get("room_id"),
        "room_password": m.get("room_password"),
        "match_start_time": m.get("match_start_time"),
        "slot_assignments": resolved_slots,
        "full_lobby": m.get("full_lobby", False),
        "slot_limit": m.get("slot_limit", 10),
        "status": m.get("status", "scheduled"),
        "results": m.get("results", []),
        "mvp": m.get("mvp"),
        "created_at": m.get("created_at"),
    }

    # Include participants for full-lobby matches
    if m.get("full_lobby") and m.get("participants"):
        out["participants"] = m["participants"]

    return out


def serialize_round_robin(rr, include_matches=False):
    out = {
        "id": str(rr["_id"]),
        "tournament_id": str(rr["tournament_id"]),
        "stage_id": str(rr["stage_id"]),
        "name": rr["name"],
        "matches_per_pair": rr["matches_per_pair"],
        "status": rr.get("status", "active"),
        "created_at": rr.get("created_at"),
    }
    pods = []
    for pod_id in rr.get("pod_ids", []):
        pod = mongo.db.stage_pods.find_one({"_id": ObjectId(pod_id)})
        if pod:
            pods.append({
                "id": str(pod["_id"]),
                "name": pod["name"],
                "participant_count": len(pod.get("participants", [])),
            })
    out["pods"] = pods

    if include_matches:
        matches = list(mongo.db.cross_pod_matches.find(
            {"round_robin_id": rr["_id"]}
        ).sort("match_number", 1))
        out["matches"] = [serialize_cross_pod_match(m) for m in matches]

    return out


def compute_cross_pod_standings(round_robin_id):
    """Compute individual team standings across all cross-pod matches in this round-robin."""
    rr = mongo.db.cross_pod_round_robin.find_one({"_id": ObjectId(round_robin_id)})
    if not rr:
        return []

    matches = list(mongo.db.cross_pod_matches.find({
        "round_robin_id": rr["_id"],
        "status": "completed"
    }))

    # Gather all participants from all pods in this round-robin
    totals = {}
    for pod_id in rr.get("pod_ids", []):
        pod = mongo.db.stage_pods.find_one({"_id": ObjectId(pod_id)})
        if not pod:
            continue
        for p in pod.get("participants", []):
            rid = p["registration_id"]
            totals[rid] = {
                "registration_id": rid,
                "user_id": p.get("user_id"),
                "name": p["name"],
                "pod_name": pod["name"],
                "pod_id": str(pod["_id"]),
                "team_members": p.get("team_members", []),
                "matches_played": 0,
                "total_kills": 0,
                "total_points": 0,
                "chicken_dinners": 0,
            }

    for m in matches:
        for res in m.get("results", []):
            rid = res.get("registration_id")
            if rid not in totals:
                continue
            totals[rid]["matches_played"] += 1
            totals[rid]["total_kills"] += res.get("kills", 0)
            totals[rid]["total_points"] += res.get("points", 0)
            if res.get("placement") == 1:
                totals[rid]["chicken_dinners"] += 1

    standings = list(totals.values())
    standings.sort(key=lambda x: (-x["total_points"], -x["total_kills"]))
    for i, s in enumerate(standings):
        s["rank"] = i + 1
    return standings


# ---------------- CREATE CROSS-POD ROUND ROBIN ----------------
@cross_pod.route("/<tournament_id>/create", methods=["POST"])
@admin_required
def create_round_robin(tournament_id):
    """Create a cross-pod round-robin for a tournament.

    Expects JSON:
      - stage_id: the stage whose pods will be paired
      - name: e.g. "Round Robin Group Phase"
      - matches_per_pair: how many matches each pair of pods plays (default 1)
    """
    try:
        t = mongo.db.tournaments.find_one({"_id": safe_object_id(tournament_id)})
        if not t:
            return jsonify({"error": "Tournament not found"}), 404

        data = request.get_json(silent=True) or {}
        stage_id = data.get("stage_id")
        name = data.get("name")
        matches_per_pair = max(int(data.get("matches_per_pair") or 1), 1)

        if not stage_id:
            return jsonify({"error": "stage_id is required"}), 400
        if not name:
            return jsonify({"error": "Round-robin name is required"}), 400

        stage = mongo.db.tournament_stages.find_one({"_id": safe_object_id(stage_id)})
        if not stage:
            return jsonify({"error": "Stage not found"}), 404

        pods = list(mongo.db.stage_pods.find({"stage_id": stage["_id"]}))
        if len(pods) < 2:
            return jsonify({"error": "Need at least 2 pods/groups to create cross-pod pairings"}), 400

        pod_ids = [str(p["_id"]) for p in pods]
        pairings = generate_round_robin_pairings(pod_ids, matches_per_pair)

        rr_doc = {
            "tournament_id": ObjectId(tournament_id),
            "stage_id": ObjectId(stage_id),
            "name": name,
            "pod_ids": pod_ids,
            "matches_per_pair": matches_per_pair,
            "pairings": pairings,
            "status": "active",
            "created_at": datetime.utcnow(),
        }
        result = mongo.db.cross_pod_round_robin.insert_one(rr_doc)
        rr_doc["_id"] = result.inserted_id

        # Create cross-pod match documents
        pod_map = {str(p["_id"]): p for p in pods}
        match_number = 0
        for pairing in pairings:
            match_number += 1
            pa = pod_map.get(pairing["pod_a"], {})
            pb = pod_map.get(pairing["pod_b"], {})
            match_doc = {
                "round_robin_id": rr_doc["_id"],
                "tournament_id": ObjectId(tournament_id),
                "pod_a_id": ObjectId(pairing["pod_a"]),
                "pod_b_id": ObjectId(pairing["pod_b"]),
                "pod_a_name": pa.get("name", ""),
                "pod_b_name": pb.get("name", ""),
                "match_number": match_number,
                "map": None,
                "room_id": None,
                "room_password": None,
                "match_start_time": None,
                "status": "scheduled",
                "results": [],
                "mvp": None,
                "created_at": datetime.utcnow(),
            }
            mongo.db.cross_pod_matches.insert_one(match_doc)

        return jsonify(serialize_round_robin(rr_doc, include_matches=True))

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ---------------- CREATE FULL LOBBY MATCHES (12 teams, no groups) ----------------
@cross_pod.route("/<tournament_id>/create-full-lobby", methods=["POST"])
@admin_required
def create_full_lobby(tournament_id):
    """Create full-lobby matches where all teams play together (no group system).
    Used for league/tournament format where every match has all teams.

    Expects JSON:
      - stage_id: the stage whose roster to use
      - name: e.g. "Day 1 - Full Lobby"
      - match_count (optional): number of matches to create (default 3, max 3)
    """
    try:
        t = mongo.db.tournaments.find_one({"_id": safe_object_id(tournament_id)})
        if not t:
            return jsonify({"error": "Tournament not found"}), 404

        data = request.get_json(silent=True) or {}
        stage_id = data.get("stage_id")
        name = data.get("name") or "Full Lobby"
        match_count = min(int(data.get("match_count") or 3), 3)  # Max 3 matches

        # Get all approved (non-disqualified) participants
        roster = get_roster_by_id(tournament_id)
        participants = list(roster.values())

        if len(participants) < 2:
            return jsonify({"error": "Need at least 2 approved participants"}), 400

        # Hardcoded 12 slots
        slot_limit = 12

        # Check if there's an existing round-robin for this stage (or tournament-level)
        query = {"tournament_id": ObjectId(tournament_id)}
        if stage_id:
            query["stage_id"] = ObjectId(stage_id)
        else:
            query["stage_id"] = {"$exists": False}

        existing_rr = mongo.db.cross_pod_round_robin.find_one(query)

        if existing_rr:
            rr_id = existing_rr["_id"]
            existing_count = mongo.db.cross_pod_matches.count_documents({"round_robin_id": rr_id})
            start_num = existing_count + 1

            maps = ["Bermuda", "Pulgatory", "Kalahari", "Livik", "Sanhok"]
            created_matches = []

            for i in range(match_count):
                match_num = start_num + i
                map_name = maps[i % len(maps)]

                match_doc = {
                    "round_robin_id": rr_id,
                    "tournament_id": ObjectId(tournament_id),
                    "pod_a_id": None,
                    "pod_b_id": None,
                    "pod_a_name": "All Teams",
                    "pod_b_name": "All Teams",
                    "match_number": match_num,
                    "map": map_name,
                    "room_id": None,
                    "room_password": None,
                    "match_start_time": None,
                    "full_lobby": True,
                    "slot_limit": slot_limit,
                    "participants": participants,
                    "status": "scheduled",
                    "results": [],
                    "mvp": None,
                    "created_at": datetime.utcnow(),
                }
                result = mongo.db.cross_pod_matches.insert_one(match_doc)
                match_doc["_id"] = result.inserted_id
                created_matches.append(match_doc)

            return jsonify({
                "message": f"Created {len(created_matches)} full-lobby matches",
                "matches": [serialize_cross_pod_match(m) for m in created_matches]
            })
        else:
            # Create new round-robin with full-lobby matches only
            pod_ids = []
            if stage_id:
                pod_ids = [str(p["_id"]) for p in list(mongo.db.stage_pods.find({"stage_id": safe_object_id(stage_id)}))]

            rr_doc = {
                "tournament_id": ObjectId(tournament_id),
                "stage_id": ObjectId(stage_id) if stage_id else None,
                "name": name,
                "pod_ids": pod_ids,
                "matches_per_pair": 0,
                "pairings": [],
                "status": "active",
                "created_at": datetime.utcnow(),
            }
            result = mongo.db.cross_pod_round_robin.insert_one(rr_doc)
            rr_doc["_id"] = result.inserted_id

            maps = ["Bermuda", "Pulgatory", "Kalahari", "Livik", "Sanhok"]
            created_matches = []
            for i in range(match_count):
                match_num = i + 1
                map_name = maps[i % len(maps)]
                match_doc = {
                    "round_robin_id": rr_doc["_id"],
                    "tournament_id": ObjectId(tournament_id),
                    "pod_a_id": None,
                    "pod_b_id": None,
                    "pod_a_name": "All Teams",
                    "pod_b_name": "All Teams",
                    "match_number": match_num,
                    "map": map_name,
                    "room_id": None,
                    "room_password": None,
                    "match_start_time": None,
                    "full_lobby": True,
                    "slot_limit": slot_limit,
                    "participants": participants,
                    "status": "scheduled",
                    "results": [],
                    "mvp": None,
                    "created_at": datetime.utcnow(),
                }
                result = mongo.db.cross_pod_matches.insert_one(match_doc)
                match_doc["_id"] = result.inserted_id
                created_matches.append(match_doc)

            return jsonify(serialize_round_robin(rr_doc, include_matches=True))

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ---------------- LIST ROUND ROBINS FOR A TOURNAMENT ----------------
@cross_pod.route("/tournament/<tournament_id>", methods=["GET"])
@jwt_required()
def list_round_robins(tournament_id):
    rrs = list(mongo.db.cross_pod_round_robin.find(
        {"tournament_id": safe_object_id(tournament_id)}
    ).sort("created_at", -1))
    return jsonify([serialize_round_robin(rr) for rr in rrs])


# ---------------- ROUND ROBIN DETAIL (incl. matches) ----------------
@cross_pod.route("/<rr_id>", methods=["GET"])
@jwt_required()
def get_round_robin(rr_id):
    rr = mongo.db.cross_pod_round_robin.find_one({"_id": safe_object_id(rr_id)})
    if not rr:
        return jsonify({"error": "Round robin not found"}), 404
    return jsonify(serialize_round_robin(rr, include_matches=True))


# ---------------- ROUND ROBIN STANDINGS ----------------
@cross_pod.route("/<rr_id>/standings", methods=["GET"])
@jwt_required()
def get_round_robin_standings(rr_id):
    rr = mongo.db.cross_pod_round_robin.find_one({"_id": safe_object_id(rr_id)})
    if not rr:
        return jsonify({"error": "Round robin not found"}), 404
    standings = compute_cross_pod_standings(rr_id)
    return jsonify(standings)


# ---------------- CROSS-POD MATCH DETAIL (with participants) ----------------
@cross_pod.route("/matches/<match_id>", methods=["GET"])
@jwt_required()
def get_cross_pod_match(match_id):
    m = mongo.db.cross_pod_matches.find_one({"_id": safe_object_id(match_id)})
    if not m:
        return jsonify({"error": "Match not found"}), 404

    out = serialize_cross_pod_match(m)

    # For full-lobby matches, use the stored participants
    if m.get("full_lobby") and m.get("participants"):
        enriched = []
        for pt in m["participants"]:
            part = dict(pt)
            part["pod_name"] = "All Teams"
            if "team_leader" not in part:
                reg = mongo.db.registrations.find_one({"_id": safe_object_id(part.get("registration_id"))})
                if reg and reg.get("team_leader"):
                    part["team_leader"] = reg["team_leader"]
            enriched.append(part)
        out["participants"] = enriched
    else:
        # Attach participants from both pods
        for pod_id_field, key in [("pod_a_id", "pod_a_participants"), ("pod_b_id", "pod_b_participants")]:
            if m.get(pod_id_field):
                pod = mongo.db.stage_pods.find_one({"_id": m[pod_id_field]})
                if pod:
                    enriched = []
                    for pt in pod.get("participants", []):
                        part = dict(pt)
                        part["pod_name"] = pod.get("name", "")
                        if "team_leader" not in part:
                            reg = mongo.db.registrations.find_one({"_id": safe_object_id(part.get("registration_id"))})
                            if reg and reg.get("team_leader"):
                                part["team_leader"] = reg["team_leader"]
                        enriched.append(part)
                    out[key] = enriched
                else:
                    out[key] = []
            else:
                out[key] = []

    return jsonify(out)


# ---------------- ADD MATCH TO A ROUND ROBIN ----------------
@cross_pod.route("/<rr_id>/matches", methods=["POST"])
@admin_required
def add_cross_pod_match(rr_id):
    """Add an extra cross-pod match (admin manually picks the two pods)."""
    rr = mongo.db.cross_pod_round_robin.find_one({"_id": safe_object_id(rr_id)})
    if not rr:
        return jsonify({"error": "Round robin not found"}), 404
    if rr.get("status") != "active":
        return jsonify({"error": "Round robin is not active"}), 400

    data = request.get_json(silent=True) or {}
    pod_a_id = data.get("pod_a_id")
    pod_b_id = data.get("pod_b_id")

    if not pod_a_id or not pod_b_id:
        return jsonify({"error": "Both pod_a_id and pod_b_id are required"}), 400
    if pod_a_id == pod_b_id:
        return jsonify({"error": "Cannot pair a pod with itself"}), 400

    pod_a = mongo.db.stage_pods.find_one({"_id": safe_object_id(pod_a_id)})
    pod_b = mongo.db.stage_pods.find_one({"_id": safe_object_id(pod_b_id)})
    if not pod_a or not pod_b:
        return jsonify({"error": "One or both pods not found"}), 404

    match_count = mongo.db.cross_pod_matches.count_documents({"round_robin_id": rr["_id"]})
    match_doc = {
        "round_robin_id": rr["_id"],
        "tournament_id": rr["tournament_id"],
        "pod_a_id": pod_a["_id"],
        "pod_b_id": pod_b["_id"],
        "pod_a_name": pod_a.get("name", ""),
        "pod_b_name": pod_b.get("name", ""),
        "match_number": match_count + 1,
        "map": data.get("map"),
        "room_id": None,
        "room_password": None,
        "match_start_time": None,
        "status": "scheduled",
        "results": [],
        "mvp": None,
        "created_at": datetime.utcnow(),
    }
    result = mongo.db.cross_pod_matches.insert_one(match_doc)
    match_doc["_id"] = result.inserted_id
    return jsonify(serialize_cross_pod_match(match_doc))


# ---------------- RELEASE ROOM FOR CROSS-POD MATCH ----------------
@cross_pod.route("/matches/<match_id>/slots", methods=["POST"])
@admin_required
def update_cross_pod_slots(match_id):
    m = mongo.db.cross_pod_matches.find_one({"_id": safe_object_id(match_id)})
    if not m:
        return jsonify({"error": "Match not found"}), 404

    data = request.get_json(silent=True) or {}
    slot_assignments = data.get("slot_assignments", {})
    slot_limit = 12 if m.get("full_lobby") else 10

    if not isinstance(slot_assignments, dict):
        return jsonify({"error": "slot_assignments must be an object"}), 400
    for slot, reg_id in slot_assignments.items():
        try:
            slot_num = int(slot)
            if slot_num < 1 or slot_num > slot_limit:
                return jsonify({"error": f"Invalid slot: {slot}. Must be 1-{slot_limit}"}), 400
        except ValueError:
            return jsonify({"error": f"Invalid slot key: {slot}"}), 400

    mongo.db.cross_pod_matches.update_one(
        {"_id": m["_id"]},
        {"$set": {"slot_assignments": slot_assignments}}
    )

    return jsonify({"message": "Slots updated successfully"})


@cross_pod.route("/matches/<match_id>/room", methods=["POST"])
@admin_required
def release_cross_pod_room(match_id):
    m = mongo.db.cross_pod_matches.find_one({"_id": safe_object_id(match_id)})
    if not m:
        return jsonify({"error": "Match not found"}), 404

    data = request.get_json(silent=True) or {}
    room_id = data.get("room_id")
    password = data.get("password")
    start_time = data.get("start_time")
    map_name = data.get("map")
    slot_assignments = data.get("slot_assignments", {})
    slot_limit = 12 if m.get("full_lobby") else 10

    if not room_id or not password:
        return jsonify({"error": "Room ID and password are required"}), 400

    # Validate slot assignments
    if slot_assignments:
        if not isinstance(slot_assignments, dict):
            return jsonify({"error": "slot_assignments must be an object"}), 400
        for slot, reg_id in slot_assignments.items():
            try:
                slot_num = int(slot)
                if slot_num < 1 or slot_num > slot_limit:
                    return jsonify({"error": f"Invalid slot number: {slot}. Must be 1-{slot_limit}"}), 400
            except ValueError:
                return jsonify({"error": f"Invalid slot key: {slot}"}), 400

    update_fields = {"room_id": room_id, "room_password": password, "match_start_time": start_time}
    if map_name:
        update_fields["map"] = map_name
    update_fields["slot_assignments"] = slot_assignments

    mongo.db.cross_pod_matches.update_one(
        {"_id": m["_id"]},
        {"$set": update_fields}
    )

    # Notify participants - full lobby uses match participants, regular uses pods
    notified_user_ids = set()
    participants_to_notify = []

    if m.get("full_lobby") and m.get("participants"):
        participants_to_notify = m["participants"]
    else:
        for pod_id_field in ["pod_a_id", "pod_b_id"]:
            if m.get(pod_id_field):
                pod = mongo.db.stage_pods.find_one({"_id": m[pod_id_field]})
                if pod:
                    participants_to_notify.extend(pod.get("participants", []))

    for participant in participants_to_notify:
        if participant.get("user_id") and participant["user_id"] not in notified_user_ids:
            notified_user_ids.add(participant["user_id"])
            create_notification(
                mongo,
                participant["user_id"],
                f"Room is live for {m['pod_a_name']} vs {m['pod_b_name']} — Match {m['match_number']}. Check the standings page!",
                ntype="room",
                tournament_id=str(m["tournament_id"])
            )
        for member in participant.get("team_members", []):
            member_uid = member.get("user_id")
            if member_uid and member_uid not in notified_user_ids:
                notified_user_ids.add(member_uid)
                create_notification(
                    mongo,
                    member_uid,
                    f"Room is live for {m['pod_a_name']} vs {m['pod_b_name']} — Match {m['match_number']}. Check the standings page!",
                    ntype="room",
                    tournament_id=str(m["tournament_id"])
                )

    return jsonify({"message": "Room released"})


# ---------------- SUBMIT CROSS-POD MATCH RESULTS ----------------
@cross_pod.route("/matches/<match_id>/results", methods=["POST"])
@admin_required
def submit_cross_pod_results(match_id):
    try:
        m = mongo.db.cross_pod_matches.find_one({"_id": safe_object_id(match_id)})
        if not m:
            return jsonify({"error": "Match not found"}), 404

        t, points_table, kill_point_value = get_tournament_scoring(str(m["tournament_id"]))
        if not t:
            return jsonify({"error": "Tournament not found"}), 404

        data = request.get_json(silent=True) or {}
        raw_results = data.get("results", [])
        if not raw_results:
            return jsonify({"error": "No results submitted"}), 400

        # Build name lookup - full lobby uses match participants, regular uses pods
        name_lookup = {}
        if m.get("full_lobby") and m.get("participants"):
            for pt in m["participants"]:
                name_lookup[pt["registration_id"]] = pt
        else:
            for pod_id_field in ["pod_a_id", "pod_b_id"]:
                if m.get(pod_id_field):
                    pod = mongo.db.stage_pods.find_one({"_id": m[pod_id_field]})
                    if pod:
                        for pt in pod.get("participants", []):
                            name_lookup[pt["registration_id"]] = pt

        prev_results = m.get("results", [])

        results = []
        for r in raw_results:
            rid = r.get("registration_id")
            if rid not in name_lookup:
                continue
            placement = int(r.get("placement", 0))
            participant = name_lookup[rid]
            registration = mongo.db.registrations.find_one({"_id": safe_object_id(rid)})

            raw_players = r.get("players")
            if raw_players:
                players = []
                leader_name = None
                if registration and registration.get("team_leader"):
                    leader_name = registration["team_leader"].get("name")
                for pl in raw_players:
                    pl_name = pl.get("name", "")
                    pl_kills = int(pl.get("kills", 0))
                    pl_user_id = None
                    if registration:
                        if leader_name and pl_name == leader_name:
                            pl_user_id = registration.get("user_id")
                        else:
                            for tm in registration.get("team_members", []):
                                if tm.get("name") == pl_name:
                                    pl_user_id = tm.get("user_id")
                                    break
                    players.append({"name": pl_name, "kills": pl_kills, "user_id": pl_user_id})
                kills = sum(pl["kills"] for pl in players)
            else:
                players = []
                kills = int(r.get("kills", 0))

            results.append({
                "registration_id": rid,
                "name": participant["name"],
                "placement": placement,
                "kills": kills,
                "points": calc_points(placement, kills, points_table, kill_point_value),
                "players": players
            })

        mvp = compute_match_mvp(results)

        mongo.db.cross_pod_matches.update_one(
            {"_id": m["_id"]},
            {"$set": {"results": results, "status": "completed", "mvp": mvp}}
        )

        # Apply kill deltas
        _apply_kill_deltas(prev_results, results, t.get("game", ""))

        # Increment tournaments_played for all participants
        for r in results:
            pt = name_lookup.get(r["registration_id"])
            if pt and pt.get("user_id"):
                increment_tournaments_played(mongo, pt["user_id"], t.get("game", ""))

        # Notify participants
        for r in results:
            pt = name_lookup.get(r["registration_id"])
            if pt and pt.get("user_id"):
                create_notification(
                    mongo,
                    pt["user_id"],
                    f"Results are out for {m['pod_a_name']} vs {m['pod_b_name']} — Match {m['match_number']}: #{r['placement']} place, {r['kills']} kills ({r['points']} pts).",
                    ntype="winner",
                    tournament_id=str(m["tournament_id"])
                )
                for member in pt.get("team_members", []):
                    member_uid = member.get("user_id")
                    if member_uid:
                        create_notification(
                            mongo,
                            member_uid,
                            f"Results are out for {m['pod_a_name']} vs {m['pod_b_name']} — Match {m['match_number']}: #{r['placement']} place, {r['kills']} kills ({r['points']} pts).",
                            ntype="winner",
                            tournament_id=str(m["tournament_id"])
                        )

        return jsonify({"message": "Results submitted", "results": results, "mvp": mvp})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _apply_kill_deltas(prev_results, new_results, game):
    prev_kills = {}
    for r in prev_results:
        for pl in r.get("players", []):
            uid = pl.get("user_id")
            if uid:
                prev_kills[uid] = prev_kills.get(uid, 0) + pl.get("kills", 0)

    new_kills = {}
    for r in new_results:
        for pl in r.get("players", []):
            uid = pl.get("user_id")
            if uid:
                new_kills[uid] = new_kills.get(uid, 0) + pl.get("kills", 0)

    all_user_ids = set(prev_kills.keys()) | set(new_kills.keys())
    for uid in all_user_ids:
        old = prev_kills.get(uid, 0)
        new = new_kills.get(uid, 0)
        delta = new - old
        if delta != 0:
            upsert_player_stats(uid, game, kills_delta=delta)


# ---------------- FINALIZE ROUND ROBIN ----------------
@cross_pod.route("/<rr_id>/finalize", methods=["POST"])
@admin_required
def finalize_round_robin(rr_id):
    rr = mongo.db.cross_pod_round_robin.find_one({"_id": safe_object_id(rr_id)})
    if not rr:
        return jsonify({"error": "Round robin not found"}), 404
    if rr.get("status") == "completed":
        return jsonify({"error": "Round robin already finalized"}), 400

    # Check all matches are completed
    incomplete = mongo.db.cross_pod_matches.count_documents({
        "round_robin_id": rr["_id"],
        "status": {"$ne": "completed"}
    })
    if incomplete > 0:
        return jsonify({"error": f"{incomplete} match(es) still not completed"}), 400

    standings = compute_cross_pod_standings(rr_id)

    mongo.db.cross_pod_round_robin.update_one(
        {"_id": rr["_id"]},
        {"$set": {"status": "completed", "final_standings": standings}}
    )

    # If this is linked to a final stage, declare winner
    stage = mongo.db.tournament_stages.find_one({"_id": rr["stage_id"]})
    if stage and stage.get("is_final") and standings:
        champion = standings[0]
        t = mongo.db.tournaments.find_one({"_id": rr["tournament_id"]})
        game = t.get("game", "") if t else ""

        update_fields = build_winner_update(champion, stage_name=stage["name"], source="cross_pod_round_robin")
        mongo.db.tournaments.update_one({"_id": rr["tournament_id"]}, {"$set": update_fields})

        # Notify all participants
        for pod_id in rr.get("pod_ids", []):
            pod = mongo.db.stage_pods.find_one({"_id": ObjectId(pod_id)})
            if pod:
                for participant in pod.get("participants", []):
                    if participant.get("user_id"):
                        increment_tournaments_played(mongo, participant["user_id"], game)
                        is_champ = participant["registration_id"] == champion["registration_id"]
                        msg = (f"Congratulations! Your team won {rr['name']}!" if is_champ
                               else f"{rr['name']} has concluded. Check the final standings!")
                        create_notification(mongo, participant["user_id"], msg, ntype="winner",
                                             tournament_id=str(rr["tournament_id"]))
                        for member in participant.get("team_members", []):
                            member_uid = member.get("user_id")
                            if member_uid:
                                member_msg = (f"Congratulations! Your team won {rr['name']}!" if is_champ
                                              else f"{rr['name']} has concluded. Check the final standings!")
                                create_notification(mongo, member_uid, member_msg, ntype="winner",
                                                     tournament_id=str(rr["tournament_id"]))

    return jsonify({"message": "Round robin finalized", "standings": standings})


# ---------------- DELETE CROSS-POD MATCH ----------------
@cross_pod.route("/matches/<match_id>", methods=["DELETE"])
@admin_required
def delete_cross_pod_match(match_id):
    m = mongo.db.cross_pod_matches.find_one({"_id": safe_object_id(match_id)})
    if not m:
        return jsonify({"error": "Match not found"}), 404
    if m.get("status") == "completed":
        return jsonify({"error": "Cannot delete a completed match"}), 400
    mongo.db.cross_pod_matches.delete_one({"_id": m["_id"]})
    return jsonify({"message": "Match deleted"})


# ---------------- DELETE ROUND ROBIN ----------------
@cross_pod.route("/<rr_id>", methods=["DELETE"])
@admin_required
def delete_round_robin(rr_id):
    rr = mongo.db.cross_pod_round_robin.find_one({"_id": safe_object_id(rr_id)})
    if not rr:
        return jsonify({"error": "Round robin not found"}), 404
    mongo.db.cross_pod_matches.delete_many({"round_robin_id": rr["_id"]})
    mongo.db.cross_pod_round_robin.delete_one({"_id": rr["_id"]})
    return jsonify({"message": "Round robin deleted"})


@cross_pod.route("/<rr_id>/fix-pairings", methods=["POST"])
@jwt_required()
def fix_round_robin_pairings(rr_id):
    """Fix existing round-robin match pairings to follow the rotating day format
    without deleting matches (preserves results).
    
    Day 1: AB, AC, BC  (matches 1-3, usually already correct)
    Day 2: BC, AB, AC  (matches 4-6)
    Day 3: AC, BC, AB  (matches 7-9)
    """
    admin_id = get_jwt_identity()
    admin = mongo.db.users.find_one({"_id": ObjectId(admin_id)})
    if not admin or admin.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403

    rr = mongo.db.cross_pod_round_robin.find_one({"_id": safe_object_id(rr_id)})
    if not rr:
        return jsonify({"error": "Round robin not found"}), 404

    pod_ids = rr.get("pod_ids", [])
    if len(pod_ids) < 2:
        return jsonify({"error": "Not enough pods"}), 400

    # Build correct rotating pairings
    pairs = []
    for i in range(len(pod_ids)):
        for j in range(i + 1, len(pod_ids)):
            pairs.append((pod_ids[i], pod_ids[j]))

    correct_pairings = []
    match_number = 0
    for m in range(rr.get("matches_per_pair", 1)):
        rotated = pairs if m == 0 else pairs[-m:] + pairs[:-m]
        for pair in rotated:
            match_number += 1
            correct_pairings.append({
                "pod_a": pair[0],
                "pod_b": pair[1],
                "match_number": match_number,
            })

    # Get existing matches sorted by match_number
    matches = list(mongo.db.cross_pod_matches.find(
        {"round_robin_id": rr["_id"]}
    ).sort("match_number", 1))

    # Build pod name lookup
    pod_map = {}
    for pid in pod_ids:
        pod = mongo.db.stage_pods.find_one({"_id": ObjectId(pid)})
        if pod:
            pod_map[pid] = pod.get("name", "Unknown")

    updated = 0
    skipped = 0
    for i, match in enumerate(matches):
        if i >= len(correct_pairings):
            break

        correct = correct_pairings[i]
        new_pod_a_id = correct["pod_a"]
        new_pod_b_id = correct["pod_b"]

        # Skip matches that already have results (don't mess with played matches)
        if match.get("results"):
            skipped += 1
            continue

        # Check if pairings are already correct
        old_pod_a = str(match.get("pod_a_id", ""))
        old_pod_b = str(match.get("pod_b_id", ""))
        if old_pod_a == new_pod_a_id and old_pod_b == new_pod_b_id:
            continue

        # Update the match
        mongo.db.cross_pod_matches.update_one(
            {"_id": match["_id"]},
            {"$set": {
                "pod_a_id": ObjectId(new_pod_a_id),
                "pod_b_id": ObjectId(new_pod_b_id),
                "pod_a_name": pod_map.get(new_pod_a_id, ""),
                "pod_b_name": pod_map.get(new_pod_b_id, ""),
            }}
        )
        updated += 1

    # Also update the pairings stored in the round-robin doc
    mongo.db.cross_pod_round_robin.update_one(
        {"_id": rr["_id"]},
        {"$set": {"pairings": correct_pairings}}
    )

    return jsonify({
        "message": f"Fixed {updated} matches, skipped {skipped} (already played)",
        "total_matches": len(matches),
        "updated": updated,
        "skipped": skipped
    })

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime
from functools import wraps

from routes.notification_routes import create_notification
from routes.player_stats_routes import upsert_player_stats, upsert_global_wins
from utils.player_stats import increment_tournaments_played, normalize_game as _normalize_game
from utils.tournament_lifecycle import build_stage_seed_distribution, build_winner_update, is_registration_open

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
    """registration_id -> {registration_id, user_id, name, team_members, team_leader} for every approved entry."""
    registrations = mongo.db.registrations.find({
        "tournament_id": ObjectId(tournament_id),
        "payment_status": {"$nin": ["rejected", "disqualified"]}
    })
    roster = {}
    seen_teams = set()
    for r in registrations:
        # For squad mode: skip teammate registrations (only keep leader)
        team_name = r.get("team_name")
        if team_name:
            if team_name in seen_teams:
                continue
            seen_teams.add(team_name)

        user = mongo.db.users.find_one({"_id": safe_object_id(r.get("user_id"))})
        display_name = team_name or (user.get("name") if user else "Unknown")
        roster[str(r["_id"])] = {
            "registration_id": str(r["_id"]),
            "user_id": r.get("user_id"),
            "name": display_name,
            "team_members": r.get("team_members", []),
            "team_leader": r.get("team_leader")
        }
    return roster


def distribute_random(participants, pod_count):
    return build_stage_seed_distribution(participants, pod_count, strategy="random")


def distribute_snake(ranked_participants, pod_count):
    return build_stage_seed_distribution(ranked_participants, pod_count, strategy="snake")


def compute_pod_standings(pod_doc):
    matches = list(mongo.db.stage_matches.find({"pod_id": pod_doc["_id"], "status": "completed"}))

    totals = {}
    for p in pod_doc.get("participants", []):
        totals[p["registration_id"]] = {
            "registration_id": p["registration_id"],
            "user_id": p.get("user_id"),
            "name": p["name"],
            "team_members": p.get("team_members", []),
            "matches_played": 0,
            "total_kills": 0,
            "total_points": 0,
            "chicken_dinners": 0
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


def compute_match_mvp(results):
    """Compute match MVP based on kills + placement points.

    MVP score = team placement points + (player kills * kill_point_value)
    Falls back to just kills if points data is unavailable.
    """
    best = None
    best_score = -1
    for r in results:
        placement = r.get("placement", 0)
        team_points = r.get("points", 0)
        kills = r.get("kills", 0)
        players = r.get("players") or [{"name": r["name"], "kills": kills}]

        for p in players:
            p_kills = p.get("kills", 0)
            # Score = player kills + team placement points bonus
            # Player contribution: their kills * kill value (approximated from team total)
            # + team placement credit so a player on a winning team gets bonus
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


def serialize_pod(p, include_matches=False):
    participants = p.get("participants", [])
    # Enrich participants with team_leader if missing (old pods created before fix)
    enriched = []
    for part in participants:
        part = dict(part)
        if "team_leader" not in part:
            reg = mongo.db.registrations.find_one({"_id": safe_object_id(part.get("registration_id"))})
            if reg and reg.get("team_leader"):
                part["team_leader"] = reg["team_leader"]
        enriched.append(part)

    out = {
        "id": str(p["_id"]),
        "stage_id": str(p["stage_id"]),
        "pod_index": p["pod_index"],
        "name": p["name"],
        "status": p.get("status", "active"),
        "participants": enriched,
        "final_standings": p.get("final_standings")
    }
    if include_matches:
        matches = list(mongo.db.stage_matches.find({"pod_id": p["_id"]}).sort("match_number", 1))
        out["matches"] = [serialize_match(m) for m in matches]
    return out


def serialize_match(m):
    return {
        "id": str(m["_id"]),
        "pod_id": str(m["pod_id"]),
        "match_number": m["match_number"],
        "map": m.get("map"),
        "room_id": m.get("room_id"),
        "room_password": m.get("room_password"),
        "match_start_time": m.get("match_start_time"),
        "status": m.get("status", "scheduled"),
        "results": m.get("results", []),
        "mvp": m.get("mvp")
    }


def serialize_stage(s, include_pods=False):
    out = {
        "id": str(s["_id"]),
        "tournament_id": str(s["tournament_id"]),
        "stage_index": s["stage_index"],
        "name": s["name"],
        "is_final": s.get("is_final", False),
        "advance_count": s.get("advance_count"),
        "status": s.get("status", "active"),
    }
    pods = list(mongo.db.stage_pods.find({"stage_id": s["_id"]}).sort("pod_index", 1))
    out["pod_count"] = len(pods)
    out["team_count"] = sum(len(p.get("participants", [])) for p in pods)
    if include_pods:
        out["pods"] = [serialize_pod(p) for p in pods]
    return out


# ---------------- CREATE A STAGE (with pods) ----------------
@stage.route("/<tournament_id>/create", methods=["POST"])
@admin_required
def create_stage(tournament_id):
    try:
        t = mongo.db.tournaments.find_one({"_id": safe_object_id(tournament_id)})
        if not t:
            return jsonify({"error": "Tournament not found"}), 404

        data = request.get_json(silent=True) or {}
        name = data.get("name")
        is_final = bool(data.get("is_final", False))
        advance_count = data.get("advance_count")
        try:
            pod_count = max(int(data.get("pod_count") or 1), 1)
        except (TypeError, ValueError):
            pod_count = 1
        seed_strategy = data.get("seed_strategy") or t.get("seed_strategy", "random")

        if not name:
            return jsonify({"error": "Stage name is required"}), 400

        existing = list(mongo.db.tournament_stages.find({"tournament_id": ObjectId(tournament_id)}))
        roster_by_id = get_roster_by_id(tournament_id)

        if not existing:
            if is_registration_open(t):
                return jsonify({"error": "Registration is still open — groups can be launched only after registration closes"}), 400
            participants = list(roster_by_id.values())
            if len(participants) < 2:
                return jsonify({"error": "Need at least 2 approved participants to start a stage"}), 400
            stage_index = 0
            distribution = distribute_random(participants, pod_count) if seed_strategy != "snake" else distribute_snake(participants, pod_count)
        else:
            prev = max(existing, key=lambda s: s["stage_index"])
            if prev.get("status") != "completed":
                return jsonify({"error": f"Finalize '{prev['name']}' before starting the next stage"}), 400

            prev_pods = list(mongo.db.stage_pods.find({"stage_id": prev["_id"]}))
            pool = []
            for pod in prev_pods:
                standings = pod.get("final_standings") or compute_pod_standings(pod)
                cutoff = prev.get("advance_count") or len(standings)
                pool.extend(standings[:cutoff])

            pool.sort(key=lambda x: (-x["total_points"], -x["total_kills"]))
            enriched = []
            for p in pool:
                base = roster_by_id.get(p["registration_id"], {})
                enriched.append({
                    "registration_id": p["registration_id"],
                    "user_id": p.get("user_id"),
                    "name": p["name"],
                    "team_members": base.get("team_members", []),
                    "team_leader": base.get("team_leader")
                })

            if len(enriched) < 2:
                return jsonify({"error": "Not enough teams advanced to start this stage"}), 400

            stage_index = prev["stage_index"] + 1
            distribution = distribute_snake(enriched, pod_count)

        doc = {
            "tournament_id": ObjectId(tournament_id),
            "stage_index": stage_index,
            "name": name,
            "is_final": is_final,
            "advance_count": int(advance_count) if advance_count else None,
            "status": "active",
            "created_at": datetime.utcnow()
        }
        result = mongo.db.tournament_stages.insert_one(doc)
        doc["_id"] = result.inserted_id

        letters = "ABCDEFGHIJKLMNOP"
        for i, pod_participants in enumerate(distribution):
            if not pod_participants:
                continue
            pod_doc = {
                "stage_id": doc["_id"],
                "tournament_id": ObjectId(tournament_id),
                "pod_index": i,
                "name": f"Group {letters[i] if i < len(letters) else i+1}" if pod_count > 1 else name,
                "participants": pod_participants,
                "status": "active",
                "final_standings": None,
                "created_at": datetime.utcnow()
            }
            mongo.db.stage_pods.insert_one(pod_doc)

        mongo.db.tournaments.update_one(
            {"_id": ObjectId(tournament_id)},
            {
                "$set": {"status": "in_progress", "grouping_status": "finalized"},
                "$push": {
                    "stage_flow": {
                        "stage_id": str(doc["_id"]),
                        "name": name,
                        "stage_index": stage_index,
                        "is_final": is_final,
                        "status": "active",
                        "created_at": datetime.utcnow()
                    }
                }
            }
        )

        if stage_index == 0:
            notify_group_assignments(tournament_id, doc["_id"], name)

        return jsonify(serialize_stage(doc, include_pods=True))

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def notify_group_assignments(tournament_id, stage_id, stage_name):
    """Tell every participant which group/pod they landed in for this stage."""
    pods = list(mongo.db.stage_pods.find({"stage_id": stage_id}))
    for pod in pods:
        for participant in pod.get("participants", []):
            if participant.get("user_id"):
                create_notification(
                    mongo,
                    participant["user_id"],
                    f"You've been placed in {pod['name']} for {stage_name}. Check My Tournaments for details.",
                    ntype="group",
                    tournament_id=str(tournament_id)
                )
                # Also notify team members
                for member in participant.get("team_members", []):
                    member_uid = member.get("user_id")
                    if member_uid:
                        create_notification(
                            mongo,
                            member_uid,
                            f"You've been placed in {pod['name']} for {stage_name}. Check My Tournaments for details.",
                            ntype="group",
                            tournament_id=str(tournament_id)
                        )


# ---------------- CREATE A STAGE WITH ADMIN-PICKED (MANUAL) GROUPS ----------------
@stage.route("/<tournament_id>/create-manual", methods=["POST"])
@admin_required
def create_manual_stage(tournament_id):
    """Same as /create but the admin hand-picks who goes in which group,
    instead of random/snake auto-distribution. Only usable for the first
    stage of a tournament (later stages still use the auto flow)."""

    t = mongo.db.tournaments.find_one({"_id": safe_object_id(tournament_id)})
    if not t:
        return jsonify({"error": "Tournament not found"}), 404

    existing = mongo.db.tournament_stages.find_one({"tournament_id": ObjectId(tournament_id)})
    if existing:
        return jsonify({"error": "Manual grouping is only available for the first stage"}), 400

    if is_registration_open(t):
        return jsonify({"error": "Registration is still open — groups can be launched only after registration closes"}), 400

    data = request.get_json(silent=True) or {}
    name = data.get("name")
    is_final = bool(data.get("is_final", False))
    advance_count = data.get("advance_count")
    groups = data.get("groups")  # [[registration_id, ...], [registration_id, ...], ...]

    if not name:
        return jsonify({"error": "Stage name is required"}), 400
    if not groups or not isinstance(groups, list) or not any(groups):
        return jsonify({"error": "At least one non-empty group is required"}), 400

    roster_by_id = get_roster_by_id(tournament_id)
    if len(roster_by_id) < 2:
        return jsonify({"error": "Need at least 2 approved participants to start a stage"}), 400

    seen = set()
    distribution = []
    for group in groups:
        pod_participants = []
        for rid in group or []:
            if rid not in roster_by_id:
                return jsonify({"error": f"Registration {rid} is not an approved participant"}), 400
            if rid in seen:
                return jsonify({"error": f"{roster_by_id[rid]['name']} was placed in more than one group"}), 400
            seen.add(rid)
            pod_participants.append(roster_by_id[rid])
        distribution.append(pod_participants)

    unassigned = [v["name"] for k, v in roster_by_id.items() if k not in seen]
    if unassigned:
        return jsonify({"error": f"These teams aren't placed in any group yet: {', '.join(unassigned)}"}), 400

    doc = {
        "tournament_id": ObjectId(tournament_id),
        "stage_index": 0,
        "name": name,
        "is_final": is_final,
        "advance_count": int(advance_count) if advance_count else None,
        "status": "active",
        "created_at": datetime.utcnow()
    }
    result = mongo.db.tournament_stages.insert_one(doc)
    doc["_id"] = result.inserted_id

    letters = "ABCDEFGHIJKLMNOP"
    for i, pod_participants in enumerate(distribution):
        if not pod_participants:
            continue
        pod_doc = {
            "stage_id": doc["_id"],
            "tournament_id": ObjectId(tournament_id),
            "pod_index": i,
            "name": f"Group {letters[i] if i < len(letters) else i+1}" if len(distribution) > 1 else name,
            "participants": pod_participants,
            "status": "active",
            "final_standings": None,
            "created_at": datetime.utcnow()
        }
        mongo.db.stage_pods.insert_one(pod_doc)

    mongo.db.tournaments.update_one(
        {"_id": ObjectId(tournament_id)},
        {
            "$set": {"status": "in_progress", "grouping_status": "finalized"},
            "$push": {
                "stage_flow": {
                    "stage_id": str(doc["_id"]),
                    "name": name,
                    "stage_index": 0,
                    "is_final": is_final,
                    "status": "active",
                    "created_at": datetime.utcnow()
                }
            }
        }
    )

    notify_group_assignments(tournament_id, doc["_id"], name)

    return jsonify(serialize_stage(doc, include_pods=True))


# ---------------- "WHICH GROUP AM I IN?" (for My Tournaments) ----------------
@stage.route("/tournament/<tournament_id>/my-group", methods=["GET"])
@jwt_required()
def my_group(tournament_id):
    user_id = get_jwt_identity()

    registration = mongo.db.registrations.find_one({
        "user_id": user_id,
        "tournament_id": safe_object_id(tournament_id),
        "payment_status": "approved"
    })
    if not registration:
        return jsonify({"group": None})

    registration_id = str(registration["_id"])

    # Most recent stage first — that's the one the player currently cares about.
    stages = list(mongo.db.tournament_stages.find(
        {"tournament_id": safe_object_id(tournament_id)}
    ).sort("stage_index", -1))

    for s in stages:
        pod = mongo.db.stage_pods.find_one({
            "stage_id": s["_id"],
            "participants.registration_id": registration_id
        })
        if pod:
            return jsonify({
                "group": {
                    "stage_name": s["name"],
                    "stage_status": s.get("status", "active"),
                    "pod_name": pod["name"],
                    "pod_id": str(pod["_id"]),
                    "teammates": [p["name"] for p in pod.get("participants", []) if p["registration_id"] != registration_id]
                }
            })

    return jsonify({"group": None})


# ---------------- LIST STAGES FOR A TOURNAMENT ----------------
@stage.route("/tournament/<tournament_id>", methods=["GET"])
@jwt_required()
def list_stages(tournament_id):
    stages = list(mongo.db.tournament_stages.find(
        {"tournament_id": safe_object_id(tournament_id)}
    ).sort("stage_index", 1))
    return jsonify([serialize_stage(s) for s in stages])


# ---------------- STAGE DETAIL (incl. pods) ----------------
@stage.route("/<stage_id>", methods=["GET"])
@jwt_required()
def get_stage(stage_id):
    s = mongo.db.tournament_stages.find_one({"_id": safe_object_id(stage_id)})
    if not s:
        return jsonify({"error": "Stage not found"}), 404
    return jsonify(serialize_stage(s, include_pods=True))


# ---------------- POD DETAIL (incl. matches) ----------------
@stage.route("/pods/<pod_id>", methods=["GET"])
@jwt_required()
def get_pod(pod_id):
    p = mongo.db.stage_pods.find_one({"_id": safe_object_id(pod_id)})
    if not p:
        return jsonify({"error": "Pod not found"}), 404
    return jsonify(serialize_pod(p, include_matches=True))


# ---------------- POD STANDINGS ----------------
@stage.route("/pods/<pod_id>/standings", methods=["GET"])
@jwt_required()
def get_pod_standings(pod_id):
    p = mongo.db.stage_pods.find_one({"_id": safe_object_id(pod_id)})
    if not p:
        return jsonify({"error": "Pod not found"}), 404
    if p.get("status") == "completed" and p.get("final_standings"):
        return jsonify(p["final_standings"])
    return jsonify(compute_pod_standings(p))


# ---------------- TEAMS NOT YET ASSIGNED TO ANY POD IN A STAGE ----------------
# Covers teams whose payment got approved *after* the stage/pods were already
# created — the pod rosters are a snapshot taken at creation time, so late
# approvals never show up automatically. This lets an admin find and add them.
@stage.route("/<stage_id>/unassigned", methods=["GET"])
@admin_required
def get_unassigned(stage_id):
    s = mongo.db.tournament_stages.find_one({"_id": safe_object_id(stage_id)})
    if not s:
        return jsonify({"error": "Stage not found"}), 404

    roster = get_roster_by_id(str(s["tournament_id"]))

    pods = list(mongo.db.stage_pods.find({"stage_id": s["_id"]}))
    assigned_ids = set()
    for p in pods:
        for participant in p.get("participants", []):
            assigned_ids.add(participant.get("registration_id"))

    unassigned = [v for k, v in roster.items() if k not in assigned_ids]
    return jsonify(unassigned)


# ---------------- ADD A LATE-APPROVED TEAM TO AN EXISTING POD ----------------
@stage.route("/pods/<pod_id>/add-participant", methods=["POST"])
@admin_required
def add_participant_to_pod(pod_id):
    p = mongo.db.stage_pods.find_one({"_id": safe_object_id(pod_id)})
    if not p:
        return jsonify({"error": "Pod not found"}), 404
    if p.get("status") == "completed":
        return jsonify({"error": "Pod already finalized"}), 400

    data = request.get_json(silent=True) or {}
    registration_id = data.get("registration_id")
    if not registration_id:
        return jsonify({"error": "registration_id is required"}), 400

    roster = get_roster_by_id(str(p["tournament_id"]))
    entry = roster.get(registration_id)
    if not entry:
        return jsonify({"error": "Registration not found or not approved for this tournament"}), 404

    # a team can only sit in one group per stage
    sibling_pods = list(mongo.db.stage_pods.find({"stage_id": p["stage_id"]}))
    for sp in sibling_pods:
        if any(part.get("registration_id") == registration_id for part in sp.get("participants", [])):
            return jsonify({"error": "This team is already assigned to a group in this stage"}), 400

    mongo.db.stage_pods.update_one(
        {"_id": p["_id"]},
        {"$push": {"participants": entry}}
    )

    updated = mongo.db.stage_pods.find_one({"_id": p["_id"]})
    return jsonify(serialize_pod(updated, include_matches=True))


# ---------------- ADD A MATCH TO A POD ----------------
@stage.route("/pods/<pod_id>/matches", methods=["POST"])
@admin_required
def add_match(pod_id):
    p = mongo.db.stage_pods.find_one({"_id": safe_object_id(pod_id)})
    if not p:
        return jsonify({"error": "Pod not found"}), 404
    if p.get("status") == "completed":
        return jsonify({"error": "Pod already finalized"}), 400

    data = request.get_json(silent=True) or {}
    match_count = mongo.db.stage_matches.count_documents({"pod_id": p["_id"]})

    doc = {
        "pod_id": p["_id"],
        "stage_id": p["stage_id"],
        "tournament_id": p["tournament_id"],
        "match_number": match_count + 1,
        "map": data.get("map"),
        "room_id": None,
        "room_password": None,
        "match_start_time": None,
        "status": "scheduled",
        "results": [],
        "mvp": None,
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

    p = mongo.db.stage_pods.find_one({"_id": m["pod_id"]})
    if p:
        notified_user_ids = set()
        for participant in p.get("participants", []):
            if participant.get("user_id") and participant["user_id"] not in notified_user_ids:
                notified_user_ids.add(participant["user_id"])
                create_notification(
                    mongo,
                    participant["user_id"],
                    f"Room is live for {p['name']} — Match {m['match_number']}. Check the standings page!",
                    ntype="room",
                    tournament_id=str(m["tournament_id"])
                )
            # Also notify team members
            for member in participant.get("team_members", []):
                member_uid = member.get("user_id")
                if member_uid and member_uid not in notified_user_ids:
                    notified_user_ids.add(member_uid)
                    create_notification(
                        mongo,
                        member_uid,
                        f"Room is live for {p['name']} — Match {m['match_number']}. Check the standings page!",
                        ntype="room",
                        tournament_id=str(m["tournament_id"])
                    )

    return jsonify({"message": "Room released"})


# ---------------- SUBMIT MATCH RESULTS (team + per-player kills) ----------------
@stage.route("/matches/<match_id>/results", methods=["POST"])
@admin_required
def submit_results(match_id):
    try:
        m = mongo.db.stage_matches.find_one({"_id": safe_object_id(match_id)})
        if not m:
            return jsonify({"error": "Match not found"}), 404

        p = mongo.db.stage_pods.find_one({"_id": m["pod_id"]})
        if not p:
            return jsonify({"error": "Pod not found"}), 404

        t, points_table, kill_point_value = get_tournament_scoring(str(m["tournament_id"]))
        if not t:
            return jsonify({"error": "Tournament not found"}), 404

        data = request.get_json(silent=True) or {}
        raw_results = data.get("results", [])
        if not raw_results:
            return jsonify({"error": "No results submitted"}), 400

        name_lookup = {pt["registration_id"]: pt for pt in p.get("participants", [])}

        # Capture previous results for delta computation
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

        mongo.db.stage_matches.update_one(
            {"_id": m["_id"]},
            {"$set": {"results": results, "status": "completed", "mvp": mvp}}
        )

        # Compute deltas and update player_stats
        _apply_kill_deltas(prev_results, results, t.get("game", ""))

        # Increment tournaments_played for all participants
        for r in results:
            pt = name_lookup.get(r["registration_id"])
            if pt and pt.get("user_id"):
                increment_tournaments_played(mongo, pt["user_id"], t.get("game", ""))

        for r in results:
            pt = name_lookup.get(r["registration_id"])
            if pt and pt.get("user_id"):
                create_notification(
                    mongo,
                    pt["user_id"],
                    f"Results are out for {p['name']} — Match {m['match_number']}: #{r['placement']} place, {r['kills']} kills ({r['points']} pts).",
                    ntype="winner",
                    tournament_id=str(m["tournament_id"])
                )
                # Also notify team members
                for member in pt.get("team_members", []):
                    member_uid = member.get("user_id")
                    if member_uid:
                        create_notification(
                            mongo,
                            member_uid,
                            f"Results are out for {p['name']} — Match {m['match_number']}: #{r['placement']} place, {r['kills']} kills ({r['points']} pts).",
                            ntype="winner",
                            tournament_id=str(m["tournament_id"])
                        )

        return jsonify({"message": "Results submitted", "results": results, "mvp": mvp})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _apply_kill_deltas(prev_results, new_results, game):
    """Compute per-user kill deltas between previous and new results,
    then apply only the delta to player_stats. Re-submitting identical
    data results in zero deltas (no-op)."""
    # Build previous kills: {user_id: kills}
    prev_kills = {}
    for r in prev_results:
        for pl in r.get("players", []):
            uid = pl.get("user_id")
            if uid:
                prev_kills[uid] = prev_kills.get(uid, 0) + pl.get("kills", 0)

    # Build new kills: {user_id: kills}
    new_kills = {}
    for r in new_results:
        for pl in r.get("players", []):
            uid = pl.get("user_id")
            if uid:
                new_kills[uid] = new_kills.get(uid, 0) + pl.get("kills", 0)

    # Apply deltas
    all_user_ids = set(prev_kills.keys()) | set(new_kills.keys())
    for uid in all_user_ids:
        old = prev_kills.get(uid, 0)
        new = new_kills.get(uid, 0)
        delta = new - old
        if delta != 0:
            upsert_player_stats(uid, game, kills_delta=delta)


# ---------------- FINALIZE A POD ----------------
@stage.route("/pods/<pod_id>/finalize", methods=["POST"])
@admin_required
def finalize_pod(pod_id):
    p = mongo.db.stage_pods.find_one({"_id": safe_object_id(pod_id)})
    if not p:
        return jsonify({"error": "Pod not found"}), 404
    if p.get("status") == "completed":
        return jsonify({"error": "Pod already finalized"}), 400

    if len(p.get("participants", [])) < 2:
        return jsonify({"error": "Pod needs at least 2 teams before it can be finalized"}), 400

    standings = compute_pod_standings(p)
    mongo.db.stage_pods.update_one(
        {"_id": p["_id"]},
        {"$set": {"status": "completed", "final_standings": standings}}
    )
    return jsonify({"message": "Pod finalized", "standings": standings})


# ---------------- FINALIZE A STAGE (requires all pods finalized) ----------------
@stage.route("/<stage_id>/finalize", methods=["POST"])
@admin_required
def finalize_stage(stage_id):
    s = mongo.db.tournament_stages.find_one({"_id": safe_object_id(stage_id)})
    if not s:
        return jsonify({"error": "Stage not found"}), 404
    if s.get("status") == "completed":
        return jsonify({"error": "Stage already finalized"}), 400

    pods = list(mongo.db.stage_pods.find({"stage_id": s["_id"]}))
    unfinished = [pod["name"] for pod in pods if pod.get("status") != "completed"]
    if unfinished:
        return jsonify({"error": f"Finalize these pods first: {', '.join(unfinished)}"}), 400

    mongo.db.tournament_stages.update_one({"_id": s["_id"]}, {"$set": {"status": "completed"}})

    if s.get("is_final"):
        # Merge every pod's final standings (normally just one pod for a Finals stage)
        merged = []
        for pod in pods:
            merged.extend(pod.get("final_standings") or [])
        merged.sort(key=lambda x: (-x["total_points"], -x["total_kills"]))

        if len(merged) < 2:
            return jsonify({"error": "Need at least 2 teams in the final stage to declare a champion"}), 400

        if merged:
            champion = merged[0]

            # Track old winner for idempotent win handling
            t = mongo.db.tournaments.find_one({"_id": s["tournament_id"]})
            old_winner_id = t.get("winner_id") if t else None
            new_winner_id = champion.get("user_id")
            game = t.get("game", "") if t else ""

            if old_winner_id != new_winner_id:
                if old_winner_id:
                    upsert_global_wins(old_winner_id, -1)
                    if game:
                        upsert_player_stats(old_winner_id, game, tournaments_won_delta=-1)
                if new_winner_id:
                    upsert_global_wins(new_winner_id, 1)
                    if game:
                        upsert_player_stats(new_winner_id, game, tournaments_won_delta=1)

            update_fields = build_winner_update(champion, stage_name=s["name"], source="final_stage")
            mongo.db.tournaments.update_one({"_id": s["tournament_id"]}, {"$set": update_fields})

            for pod in pods:
                for participant in pod.get("participants", []):
                    if participant.get("user_id"):
                        increment_tournaments_played(mongo, participant["user_id"], t.get("game", ""))
                        is_champ = participant["registration_id"] == champion["registration_id"]
                        msg = (f"Congratulations! Your team won {s['name']}!" if is_champ
                               else f"{s['name']} has concluded. Check the final standings!")
                        create_notification(mongo, participant["user_id"], msg, ntype="winner",
                                             tournament_id=str(s["tournament_id"]))
                        # Also notify team members
                        for member in participant.get("team_members", []):
                            member_uid = member.get("user_id")
                            if member_uid:
                                member_msg = (f"Congratulations! Your team won {s['name']}!" if is_champ
                                              else f"{s['name']} has concluded. Check the final standings!")
                                create_notification(mongo, member_uid, member_msg, ntype="winner",
                                                     tournament_id=str(s["tournament_id"]))

    return jsonify({"message": "Stage finalized"})


# ---------------- TOURNAMENT-WIDE STATS (overall leaderboard, frags, MVPs, chicken dinners) ----------------
@stage.route("/<stage_id>", methods=["DELETE"])
@admin_required
def delete_stage(stage_id):
    """Cleanup tool for admins — removes a stage plus its pods and matches.
    Mainly useful for clearing out broken/legacy test stages, or a stage
    started by mistake before any results were entered."""
    s = mongo.db.tournament_stages.find_one({"_id": safe_object_id(stage_id)})
    if not s:
        return jsonify({"error": "Stage not found"}), 404

    pods = list(mongo.db.stage_pods.find({"stage_id": s["_id"]}))
    pod_ids = [p["_id"] for p in pods]

    mongo.db.stage_matches.delete_many({"pod_id": {"$in": pod_ids}})
    mongo.db.stage_pods.delete_many({"stage_id": s["_id"]})
    mongo.db.tournament_stages.delete_one({"_id": s["_id"]})

    # If no stages remain for this tournament, drop it back out of "in_progress"
    remaining = mongo.db.tournament_stages.count_documents({"tournament_id": s["tournament_id"]})
    if remaining == 0:
        mongo.db.tournaments.update_one(
            {"_id": s["tournament_id"]},
            {"$set": {"status": "upcoming"}}
        )

    return jsonify({"message": "Stage deleted"})


@stage.route("/tournament/<tournament_id>/stats", methods=["GET"])
@jwt_required()
def tournament_stats(tournament_id):
    tid = safe_object_id(tournament_id)
    # Include BOTH stage_matches AND cross_pod_matches
    stage_matches = list(mongo.db.stage_matches.find({"tournament_id": tid, "status": "completed"}))
    cross_pod_matches = list(mongo.db.cross_pod_matches.find({"tournament_id": tid, "status": "completed"}))
    matches = stage_matches + cross_pod_matches

    team_totals = {}
    player_totals = {}
    mvp_counts = {}

    for m in matches:
        for r in m.get("results", []):
            rid = r["registration_id"]
            team = team_totals.setdefault(rid, {
                "registration_id": rid, "name": r["name"],
                "total_points": 0, "total_kills": 0, "matches_played": 0, "chicken_dinners": 0
            })
            team["total_points"] += r.get("points", 0)
            team["total_kills"] += r.get("kills", 0)
            team["matches_played"] += 1
            if r.get("placement") == 1:
                team["chicken_dinners"] += 1

            players = r.get("players") or [{"name": r["name"], "kills": r.get("kills", 0)}]
            for pl in players:
                key = f"{rid}::{pl['name']}"
                entry = player_totals.setdefault(key, {
                    "name": pl["name"], "team_name": r["name"], "registration_id": rid, "total_kills": 0
                })
                entry["total_kills"] += pl.get("kills", 0)

        mvp = m.get("mvp")
        if mvp:
            key = f"{mvp['registration_id']}::{mvp['name']}"
            entry = mvp_counts.setdefault(key, {
                "name": mvp["name"], "team_name": mvp["team_name"], "count": 0
            })
            entry["count"] += 1

    def ranked(items, sort_key):
        out = sorted(items, key=sort_key)
        for i, x in enumerate(out):
            x["rank"] = i + 1
        return out

    overall_leaderboard = ranked(list(team_totals.values()), lambda x: (-x["total_points"], -x["total_kills"]))
    team_frags = ranked(list(team_totals.values()), lambda x: -x["total_kills"])
    individual_frags = ranked(list(player_totals.values()), lambda x: -x["total_kills"])
    chicken_dinners = ranked(
        [t for t in team_totals.values() if t["chicken_dinners"] > 0],
        lambda x: -x["chicken_dinners"]
    )
    mvp_leaderboard = ranked(list(mvp_counts.values()), lambda x: -x["count"])

    return jsonify({
        "overall_leaderboard": overall_leaderboard,
        "team_frags": team_frags,
        "individual_frags": individual_frags,
        "chicken_dinners": chicken_dinners,
        "mvp_leaderboard": mvp_leaderboard
    })


# ---------------- TEMP MIGRATION: FIX KILL STATS + GAME NAMES ----------------
@stage.route("/admin/fix-kill-stats", methods=["POST"])
@admin_required
def fix_kill_stats():
    """One-time migration: reset player_stats.total_kills and recompute
    from ALL completed match results (stage_matches + cross_pod_matches).
    Also fixes game name normalization (e.g. 'Free Fire' -> 'FREE_FIRE').

    Safe to re-run (idempotent).
    """
    from bson.objectid import ObjectId as _OID

    # 0. Fix game name normalization: merge misnamed docs into correct ones
    game_name_fixes = {
        "Free Fire": "FREE_FIRE",
        "free fire": "FREE_FIRE",
        "freefire": "FREE_FIRE",
        "BGMI": "BGMI",
        "bgmi": "BGMI",
        "CODM": "CODM",
        "codm": "CODM",
    }
    for wrong_name, correct_name in game_name_fixes.items():
        if wrong_name == correct_name:
            continue
        bad_docs = list(mongo.db.player_stats.find({"game": wrong_name}))
        for bad_doc in bad_docs:
            uid = bad_doc.get("user_id")
            good_doc = mongo.db.player_stats.find_one({"user_id": uid, "game": correct_name})
            if good_doc:
                # Merge: take the higher values
                mongo.db.player_stats.update_one(
                    {"_id": good_doc["_id"]},
                    {"$inc": {
                        "total_kills": bad_doc.get("total_kills", 0),
                        "tournaments_played": bad_doc.get("tournaments_played", 0),
                        "tournaments_won": bad_doc.get("tournaments_won", 0),
                    }}
                )
                mongo.db.player_stats.delete_one({"_id": bad_doc["_id"]})
            else:
                # Just rename the game field
                mongo.db.player_stats.update_one(
                    {"_id": bad_doc["_id"]},
                    {"$set": {"game": correct_name}}
                )

    # 1. Reset all total_kills to 0
    mongo.db.player_stats.update_many({}, {"$set": {"total_kills": 0}})

    # 2. Accumulate per-user kills from ALL completed matches
    kill_deltas = {}
    tp_counts = {}

    # Include BOTH stage_matches AND cross_pod_matches
    all_matches = list(mongo.db.stage_matches.find({"status": "completed"}))
    all_matches += list(mongo.db.cross_pod_matches.find({"status": "completed"}))

    for m in all_matches:
        tid = m.get("tournament_id")
        t = mongo.db.tournaments.find_one({"_id": tid}) if tid else None
        game = _normalize_game(t.get("game", "BGMI")) if t else "BGMI"
        tid_str = str(tid) if tid else None

        # For stage_matches, get pod participants for user_id mapping
        pod_user = {}
        if m.get("pod_id"):
            pod = mongo.db.stage_pods.find_one({"_id": m["pod_id"]})
            if pod:
                for pt in pod.get("participants", []):
                    if pt.get("user_id"):
                        pod_user[pt["registration_id"]] = pt["user_id"]

        # For cross_pod_matches, get participants from both pods
        if m.get("pod_a_id") and m.get("pod_b_id"):
            for pod_id_field in ["pod_a_id", "pod_b_id"]:
                pod = mongo.db.stage_pods.find_one({"_id": m[pod_id_field]})
                if pod:
                    for pt in pod.get("participants", []):
                        if pt.get("user_id"):
                            pod_user[pt["registration_id"]] = pt["user_id"]

        for res in m.get("results", []):
            rid = res.get("registration_id")
            players = res.get("players") or []
            if players:
                for pl in players:
                    uid = pl.get("user_id")
                    if not uid:
                        continue
                    kills = pl.get("kills", 0) or 0
                    if kills > 0:
                        key = (uid, game)
                        kill_deltas[key] = kill_deltas.get(key, 0) + kills
                        if tid_str:
                            tp_counts.setdefault(key, set()).add(tid_str)
            else:
                uid = pod_user.get(rid)
                if not uid:
                    continue
                kills = res.get("kills", 0) or 0
                if kills > 0:
                    key = (uid, game)
                    kill_deltas[key] = kill_deltas.get(key, 0) + kills
                    if tid_str:
                        tp_counts.setdefault(key, set()).add(tid_str)

    # 3. Apply kills
    for (uid, game), delta in kill_deltas.items():
        if delta <= 0:
            continue
        mongo.db.player_stats.update_one(
            {"user_id": uid, "game": game},
            {"$inc": {"total_kills": delta},
             "$setOnInsert": {"user_id": uid, "game": game}},
            upsert=True,
        )

    # 4. Set tournaments_played
    for (uid, game), tids in tp_counts.items():
        mongo.db.player_stats.update_one(
            {"user_id": uid, "game": game},
            {"$set": {"tournaments_played": len(tids)}},
            upsert=True,
        )

    # 5. Backfill usernames
    for (uid, game) in kill_deltas.keys():
        doc = mongo.db.player_stats.find_one({"user_id": uid, "game": game})
        if doc and not doc.get("username"):
            user = mongo.db.users.find_one({"_id": safe_object_id(uid)})
            if user:
                mongo.db.player_stats.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"username": user.get("username", "")}}
                )

    summary = []
    for (uid, game), delta in sorted(kill_deltas.items(), key=lambda x: -x[1])[:20]:
        user = mongo.db.users.find_one({"_id": safe_object_id(uid)})
        name = user.get("username", uid) if user else uid
        summary.append(f"{name}: +{delta} kills ({game})")

    return jsonify({
        "message": f"Fixed {len(kill_deltas)} user+game pairs from {len(all_matches)} matches",
        "top_20": summary
    })

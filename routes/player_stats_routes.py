from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from bson.errors import InvalidId
from functools import wraps

player_stats = Blueprint("player_stats", __name__)
mongo = None


def init_player_stats_routes(mongo_instance):
    global mongo
    mongo = mongo_instance
    _ensure_indexes()


def _ensure_indexes():
    try:
        mongo.db.player_stats.create_index(
            [("user_id", 1), ("game", 1)], unique=True
        )
    except Exception:
        pass


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


# ---------------- HELPER: upsert per-game stats atomically ----------------
def upsert_player_stats(user_id, game, kills_delta=0, tournaments_played_delta=0, tournaments_won_delta=0):
    """Atomically update a player's stats for a specific game.
    All deltas are idempotent — re-applying the same delta twice is NOT safe;
    callers must compute the correct delta before calling."""
    mongo.db.player_stats.update_one(
        {"user_id": user_id, "game": game},
        {
            "$inc": {
                "total_kills": kills_delta,
                "tournaments_played": tournaments_played_delta,
                "tournaments_won": tournaments_won_delta,
            },
            "$setOnInsert": {
                "user_id": user_id,
                "game": game,
            }
        },
        upsert=True
    )


def upsert_global_wins(user_id, delta):
    """Atomically update a user's global tournament win count."""
    mongo.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$inc": {"tournaments_won": delta}}
    )


def get_player_stats(user_id, game):
    """Get a player's stats for a specific game."""
    doc = mongo.db.player_stats.find_one({"user_id": user_id, "game": game})
    if doc:
        return {
            "user_id": doc["user_id"],
            "game": doc["game"],
            "total_kills": doc.get("total_kills", 0),
            "tournaments_played": doc.get("tournaments_played", 0),
            "tournaments_won": doc.get("tournaments_won", 0),
        }
    return {
        "user_id": user_id,
        "game": game,
        "total_kills": 0,
        "tournaments_played": 0,
        "tournaments_won": 0,
    }


def get_user_profile_with_stats(user_id):
    """Get user profile with stats for all their games."""
    user = mongo.db.users.find_one({"_id": safe_object_id(user_id)})
    if not user:
        return None

    game_stats = list(mongo.db.player_stats.find({"user_id": user_id}))
    games = {}
    for gs in game_stats:
        games[gs["game"]] = {
            "total_kills": gs.get("total_kills", 0),
            "tournaments_played": gs.get("tournaments_played", 0),
            "tournaments_won": gs.get("tournaments_won", 0),
        }

    return {
        "user_id": str(user["_id"]),
        "username": user.get("username", ""),
        "name": user.get("name", ""),
        "college": user.get("college", ""),
        "avatarId": user.get("avatarId"),
        "tournaments_won": user.get("tournaments_won", 0),
        "games": games,
    }


# ---------------- GET PLAYER STATS (by user_id or "me") ----------------
@player_stats.route("/player/<target>", methods=["GET"])
@jwt_required()
def get_stats(target):
    if target == "me":
        target = get_jwt_identity()

    user = mongo.db.users.find_one({"_id": safe_object_id(target)})
    if not user:
        return jsonify({"error": "User not found"}), 404

    profile = get_user_profile_with_stats(target)
    return jsonify(profile)


# ---------------- LEADERBOARD: Global (by tournaments_won) ----------------
@player_stats.route("/leaderboard/global", methods=["GET"])
def global_leaderboard():
    pipeline = [
        {"$group": {
            "_id": "$user_id",
            "total_kills": {"$sum": "$total_kills"},
            "games_played": {"$sum": "$tournaments_played"},
            "games_won": {"$sum": "$tournaments_won"},
        }},
        {"$sort": {"games_won": -1, "total_kills": -1, "_id": 1}},
        {"$limit": 100},
    ]
    stats_docs = list(mongo.db.player_stats.aggregate(pipeline))

    result = []
    for entry in stats_docs:
        user_id = entry["_id"]
        user = mongo.db.users.find_one({"_id": safe_object_id(user_id)})
        if not user:
            continue
        result.append({
            "user_id": user_id,
            "username": user.get("username", ""),
            "name": user.get("name", ""),
            "college": user.get("college", ""),
            "avatarId": user.get("avatarId"),
            "tournaments_won": entry["games_won"],
            "total_kills": entry["total_kills"],
        })

    return jsonify(result)


# ---------------- LEADERBOARD: Per-game (by total_kills for that game) ----------------
@player_stats.route("/leaderboard/game/<game>", methods=["GET"])
def game_leaderboard(game):
    pipeline = [
        {"$match": {"game": game}},
        {"$sort": {"total_kills": -1, "user_id": 1}},
        {"$limit": 100},
    ]
    stats_docs = list(mongo.db.player_stats.aggregate(pipeline))

    result = []
    for entry in stats_docs:
        user_id = entry["user_id"]
        user = mongo.db.users.find_one({"_id": safe_object_id(user_id)})
        if not user:
            continue
        result.append({
            "user_id": user_id,
            "username": user.get("username", ""),
            "name": user.get("name", ""),
            "college": user.get("college", ""),
            "avatarId": user.get("avatarId"),
            "total_kills": entry.get("total_kills", 0),
            "tournaments_played": entry.get("tournaments_played", 0),
            "tournaments_won": entry.get("tournaments_won", 0),
        })

    return jsonify(result)

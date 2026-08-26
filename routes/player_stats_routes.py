from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from bson import ObjectId
from utils.player_stats import (
    update_kills,
    increment_tournaments_played,
    increment_tournaments_won,
    get_player_stats,
    get_global_leaderboard,
    get_bgmi_leaderboard,
    get_free_fire_leaderboard,
)
from functools import wraps

player_stats = Blueprint("player_stats", __name__)
mongo = None


def init_player_stats_routes(mongo_instance):
    global mongo
    mongo = mongo_instance


def admin_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        user_id = get_jwt_identity()
        user = mongo.db.users.find_one({"_id": ObjectId(user_id)})
        if not user or user.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper

def upsert_player_stats(
    user_id,
    game,
    kills_delta=0,
    tournaments_played_delta=0,
    tournaments_won_delta=0
):
    """Atomically update a player's per-game stats."""
    from utils.player_stats import normalize_game
    game_norm = normalize_game(game)
    mongo.db.player_stats.update_one(
        {"user_id": user_id, "game": game_norm},
        {
            "$inc": {
                "total_kills": kills_delta,
                "tournaments_played": tournaments_played_delta,
                "tournaments_won": tournaments_won_delta,
            },
            "$setOnInsert": {
                "user_id": user_id,
                "game": game_norm,
            }
        },
        upsert=True
    )


def upsert_global_wins(user_id, delta):
    """Atomically update a user's global tournament win count in both users and player_stats."""
    mongo.db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$inc": {"tournaments_won": delta}}
    )
    # Also update player_stats with game="GLOBAL" so the global leaderboard works
    mongo.db.player_stats.update_one(
        {"user_id": user_id, "game": "GLOBAL"},
        {
            "$inc": {"tournaments_won": delta},
            "$setOnInsert": {
                "user_id": user_id,
                "game": "GLOBAL",
                "username": "",
                "total_kills": 0,
                "tournaments_played": 0,
            }
        },
        upsert=True
    )

# ---- PLAYER STATS ----

@player_stats.route("/player/<user_id>/stats", methods=["GET"])
@jwt_required()
def get_user_stats(user_id):
    """Get stats for a specific user."""
    game = request.args.get("game")
    if game:
        stats = get_player_stats(mongo, user_id, game)
    else:
        stats = get_player_stats(mongo, user_id)
    return jsonify({"stats": stats})


@player_stats.route("/me/stats", methods=["GET"])
@jwt_required()
def me_stats():
    """Get stats for the authenticated user."""
    user_id = get_jwt_identity()
    game = request.args.get("game")
    if game:
        stats = get_player_stats(mongo, user_id, game)
    else:
        stats = get_player_stats(mongo, user_id)
    return jsonify({"stats": stats})


# ---- LEADERBOARDS ----

@player_stats.route("/leaderboard/global", methods=["GET"])
def global_leaderboard():
    """Global leaderboard ranked by tournament wins across all games."""
    data = get_global_leaderboard(mongo)
    return jsonify({"leaderboard": data})


@player_stats.route("/leaderboard/bgmi", methods=["GET"])
def bgmi_leaderboard():
    """BGMI leaderboard ranked by total kills."""
    data = get_bgmi_leaderboard(mongo)
    return jsonify({"leaderboard": data})


@player_stats.route("/leaderboard/free-fire", methods=["GET"])
def free_fire_leaderboard():
    """Free Fire leaderboard ranked by total kills."""
    data = get_free_fire_leaderboard(mongo)
    return jsonify({"leaderboard": data})


# ---- KILL STATS UPDATE (from match results) ----

@player_stats.route("/update-kills", methods=["POST"])
@jwt_required()
def update_kills_endpoint():
    """Update kill stats for a player from a match result.

    Expected payload:
    {
        "game": "BGMI" | "FREE_FIRE",
        "kills": number
    }
    The kills value is treated as a delta added to the player's total.
    """
    user_id = get_jwt_identity()
    data = request.json or {}
    game = data.get("game", "").strip().upper()
    kills = int(data.get("kills", 0) or 0)

    if not game:
        return jsonify({"error": "Game is required"}), 400

    # Fetch username from users collection
    users = mongo.db.users
    user = users.find_one({"_id": ObjectId(user_id)})
    username = user.get("username", "") if user else ""

    updated = update_kills(mongo, user_id, username, game, kills)
    return jsonify({
        "message": f"Kills updated for {game}",
        "stats": {
            "game": updated.get("game", game),
            "total_kills": updated.get("total_kills", 0),
            "tournaments_played": updated.get("tournaments_played", 0),
            "tournaments_won": updated.get("tournaments_won", 0),
        }
    })


@player_stats.route("/increment-tournaments-played", methods=["POST"])
@jwt_required()
def increment_tournaments_played_endpoint():
    """Increment tournaments played count."""
    user_id = get_jwt_identity()
    data = request.json or {}
    game = data.get("game", "").strip().upper()

    if not game:
        return jsonify({"error": "Game is required"}), 400

    users = mongo.db.users
    user = users.find_one({"_id": ObjectId(user_id)})
    username = user.get("username", "") if user else ""

    updated = increment_tournaments_played(mongo, user_id, game)
    return jsonify({
        "message": f"Tournaments played incremented for {game}",
        "stats": {
            "game": updated.get("game", game),
            "total_kills": updated.get("total_kills", 0),
            "tournaments_played": updated.get("tournaments_played", 0),
            "tournaments_won": updated.get("tournaments_won", 0),
        }
    })


@player_stats.route("/increment-tournaments-won", methods=["POST"])
@jwt_required()
def increment_tournaments_won_endpoint():
    """Increment tournaments won count (game-specific + global)."""
    user_id = get_jwt_identity()
    data = request.json or {}
    game = data.get("game", "").strip().upper()

    if not game:
        return jsonify({"error": "Game is required"}), 400

    users = mongo.db.users
    user = users.find_one({"_id": ObjectId(user_id)})
    username = user.get("username", "") if user else ""

    updated = increment_tournaments_won(mongo, user_id, username, game)
    return jsonify({
        "message": f"Tournaments won incremented for {game}",
        "stats": {
            "game": updated.get("game", game),
            "total_kills": updated.get("total_kills", 0),
            "tournaments_played": updated.get("tournaments_played", 0),
            "tournaments_won": updated.get("tournaments_won", 0),
        }
    })


# ---- ADMIN: RESET PLAYER LEADERBOARD ----

@player_stats.route("/admin/reset-player-stats", methods=["POST"])
@admin_required
def reset_player_stats():
    """Reset specific stat for a player. Admin only.

    Expected payload:
    {
        "user_id": "the user id",
        "stat": "tournaments_won" | "total_kills" | "tournaments_played",
        "game": "BGMI" | "FREE_FIRE" | "GLOBAL"  (optional, defaults to GLOBAL)
    }
    """
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    stat = data.get("stat", "tournaments_won")
    game = (data.get("game") or "GLOBAL").strip().upper()

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    user = mongo.db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        return jsonify({"error": "User not found"}), 404

    valid_stats = ["tournaments_won", "total_kills", "tournaments_played"]
    if stat not in valid_stats:
        return jsonify({"error": f"stat must be one of {valid_stats}"}), 400

    # Update player_stats
    mongo.db.player_stats.update_one(
        {"user_id": user_id, "game": game},
        {"$set": {stat: 0}}
    )

    # Also sync tournaments_won on users doc
    if stat == "tournaments_won" and game == "GLOBAL":
        mongo.db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"tournaments_won": 0}}
        )

    return jsonify({
        "message": f"Reset {stat} to 0 for {user.get('name', user.get('username', user_id))} ({game})"
    })
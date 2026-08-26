from datetime import datetime
from typing import Any, Dict, List, Optional
from bson import ObjectId


def normalize_game(game: str) -> str:
    """Normalize game identifier to internal code.

    Use short internal codes for leaderboard and stats logic.
    Accept both display names and coded values.
    """
    game_map = {
        "bgmi": "BGMI",
        "free fire": "FREE_FIRE",
        "freefire": "FREE_FIRE",
        "codm": "CODM",
        "valorant": "VALORANT",
    }
    key = game.strip().lower()
    return game_map.get(key, game.strip().upper())


def ensure_player_stats(mongo, user_id: str, username: str, game: str = "BGMI") -> Dict[str, Any]:
    """Ensure a player_stats document exists for the given user+game.

    Creates a document if one doesn't exist. One document per (user_id, game) pair.
    """
    stats_col = mongo.db.player_stats

    # Ensure index for fast lookups
    idx = stats_col.index_information().get("user_id_game_idx")
    if not idx:
        stats_col.create_index(
            [("user_id", 1), ("game", 1)],
            unique=True,
            name="user_id_game_idx"
        )

    game_norm = normalize_game(game)
    existing = stats_col.find_one({"user_id": user_id, "game": game_norm})

    if existing:
        return existing

    new_doc = {
        "user_id": user_id,
        "username": username,
        "game": game_norm,
        "total_kills": 0,
        "tournaments_played": 0,
        "tournaments_won": 0,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }

    stats_col.insert_one(new_doc)
    return new_doc


def update_kills(mongo, user_id: str, username: str, game: str, kills: int,
                 match_id: str = None) -> Dict[str, Any]:
    """Update player kill stats for a specific game.

    Idempotent when match_id is provided: if the same (match_id, user_id) kill
    contribution has already been recorded with the same kills value, no change
    occurs. If the kills value differs, only the delta is applied.

    Args:
        mongo: PyMongo instance
        user_id: The user's permanent ID
        username: The user's display username
        game: Normalized game code (BGMI, FREE_FIRE, etc.)
        kills: Number of kills to contribute/add
        match_id: The stage_matches _id string. If provided, idempotent tracking
                  via match_kill_contributions is used. If None, additive update.

    Returns:
        Updated stats document
    """
    stats_col = mongo.db.player_stats
    game_norm = normalize_game(game)

    # Ensure a stats document exists for this user+game
    stats_doc = ensure_player_stats(mongo, user_id, username, game_norm)
    stats_col = mongo.db.player_stats

    # Find the specific stats document for this user+game
    doc = stats_col.find_one({"user_id": user_id, "game": game_norm})
    if not doc:
        # Create new if somehow missing
        new_doc = {
            "user_id": user_id,
            "username": username,
            "game": game_norm,
            "total_kills": 0,
            "tournaments_played": 0,
            "tournaments_won": 0,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        stats_col.insert_one(new_doc)
        doc = new_doc

    if match_id:
        # Idempotent mode: use match_kill_contributions to track what's been counted
        from routes.stage_routes import _ensure_match_kill_index

        _ensure_match_kill_index(mongo)

        contributions_col = mongo.db.match_kill_contributions

        # Check if this (match_id, user_id) already has a contribution recorded
        existing_contrib = contributions_col.find_one({
            "match_id": match_id,
            "user_id": user_id
        })

        if existing_contrib:
            # Already recorded - check if kills value is the same
            old_kills = existing_contrib.get("kills", 0)
            if old_kills == kills:
                # Truly idempotent - same kills already recorded, no change needed
                return doc
            else:
                # Kills changed - apply delta
                delta = kills - old_kills
                if delta != 0:
                    doc["total_kills"] = doc.get("total_kills", 0) + delta
                    doc["updated_at"] = datetime.utcnow()
                    stats_col.replace_one({"_id": doc["_id"]}, doc)
                    # Update the contribution record
                    contributions_col.update_one(
                        {"match_id": match_id, "user_id": user_id},
                        {"$set": {"kills": kills, "updated_at": datetime.utcnow()}}
                    )
                return doc
        else:
            # First time this match+user is being recorded
            # Record the contribution and add kills
            contributions_col.insert_one({
                "match_id": match_id,
                "user_id": user_id,
                "kills": kills,
                "username": username,
                "recorded_at": datetime.utcnow()
            })
            doc["total_kills"] = doc.get("total_kills", 0) + kills
            doc["updated_at"] = datetime.utcnow()
            stats_col.replace_one({"_id": doc["_id"]}, doc)
            return doc
    else:
        # Non-idempotent mode (additive): simply add kills
        # This is for cases where match_id isn't available or for backward compat
        doc["total_kills"] = doc.get("total_kills", 0) + kills
        doc["updated_at"] = datetime.utcnow()
        stats_col.replace_one({"_id": doc["_id"]}, doc)
        return doc


def increment_tournaments_played(mongo, user_id: str, game: str) -> Dict[str, Any]:
    """Increment tournaments_played count for a user in a specific game."""
    stats_col = mongo.db.player_stats
    game_norm = normalize_game(game)

    doc = stats_col.find_one({"user_id": user_id, "game": game_norm})
    if not doc:
        # Look up username from users collection
        username = ""
        try:
            user = mongo.db.users.find_one({"_id": ObjectId(user_id)})
            if user:
                username = user.get("username", "")
        except Exception:
            pass

        new_doc = {
            "user_id": user_id,
            "username": username,
            "game": game_norm,
            "total_kills": 0,
            "tournaments_played": 1,
            "tournaments_won": 0,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        stats_col.insert_one(new_doc)
        return new_doc

    doc["tournaments_played"] = doc.get("tournaments_played", 0) + 1
    doc["updated_at"] = datetime.utcnow()

    stats_col.replace_one({"_id": doc["_id"]}, doc)
    return doc


def increment_tournaments_won(mongo, user_id: str, username: str, game: str) -> Dict[str, Any]:
    """Increment tournaments_won count for a user in a specific game.

    Also increments the Global tournament wins counter.
    """
    stats_col = mongo.db.player_stats
    game_norm = normalize_game(game)

    # Increment game-specific tournaments won
    doc = stats_col.find_one({"user_id": user_id, "game": game_norm})
    if not doc:
        new_doc = {
            "user_id": user_id,
            "username": username,
            "game": game_norm,
            "total_kills": 0,
            "tournaments_played": 0,
            "tournaments_won": 1,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        stats_col.insert_one(new_doc)
        return new_doc

    doc["tournaments_won"] = doc.get("tournaments_won", 0) + 1
    doc["updated_at"] = datetime.utcnow()

    stats_col.replace_one({"_id": doc["_id"]}, doc)

    # Also increment global tournament wins (game-agnostic)
    global_doc = stats_col.find_one({"user_id": user_id, "game": "GLOBAL"})
    if not global_doc:
        global_new = {
            "user_id": user_id,
            "username": username,
            "game": "GLOBAL",
            "total_kills": 0,
            "tournaments_played": 0,
            "tournaments_won": 1,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        stats_col.insert_one(global_new)
    else:
        global_doc = stats_col.find_one({"user_id": user_id, "game": "GLOBAL"})
        if global_doc:
            global_doc["tournaments_won"] = global_doc.get("tournaments_won", 0) + 1
            global_doc["updated_at"] = datetime.utcnow()
            stats_col.replace_one(
                {"_id": global_doc["_id"]}, global_doc
            )

    return doc


def get_player_stats(mongo, user_id: str, game: str = None) -> Dict[str, Any]:
    """Retrieve player stats, optionally filtered by game.

    If game is None, returns all stats for the user across all games.
    """
    stats_col = mongo.db.player_stats

    if game:
        game_norm = normalize_game(game)
        doc = stats_col.find_one({"user_id": user_id, "game": game_norm})
        if doc:
            return doc
        return {"user_id": user_id, "username": "", "game": game_norm, "total_kills": 0, "tournaments_played": 0, "tournaments_won": 0, "created_at": None, "updated_at": None}

    # Return all stats for this user
    cursor = stats_col.find({"user_id": user_id})
    results = list(cursor)
    # Ensure each result has consistent keys
    normalized = []
    for doc in results:
        normalized.append({
            "user_id": doc.get("user_id"),
            "username": doc.get("username", ""),
            "game": doc.get("game", ""),
            "total_kills": doc.get("total_kills", 0),
            "tournaments_played": doc.get("tournaments_played", 0),
            "tournaments_won": doc.get("tournaments_won", 0),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
        })
    return normalized if normalized else [{"user_id": user_id, "username": "", "game": "", "total_kills": 0, "tournaments_played": 0, "tournaments_won": 0, "created_at": None, "updated_at": None}]


def get_global_leaderboard(mongo) -> List[Dict[str, Any]]:
    """Get global leaderboard sorted by tournament wins (desc).

    Returns list of {user_id, username, name, college, avatarId, tournaments_won} sorted by wins desc.
    """
    stats_col = mongo.db.player_stats
    users_col = mongo.db.users
    cursor = stats_col.find({"game": "GLOBAL", "tournaments_won": {"$gt": 0}}).sort("tournaments_won", -1)
    results = list(cursor)
    normalized = []
    for doc in results:
        uid = doc.get("user_id")
        user = None
        if uid:
            try:
                user = users_col.find_one({"_id": ObjectId(uid)})
            except Exception:
                user = users_col.find_one({"_id": uid})
        normalized.append({
            "user_id": uid,
            "username": doc.get("username", ""),
            "name": user.get("name", "") if user else "",
            "college": user.get("college", "") if user else "",
            "avatarId": user.get("avatarId") if user else None,
            "tournaments_won": doc.get("tournaments_won", 0),
        })
    return sorted(normalized, key=lambda x: (-x["tournaments_won"], x["username"]))


def get_bgmi_leaderboard(mongo) -> List[Dict[str, Any]]:
    """Get BGMI leaderboard sorted by total kills (desc)."""
    stats_col = mongo.db.player_stats
    users_col = mongo.db.users
    cursor = stats_col.find({"game": "BGMI"}).sort("total_kills", -1)
    results = list(cursor)
    normalized = []
    for doc in results:
        uid = doc.get("user_id")
        user = None
        if uid:
            try:
                user = users_col.find_one({"_id": ObjectId(uid)})
            except Exception:
                user = users_col.find_one({"_id": uid})
        normalized.append({
            "user_id": uid,
            "username": doc.get("username", ""),
            "name": user.get("name", "") if user else "",
            "college": user.get("college", "") if user else "",
            "avatarId": user.get("avatarId") if user else None,
            "total_kills": doc.get("total_kills", 0),
            "tournaments_played": doc.get("tournaments_played", 0),
        })
    return sorted(normalized, key=lambda x: (-x["total_kills"], x["username"]))


def _decrement_tournaments_won(mongo, user_id: str, username: str, game: str) -> None:
    """Decrement tournaments_won count for a user in a specific game.

    Ensures total_wins never goes below 0.
    """
    if not user_id:
        return
    stats_col = mongo.db.player_stats
    game_norm = normalize_game(game)

    doc = stats_col.find_one({"user_id": user_id, "game": game_norm})
    if not doc:
        return

    current_won = doc.get("tournaments_won", 0)
    if current_won <= 0:
        return

    doc["tournaments_won"] = current_won - 1
    doc["updated_at"] = datetime.utcnow()

    stats_col.replace_one({"_id": doc["_id"]}, doc)


def get_free_fire_leaderboard(mongo) -> List[Dict[str, Any]]:
    """Get Free Fire leaderboard sorted by total kills (desc)."""
    stats_col = mongo.db.player_stats
    users_col = mongo.db.users
    cursor = stats_col.find({"game": "FREE_FIRE"}).sort("total_kills", -1)
    results = list(cursor)
    normalized = []
    for doc in results:
        uid = doc.get("user_id")
        user = None
        if uid:
            try:
                user = users_col.find_one({"_id": ObjectId(uid)})
            except Exception:
                user = users_col.find_one({"_id": uid})
        normalized.append({
            "user_id": uid,
            "username": doc.get("username", ""),
            "name": user.get("name", "") if user else "",
            "college": user.get("college", "") if user else "",
            "avatarId": user.get("avatarId") if user else None,
            "total_kills": doc.get("total_kills", 0),
            "tournaments_played": doc.get("tournaments_played", 0),
        })
    return sorted(normalized, key=lambda x: (-x["total_kills"], x["username"]))


def _ensure_match_kill_index(mongo):
    """Ensure index on match_kill_contributions for fast lookups."""
    col = mongo.db.match_kill_contributions
    idx = col.index_information().get("match_user_idx")
    if not idx:
        col.create_index([("match_id", 1), ("user_id", 1)], unique=True, name="match_user_idx")
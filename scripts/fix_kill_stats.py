"""One-time migration: retroactively apply per-player kills from completed
stage_matches into player_stats, since _apply_kill_deltas was broken by
missing imports.

Idempotent — safe to re-run (resets kills then recomputes from scratch).

Run:  python scripts/fix_kill_stats.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from pymongo import MongoClient
from bson import ObjectId

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/campus_clash")
client = MongoClient(MONGO_URI)
db = client.get_default_db()


def normalize_game(game):
    m = {"bgmi": "BGMI", "free fire": "FREE_FIRE", "freefire": "FREE_FIRE",
         "codm": "CODM", "valorant": "VALORANT"}
    return m.get(game.strip().lower(), game.strip().upper())


# ── Step 1: Reset all total_kills to 0 (idempotent) ──
db.player_stats.update_many({}, {"$set": {"total_kills": 0}})
print("Reset all total_kills to 0")

# ── Step 2: Accumulate per-user kill deltas from completed matches ──
kill_deltas = {}   # (user_id, game) -> total kills
tp_counts = {}     # (user_id, game) -> set of tournament_id strings

matches = list(db.stage_matches.find({"status": "completed"}))
print(f"Found {len(matches)} completed matches")

for m in matches:
    tid = m.get("tournament_id")
    t = db.tournaments.find_one({"_id": tid}) if tid else None
    game_raw = t.get("game", "BGMI") if t else "BGMI"
    game = normalize_game(game_raw)
    tid_str = str(tid) if tid else None

    pod = db.stage_pods.find_one({"_id": m.get("pod_id")}) if m.get("pod_id") else None
    # registration_id -> user_id map from pod participants
    pod_user = {}
    if pod:
        for pt in pod.get("participants", []):
            if pt.get("user_id"):
                pod_user[pt["registration_id"]] = pt["user_id"]

    for res in m.get("results", []):
        rid = res.get("registration_id")

        # Per-player kills (squad mode)
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
            # Solo mode: team-level kills, look up user_id from pod participants
            uid = pod_user.get(rid)
            if not uid:
                continue
            kills = res.get("kills", 0) or 0
            if kills > 0:
                key = (uid, game)
                kill_deltas[key] = kill_deltas.get(key, 0) + kills
                if tid_str:
                    tp_counts.setdefault(key, set()).add(tid_str)

print(f"Computed kill deltas for {len(kill_deltas)} user+game pairs")

# ── Step 3: Apply kills ──
for (uid, game), delta in kill_deltas.items():
    if delta <= 0:
        continue
    db.player_stats.update_one(
        {"user_id": uid, "game": game},
        {"$inc": {"total_kills": delta},
         "$setOnInsert": {"user_id": uid, "game": game}},
        upsert=True,
    )

# ── Step 4: Set tournaments_played from distinct tournament IDs ──
for (uid, game), tids in tp_counts.items():
    db.player_stats.update_one(
        {"user_id": uid, "game": game},
        {"$set": {"tournaments_played": len(tids)}},
        upsert=True,
    )

# ── Step 5: Backfill usernames ──
for (uid, game) in kill_deltas.keys():
    doc = db.player_stats.find_one({"user_id": uid, "game": game})
    if doc and not doc.get("username"):
        try:
            user = db.users.find_one({"_id": ObjectId(uid)})
            if user:
                db.player_stats.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"username": user.get("username", "")}}
                )
        except Exception:
            pass

print("\nDone. Top 20 by kills:")
for (uid, game), delta in sorted(kill_deltas.items(), key=lambda x: -x[1])[:20]:
    try:
        user = db.users.find_one({"_id": ObjectId(uid)})
        name = user.get("username", uid) if user else uid
    except Exception:
        name = uid
    tp = len(tp_counts.get((uid, game), set()))
    print(f"  {str(name):20s}  {game:12s}  kills +{delta:4d}  tournaments {tp}")

print(f"\nTotal: {len(kill_deltas)} user+game pairs updated")

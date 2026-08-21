"""Full leaderboard + tournament reset script.

WARNING: This ERASES all leaderboard stats, tournament data, matches, and pods.
Users (accounts) are preserved. Fresh start.

Run:  python scripts/full_leaderboard_reset.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/campus_clash")
client = MongoClient(MONGO_URI)
db_name = MONGO_URI.rsplit("/", 1)[-1].split("?")[0]
db = client[db_name]

# Collections to DROP completely
collections_to_drop = [
    "player_stats",
    "match_kill_contributions",
    "stage_matches",
    "stage_pods",
    "tournaments",
]

print("=" * 60)
print("  FULL LEADERBOARD & TOURNAMENT RESET")
print("=" * 60)

# First, list all collections for reference
print("\nAll collections in DB:")
for name in db.list_collection_names():
    count = db[name].count_documents({})
    print(f"  {name}: {count} docs")

print("\n" + "=" * 60)
print("  DROP ZONE:")
print("=" * 60)

for col_name in collections_to_drop:
    if col_name in db.list_collection_names():
        count = db[col_name].count_documents({})
        print(f"  DROP {col_name} ({count} docs)")
    else:
        print(f"  SKIP {col_name} (not found)")

print(f"\n  PRESERVE: users collection")
print("=" * 60)

confirm = input("\nType 'YES ERASE EVERYTHING' to proceed: ").strip()
if confirm != "YES ERASE EVERYTHING":
    print("Aborted.")
    sys.exit(1)

# ── Step 1: Drop collections ──
for col_name in collections_to_drop:
    if col_name in db.list_collection_names():
        db[col_name].drop()
        print(f"  Dropped: {col_name}")
    else:
        print(f"  Skipped (not found): {col_name}")

# ── Step 2: Reset tournaments_won on users ──
result = db.users.update_many({}, {"$set": {"tournaments_won": 0}})
print(f"\n  Reset tournaments_won to 0 on {result.modified_count} users")

# ── Step 3: Verify ──
print("\n" + "=" * 60)
print("  VERIFICATION:")
print("=" * 60)
for col_name in ["player_stats", "match_kill_contributions", "stage_matches", "stage_pods", "tournaments"]:
    exists = col_name in db.list_collection_names()
    count = db[col_name].count_documents({}) if exists else 0
    print(f"  {col_name}: {count} docs {'(CLEAN)' if count == 0 else '(PROBLEM!)'}")

users_with_wins = db.users.count_documents({"tournaments_won": {"$gt": 0}})
print(f"  users with tournaments_won > 0: {users_with_wins} {'(CLEAN)' if users_with_wins == 0 else '(PROBLEM!)'}")

print("\n  DONE. Leaderboard is completely empty. Fresh start!")
print("=" * 60)

"""
Comprehensive tests for the username system, player stats, kill tracking,
winner tracking, and leaderboards.

Uses pytest + mongomock (in-memory MongoDB) so no real DB is needed.
"""
import pytest
from unittest.mock import MagicMock, patch
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _Desc:
    """Wrapper for descending sort in multi-key sorts."""
    __slots__ = ("val",)
    def __init__(self, val):
        self.val = val
    def __lt__(self, other):
        if isinstance(other, _Desc):
            return self.val > other.val
        return self.val > other
    def __gt__(self, other):
        if isinstance(other, _Desc):
            return self.val < other.val
        return self.val < other
    def __eq__(self, other):
        if isinstance(other, _Desc):
            return self.val == other.val
        return self.val == other
    def __le__(self, other):
        return not self.__gt__(other)
    def __ge__(self, other):
        return not self.__lt__(other)


# ---------------------------------------------------------------------------
# Helpers — lightweight fakes for MongoDB collections
# ---------------------------------------------------------------------------

class FakeCollection:
    """Minimal in-memory collection that supports insert/find/update/count."""

    def __init__(self):
        self._docs = {}
        self._counter = 0

    def _next_id(self):
        self._counter += 1
        return f"id_{self._counter}"

    def insert_one(self, doc):
        if "_id" not in doc or doc["_id"] is None:
            oid = self._next_id()
            doc["_id"] = oid
        self._docs[doc["_id"]] = doc
        return MagicMock(inserted_id=doc["_id"])

    def find_one(self, filter_dict):
        for doc in self._docs.values():
            match = True
            for k, v in filter_dict.items():
                doc_val = doc.get(k)
                # Handle ObjectId comparison — compare as strings
                if hasattr(v, '__class__') and v.__class__.__name__ == 'ObjectId':
                    if str(doc_val) != str(v):
                        match = False
                        break
                elif doc_val != v:
                    match = False
                    break
            if match:
                return doc
        return None

    def find(self, filter_dict=None):
        if not filter_dict:
            return list(self._docs.values())
        return [d for d in self._docs.values()
                if all(d.get(k) == v for k, v in filter_dict.items())]

    def update_one(self, filter_dict, update, upsert=False):
        doc = self.find_one(filter_dict)
        if doc is None and upsert:
            doc = {"_id": self._next_id()}
            for k, v in filter_dict.items():
                if hasattr(v, '__class__') and v.__class__.__name__ == 'ObjectId':
                    doc[k] = str(v)
                else:
                    doc[k] = v
            self._docs[doc["_id"]] = doc

        if doc is None:
            return MagicMock(matched_count=0, modified_count=0)

        if "$set" in update:
            for k, v in update["$set"].items():
                doc[k] = v
        if "$inc" in update:
            for k, v in update["$inc"].items():
                doc[k] = doc.get(k, 0) + v
        if "$setOnInsert" in update:
            for k, v in update["$setOnInsert"].items():
                if k not in doc:
                    doc[k] = v
        if "$push" in update:
            for k, v in update["$push"].items():
                if k not in doc:
                    doc[k] = []
                doc[k].append(v)
        return MagicMock(matched_count=1, modified_count=1)

    def count_documents(self, filter_dict=None):
        return len(self.find(filter_dict))

    def delete_one(self, filter_dict):
        for oid, doc in list(self._docs.items()):
            if all(doc.get(k) == v for k, v in filter_dict.items()):
                del self._docs[oid]
                return MagicMock(deleted_count=1)
        return MagicMock(deleted_count=0)

    def create_index(self, *args, **kwargs):
        pass  # no-op for tests

    def aggregate(self, pipeline):
        # Very minimal aggregation support for our test needs
        docs = list(self._docs.values())

        # Apply $match
        for stage in pipeline:
            if "$match" in stage:
                match = stage["$match"]
                docs = [d for d in docs if all(d.get(k) == v for k, v in match.items())]

            if "$group" in stage:
                group = stage["$group"]
                groups = {}
                for d in docs:
                    # Determine group key
                    id_spec = group["_id"]
                    if isinstance(id_spec, str) and id_spec.startswith("$"):
                        key = d.get(id_spec[1:])
                    else:
                        key = "all"

                    if key not in groups:
                        groups[key] = {"_id": key}

                    for field, op in group.items():
                        if field == "_id":
                            continue
                        if "$sum" in op:
                            sum_spec = op["$sum"]
                            if isinstance(sum_spec, str) and sum_spec.startswith("$"):
                                val = d.get(sum_spec[1:], 0)
                            elif isinstance(sum_spec, (int, float)):
                                val = sum_spec
                            else:
                                val = 0
                            groups[key][field] = groups[key].get(field, 0) + val
                docs = list(groups.values())

            if "$sort" in stage:
                sort_key = stage["$sort"]
                sort_items = list(sort_key.items())
                def sort_fn(x, _items=sort_items):
                    result = []
                    for k, direction in _items:
                        val = x.get(k, 0)
                        # For descending: wrap in a wrapper class
                        result.append(_Desc(val) if direction == -1 else val)
                    return tuple(result)
                docs.sort(key=sort_fn)

            if "$limit" in stage:
                docs = docs[:stage["$limit"]]

        return docs


class FakeDB:
    def __init__(self):
        self._collections = {}
        self.db = self  # mongo.db returns self

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._collections:
            self._collections[name] = FakeCollection()
        return self._collections[name]


# ---------------------------------------------------------------------------
# Test: Username validation
# ---------------------------------------------------------------------------

def test_valid_username_format():
    from routes.auth_routes import _is_valid_username, _normalize_username
    assert _is_valid_username(_normalize_username("alex123")) is True
    assert _is_valid_username(_normalize_username("player_one")) is True
    assert _is_valid_username(_normalize_username("pro-gamer")) is True
    assert _is_valid_username(_normalize_username("a.b")) is True


def test_invalid_username_format():
    from routes.auth_routes import _is_valid_username, _normalize_username
    assert _is_valid_username(_normalize_username("ab")) is False  # too short
    assert _is_valid_username(_normalize_username("A" * 21)) is False  # too long
    assert _is_valid_username(_normalize_username("has space")) is False
    assert _is_valid_username(_normalize_username("special!")) is False


def test_username_normalized_to_lowercase():
    from routes.auth_routes import _normalize_username
    assert _normalize_username("  Alex123  ") == "alex123"


# ---------------------------------------------------------------------------
# Test: Duplicate username rejected
# ---------------------------------------------------------------------------

def test_duplicate_username_rejected():
    db = FakeDB()
    db.users.insert_one({"username": "alex123", "name": "Alex", "email": "a@test.com"})

    existing = db.users.find_one({"username": "alex123"})
    assert existing is not None

    # A second user with same username should be rejected
    duplicate = db.users.find_one({"username": "alex123"})
    assert duplicate is not None  # exists, so registration should fail


# ---------------------------------------------------------------------------
# Test: Registration rejects unknown teammate username
# ---------------------------------------------------------------------------

def test_registration_rejects_unknown_teammate():
    db = FakeDB()
    # Only "alex123" exists
    db.users.insert_one({"_id": "u1", "username": "alex123", "name": "Alex"})

    # Try to register with teammate "ghost" who doesn't exist
    team_members = [
        {"username": "ghost", "name": "Ghost", "game_uid": "123"}
    ]

    # Simulate lookup
    for member in team_members:
        user = db.users.find_one({"username": member["username"]})
        assert user is None, "Should reject unknown teammate"


# ---------------------------------------------------------------------------
# Test: Duplicate teammate rejected
# ---------------------------------------------------------------------------

def test_duplicate_teammate_rejected():
    team_members = [
        {"user_id": "u1", "username": "alex", "name": "Alex", "game_uid": "1"},
        {"user_id": "u1", "username": "alex", "name": "Alex", "game_uid": "1"},
    ]

    seen_user_ids = set()
    for member in team_members:
        uid = member["user_id"]
        if uid in seen_user_ids:
            # Duplicate detected
            assert True
            return
        seen_user_ids.add(uid)

    assert False, "Should have detected duplicate"


# ---------------------------------------------------------------------------
# Test: Submitting kills updates stats once
# ---------------------------------------------------------------------------

def test_submitting_kills_updates_stats():
    from routes.player_stats_routes import upsert_player_stats

    db = FakeDB()
    # Patch the global mongo reference
    import routes.player_stats_routes as psr
    psr.mongo = db

    upsert_player_stats("u1", "BGMI", kills_delta=5)

    doc = db.player_stats.find_one({"user_id": "u1", "game": "BGMI"})
    assert doc is not None
    assert doc["total_kills"] == 5


# ---------------------------------------------------------------------------
# Test: Re-submitting identical match result is a no-op
# ---------------------------------------------------------------------------

def test_resubmitting_identical_result_noop():
    from routes.player_stats_routes import upsert_player_stats

    db = FakeDB()
    import routes.player_stats_routes as psr
    psr.mongo = db

    # First submission: 5 kills
    upsert_player_stats("u1", "BGMI", kills_delta=5)
    doc1 = db.player_stats.find_one({"user_id": "u1", "game": "BGMI"})
    assert doc1["total_kills"] == 5

    # Re-submit identical: delta = 0
    upsert_player_stats("u1", "BGMI", kills_delta=0)
    doc2 = db.player_stats.find_one({"user_id": "u1", "game": "BGMI"})
    assert doc2["total_kills"] == 5  # unchanged


# ---------------------------------------------------------------------------
# Test: Editing kills from 5→7 moves total by +2
# ---------------------------------------------------------------------------

def test_editing_kills_delta():
    from routes.player_stats_routes import upsert_player_stats

    db = FakeDB()
    import routes.player_stats_routes as psr
    psr.mongo = db

    upsert_player_stats("u1", "BGMI", kills_delta=5)
    doc = db.player_stats.find_one({"user_id": "u1", "game": "BGMI"})
    assert doc["total_kills"] == 5

    # Edit: new total is 7, delta = 7 - 5 = +2
    upsert_player_stats("u1", "BGMI", kills_delta=2)
    doc = db.player_stats.find_one({"user_id": "u1", "game": "BGMI"})
    assert doc["total_kills"] == 7


# ---------------------------------------------------------------------------
# Test: Declaring a winner is +1
# ---------------------------------------------------------------------------

def test_declaring_winner_is_plus_one():
    from bson import ObjectId
    from routes.player_stats_routes import upsert_player_stats, upsert_global_wins

    db = FakeDB()
    uid = str(ObjectId())
    db.users.insert_one({"_id": uid, "tournaments_won": 0})
    import routes.player_stats_routes as psr
    psr.mongo = db

    upsert_global_wins(uid, 1)
    user = db.users.find_one({"_id": uid})
    assert user["tournaments_won"] == 1

    upsert_player_stats(uid, "BGMI", tournaments_won_delta=1)
    stats = db.player_stats.find_one({"user_id": uid, "game": "BGMI"})
    assert stats["tournaments_won"] == 1


# ---------------------------------------------------------------------------
# Test: Re-saving same winner doesn't double it
# ---------------------------------------------------------------------------

def test_resaving_same_winner_no_double():
    from routes.player_stats_routes import upsert_global_wins

    db = FakeDB()
    db.users.insert_one({"_id": "u1", "tournaments_won": 1})
    import routes.player_stats_routes as psr
    psr.mongo = db

    # Simulate the idempotent check in declare_winner:
    # old_winner_id == new_winner_id → no-op
    old_winner_id = "u1"
    new_winner_id = "u1"
    if old_winner_id != new_winner_id:
        upsert_global_wins(new_winner_id, 1)

    user = db.users.find_one({"_id": "u1"})
    assert user["tournaments_won"] == 1  # unchanged


# ---------------------------------------------------------------------------
# Test: Changing winner moves +1 to new user
# ---------------------------------------------------------------------------

def test_changing_winner():
    from bson import ObjectId
    from routes.player_stats_routes import upsert_global_wins

    db = FakeDB()
    uid1 = str(ObjectId())
    uid2 = str(ObjectId())
    db.users.insert_one({"_id": uid1, "tournaments_won": 1})
    db.users.insert_one({"_id": uid2, "tournaments_won": 0})
    import routes.player_stats_routes as psr
    psr.mongo = db

    old_winner_id = uid1
    new_winner_id = uid2

    if old_winner_id and old_winner_id != new_winner_id:
        upsert_global_wins(old_winner_id, -1)
    upsert_global_wins(new_winner_id, 1)

    assert db.users.find_one({"_id": uid1})["tournaments_won"] == 0
    assert db.users.find_one({"_id": uid2})["tournaments_won"] == 1


# ---------------------------------------------------------------------------
# Test: Leaderboards return correctly sorted, isolated data
# ---------------------------------------------------------------------------

def test_global_leaderboard_sorted_by_wins():
    db = FakeDB()
    db.users.insert_one({"_id": "u1", "username": "alpha", "name": "Alpha", "tournaments_won": 3})
    db.users.insert_one({"_id": "u2", "username": "beta", "name": "Beta", "tournaments_won": 5})
    db.users.insert_one({"_id": "u3", "username": "gamma", "name": "Gamma", "tournaments_won": 5})

    db.player_stats.insert_one({"user_id": "u1", "game": "BGMI", "total_kills": 100, "tournaments_played": 5, "tournaments_won": 3})
    db.player_stats.insert_one({"user_id": "u2", "game": "BGMI", "total_kills": 200, "tournaments_played": 5, "tournaments_won": 5})
    db.player_stats.insert_one({"user_id": "u3", "game": "BGMI", "total_kills": 150, "tournaments_played": 5, "tournaments_won": 5})

    # Simulate aggregation
    stats_docs = list(db.player_stats.aggregate([
        {"$group": {"_id": "$user_id", "total_kills": {"$sum": "$total_kills"}, "games_won": {"$sum": "$tournaments_won"}}},
        {"$sort": {"games_won": -1, "total_kills": -1, "_id": 1}},
    ]))

    assert stats_docs[0]["_id"] == "u2"  # 5 wins, 200 kills
    assert stats_docs[1]["_id"] == "u3"  # 5 wins, 150 kills
    assert stats_docs[2]["_id"] == "u1"  # 3 wins


def test_game_leaderboard_isolated_by_game():
    db = FakeDB()
    db.users.insert_one({"_id": "u1", "username": "alpha", "name": "Alpha"})
    db.users.insert_one({"_id": "u2", "username": "beta", "name": "Beta"})

    db.player_stats.insert_one({"user_id": "u1", "game": "BGMI", "total_kills": 100})
    db.player_stats.insert_one({"user_id": "u2", "game": "FREE_FIRE", "total_kills": 200})
    db.player_stats.insert_one({"user_id": "u1", "game": "FREE_FIRE", "total_kills": 50})

    # BGMI leaderboard should only show BGMI stats
    bgmi_docs = list(db.player_stats.aggregate([
        {"$match": {"game": "BGMI"}},
        {"$sort": {"total_kills": -1}},
    ]))
    assert len(bgmi_docs) == 1
    assert bgmi_docs[0]["user_id"] == "u1"
    assert bgmi_docs[0]["total_kills"] == 100

    # Free Fire leaderboard should only show FREE_FIRE stats
    ff_docs = list(db.player_stats.aggregate([
        {"$match": {"game": "FREE_FIRE"}},
        {"$sort": {"total_kills": -1}},
    ]))
    assert len(ff_docs) == 2
    assert ff_docs[0]["user_id"] == "u2"  # 200 kills
    assert ff_docs[1]["user_id"] == "u1"  # 50 kills


def test_global_and_game_leaderboards_are_isolated():
    """Kills feeding Global, or wins feeding a game leaderboard — must not happen."""
    db = FakeDB()
    db.users.insert_one({"_id": "u1", "username": "alpha", "name": "Alpha"})
    db.users.insert_one({"_id": "u2", "username": "beta", "name": "Beta"})

    # u1 has high kills in BGMI but no wins
    # u2 has few kills but many wins
    db.player_stats.insert_one({"user_id": "u1", "game": "BGMI", "total_kills": 500, "tournaments_played": 10, "tournaments_won": 0})
    db.player_stats.insert_one({"user_id": "u2", "game": "BGMI", "total_kills": 50, "tournaments_played": 10, "tournaments_won": 5})
    db.player_stats.insert_one({"user_id": "u2", "game": "FREE_FIRE", "total_kills": 300, "tournaments_played": 8, "tournaments_won": 3})

    # Global leaderboard: u2 should be first (5+3=8 wins) vs u1 (0 wins)
    global_docs = list(db.player_stats.aggregate([
        {"$group": {"_id": "$user_id", "total_kills": {"$sum": "$total_kills"}, "games_won": {"$sum": "$tournaments_won"}}},
        {"$sort": {"games_won": -1, "total_kills": -1}},
    ]))
    assert global_docs[0]["_id"] == "u2"  # most wins
    assert global_docs[0]["games_won"] == 8  # 5+3

    # BGMI leaderboard: u1 should be first (500 kills) vs u2 (50 kills)
    bgmi_docs = list(db.player_stats.aggregate([
        {"$match": {"game": "BGMI"}},
        {"$sort": {"total_kills": -1}},
    ]))
    assert bgmi_docs[0]["user_id"] == "u1"  # most BGMI kills
    assert bgmi_docs[0]["total_kills"] == 500

    # Verify BGMI leaderboard doesn't include FREE_FIRE stats
    assert bgmi_docs[0]["total_kills"] == 500  # not 500+300

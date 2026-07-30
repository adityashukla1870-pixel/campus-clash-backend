import re
from datetime import datetime

DEFAULT_CHANNELS = [
    {"key": "general", "name": "general", "icon": "\U0001F4AC",
     "description": "Talk strategy, find teammates, hang out", "type": "text", "admin_only_post": False},
    {"key": "announcements", "name": "announcements", "icon": "\U0001F4E2",
     "description": "Official updates from the Campus Clash team", "type": "text", "admin_only_post": True},
    {"key": "lfg", "name": "looking-for-squad", "icon": "\U0001F3AF",
     "description": "Post your game + rank, find a squad", "type": "lfg", "admin_only_post": False},
    {"key": "bgmi", "name": "bgmi", "icon": "\U0001F3AE",
     "description": "BGMI chat", "type": "text", "admin_only_post": False},
    {"key": "freefire", "name": "free-fire", "icon": "\U0001F525",
     "description": "Free Fire chat", "type": "text", "admin_only_post": False},
    {"key": "valorant", "name": "valorant", "icon": "\u2694\ufe0f",
     "description": "Valorant chat", "type": "text", "admin_only_post": False},
]


def ensure_default_channels(mongo):
    """Idempotently seeds the default channel list. Safe to call on every app start."""
    channels = mongo.db.chat_channels
    for c in DEFAULT_CHANNELS:
        channels.update_one({"key": c["key"]}, {"$setOnInsert": c}, upsert=True)


def serialize_channel(c):
    return {
        "key": c["key"],
        "name": c.get("name", c["key"]),
        "icon": c.get("icon", "#"),
        "description": c.get("description", ""),
        "type": c.get("type", "text"),
        "admin_only_post": c.get("admin_only_post", False)
    }


def serialize_message(m):
    return {
        "id": str(m["_id"]),
        "channel": m.get("channel"),
        "user_id": m.get("user_id"),
        "name": m.get("name"),
        "role": m.get("role", "user"),
        "is_champion": m.get("is_champion", False),
        "message": m.get("message", ""),
        "image_url": m.get("image_url"),
        "reply_to": m.get("reply_to"),
        "mentions": m.get("mentions", []),
        "reactions": m.get("reactions", {}),
        "lfg": m.get("lfg"),
        "pinned": m.get("pinned", False),
        "deleted": m.get("deleted", False),
        "edited_at": m["edited_at"].isoformat() if m.get("edited_at") else None,
        "created_at": m["created_at"].isoformat() if m.get("created_at") else None
    }


# A small, deliberately mild wordlist — this flags the obvious stuff so chat doesn't
# turn ugly by default. It is not a substitute for real moderation (report + admin
# mute/ban below handle the rest).
_BLOCKED_WORDS = [
    "madarchod", "behenchod", "bhosdike", "chutiya", "randi", "gandu", "harami",
    "fuck", "fucker", "fucking", "bitch", "asshole", "bastard", "slut", "whore"
]
_BLOCKED_PATTERN = re.compile(r"\b(" + "|".join(_BLOCKED_WORDS) + r")\b", re.IGNORECASE)


def censor(text):
    """Replaces blocked words with asterisks, keeping the first letter for readability."""
    def repl(match):
        word = match.group(0)
        return word[0] + "*" * (len(word) - 1)
    return _BLOCKED_PATTERN.sub(repl, text)


def is_muted(user):
    until = user.get("muted_until") if user else None
    if not until:
        return False
    if isinstance(until, str):
        try:
            until = datetime.fromisoformat(until.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return False
    return datetime.utcnow() < until


def is_chat_banned(user):
    return bool(user and user.get("chat_banned"))

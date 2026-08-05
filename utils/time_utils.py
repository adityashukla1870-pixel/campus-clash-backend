"""
PyMongo returns datetimes without timezone info by default, even though
they're always stored as UTC internally. Calling .isoformat() on a naive
datetime produces a string with no "Z"/offset — and JavaScript's `new
Date(...)` treats a timezone-less ISO string as LOCAL time, not UTC. For
users in IST that silently shifts every timestamp by 5:30 hours, which is
exactly the kind of bug that makes a countdown say "Match Started" when the
match is actually still 5+ hours away.

Always use this instead of calling `.isoformat()` directly on a Mongo datetime.
"""

from datetime import timezone


def to_utc_iso(dt):
    """Convert a (possibly naive) datetime from Mongo into a proper UTC ISO
    string. Returns None if dt is falsy."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

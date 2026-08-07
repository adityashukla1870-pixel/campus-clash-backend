from datetime import datetime, timedelta

from models.chat_model import can_delete_message, is_muted


def test_can_delete_message_allows_owner_and_admin():
    message = {"user_id": "user-1"}

    assert can_delete_message({"_id": "user-1", "role": "player"}, message) is True
    assert can_delete_message({"_id": "user-2", "role": "admin"}, message) is True
    assert can_delete_message({"_id": "user-2", "role": "player"}, message) is False


def test_is_muted_accepts_iso_string_for_future_window():
    user = {"muted_until": (datetime.utcnow() + timedelta(minutes=5)).isoformat()}

    assert is_muted(user) is True

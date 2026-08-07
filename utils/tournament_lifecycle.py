from __future__ import annotations

import random
from typing import Any, Dict, List, Optional


DEFAULT_POINTS_TABLE = {"1": 10, "2": 6, "3": 5, "4": 4, "5": 3, "6": 2, "7": 2, "8": 1, "9": 1}
DEFAULT_KILL_POINT = 1


def build_stage_seed_distribution(participants: List[Dict[str, Any]], pod_count: int, strategy: str = "random") -> List[List[Dict[str, Any]]]:
    """Create a seed distribution for a stage using esports-friendly patterns.

    - random: simple shuffle and round-robin
    - snake: distributes ranked teams in a snake pattern so nearby teams do not meet early
    """
    if pod_count < 1:
        raise ValueError("pod_count must be at least 1")

    if not participants:
        return [[] for _ in range(pod_count)]

    if strategy == "snake":
        ordered = list(participants)
        pods = [[] for _ in range(pod_count)]
        chunk_size = max(1, (len(ordered) + pod_count - 1) // pod_count)
        for index, participant in enumerate(ordered):
            pod_index = index // chunk_size
            if pod_index >= pod_count:
                pod_index = pod_count - 1
            pods[pod_index].append(participant)
        return pods

    shuffled = list(participants)
    random.shuffle(shuffled)
    pods = [[] for _ in range(pod_count)]
    for index, participant in enumerate(shuffled):
        pods[index % pod_count].append(participant)
    return pods


def build_winner_update(winner: Dict[str, Any], stage_name: Optional[str] = None, source: str = "final_stage") -> Dict[str, Any]:
    """Create a normalized winner payload that can be shared by stage finalization and manual winner declaration."""
    if not winner:
        raise ValueError("winner must not be empty")

    update_fields = {
        "status": "completed",
        "winner_registration_id": winner.get("registration_id"),
        "winner_name": winner.get("name"),
        "winner_source": source,
    }
    if winner.get("user_id"):
        update_fields["winner_id"] = winner.get("user_id")
    if stage_name:
        update_fields["winner_stage_name"] = stage_name
    return update_fields


def normalize_tournament_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize create-tournament payloads so the same schema is used across quick and full formats."""
    points_table = data.get("points_table") or DEFAULT_POINTS_TABLE
    kill_point_value = data.get("kill_point_value", DEFAULT_KILL_POINT)

    return {
        "name": data.get("name"),
        "game": data.get("game"),
        "entry_fee": int(data.get("entry_fee", 0)),
        "prize_pool": int(data.get("prize_pool", 0)),
        "max_players": int(data.get("max_players", 100)),
        "mode": data.get("mode", "solo"),
        "team_size": int(data.get("team_size", 1)),
        "format": data.get("format", "quick"),
        "points_table": points_table,
        "kill_point_value": int(kill_point_value),
        "status": "upcoming",
        "room_id": None,
        "room_password": None,
        "match_start_time": None,
        "bracket": None,
        "winner_id": None,
        "winner_registration_id": None,
        "winner_name": None,
        "winner_source": None,
        "winner_stage_name": None,
        "stage_flow": [],
    }

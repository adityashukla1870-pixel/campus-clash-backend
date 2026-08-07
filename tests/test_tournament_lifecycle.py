from utils.tournament_lifecycle import build_stage_seed_distribution, build_winner_update


def test_build_stage_seed_distribution_snake():
    participants = [
        {"registration_id": f"r{i}", "name": f"Team {i}"}
        for i in range(1, 7)
    ]

    distribution = build_stage_seed_distribution(participants, pod_count=3, strategy="snake")

    assert len(distribution) == 3
    assert [len(pod) for pod in distribution] == [2, 2, 2]
    assert [entry["registration_id"] for entry in distribution[0]] == ["r1", "r2"]
    assert [entry["registration_id"] for entry in distribution[1]] == ["r3", "r4"]
    assert [entry["registration_id"] for entry in distribution[2]] == ["r5", "r6"]


def test_build_winner_update_uses_registration_and_user():
    winner = {"registration_id": "reg-123", "name": "Team Alpha", "user_id": "user-123"}

    payload = build_winner_update(winner, "Campus Clash Finals", source="final_stage")

    assert payload["winner_registration_id"] == "reg-123"
    assert payload["winner_id"] == "user-123"
    assert payload["winner_name"] == "Team Alpha"
    assert payload["status"] == "completed"
    assert payload["winner_source"] == "final_stage"

from datetime import datetime

def create_tournament(mongo, data):
    tournaments = mongo.db.tournaments

    tournament = {
        "title": data["title"],
        "game": data["game"],
        "entry_fee": data["entry_fee"],
        "prize_pool": data["prize_pool"],
        "total_slots": data["total_slots"],
        "filled_slots": 0,
        "date_time": data["date_time"],
        "status": "upcoming",
        "room_id": "",
        "room_password": "",
        "created_at": datetime.utcnow()
    }

    tournaments.insert_one(tournament)
    return {"message": "Tournament created"}

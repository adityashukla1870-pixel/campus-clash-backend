def create_user(mongo, data):
    users = mongo.db.users   # users collection access

    # check email already exist
    existing = users.find_one({"email": data["email"]})
    if existing:
        return {"error": "Email already registered"}

    users.insert_one(data)   # insert user
    return {"message": "User registered successfully"}

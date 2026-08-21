import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/campus_clash")
client = MongoClient(MONGO_URI)
db_name = MONGO_URI.rsplit("/", 1)[-1].split("?")[0]
db = client[db_name]

collections_to_drop = ["registrations", "tournament_stages", "notifications", "chat_messages", "chat_reports"]

for col_name in collections_to_drop:
    if col_name in db.list_collection_names():
        count = db[col_name].count_documents({})
        db[col_name].drop()
        print(f"Dropped {col_name} ({count} docs)")
    else:
        print(f"Skipped {col_name} (not found)")

print("\nRemaining collections:")
for name in db.list_collection_names():
    count = db[name].count_documents({})
    print(f"  {name}: {count} docs")

print("\nAll clean!")

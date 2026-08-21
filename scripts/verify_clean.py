import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/campus_clash")
client = MongoClient(MONGO_URI)
db_name = MONGO_URI.rsplit("/", 1)[-1].split("?")[0]
db = client[db_name]

print("=== FINAL DB STATE ===")
for name in db.list_collection_names():
    count = db[name].count_documents({})
    print(f"  {name}: {count} docs")
print("=== DONE ===")

from werkzeug.security import generate_password_hash
import pymongo

conn = pymongo.MongoClient('mongodb+srv://adityashukla1870:GHH683MehNHPN@cluster0.gvkakaf.mongodb.net/campus_clash?retryWrites=true&w=majority&appName=Cluster0')
db = conn['campus_clash']

email = "adityashukla1870@gmail.com"
new_password = "Test@1234"

result = db.users.update_one(
    {"email": email},
    {"$set": {"password": generate_password_hash(new_password), "username": "adityashukla"}}
)

if result.matched_count:
    print(f"Updated password for {email}")
    print(f"Username set to: adityashukla")
    print(f"Password: {new_password}")
else:
    print("User not found")

# Verify
user = db.users.find_one({"email": email})
print(f"Username field: {user.get('username')}")

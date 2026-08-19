import pymongo
conn = pymongo.MongoClient('mongodb+srv://adityashukla1870:GHH683MehNHPN@cluster0.gvkakaf.mongodb.net/campus_clash?retryWrites=true&w=majority&appName=Cluster0')
db = conn['campus_clash']
users = list(db.users.find({}, {'name': 1, 'email': 1, 'username': 1}))
print(f"Total users: {len(users)}")
for u in users[:10]:
    print(f"  name={u.get('name')}, email={u.get('email')}, username={u.get('username')}")

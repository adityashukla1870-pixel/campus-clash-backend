import os
from dotenv import load_dotenv
from datetime import timedelta


load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "fallback-secret")
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/campus_clash")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "jwt-secret")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=10)
    


from flask import Flask
from flask_cors import CORS
from flask_pymongo import PyMongo
from flask_jwt_extended import JWTManager
from config import Config

from routes.auth_routes import auth, init_auth_routes
from routes.tournament_routes import tournament, init_tournament_routes

app = Flask(__name__)
app.config.from_object(Config)

CORS(app)

print("MONGO_URI VALUE:", app.config.get("MONGO_URI"))
mongo = PyMongo(app)

jwt = JWTManager(app)

# Initialize routes with mongo
init_auth_routes(mongo)
init_tournament_routes(mongo)

# Register blueprints
app.register_blueprint(auth, url_prefix="/auth")
app.register_blueprint(tournament, url_prefix="/tournament")

@app.route("/")
def home():
    return "Advanced Campus Clash Backend Running"

if __name__ == "__main__":
    app.run(debug=True)
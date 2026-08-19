from flask import Flask
from flask_cors import CORS
from flask_pymongo import PyMongo
from flask_jwt_extended import JWTManager
from flask_socketio import SocketIO
from config import Config

from routes.auth_routes import auth, init_auth_routes
from routes.tournament_routes import tournament, init_tournament_routes
from routes.notification_routes import notification, init_notification_routes
from routes.chat_routes import chat, init_chat_routes
from routes.stage_routes import stage, init_stage_routes
from routes.avatar_routes import avatars, init_avatar_routes
from routes.player_stats_routes import player_stats, init_player_stats_routes
from chat_events import register_chat_events
from flask import send_from_directory



app = Flask(__name__)
app.config.from_object(Config)

CORS(app, supports_credentials=True, origins=[
    "https://campus-clash-og.vercel.app",
    "http://localhost:5173",
    "http://localhost:3000",
], allow_headers=["Content-Type", "Authorization"], methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])

mongo = PyMongo(app)
jwt = JWTManager(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Inject mongo into route files
init_auth_routes(mongo)
init_tournament_routes(mongo)
init_notification_routes(mongo)
init_chat_routes(mongo)
init_stage_routes(mongo)
init_avatar_routes(mongo)
init_player_stats_routes(mongo)
register_chat_events(socketio, mongo)

# Register blueprints
app.register_blueprint(auth, url_prefix="/auth")
app.register_blueprint(tournament, url_prefix="/tournament")
app.register_blueprint(notification, url_prefix="/notifications")
app.register_blueprint(chat, url_prefix="/chat")
app.register_blueprint(stage, url_prefix="/stages")
app.register_blueprint(avatars, url_prefix="/avatars")
app.register_blueprint(player_stats, url_prefix="/stats")

@app.route('/uploads/<path:filename>')
def get_file(filename):
    return send_from_directory('uploads', filename)

@app.route("/")
def home():
    return "Advanced Campus Clash Backend Running"

@app.route('/api/health')
def health():
    return {"status": "online", "provider": "Groq", "models": "GROQ_MODELS"}

if __name__ == "__main__":
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)

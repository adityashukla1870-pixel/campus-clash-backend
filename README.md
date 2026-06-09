# Campus Clash Backend

A Flask-based REST API backend for the Campus Clash tournament management platform. This application enables users to register for tournaments, manage payments, participate in gaming tournaments, and provides admin controls for tournament management.

## 🎮 Features

- **User Authentication** - JWT-based secure authentication with role-based access control
- **Tournament Management** - Create, list, and manage gaming tournaments
- **Registration & Payment System** - Handle tournament registrations with payment verification
- **Admin Dashboard** - Approve/reject payments, manage rooms, and declare winners
- **File Upload** - Secure payment proof uploads with file management
- **Room Management** - Release match rooms with credentials and start times
- **Match Timer** - Track match start times and durations
- **Winner Declaration** - Admin ability to declare tournament winners
- **Participant Tracking** - View approved participants in tournaments

## 🛠️ Tech Stack

- **Framework:** Flask 2.3.3
- **Database:** MongoDB
- **Authentication:** Flask-JWT-Extended
- **Database Driver:** PyMongo
- **CORS:** Flask-CORS
- **Security:** Werkzeug (password hashing, file handling)
- **Environment:** python-dotenv

## 📋 Prerequisites

- Python 3.8 or higher
- MongoDB (local or remote instance)
- pip (Python package manager)

## 🚀 Installation & Setup

### 1. Clone the Repository
```bash
git clone https://github.com/adityashukla1870-pixel/campus-clash-backend.git
cd campus-clash-backend
```

### 2. Create Virtual Environment
```bash
python -m venv venv

# Activate virtual environment
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables
Create a `.env` file in the project root with the following variables:

```env
SECRET_KEY=your-secret-key-here
JWT_SECRET_KEY=your-jwt-secret-here
MONGO_URI=mongodb://localhost:27017/campus_clash
```

**Example values for development:**
```env
SECRET_KEY=campusclashsecret
JWT_SECRET_KEY=superjwtsecret
MONGO_URI=mongodb://localhost:27017/campus_clash
```

### 5. Ensure MongoDB is Running
Make sure MongoDB is running on your machine (default: `localhost:27017`)

### 6. Run the Application
```bash
python app.py
```

The server will start at `http://localhost:5000`

## 📁 Project Structure

```
campus-clash-backend/
├── app.py                      # Main Flask application
├── config.py                   # Configuration settings
├── requirements.txt            # Project dependencies
├── .env                        # Environment variables (git-ignored)
├── routes/
│   ├── auth_routes.py         # Authentication endpoints
│   └── tournament_routes.py    # Tournament management endpoints
├── models/                     # Data models (MongoDB schemas)
├── utils/
│   └── code_generator.py       # Payment code generation utility
├── uploads/                    # User uploaded files (payment proofs)
└── .vscode/                    # VS Code workspace settings
```

## 🔌 API Endpoints

### Authentication Routes (`/auth`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/register` | Register a new user |
| POST | `/auth/login` | Login and get JWT token |
| GET | `/auth/profile` | Get authenticated user profile |

### Tournament Routes (`/tournament`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---|
| POST | `/tournament/create` | Create a new tournament | ✅ Admin |
| GET | `/tournament/all` | Get all tournaments | ✅ |
| GET | `/tournament/<tournament_id>` | Get tournament details | ✅ |
| POST | `/tournament/register/<tournament_id>` | Register for tournament | ✅ |
| POST | `/tournament/upload-payment/<registration_id>` | Upload payment proof | ✅ |
| GET | `/tournament/my-tournaments` | Get user's tournaments | ✅ |
| GET | `/tournament/participants/<tournament_id>` | Get tournament participants | ✅ |
| GET | `/tournament/room/<tournament_id>` | Get match room details | ✅ |
| POST | `/tournament/admin/pending-payments` | List pending payments | ✅ Admin |
| POST | `/tournament/admin/approve/<registration_id>` | Approve payment | ✅ Admin |
| POST | `/tournament/admin/reject/<registration_id>` | Reject payment | ✅ Admin |
| POST | `/tournament/admin/release-room/<tournament_id>` | Release match room | ✅ Admin |
| POST | `/tournament/admin/declare-winner` | Declare tournament winner | ✅ Admin |

### File Upload

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/uploads/<filename>` | Retrieve uploaded file |

## 🔐 Authentication

The API uses JWT (JSON Web Tokens) for authentication. 

**To use protected endpoints:**
1. Register a user via `/auth/register`
2. Login via `/auth/login` to get a token
3. Include the token in the `Authorization` header: `Authorization: Bearer <your_token>`

**Roles:**
- `player` - Regular users
- `admin` - Tournament administrators

## 📝 Example Requests

### Register a User
```bash
POST /auth/register
Content-Type: application/json

{
  "name": "John Doe",
  "email": "john@example.com",
  "password": "securepassword",
  "college": "XYZ University",
  "game_uid": "john_uid_123"
}
```

### Login
```bash
POST /auth/login
Content-Type: application/json

{
  "email": "john@example.com",
  "password": "securepassword"
}

Response:
{
  "message": "Login successful",
  "token": "eyJ0eXAiOiJKV1QiLCJhbGc..."
}
```

### Create Tournament (Admin)
```bash
POST /tournament/create
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "PUBG Championship 2024",
  "game": "PUBG",
  "entry_fee": 500,
  "prize_pool": 50000,
  "max_players": 128
}
```

## 🗄️ Database Collections

### Users Collection
```json
{
  "_id": ObjectId,
  "name": "string",
  "email": "string",
  "password": "hashed_password",
  "college": "string",
  "game_uid": "string",
  "role": "player|admin"
}
```

### Tournaments Collection
```json
{
  "_id": ObjectId,
  "name": "string",
  "game": "string",
  "entry_fee": number,
  "prize_pool": number,
  "max_players": number,
  "players": [array],
  "room_id": "string|null",
  "room_password": "string|null",
  "match_start_time": "datetime|null",
  "winner_id": "string|null",
  "created_at": "datetime"
}
```

### Registrations Collection
```json
{
  "_id": ObjectId,
  "user_id": "string",
  "tournament_id": ObjectId,
  "payment_code": "string",
  "payment_status": "pending|approved|rejected",
  "utr": "string|null",
  "screenshot": "string|null"
}
```

## ⚙️ Configuration

Edit `config.py` to modify application settings:

```python
class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "fallback-secret")
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/campus_clash")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "jwt-secret")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=10)
```

## 🐛 Troubleshooting

### MongoDB Connection Error
- Ensure MongoDB is running: `mongod`
- Check `MONGO_URI` in `.env` file
- Verify MongoDB is listening on the correct port

### JWT Token Errors
- Token may have expired (valid for 10 hours)
- Generate a new token via `/auth/login`
- Ensure token is included in `Authorization: Bearer <token>` header

### File Upload Issues
- Check if `uploads/` directory exists
- Ensure proper file permissions
- Verify file size limits (if applicable)

## 📦 Deployment

### Using Gunicorn (Production)
```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

### Using Docker
Create a `Dockerfile`:
```dockerfile
FROM python:3.9
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
```

## 🚦 Development Notes

- Keep `.env` file in `.gitignore` (never commit secrets)
- Use virtual environments for dependency isolation
- Implement proper error handling and logging
- Add unit tests for API endpoints
- Validate all user inputs before processing
- Use HTTPS in production environments

## 📄 License

This project is open source and available under the MIT License.

## 👤 Author

**Aditya Shukla**
- GitHub: [@adityashukla1870-pixel](https://github.com/adityashukla1870-pixel)

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📞 Support

For issues or questions, please open an issue on the [GitHub repository](https://github.com/adityashukla1870-pixel/campus-clash-backend/issues).

---

**Last Updated:** June 2026

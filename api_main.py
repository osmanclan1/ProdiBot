import datetime
import dateparser
import pytz
import os
import requests
import boto3
import json
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import List, Optional
from jose import JWTError, jwt
from datetime import datetime, timedelta
import asyncio
from dotenv import load_dotenv

load_dotenv()

# Import CORS middleware
from fastapi.middleware.cors import CORSMiddleware

# Import our shared logic!
import db_utils 

# --- AWS Secrets Manager Integration ---
def load_secrets_from_aws():
    """
    Fetches secrets from AWS Secrets Manager and loads them
    into environment variables.
    """
    secret_name = "prodibot/secrets"  # The name you set in AWS Secrets Manager
    region_name = "us-east-1"         # Your EC2 instance's region

    try:
        # Create a Secrets Manager client
        session = boto3.session.Session()
        client = session.client(
            service_name='secretsmanager',
            region_name=region_name
        )

        print("Fetching secrets from AWS Secrets Manager...")
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except Exception as e:
        print(f"Error fetching secrets from AWS: {e}")
        # Fall back to environment variables or .env file
        print("Falling back to environment variables...")
        return False

    # The secret is returned as a JSON string
    secret_string = get_secret_value_response['SecretString']
    secrets = json.loads(secret_string)

    # Load each key-value pair into the environment
    for key, value in secrets.items():
        os.environ[key] = value

    print("Successfully loaded secrets from AWS Secrets Manager.")
    return True

# Try AWS first, fall back to .env
aws_success = load_secrets_from_aws()
if not aws_success:
    load_dotenv()  # Load environment variables from .env file

# --- Load Environment Variables ---
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
SECRET_KEY = os.environ.get("SECRET_KEY")  # For signing JWTs
if not all([DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, SECRET_KEY]):
    print("FATAL ERROR: Missing OAuth2 or SECRET_KEY environment variables!")
    exit()

# --- Auth Configuration ---
# Your EC2's public IP
API_BASE_URL = "http://3.83.248.40:8000"

# Your Netlify site's URL
FRONTEND_URL = "https://omklsd.netlify.app"

DISCORD_AUTH_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_URL = "https://discord.com/api/users/@me"

# JWT settings
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

# --- Pydantic Models (Data Validation) ---
class ReminderRequest(BaseModel):
    taskName: str
    taskDesc: str | None = None
    dueDate: str  # e.g., "2025-11-10"
    dueTime: str  # e.g., "14:30"
    priority: str

class ReminderItem(BaseModel):
    reminder_id: str
    task: str
    remind_time_utc: str
    is_recurring: bool = False
    recurrence_rule: str | None = None

class User(BaseModel):
    id: str
    username: str
    avatar: str | None = None

# --- FastAPI App ---
app = FastAPI(
    title="Prodibot API",
    description="API for the Prodibot web interface to interact with DynamoDB.",
    version="1.0.0"
)

# --- Add CORS Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],  # Only allow your frontend
    allow_credentials=True,       # Fine to leave as True
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Authentication Helpers ---

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Creates a signed JWT."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(request: Request) -> User:
    """
    Read JWT from Authorization: Bearer <token> header.
    No cookies involved.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = auth_header.split(" ", 1)[1]

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        username: str = payload.get("username")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        return User(id=user_id, username=username)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# --- Auth Endpoints ---

@app.get("/api/login")
async def login():
    """
    Redirects the user to Discord's login page.
    """
    scope = "identify"
    redirect_uri = f"{API_BASE_URL}/api/auth/callback"
    auth_url = (
        f"{DISCORD_AUTH_URL}"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scope}"
    )
    return RedirectResponse(url=auth_url)

@app.get("/api/auth/callback")
async def auth_callback(code: str):
    """
    Handles the callback from Discord. Exchanges the code for a token,
    fetches the user, creates a JWT, and redirects to the frontend
    with the JWT in the URL fragment (#token=...).
    """
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": f"{API_BASE_URL}/api/auth/callback",
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(DISCORD_TOKEN_URL, data=data, headers=headers)
    r.raise_for_status()
    token_data = r.json()

    headers = {"Authorization": f"Bearer {token_data['access_token']}"}
    user_r = requests.get(DISCORD_API_URL, headers=headers)
    user_r.raise_for_status()
    user_json = user_r.json()

    user = User(id=user_json["id"], username=user_json["username"])
    expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.id, "username": user.username},
        expires_delta=expires,
    )

    # Redirect to the frontend with token in URL fragment
    redirect_url = f"{FRONTEND_URL}#token={access_token}"
    return RedirectResponse(url=redirect_url, status_code=302)

@app.get("/api/me", response_model=User)
async def get_me(current_user: User = Depends(get_current_user)):
    """
    Frontend uses this to validate the token and get user info.
    """
    return current_user

@app.get("/api/logout")
async def logout():
    """
    Backend-side logout is trivial now; real logout is frontend
    clearing localStorage.
    """
    return RedirectResponse(url=FRONTEND_URL)

# --- API Endpoints ---

@app.post("/api/create-reminder", status_code=201)
async def create_reminder_endpoint(
    reminder: ReminderRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Endpoint to create a new one-time reminder.
    Uses the authenticated user's ID.
    """
    # This is the fallback channel if DMs fail.
    channel_id = "321078607772385280"  # <-- REPLACE THIS

    try:
        time_str = f"{reminder.dueDate} {reminder.dueTime}"
        remind_time = dateparser.parse(
            time_str,
            settings={
                "TIMEZONE": "America/Chicago",
                "RETURN_AS_TIMEZONE_AWARE": True,
            },
        )

        if not remind_time or remind_time <= datetime.now(db_utils.LOCAL_TZ):
            raise ValueError("Invalid or past time provided.")

        full_task = reminder.taskName
        if reminder.taskDesc:
            full_task += f" (Notes: {reminder.taskDesc})"

        success = await db_utils.add_reminder_to_db(
            author_id=current_user.id,
            channel_id=channel_id,
            remind_time=remind_time,
            task=full_task,
            is_recurring=False,
        )

        if not success:
            raise HTTPException(
                status_code=500,
                detail="Failed to save reminder to database.",
            )

        return {
            "message": "Reminder created successfully",
            "task": full_task,
            "remind_time": remind_time.isoformat(),
        }
    except Exception as e:
        print(f"[API ERROR] {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Error processing reminder: {str(e)}",
        )

@app.get("/api/my-reminders", response_model=List[ReminderItem])
async def get_my_reminders(current_user: User = Depends(get_current_user)):
    """
    Endpoint to fetch all PENDING reminders for the logged-in user.
    """
    try:
        # Our db_utils.reminders_table is sync, so we wrap the call
        response = await asyncio.to_thread(
            db_utils.reminders_table.query,
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": current_user.id},
        )
        items = response.get("Items", [])

        pending_items = [
            item
            for item in items
            if item.get("status", "PENDING") == "PENDING"
        ]
        pending_items.sort(key=lambda r: r["remind_time_utc"])

        return [
            ReminderItem(
                reminder_id=item["reminder_id"],
                task=item["task"],
                remind_time_utc=item["remind_time_utc"],
                is_recurring=item.get("is_recurring", False),
                recurrence_rule=item.get("recurrence_rule"),
            )
            for item in pending_items
        ]
    except Exception as e:
        print(f"[API ERROR] {e}")
        raise HTTPException(
            status_code=500, detail=f"Error fetching reminders: {str(e)}"
        )

@app.delete("/api/delete-reminder/{reminder_id}", status_code=200)
async def delete_reminder_endpoint(
    reminder_id: str,
    current_user: User = Depends(get_current_user),
):
    """
    Endpoint to delete a PENDING reminder.
    """
    try:
        await asyncio.to_thread(
            db_utils.reminders_table.delete_item,
            Key={
                "user_id": current_user.id,
                "reminder_id": reminder_id,
            },
        )
        return {"message": "Reminder deleted successfully"}
    except Exception as e:
        print(f"[API ERROR] Failed to delete reminder {reminder_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error deleting reminder: {e}",
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_main:app", host="127.0.0.1", port=8000, reload=True)

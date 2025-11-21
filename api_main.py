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
from magnum import Magnum
from dotenv import load_dotenv
from urllib.parse import quote



load_dotenv()

# Import CORS middleware
from fastapi.middleware.cors import CORSMiddleware

# Import shared DB logic
import db_utils 


# --- AWS Secrets Manager Integration ---
def load_secrets_from_aws():
    secret_name = "prodibot/secrets"
    region_name = "us-east-1"

    try:
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
        print("Falling back to environment variables...")
        return False

    secret_string = get_secret_value_response['SecretString']
    secrets = json.loads(secret_string)

    for key, value in secrets.items():
        os.environ[key] = value

    print("Successfully loaded secrets from AWS Secrets Manager.")
    return True


aws_success = load_secrets_from_aws()
if not aws_success:
    load_dotenv()


# --- Environment Vars ---
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
SECRET_KEY = os.environ.get("SECRET_KEY")

if not all([DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, SECRET_KEY]):
    print("FATAL ERROR: Missing OAuth2 or SECRET_KEY environment variables!")
    exit()


# --- Config ---
API_BASE_URL = "http://3.83.248.40:8000"
FRONTEND_URL = "https://d1wdxkpmgii4om.cloudfront.net/"

DISCORD_AUTH_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_URL = "https://discord.com/api/users/@me"

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days


# --- Pydantic Models ---
class ReminderRequest(BaseModel):
    taskName: str
    taskDesc: Optional[str] = None
    dueDate: str
    dueTime: str
    priority: str


class ReminderItem(BaseModel):
    reminder_id: str
    task: str
    remind_time_utc: str
    is_recurring: bool = False
    recurrence_rule: Optional[str] = None


class User(BaseModel):
    id: str
    username: str
    avatar: Optional[str] = None


# --- FastAPI App ---
app = FastAPI(title="Prodibot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- JWT Helpers ---
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()

    expire = datetime.utcnow() + (
        expires_delta if expires_delta else timedelta(minutes=15)
    )
    to_encode.update({"exp": expire})

    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(request: Request) -> User:
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = auth_header.split(" ", 1)[1]

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = payload.get("sub")
        username = payload.get("username")

        if uid is None:
            raise HTTPException(status_code=401, detail="Invalid token payload")

        return User(id=uid, username=username)

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ==========================
#   AUTH ROUTES (UPDATED)
# ==========================

@app.get("/api/login")
async def login():
    """
    Sends user to Discord's authorize URL.
    MUST URL-encode redirect_uri or Discord blocks silently.
    """
    scope = "identify"
    redirect_raw = f"{API_BASE_URL}/api/auth/callback"
    redirect_encoded = quote(redirect_raw, safe="")

    auth_url = (
        f"{DISCORD_AUTH_URL}"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={redirect_encoded}"
        f"&response_type=code"
        f"&scope={scope}"
    )

    print("[OAUTH] Redirecting to:", auth_url)
    return RedirectResponse(url=auth_url)


@app.get("/api/auth/callback")
async def auth_callback(code: str):
    """
    Discord redirects here with ?code=
    We exchange it for a token, fetch user info, create JWT, 
    then redirect to frontend with #token=<jwt>.
    """

    redirect_raw = f"{API_BASE_URL}/api/auth/callback"

    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_raw,   # raw for POST
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    # Exchange code for token
    token_res = requests.post(DISCORD_TOKEN_URL, data=data, headers=headers)
    print("[OAUTH] Token response:", token_res.status_code, token_res.text)
    token_res.raise_for_status()

    token_json = token_res.json()

    # Fetch Discord user info
    headers = {"Authorization": f"Bearer {token_json['access_token']}"}
    user_res = requests.get(DISCORD_API_URL, headers=headers)
    print("[OAUTH] User response:", user_res.status_code, user_res.text)
    user_res.raise_for_status()

    user_json = user_res.json()

    # Create our JWT
    user = User(id=user_json["id"], username=user_json["username"])
    expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    jwt_token = create_access_token(
        data={"sub": user.id, "username": user.username},
        expires_delta=expires
    )

    # Redirect to frontend with token
    final_redirect = f"{FRONTEND_URL}#token={jwt_token}"
    print("[OAUTH] Final redirect:", final_redirect)

    return RedirectResponse(url=final_redirect, status_code=302)


@app.get("/api/me", response_model=User)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@app.get("/api/logout")
async def logout():
    return RedirectResponse(url=FRONTEND_URL)


# ==========================
#   REMINDER API
# ==========================

@app.post("/api/create-reminder", status_code=201)
async def create_reminder_endpoint(
    reminder: ReminderRequest,
    current_user: User = Depends(get_current_user)
):
    channel_id = "321078607772385280"  # change this

    try:
        time_str = f"{reminder.dueDate} {reminder.dueTime}"
        remind_time = dateparser.parse(
            time_str,
            settings={
                "TIMEZONE": "America/Chicago",
                "RETURN_AS_TIMEZONE_AWARE": True
            }
        )

        if not remind_time or remind_time <= datetime.now(db_utils.LOCAL_TZ):
            raise ValueError("Invalid or past time.")

        full_task = reminder.taskName
        if reminder.taskDesc:
            full_task += f" (Notes: {reminder.taskDesc})"

        success = await db_utils.add_reminder_to_db(
            author_id=current_user.id,
            channel_id=channel_id,
            remind_time=remind_time,
            task=full_task,
            is_recurring=False
        )

        if not success:
            raise HTTPException(500, "Failed to save reminder to DB.")

        return {
            "message": "Reminder created",
            "task": full_task,
            "remind_time": remind_time.isoformat()
        }

    except Exception as e:
        print("[API ERROR]", e)
        raise HTTPException(400, f"Error: {str(e)}")


@app.get("/api/my-reminders", response_model=List[ReminderItem])
async def get_my_reminders(current_user: User = Depends(get_current_user)):
    try:
        response = await asyncio.to_thread(
            db_utils.reminders_table.query,
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": current_user.id}
        )
        items = response.get("Items", [])

        pending = [
            item for item in items
            if item.get("status", "PENDING") == "PENDING"
        ]
        pending.sort(key=lambda r: r["remind_time_utc"])

        return [
            ReminderItem(
                reminder_id=item["reminder_id"],
                task=item["task"],
                remind_time_utc=item["remind_time_utc"],
                is_recurring=item.get("is_recurring", False),
                recurrence_rule=item.get("recurrence_rule")
            )
            for item in pending
        ]

    except Exception as e:
        print("[API ERROR]", e)
        raise HTTPException(500, f"Error: {str(e)}")


@app.delete("/api/delete-reminder/{reminder_id}", status_code=200)
async def delete_reminder_endpoint(
    reminder_id: str,
    current_user: User = Depends(get_current_user)
):
    try:
        await asyncio.to_thread(
            db_utils.reminders_table.delete_item,
            Key={
                "user_id": current_user.id,
                "reminder_id": reminder_id
            }
        )
        return {"message": "Deleted."}

    except Exception as e:
        print("[API ERROR]", e)
        raise HTTPException(500, f"Error: {str(e)}")

handler = Magnum(app)
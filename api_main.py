# api_main.py
import datetime
import dateparser
import pytz
import os
import requests
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import List, Optional
from jose import JWTError, jwt
from datetime import datetime, timedelta
import asyncio
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

# Import CORS middleware
from fastapi.middleware.cors import CORSMiddleware

# Import our shared logic!
import db_utils 

# --- Load Environment Variables ---
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
SECRET_KEY = os.environ.get("SECRET_KEY") # For signing cookies
if not all([DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, SECRET_KEY]):
    print("FATAL ERROR: Missing OAuth2 or SECRET_KEY environment variables!")
    exit()

# --- Auth Configuration ---
API_BASE_URL = "http://127.0.0.1:8000"
FRONTEND_URL = "http://127.0.0.1:5500" # URL of your Live Server
DISCORD_AUTH_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_URL = "https://discord.com/api/users/@me"

# JWT (Cookie) settings
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 # 7 days

# --- Pydantic Models (Data Validation) ---
class ReminderRequest(BaseModel):
    taskName: str
    taskDesc: str | None = None
    dueDate: str # e.g., "2025-11-10"
    dueTime: str # e.g., "14:30"
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
    allow_origins=[FRONTEND_URL], # Only allow your frontend
    allow_credentials=True, # This is CRITICAL for cookies
    allow_methods=["*"],    # Allow all methods (GET, POST, DELETE, etc.)
    allow_headers=["*"],    # Allow all headers
)

# --- Authentication Helpers ---

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Creates a signed JWT (the cookie value)."""
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
    FastAPI Dependency to get the current user.
    It reads the cookie, decodes it, and returns the user data.
    If the cookie is missing or invalid, it raises an exception.
    """
    token = request.cookies.get("prodibot_session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub") # 'sub' is standard for user ID
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
    auth_url = f"{DISCORD_AUTH_URL}?client_id={DISCORD_CLIENT_ID}&redirect_uri={redirect_uri}&response_type=code&scope={scope}"
    return RedirectResponse(url=auth_url)

@app.get("/api/auth/callback")
async def auth_callback(code: str):
    """
    Handles the callback from Discord. Exchanges the code for a token,
    fetches the user, creates a cookie, and redirects to the frontend.
    """
    data = {
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': f"{API_BASE_URL}/api/auth/callback"
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    r = requests.post(DISCORD_TOKEN_URL, data=data, headers=headers)
    r.raise_for_status() # Raise an error if Discord returns one
    token_data = r.json()
    
    headers = {'Authorization': f"Bearer {token_data['access_token']}"}
    user_r = requests.get(DISCORD_API_URL, headers=headers)
    user_r.raise_for_status()
    user_json = user_r.json()
    
    user = User(id=user_json['id'], username=user_json['username'])
    expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.id, "username": user.username}, 
        expires_delta=expires
    )
    
    # Redirect to the FRONTEND's URL
    response = RedirectResponse(url=FRONTEND_URL) 
    response.set_cookie(
        key="prodibot_session", 
        value=access_token, 
        httponly=True, 
        max_age=expires.total_seconds(),
        samesite="lax"
    )
    return response

@app.get("/api/me", response_model=User)
async def get_me(current_user: User = Depends(get_current_user)):
    """
    A simple endpoint the frontend can use to check if the
    user is already logged in and get their info.
    """
    return current_user

@app.get("/api/logout")
async def logout():
    """Logs the user out by clearing the cookie."""
    response = RedirectResponse(url=FRONTEND_URL)
    response.delete_cookie(key="prodibot_session")
    return response

# --- API Endpoints ---

@app.post("/api/create-reminder", status_code=201)
async def create_reminder_endpoint(
    reminder: ReminderRequest, 
    current_user: User = Depends(get_current_user)
):
    """
    Endpoint to create a new one-time reminder.
    Now uses the authenticated user's ID.
    """
    # !! IMPORTANT !!
    # Replace this with a REAL channel ID from your server
    # This is the fallback channel if DMs fail.
    channel_id = "321078607772385280" # <-- REPLACE THIS
    
    try:
        time_str = f"{reminder.dueDate} {reminder.dueTime}"
        remind_time = dateparser.parse(
            time_str, 
            settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True}
        )
        
        if not remind_time or remind_time <= datetime.now(db_utils.LOCAL_TZ):
            raise ValueError("Invalid or past time provided.")

        full_task = reminder.taskName
        if reminder.taskDesc:
            full_task += f" (Notes: {reminder.taskDesc})"
        
        # We now use the *real* user ID from the cookie!
        success = await db_utils.add_reminder_to_db(
            author_id=current_user.id,
            channel_id=channel_id,
            remind_time=remind_time,
            task=full_task,
            is_recurring=False
        )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to save reminder to database.")
        
        return {
            "message": "Reminder created successfully", 
            "task": full_task,
            "remind_time": remind_time.isoformat()
        }
    except Exception as e:
        print(f"[API ERROR] {e}")
        raise HTTPException(status_code=400, detail=f"Error processing reminder: {str(e)}")


@app.get("/api/my-reminders", response_model=List[ReminderItem])
async def get_my_reminders(current_user: User = Depends(get_current_user)):
    """
    Endpoint to fetch all PENDING reminders for the logged-in user.
    """
    try:
        # Our db_utils.reminders_table is sync, so we wrap the call
        response = await asyncio.to_thread(
            db_utils.reminders_table.query,
            KeyConditionExpression='user_id = :uid',
            ExpressionAttributeValues={':uid': current_user.id}
        )
        items = response.get('Items', [])
        
        pending_items = [item for item in items if item.get('status', 'PENDING') == 'PENDING']
        pending_items.sort(key=lambda r: r['remind_time_utc'])
        
        return [
            ReminderItem(
                reminder_id=item['reminder_id'],
                task=item['task'],
                remind_time_utc=item['remind_time_utc'],
                is_recurring=item.get('is_recurring', False),
                recurrence_rule=item.get('recurrence_rule')
            ) for item in pending_items
        ]
    except Exception as e:
        print(f"[API ERROR] {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching reminders: {str(e)}")

@app.delete("/api/delete-reminder/{reminder_id}", status_code=200)
async def delete_reminder_endpoint(
    reminder_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Endpoint to delete a PENDING reminder.
    """
    try:
        # Run the blocking boto3 call in a thread
        await asyncio.to_thread(
            db_utils.reminders_table.delete_item,
            Key={
                'user_id': current_user.id,
                'reminder_id': reminder_id
            }
        )
        return {"message": "Reminder deleted successfully"}
    except Exception as e:
        print(f"[API ERROR] Failed to delete reminder {reminder_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error deleting reminder: {e}")

# ... (We will add file upload endpoints next) ...

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_main:app", host="127.0.0.1", port=8000, reload=True)
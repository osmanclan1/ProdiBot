# db_utils.py
import datetime
import pytz
import boto3
import uuid
import dateparser
import os
import asyncio # <-- Added asyncio
from dotenv import load_dotenv

load_dotenv()

# --- Set our "home" timezone ---
LOCAL_TZ = pytz.timezone('America/Chicago')

# --- DynamoDB Setup ---
try:
    dynamodb = boto3.resource('dynamodb', region_name="us-east-1") 
    
    # Table for PENDING reminders
    DYNAMO_REMINDER_TABLE_NAME = 'ProdibotDB'
    DYNAMO_REMINDER_GSI_NAME = 'StatusandTime'
    reminders_table = dynamodb.Table(DYNAMO_REMINDER_TABLE_NAME)
    
    # Table for ACTIVE conversations and follow-up states
    DYNAMO_STATE_TABLE_NAME = 'ProdibotStateDB'
    DYNAMO_STATE_GSI_NAME = 'StatusandTime'
    state_table = dynamodb.Table(DYNAMO_STATE_TABLE_NAME)
    
    print(f"[db_utils] Successfully connected to DynamoDB tables: {DYNAMO_REMINDER_TABLE_NAME} and {DYNAMO_STATE_TABLE_NAME}")

except Exception as e:
    print(f"[db_utils] ERROR: Could not connect to DynamoDB. {e}"); exit()

# --- DB-based Memory Helpers (Now Async) ---

async def get_task_context(user_id):
    """(Async) Fetches the active task state from DynamoDB."""
    try:
        response = await asyncio.to_thread(
            state_table.get_item,
            Key={'user_id': str(user_id)}
        )
        return response.get('Item', None)
    except Exception as e:
        print(f"[db_utils] ERROR fetching context for user {user_id}: {e}")
        return None

async def add_memory_message(user_id, role, content, max_messages=8):
    """(Async) Adds a message to a user's conversation log in DynamoDB."""
    try:
        # Append the new message
        await asyncio.to_thread(
            state_table.update_item,
            Key={'user_id': str(user_id)},
            UpdateExpression="SET messages = list_append(if_not_exists(messages, :empty_list), :new_msg)",
            ExpressionAttributeValues={
                ':new_msg': [{'role': role, 'content': content}],
                ':empty_list': []
            }
        )
        
        # Now, check and trim if necessary
        context = await get_task_context(user_id) # Await the async function
        if context and len(context.get('messages', [])) > max_messages:
            # Trim the *oldest* message (the first one in the list)
            await asyncio.to_thread(
                state_table.update_item,
                Key={'user_id': str(user_id)},
                UpdateExpression="REMOVE messages[0]"
            )
        print(f"[db_utils] Added memory message for {user_id}. Role: {role}")
    except Exception as e:
        print(f"[db_utils] ERROR adding memory message for {user_id}: {e}")

async def create_task_state(user_id, task, initial_message_content):
    """(Async) Creates a new state item in DynamoDB when a reminder is sent."""
    try:
        now = datetime.datetime.now(LOCAL_TZ)
        
        # Set timers for nudging and final deletion
        next_nudge_time = now + datetime.timedelta(hours=8)
        despawn_time = now + datetime.timedelta(hours=24) 

        state_item = {
            'user_id': str(user_id),
            'task': task,
            'status': 'WAITING_FOR_REPLY', # Initial state
            'next_action_time': next_nudge_time.isoformat(), # GSI Sort Key
            'despawn_time': despawn_time.isoformat(), # The 24-hour kill switch
            'messages': [
                {'role': 'assistant', 'content': initial_message_content}
            ],
        }
        
        await asyncio.to_thread(state_table.put_item, Item=state_item)
        
        print(f"[db_utils] Created task state for {user_id}. First nudge at: {next_nudge_time.isoformat()}")
        return True
    except Exception as e:
        print(f"[db_utils] ERROR creating task state for {user_id}: {e}")
        return False

# --- Recurring Reminder Helpers (Sync - No I/O) ---

def parse_days_string(days_str):
    days_str = days_str.lower().strip()
    if days_str == 'everyday': return list(range(7))
    day_map = {'mon': 0, 'monday': 0, 'tue': 1, 'tues': 1, 'tuesday': 1, 'wed': 2, 'wednesday': 2, 'thu': 3, 'thur': 3, 'thurs': 3, 'thursday': 3, 'fri': 4, 'friday': 4, 'sat': 5, 'saturday': 5, 'sun': 6, 'sunday': 6}
    selected_days = set()
    parts = [p.strip() for p in days_str.replace('/', ',').replace(' ', ',').split(',') if p.strip()]
    for part in parts:
        if part in day_map: selected_days.add(day_map[part])
        elif 'm' in part and 'w' in part and 'f' in part: selected_days.update([0, 2, 4])
    if not selected_days:
        for char in days_str:
            if char == 'm': selected_days.add(0)
            elif char == 't' and 'h' not in days_str: selected_days.add(1)
            elif char == 'w': selected_days.add(2)
            elif char == 'h': selected_days.add(3)
            elif char == 'f': selected_days.add(4)
            elif char == 's': selected_days.add(5)
    return sorted(list(selected_days))

def calculate_next_occurrence(now_local, target_weekdays, target_time):
    today_weekday = now_local.weekday()
    if today_weekday in target_weekdays and now_local.time() < target_time:
        next_datetime_naive = datetime.datetime.combine(now_local.date(), target_time)
        return LOCAL_TZ.localize(next_datetime_naive)
    next_day_weekday = None
    for day in sorted(target_weekdays):
        if day > today_weekday: next_day_weekday = day; break
    days_to_add = 0
    if next_day_weekday is None:
        next_day_weekday = sorted(target_weekdays)[0]
        days_to_add = (next_day_weekday - today_weekday + 7) % 7
    else: days_to_add = (next_day_weekday - today_weekday)
    next_date = now_local.date() + datetime.timedelta(days=days_to_add)
    next_datetime_naive = datetime.datetime.combine(next_date, target_time)
    return LOCAL_TZ.localize(next_datetime_naive)

def calculate_next_from_rule(rule_str):
    try:
        parts = rule_str.split(':')
        if parts[0] != 'WEEKLY' or len(parts) != 3: print(f"[db_utils] Invalid rule format: {rule_str}"); return None
        target_weekdays = [int(d) for d in parts[1].split(',')]
        target_time = datetime.datetime.strptime(parts[2], '%H:%M').time()
        now_local = datetime.datetime.now(LOCAL_TZ)
        return calculate_next_occurrence(now_local, target_weekdays, target_time)
    except Exception as e: print(f"[db_utils] Error parsing rule {rule_str}: {e}"); return None

# --- Add Reminder to DB (Now Async) ---
async def add_reminder_to_db(author_id, channel_id, remind_time, task, is_recurring=False, recurrence_rule=None):
    """(Async) Adds a PENDING reminder to the database."""
    try:
        reminder_id = str(uuid.uuid4())
        remind_time_iso = remind_time.isoformat()
        item_to_put = {
            'user_id': str(author_id), 'reminder_id': reminder_id,
            'channel_id': str(channel_id), 
            'remind_time_utc': remind_time_iso, 
            'task': task, 'status': 'PENDING'
        }
        if is_recurring:
            item_to_put['is_recurring'] = True
            item_to_put['recurrence_rule'] = recurrence_rule
        
        await asyncio.to_thread(reminders_table.put_item, Item=item_to_put)
        
        print(f"[db_utils] Added {'RECURRING' if is_recurring else ''} reminder to DB. User: {author_id}, ID: {reminder_id}, Time: {remind_time_iso}")
        return True
    except Exception as e:
        print(f"[db_utils] ERROR adding reminder to DB: {e}"); return False

# --- Helper for Admin Update/Delete (Now Async) ---
async def find_reminder_by_id(short_id):
    """(Async) Scans the reminders_table for a matching short_id."""
    try:
        response = await asyncio.to_thread(
            reminders_table.scan,
            FilterExpression='begins_with(reminder_id, :sid)',
            ExpressionAttributeValues={':sid': short_id}
        )
        items = response.get('Items', [])
        if not items: return None, f"I couldn't find a reminder with an ID starting with `{short_id}`."
        if len(items) > 1: return None, f"That ID is ambiguous and matches {len(items)} reminders."
        return items[0], None
    except Exception as e:
        return None, f"An error occurred while searching: {e}"
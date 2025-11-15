import discord
from discord.ext import tasks, commands
import datetime
import asyncio
import os
import random
from openai import OpenAI
from icalendar import Calendar
import pytz # Still need this for on_message
import io
# import boto3 # No longer needed here
import uuid
import dateparser
import csv
from dotenv import load_dotenv
import json

# Basic logging setup (used throughout the file as `log`)
import logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("prodibot")

# --- NEW: Import our shared database logic ---
import db_utils

load_dotenv()

# --- Configuration ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# --- Set our "home" timezone (from db_utils) ---
LOCAL_TZ = db_utils.LOCAL_TZ

# --- DynamoDB Setup ---
# (This is now handled entirely in db_utils.py)
# We can access tables via db_utils.reminders_table and db_utils.state_table
print(f"bot.py is referencing DB tables from db_utils.")

# --- Admin User List ---
ADMIN_USER_IDS = [
    321078607772385280, # Your ID
    720677158736887808  # Added user
]

if not DISCORD_TOKEN or not OPENAI_API_KEY:
    print("="*50); print("ERROR: DISCORD_TOKEN or OPENAI_API_KEY is missing."); print("="*50); exit()
try:
    client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    print(f"Error initializing OpenAI client: {e}"); exit()

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- Bot's "Memory" ---
MAX_MEMORY_MESSAGES = 8 # Max messages to keep in conversation log
RE_REMINDER_PHRASES = [
    "Just a friendly nudge!", "How's that task coming along?",
    "Just checking in on this again.", "Hope you haven't forgotten about this!",
]

# --- NEW: DB-based Memory Helpers ---
# (All functions moved to db_utils.py)

# --- AI Functions (Merged) ---

async def get_task_status_from_ai(user_message, user_id):
    """Classifies user's reply as DONE or NOT_DONE, using DB context."""
    print(f"[Log] Classifying user message: '{user_message}'")
    
    # Use db_utils to get context
    context = db_utils.get_task_context(user_id) 
    
    instruction = context.get("task", "") if context else ""
    history = context.get("messages", []) if context else []
    history_json = json.dumps(history[-4:], ensure_ascii=False) 
    
    system_prompt = (
        "You are a simple classification bot. The user is replying about a task. "
        "Your *only* job is to determine if their message means the task is complete. "
        "- If the user says 'done', 'yep', 'finished', 'I did it', 'all set', etc., you MUST respond with the single string: [TASK_DONE] "
        "- If the user says 'not yet', 'nah', 'I don't want to', 'in a bit', or anything else, you MUST respond with the single string: [TASK_NOT_DONE] "
        "Do not say anything else. Your entire response must be *only* one of those two strings."
    )
    user_prompt = f"Task: {instruction}\n\nRecent messages (JSON list of role/content pairs):\n{history_json}\n\nUser now says: {user_message}"

    try:
        completion = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini", messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            max_tokens=5, temperature=0.0
        )
        response_text = completion.choices[0].message.content.strip()
        if response_text == "[TASK_DONE]":
            print("[Log] AI classified as: [TASK_DONE]"); return "[TASK_DONE]"
        else:
            print("[Log] AI classified as: [TASK_NOT_DONE]"); return "[TASK_NOT_DONE]"
    except Exception as e:
        print(f"[Log] ERROR calling OpenAI for classification: {e}"); return "[TASK_NOT_DONE]"

async def get_memory_chat_reply(user_id):
    """Generates a conversational reply based on the task and DB message history."""
    
    # Use db_utils to get context
    context = db_utils.get_task_context(user_id)
    
    if not context:
        print(f"[Log] ERROR: get_memory_chat_reply called for user {user_id} with no context.")
        return None

    instruction = context.get("task", "")
    messages = context.get("messages", [])
    
    if not instruction:
        print(f"[Log] ERROR: User {user_id} has context but no task instruction.")
        return None
    
    system_prompt = (
        f"You are a task manager checking on the user's progress for: {instruction}\n"
        "Your role is to:\n- Check if the task has been completed\n- Ask for status updates\n"
        "- Hold the user accountable\n- Redirect off-topic conversation back to completion status\n"
        "- Do NOT provide help, guidance, or advice - only check completion status\n"
        "Keep responses brief, direct, and focused on completion status. Be professional but firm."
    )
    
    openai_messages = [{"role": "system", "content": system_prompt}]
    openai_messages.extend([{"role": msg["role"], "content": msg["content"]} for msg in messages])

    try:
        completion = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini", messages=openai_messages, max_tokens=200, temperature=0.7
        )
        reply = completion.choices[0].message.content.strip()
        print(f"[Log] OpenAI chat reply: {reply[:50]}...")
        return reply
    except Exception as e:
        print(f"[Log] ERROR calling OpenAI for chat reply: {e}")
        return None

# --- Recurring Reminder Helpers (Unchanged) ---
# (All functions moved to db_utils.py)

# --- Add Reminder to DB (for PENDING reminders) ---
# (All functions moved to db_utils.py)

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})'); print('Bot is ready.')
    check_reminders.start(); check_followups.start()

@bot.event
# In bot.py

@bot.event
async def on_message(message):
    if message.author == bot.user: return

    # --- Attachment Handlers (Unchanged) ---
    
    # Handle calendar file upload
    if message.attachments and message.content == "!importcalendar":
        if message.author.id not in ADMIN_USER_IDS:
             await message.channel.send("Sorry, only bot admins can import a calendar."); return
        attachment = message.attachments[0]
        if attachment.filename.endswith(".ics"):
            await message.add_reaction("🔄") 
            try:
                file_content = await attachment.read()
                gcal = Calendar.from_ical(file_content)
                reminders_added = 0; reminders_past = 0
                now_local = datetime.datetime.now(LOCAL_TZ) 
                for component in gcal.walk():
                    if component.name == "VEVENT":
                        summary = str(component.get('summary'))
                        dtstart = component.get('dtstart').dt
                        if isinstance(dtstart, datetime.datetime):
                            dtstart_local = dtstart.astimezone(LOCAL_TZ) if dtstart.tzinfo else LOCAL_TZ.localize(dtstart)
                        elif isinstance(dtstart, datetime.date):
                            dtstart_local = LOCAL_TZ.localize(datetime.datetime.combine(dtstart, datetime.time(23, 59, 59)))
                        else: continue
                        remind_time = dtstart_local - datetime.timedelta(hours=24)
                        if remind_time > now_local: 
                            # Use async db_utils function
                            await db_utils.add_reminder_to_db(message.author.id, message.channel.id, remind_time, f"(From Calendar) {summary}")
                            reminders_added += 1
                        else: reminders_past += 1
                log.info(f"Calendar processed. Added {reminders_added}, Skipped {reminders_past}.")
                await message.channel.send(f"✅ Calendar imported! I added **{reminders_added}** new reminders. I skipped {reminders_past} events in the past.")
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("✅")
            except Exception as e:
                log.critical(f"FAILED to parse calendar: {e}"); await message.channel.send(f"❌ Error parsing `.ics` file. Error: {e}")
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("❌")
        else: await message.channel.send("That doesn't look like an `.ics` file. Please upload a valid calendar file.")
        return 

    # Handle CSV task file upload
    if message.attachments and message.content == "!importtasks":
        attachment = message.attachments[0]
        if attachment.filename.endswith(".csv"):
            await message.add_reaction("🔄") 
            try:
                file_content_bytes = await attachment.read()
                file_content_string = file_content_bytes.decode('utf-8')
                reader = csv.DictReader(io.StringIO(file_content_string))
                reminders_added = 0; reminders_past = 0; errors_found = 0
                now_local = datetime.datetime.now(LOCAL_TZ) 
                for row in reader:
                    try:
                        task = row['Task']; course = row.get('Course', ''); due_date = row['DueDate']; due_time = row['DueTime']
                        datetime_str = f"{due_date} {due_time}"
                        due_datetime = dateparser.parse(datetime_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
                        if not due_datetime: log.warning(f"Failed to parse date: {datetime_str}"); errors_found += 1; continue
                        remind_time = due_datetime - datetime.timedelta(hours=48)
                        full_task_str = f"({course}) {task}" if course else task
                        if remind_time > now_local: 
                            # Use async db_utils function
                            await db_utils.add_reminder_to_db(message.author.id, message.channel.id, remind_time, full_task_str)
                            reminders_added += 1
                        else: reminders_past += 1
                    except Exception as e: log.warning(f"Error processing CSV row: {e} (Row: {row})"); errors_found += 1
                log.info(f"CSV processed. Added {reminders_added}, Skipped {reminders_past}, Errors {errors_found}.")
                response_msg = f"✅ CSV imported! I added **{reminders_added}** new reminders."
                if reminders_past > 0: response_msg += f" I skipped {reminders_past} events in the past."
                if errors_found > 0: response_msg += f" I found **{errors_found} rows** I couldn't read."
                await message.channel.send(response_msg)
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("✅")
            except Exception as e:
                log.critical(f"FAILED to parse CSV: {e}"); await message.channel.send(f"❌ Error parsing `.csv` file. Error: {e}")
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("❌")
        else: await message.channel.send("That doesn't look like a `.csv` file. Please upload a valid CSV.")
        return 

    # --- MODIFIED: DB-based DM Follow-up Logic ---
    if isinstance(message.channel, discord.DMChannel) and not message.content.startswith("!"):
        user_id = message.author.id
        
        # --- FIX 1: Must 'await' async functions ---
        context = await db_utils.get_task_context(user_id)
        
        if not context:
            # User has no active task. 
            if not message.content.startswith("!"):
                await message.channel.send("I'm Prodibot! I track task completion. To start, set a reminder for yourself using `!remindme` or `!remindat` in any server channel I'm in.\n\nOnce you have an active task, I'll check on your progress here in our DMs.")
            await bot.process_commands(message) # Still process commands
            return

        # --- User HAS an active task. This is the main AI loop. ---
        
        # 1. Add user's message to memory
        log.info(f"[DM USER] {user_id}: {message.content}")
        # --- FIX 2: Must 'await' async functions ---
        await db_utils.add_memory_message(user_id, "user", message.content, MAX_MEMORY_MESSAGES)
        
        current_status = context.get('status', 'WAITING_FOR_REPLY')

        if current_status == "WAITING_FOR_REPLY":
            # Bot is waiting for a "done" or "not done" reply. Use the classifier.
            async with message.channel.typing():
                status = await get_task_status_from_ai(message.content, user_id)
            
            if status == "[TASK_DONE]":
                reply = "Great job! Way to get it done. I'll check this off the list. ✅"
                await message.channel.send(reply)
                log.info(f"[DM BOT]: {reply}")
                # Task is done, clear the state from DB
                # --- FIX 3: Must 'await' and run in thread ---
                await asyncio.to_thread(db_utils.state_table.delete_item, Key={'user_id': str(user_id)})
                log.info(f"Task complete for user {user_id}. State deleted.")
            
            else: # [TASK_NOT_DONE]
                reply = "Okay, no worries. I'll check in with you again in a bit!"
                await message.channel.send(reply)
                log.info(f"[DM BOT]: {reply}")
                # --- FIX 4: Must 'await' async functions ---
                await db_utils.add_memory_message(user_id, "assistant", reply, MAX_MEMORY_MESSAGES)
                
                # Set user to "snooze" (WAITING_TO_REMIND)
                now = datetime.datetime.now(LOCAL_TZ)
                random_minutes = random.randint(15, 180)
                next_action_time = now + datetime.timedelta(minutes=random_minutes)
                
                # The user replied, so we reset the 24-hour despawn timer
                new_despawn_time = now + datetime.timedelta(hours=24)
                
                # --- FIX 5: Must 'await' and run in thread ---
                await asyncio.to_thread(
                    db_utils.state_table.update_item,
                    Key={'user_id': str(user_id)},
                    UpdateExpression="SET #s = :s, #nat = :nat, #dt = :dt",
                    ExpressionAttributeNames={
                        '#s': 'status', 
                        '#nat': 'next_action_time',
                        '#dt': 'despawn_time'
                    },
                    ExpressionAttributeValues={
                        ':s': 'WAITING_TO_REMIND',
                        ':nat': next_action_time.isoformat(),
                        ':dt': new_despawn_time.isoformat()
                    }
                )
                log.info(f"User {user_id} not done. Next check-in at {next_action_time.isoformat()}")

        else: # (status == "WAITING_TO_REMIND")
            # User is "snoozing" but messaged the bot anyway. Use the Chatbot AI.
            async with message.channel.typing():
                bot_reply = await get_memory_chat_reply(user_id)
            
            if bot_reply:
                await message.channel.send(bot_reply)
                log.info(f"[DM BOT]: {bot_reply}")
                # --- FIX 6: Must 'await' async functions ---
                await db_utils.add_memory_message(user_id, "assistant", bot_reply, MAX_MEMORY_MESSAGES)
            else:
                await message.channel.send("Sorry, I'm having trouble processing that. I'll check in with you later about your task.")

        return # We've handled the DM, don't process commands

    # Only process commands if no attachment logic or DM logic was triggered
    await bot.process_commands(message)

@tasks.loop(seconds=15)
async def check_reminders():
    """Checks ProdibotDB for PENDING reminders to send."""
    now_local = datetime.datetime.now(LOCAL_TZ)
    now_local_iso = now_local.isoformat()
    
    try:
        response = await asyncio.to_thread(
            db_utils.reminders_table.query,
            IndexName=db_utils.DYNAMO_REMINDER_GSI_NAME,
            KeyConditionExpression='#s = :s AND remind_time_utc <= :now',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':s': 'PENDING', ':now': now_local_iso}
        )
        due_reminders = response.get('Items', [])
        
        if due_reminders:
            log.info(f"FOUND {len(due_reminders)} due reminder(s)!")

        for reminder in due_reminders:
            task = reminder['task']
            author_id = int(reminder['user_id'])
            reminder_id = reminder['reminder_id']
            channel_id = int(reminder['channel_id'])
            sent_successfully = False
            
            log.info(f"Processing reminder {reminder_id} for user {author_id}: '{task}'")

            try:
                user = await bot.fetch_user(author_id)
                if not user:
                    log.warning(f"Could not fetch user with ID {author_id}. Skipping reminder {reminder_id}.")
                    continue 

                # --- THIS IS THE FIX ---
                # We must 'await' this async function
                context = await db_utils.get_task_context(author_id)
                if context:
                # --- END OF FIX ---
                    log.warning(f"User {author_id} already has an active task. Deleting duplicate reminder {reminder_id}.")
                    sent_successfully = True 
                else:
                    # --- ATTEMPT 1: SEND DM ---
                    try:
                        reply_content = f"Hey {user.mention}, this is your reminder to: **{task}**\n\nDid you get that done?"
                        await user.send(reply_content)
                        sent_successfully = True
                        log.info(f"Successfully sent DM to user {author_id} for reminder {reminder_id}.")
                        # create_task_state is now async
                        await db_utils.create_task_state(author_id, task, reply_content)
                    
                    except discord.errors.Forbidden:
                        log.warning(f"DM FAILED for {author_id} (Forbidden). Attempting public fallback to channel {channel_id}.")
                        
                        # --- ATTEMPT 2: PUBLIC FALLBACK ---
                        try:
                            channel = await bot.fetch_channel(channel_id)
                            if not channel:
                                log.error(f"PUBLIC FALLBACK FAILED: Could not find channel with ID {channel_id} for reminder {reminder_id}.")
                                continue 
                            
                            reply_content = f"Hey {user.mention}, I tried to DM you this reminder but your DMs are off!\n\n**Task:** {task}\n\nDid you get that done?"
                            await channel.send(reply_content)
                            sent_successfully = True
                            log.info(f"Successfully sent public fallback to channel {channel_id} for user {author_id}.")
                            await db_utils.create_task_state(author_id, task, reply_content)
                        
                        except discord.errors.Forbidden:
                            log.error(f"PUBLIC FALLBACK FAILED: Bot does not have permissions in channel {channel_id} for reminder {reminder_id}.")
                        except Exception as e:
                            log.error(f"PUBLIC FALLBACK FAILED: Unknown error: {e}")
                    
                    except Exception as e:
                        log.error(f"UNKNOWN DM ERROR trying to send to user {author_id}: {e}")
                
                # --- Post-Send Cleanup ---
                if sent_successfully:
                    log.info(f"Deleting reminder {reminder_id} from database.")
                    await asyncio.to_thread(db_utils.reminders_table.delete_item, Key={'user_id': str(author_id), 'reminder_id': reminder_id})
                    
                    if reminder.get('is_recurring', False):
                        rule = reminder.get('recurrence_rule')
                        if rule and rule != 'NONE':
                            log.info(f"Rescheduling recurring reminder {reminder_id} with rule: {rule}")
                            try:
                                next_remind_time = db_utils.calculate_next_from_rule(rule)
                                if next_remind_time:
                                    await db_utils.add_reminder_to_db(
                                        author_id, channel_id, 
                                        next_remind_time, task, 
                                        is_recurring=True, recurrence_rule=rule
                                    )
                                    log.info(f"Successfully rescheduled {reminder_id}. Next at: {next_remind_time.isoformat()}")
                            except Exception as e:
                                log.critical(f"CRITICAL ERROR rescheduling reminder {reminder_id}: {e}")
                else:
                    log.warning(f"Failed to send reminder {reminder_id} for user {author_id}. Will retry next loop.")

            except Exception as e:
                log.critical(f"CRITICAL error in check_reminders sub-loop for reminder {reminder_id}: {e}")
                try:
                    await asyncio.to_thread(db_utils.reminders_table.delete_item, Key={'user_id': reminder['user_id'], 'reminder_id': reminder['reminder_id']})
                    log.error(f"Deleted erroring reminder {reminder['erroring_reminder']} to prevent loop.")
                except Exception as del_e:
                    log.critical(f"FAILED to delete erroring reminder: {del_e}")

    except Exception as e:
        log.critical(f"An unexpected error occurred querying DynamoDB (Reminders): {e}")
@check_reminders.before_loop
async def before_check_reminders():
    await bot.wait_until_ready()
    print("Reminder check loop is starting.")

@tasks.loop(seconds=30)
async def check_followups():
    """
    Checks ProdibotStateDB for all state-based actions:
    1. 'WAITING_TO_REMIND': User's "snooze" is over. Nudge them and move to WAITING_FOR_REPLY.
    2. 'WAITING_FOR_REPLY': User has been "ghosting." Nudge them and check for final despawn.
    """
    now = datetime.datetime.now(LOCAL_TZ)
    now_iso = now.isoformat()
    
    try:
        # --- Action 1: Handle "Snoozed" users ---
        # Use db_utils table object
        response_snooze = db_utils.state_table.query(
            IndexName=db_utils.DYNAMO_STATE_GSI_NAME,
            KeyConditionExpression='#s = :s AND #nat <= :now', # Use placeholder #nat
            ExpressionAttributeNames={
                '#s': 'status',
                '#nat': 'next_action_time' # Map #nat to the attribute name
            },
            ExpressionAttributeValues={
                ':s': 'WAITING_TO_REMIND',
                ':now': now_iso
            }
        )
        
        for item in response_snooze.get('Items', []):
            user_id = int(item['user_id'])
            task = item['task']
            print(f"[Log] Snooze over for user {user_id}. Nudging for task: {task}")
            try:
                user = await bot.fetch_user(user_id)
                if user:
                    phrase = random.choice(RE_REMINDER_PHRASES)
                    reply = f"Hey! Just checking back in on that task: **{task}**\n\n{phrase}\n\nDid you get that done?"
                    await user.send(reply)
                    
                    # Use db_utils function
                    db_utils.add_memory_message(user_id, "assistant", reply, MAX_MEMORY_MESSAGES)
                    
                    # Move user back to WAITING_FOR_REPLY and set new timers
                    next_nudge_time = now + datetime.timedelta(hours=8)
                    new_despawn_time = now + datetime.timedelta(hours=24) # Reset 24h clock

                    # Use db_utils table object
                    db_utils.state_table.update_item(
                        Key={'user_id': str(user_id)},
                        UpdateExpression="SET #s = :s, #nat = :nat, #dt = :dt",
                        ExpressionAttributeNames={
                            '#s': 'status',
                            '#nat': 'next_action_time',
                            '#dt': 'despawn_time'
                        },
                        ExpressionAttributeValues={
                            ':s': 'WAITING_FOR_REPLY',
                            ':nat': next_nudge_time.isoformat(),
                            ':dt': new_despawn_time.isoformat()
                        }
                    )
            except Exception as e:
                print(f"[Log] Error processing snooze for {user_id}: {e}")
                # Safer to just delete the state if we can't DM them
                # Use db_utils table object
                db_utils.state_table.delete_item(Key={'user_id': str(user_id)})

        # --- Action 2: Handle "Ghosting" users (and final cleanup) ---
        # Use db_utils table object
        response_ghost = db_utils.state_table.query(
            IndexName=db_utils.DYNAMO_STATE_GSI_NAME,
            KeyConditionExpression='#s = :s AND #nat <= :now', # Use placeholder #nat
            ExpressionAttributeNames={
                '#s': 'status',
                '#nat': 'next_action_time' # Map #nat to the attribute name
            },
            ExpressionAttributeValues={
                ':s': 'WAITING_FOR_REPLY',
                ':now': now_iso
            }
        )

        for item in response_ghost.get('Items', []):
            user_id = int(item['user_id'])
            task = item['task']
            despawn_time = datetime.datetime.fromisoformat(item['despawn_time'])

            # --- Sub-Action 2a: Check for FINAL deletion ---
            if despawn_time <= now:
                print(f"[Log] Despawn time reached for user {user_id} on task: {task}. Deleting state.")
                try:
                    user = await bot.fetch_user(user_id)
                    if user:
                        await user.send(f"Hey, I haven't heard back from you about: **{task}**.\n\nI'm going to close this reminder for now. You can always set a new one if you still need to do it!")
                    
                    # Delete the state
                    # Use db_utils table object
                    db_utils.state_table.delete_item(Key={'user_id': str(user_id)})
                
                except Exception as e:
                    print(f"[Log] Error sending final despawn message to {user_id}: {e}")
                    # Still delete the state even if DM fails
                    # Use db_utils table object
                    db_utils.state_table.delete_item(Key={'user_id': str(user_id)})
                
                continue # Skip to the next user

            # --- Sub-Action 2b: Nudge the user (they haven't despawned yet) ---
            print(f"[Log] Ghost-nudge for user {user_id} for task: {task}")
            try:
                user = await bot.fetch_user(user_id)
                if user:
                    phrase = random.choice(RE_REMINDER_PHRASES)
                    reply = f"Hey! Just checking in on that task: **{task}**\n\n{phrase}\n\nDid you get that done?"
                    await user.send(reply)
                    
                    # Use db_utils function
                    db_utils.add_memory_message(user_id, "assistant", reply, MAX_MEMORY_MESSAGES)
                    
                    # Re-arm the *next* nudge timer (e.g., in another 8 hours)
                    # We do NOT reset the despawn_time.
                    next_nudge_time = now + datetime.timedelta(hours=8)

                    # Use db_utils table object
                    db_utils.state_table.update_item(
                        Key={'user_id': str(user_id)},
                        UpdateExpression="SET #nat = :nat",
                        ExpressionAttributeNames={'#nat': 'next_action_time'},
                        ExpressionAttributeValues={':nat': next_nudge_time.isoformat()}
                    )
            except Exception as e:
                print(f"[Log] Error ghost-nudging {user_id}: {e}")
                # If we can't DM them, just delete the state
                # Use db_utils table object
                db_utils.state_table.delete_item(Key={'user_id': str(user_id)})

    except Exception as e:
        print(f"[Log] An unexpected error occurred querying DynamoDB (State): {e}")


@check_followups.before_loop
async def before_check_followups():
    await bot.wait_until_ready()
    print("Follow-up check loop is starting.")

# --- Bot Commands ---

# Helper decorator for admin commands
def admin_only():
    def predicate(ctx):
        return ctx.author.id in ADMIN_USER_IDS
    return commands.check(predicate)

@bot.command(name='listreminders', help='(Admin only) Lists all upcoming reminders from the database.')
@admin_only()
async def listreminders(ctx):
    try:
        # Use db_utils table object
        response = db_utils.reminders_table.query(
            KeyConditionExpression='user_id = :uid',
            ExpressionAttributeValues={':uid': str(ctx.author.id)}
        )
        items = response.get('Items', [])
        if not items:
            await ctx.send("You have no reminders assigned to *you* in the database!"); return
        
        items.sort(key=lambda r: r['remind_time_utc'])
        response_message = f"**You have {len(items)} upcoming reminders in the DB:**\n\n"
        for i, item in enumerate(items):
            task = item['task']; task = (task[:50] + "...") if len(task) > 50 else task
            remind_time_obj = datetime.datetime.fromisoformat(item['remind_time_utc'])
            time_str = f"<t:{int(remind_time_obj.timestamp())}:f>"
            reminder_id_short = item['reminder_id'].split('-')[0]
            recur_str = " (🔄 Recurring)" if item.get('is_recurring', False) else ""
            
            response_message += f"**{i+1}.** {task}{recur_str}\n    *Due: {time_str}*\n    *ID: `{reminder_id_short}`*\n"
            if len(response_message) > 1800:
                await ctx.send(response_message); response_message = ""
        if response_message: await ctx.send(response_message)
    except Exception as e: await ctx.send(f"An error occurred while fetching reminders: {e}")

@bot.command(name='importcalendar', help='(Admin only) Upload your .ics calendar file to import all deadlines.')
@admin_only()
async def importcalendar(ctx):
    await ctx.send(f"Okay, {ctx.author.mention}! Please **drag and drop** your `.ics` file and **type `!importcalendar` in the comment**.")

@bot.command(name='importtasks', help='Upload your .csv task file to import all deadlines.')
async def importtasks(ctx):
    await ctx.send(f"Okay, {ctx.author.mention}! Please **drag and drop** your `.csv` file and **type `!importtasks` in the comment**.\n\nMake sure your file has columns: `Task`, `DueDate`, `DueTime`, and (optionally) `Course`.")

@bot.command(name='remindme', help='Sets a reminder. Usage: !remindme <minutes> <task>')
async def remindme(ctx, minutes: int, *, task: str):
    try:
        if minutes <= 0: await ctx.send("Please provide a positive number of minutes!"); return
        now = datetime.datetime.now(LOCAL_TZ) 
        remind_time = now + datetime.timedelta(minutes=minutes)
        # Use db_utils function
        if await db_utils.add_reminder_to_db(ctx.author.id, ctx.channel.id, remind_time, task):
            await ctx.send(f"Okay, {ctx.author.mention}! I'll remind you to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
        else: await ctx.send("Sorry, I had an error saving that reminder to the database.")
    except Exception as e: await ctx.send(f"An error occurred: {e}")

@bot.command(name='remindat', help='Sets a reminder. Usage: !remindat "<time>" <task>')
async def remindat(ctx, time_str: str, *, task: str):
    try:
        remind_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
        if not remind_time: await ctx.send(f'Sorry, I couldn\'t understand the time "{time_str}".'); return
        if remind_time <= datetime.datetime.now(LOCAL_TZ): await ctx.send(f"That time is in the past! Please provide a future time."); return
        # Use db_utils function
        if await db_utils.add_reminder_to_db(ctx.author.id, ctx.channel.id, remind_time, task):
             await ctx.send(f"Got it, {ctx.author.mention}! I'll remind you to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
        else: await ctx.send("Sorry, I had an error saving that reminder to the database.")
    except Exception as e: await ctx.send(f"An error occurred: {e}")

@bot.command(name='setreminder', help='(Admin only) Sets a reminder for users. Usage: !setreminder <@user1 ...> "<time>" <task>')
@admin_only()
async def setreminder(ctx, users: commands.Greedy[discord.User], time_str: str, *, task: str):
    if not users: await ctx.send("You must specify at least one user!"); return
    try:
        remind_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
        if not remind_time: await ctx.send(f'Sorry, I couldn\'t understand the time "{time_str}".'); return
        if remind_time <= datetime.datetime.now(LOCAL_TZ): await ctx.send(f"That time is in the past!"); return
        success_users = []; fail_users = []
        for user in users:
            # Use db_utils function
            if await db_utils.add_reminder_to_db(user.id, ctx.channel.id, remind_time, task): success_users.append(user.mention)
            else: fail_users.append(user.mention)
        response_msg = ""
        if success_users: response_msg += f"✅ Got it! I'll remind {', '.join(success_users)} to **{task}** at <t:{int(remind_time.timestamp())}:f>.\n"
        if fail_users: response_msg += f"❌ I failed to set a reminder for {', '.join(fail_users)}."
        await ctx.send(response_msg)
    except Exception as e: await ctx.send(f"An error occurred: {e}")

@bot.command(name='routinereminder', help='(Admin only) Sets a recurring reminder. Usage: !routinereminder <@user1 ...> "<days>" "<time>" <task>')
@admin_only()
async def routinereminder(ctx, users: commands.Greedy[discord.User], days_str: str, time_str: str, *, task: str):
    if not users: await ctx.send("You must specify at least one user!"); return
    try:
        # Use db_utils function
        target_weekdays = db_utils.parse_days_string(days_str)
        if not target_weekdays: await ctx.send(f"I couldn't understand the days: \"{days_str}\"."); return
        parsed_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
        if not parsed_time: await ctx.send(f"I couldn't understand the time: \"{time_str}\"."); return
        target_time = parsed_time.time()
        rule_str = f"WEEKLY:{','.join(map(str, target_weekdays))}:{target_time.strftime('%H:%M')}"
        # Use db_utils function
        first_occurrence_time = db_utils.calculate_next_occurrence(datetime.datetime.now(LOCAL_TZ), target_weekdays, target_time)
        success_users = []; fail_users = []
        for user in users:
            # Use db_utils function
            if await db_utils.add_reminder_to_db(user.id, ctx.channel.id, first_occurrence_time, task, is_recurring=True, recurrence_rule=rule_str):
                success_users.append(user.mention)
            else: fail_users.append(user.mention)
        day_names = ['Mon', 'Tues', 'Wed', 'Thurs', 'Fri', 'Sat', 'Sun']
        human_days = ", ".join([day_names[d] for d in target_weekdays]) if len(target_weekdays) < 7 else "everyday"
        response_msg = ""
        if success_users:
            response_msg += f"✅ Set recurring reminder for {', '.join(success_users)}: **{task}**\n"
            response_msg += f"   *When:* {human_days} at {target_time.strftime('%I:%M %p %Z')}\n"
            response_msg += f"   *First one is:* <t:{int(first_occurrence_time.timestamp())}:f>"
        if fail_users: response_msg += f"\n❌ I failed to set the recurring reminder for {', '.join(fail_users)}."
        await ctx.send(response_msg)
    except Exception as e: await ctx.send(f"An error occurred: {e}")

# --- Helper for Admin Update/Delete commands ---
# (This is now in db_utils.py)

@bot.command(name='deletereminder', help='(Admin only) Deletes a reminder. Usage: !deletereminder <id>')
@admin_only()
async def deletereminder(ctx, short_id: str):
    # Use db_utils function
    item, error = db_utils.find_reminder_by_id(short_id)
    if error: await ctx.send(error); return
    try:
        # Use db_utils table object
        db_utils.reminders_table.delete_item(Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']})
        await ctx.send(f"✅ Successfully deleted reminder: **{item['task']}** (for user <@{item['user_id']}>)")
    except Exception as e: await ctx.send(f"An error occurred while deleting: {e}")

@bot.command(name='updatetask', help='(Admin only) Updates a task. Usage: !updatetask <id> <new task>')
@admin_only()
async def updatetask(ctx, short_id: str, *, new_task: str):
    # Use db_utils function
    item, error = db_utils.find_reminder_by_id(short_id)
    if error: await ctx.send(error); return
    try:
        # Use db_utils table object
        db_utils.reminders_table.update_item(
            Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']},
            UpdateExpression="set task = :t", ExpressionAttributeValues={':t': new_task}
        )
        await ctx.send(f"✅ Task updated for `{short_id}`!\n**Old:** {item['task']}\n**New:** {new_task}")
    except Exception as e: await ctx.send(f"An error occurred while updating: {e}")

@bot.command(name='updatetime', help='(Admin only) Updates time. Usage: !updatetime <id> "<time>"')
@admin_only()
async def updatetime(ctx, short_id: str, time_str: str):
    # Use db_utils function
    item, error = db_utils.find_reminder_by_id(short_id)
    if error: await ctx.send(error); return
    try:
        new_remind_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
        if not new_remind_time: await ctx.send(f'Sorry, I couldn\'t understand the time "{time_str}".'); return
        if new_remind_time <= datetime.datetime.now(LOCAL_TZ): await ctx.send(f"That time is in the past!"); return

        new_remind_time_iso = new_remind_time.isoformat()
        
        # Update logic: Delete and replace to ensure GSI is updated
        # Use db_utils table object
        db_utils.reminders_table.delete_item(Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']})
        item['remind_time_utc'] = new_remind_time_iso 
        if 'is_recurring' in item:
            item['is_recurring'] = False; item['recurrence_rule'] = 'NONE'
        # Use db_utils table object
        db_utils.reminders_table.put_item(Item=item)
        
        new_time_discord = f"<t:{int(new_remind_time.timestamp())}:f>"
        await ctx.send(f"✅ Time updated for **{item['task']}**!\n**New Time:** {new_time_discord}\n*(Note: This action made the reminder non-recurring.)*")
    except Exception as e: await ctx.send(f"An error occurred while updating: {e}")

# --- NEW: Admin Memory Commands (DB-based) ---

@bot.command(name='memdump', help='(Admin only) Shows active task state for a user. Usage: !memdump <@user>')
@admin_only()
async def memdump(ctx, user: discord.User):
    if not user:
        await ctx.send("You must mention a user. Usage: `!memdump @User`")
        return
        
    # Use db_utils function
    context = db_utils.get_task_context(user.id)
    if not context:
        await ctx.send(f"No active task state found for {user.mention}."); return
    
    # Pretty-print the context
    response_msg = (
        f"**Active Task State for {user.mention}**\n\n"
        f"**Task:** `{context.get('task')}`\n"
        f"**Status:** `{context.get('status')}`\n"
        f"**Next Nudge:** `{context.get('next_action_time')}`\n"
        f"**Despawn Time:** `{context.get('despawn_time')}`\n\n"
        f"**History:**\n"
    )
    
    messages = context.get('messages', [])
    if not messages:
        response_msg += "  (No messages in history)"
    
    for msg in messages:
        line = f"  - **{msg.get('role', 'unknown')}:** {msg.get('content', '')[:100]}\n"
        if len(response_msg) + len(line) > 1900:
            response_msg += "... (message history truncated)"
            break
        response_msg += line
            
    await ctx.send(response_msg)

@bot.command(name='memclear', help='(Admin only) Clears active task state for a user. Usage: !memclear <@user>')
@admin_only()
async def memclear(ctx, user: discord.User):
    if not user:
        await ctx.send("You must mention a user. Usage: `!memclear @User`")
        return

    # Use db_utils function
    context = db_utils.get_task_context(user.id)
    if not context:
        await ctx.send(f"No active task state found for {user.mention}."); return
    
    try:
        # Use db_utils table object
        db_utils.state_table.delete_item(Key={'user_id': str(user.id)})
        await ctx.send(f"✅ Successfully cleared the active task state for {user.mention}.")
        print(f"[Log] Admin {ctx.author.id} cleared state for {user.id}")
    except Exception as e:
        await ctx.send(f"An error occurred while clearing state: {e}")
        print(f"[Log] ERROR clearing state for {user.id}: {e}")

# --- Run the Bot ---
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("="*50); print("ERROR: Invalid DISCORD_TOKEN."); print("="*50)
    except Exception as e:
        print(f"An error occurred while running the bot: {e}")
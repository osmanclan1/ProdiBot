import discord
from discord.ext import tasks, commands
import datetime
import asyncio
import os
import random
from openai import OpenAI
from icalendar import Calendar
import pytz
import io
import boto3
import uuid
import shlex
import dateparser
import csv 
import io  
from dotenv import load_dotenv
load_dotenv()
# --- Configuration ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# --- Set our "home" timezone ---
LOCAL_TZ = pytz.timezone('America/Chicago')

# --- DynamoDB Setup ---
try:
    dynamodb = boto3.resource('dynamodb', region_name="us-east-1") 
    DYNAMO_TABLE_NAME = 'ProdibotDB'
    DYNAMO_GSI_NAME = 'StatusandTime'
    db_table = dynamodb.Table(DYNAMO_TABLE_NAME)
    print(f"Successfully connected to DynamoDB table: {DYNAMO_TABLE_NAME}")
except Exception as e:
    print(f"ERROR: Could not connect to DynamoDB. {e}"); exit()

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
active_followups = {}
RE_REMINDER_PHRASES = [
    "Just a friendly nudge!", "How's that task coming along?",
    "Just checking in on this again.", "Hope you haven't forgotten about this!",
]

# --- AI Function ---
async def get_task_status_from_ai(user_message):
    print(f"[Log] Classifying user message: '{user_message}'")
    system_prompt = (
        "You are a simple classification bot. The user is replying about a task. "
        "Your *only* job is to determine if their message means the task is complete. "
        "- If the user says 'done', 'yep', 'finished', 'I did it', 'all set', etc., you MUST respond with the single string: [TASK_DONE] "
        "- If the user says 'not yet', 'nah', 'I don't want to', 'in a bit', or anything else, you MUST respond with the single string: [TASK_NOT_DONE] "
        "Do not say anything else. Your entire response must be *only* one of those two strings."
    )
    try:
        completion = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini", messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}],
            max_tokens=5, temperature=0.0
        )
        response_text = completion.choices[0].message.content.strip()
        if response_text == "[TASK_DONE]":
            print("[Log] AI classified as: [TASK_DONE]"); return "[TASK_DONE]"
        else:
            print("[Log] AI classified as: [TASK_NOT_DONE]"); return "[TASK_NOT_DONE]"
    except Exception as e:
        print(f"[Log] ERROR calling OpenAI for classification: {e}"); return "[TASK_NOT_DONE]"

# --- NEW: Helper functions for recurring reminders ---

def parse_days_string(days_str):
    """Parses a day string (e.g., "Mon,Wed,Fri", "everyday") into a list of weekday indices."""
    days_str = days_str.lower().strip()
    if days_str == 'everyday':
        return list(range(7)) # [0, 1, 2, 3, 4, 5, 6]
    
    day_map = {
        'mon': 0, 'monday': 0,
        'tue': 1, 'tues': 1, 'tuesday': 1,
        'wed': 2, 'wednesday': 2,
        'thu': 3, 'thur': 3, 'thurs': 3, 'thursday': 3,
        'fri': 4, 'friday': 4,
        'sat': 5, 'saturday': 5,
        'sun': 6, 'sunday': 6
    }
    
    selected_days = set()
    # Split by comma, slash, or space
    parts = [p.strip() for p in days_str.replace('/', ',').replace(' ', ',').split(',') if p.strip()]
    
    for part in parts:
        if part in day_map:
            selected_days.add(day_map[part])
        # Check for "MWF" style
        elif 'm' in part and 'w' in part and 'f' in part:
            selected_days.update([0, 2, 4])

    # Handle single-letter days if no other matches found
    if not selected_days:
        for char in days_str:
            if char == 'm': selected_days.add(0)
            elif char == 't' and 'h' not in days_str: selected_days.add(1) # Tuesday
            elif char == 'w': selected_days.add(2)
            elif char == 'h': selected_days.add(3) # Thursday
            elif char == 'f': selected_days.add(4)
            elif char == 's': selected_days.add(5) # Saturday
            # Sunday 'u' is less common, 'sun' is better
    
    return sorted(list(selected_days))

def calculate_next_occurrence(now_local, target_weekdays, target_time):
    """Calculates the first occurrence of a reminder from 'now'."""
    today_weekday = now_local.weekday() # Mon=0, Sun=6
    
    # Check if today is a target day and time hasn't passed
    if today_weekday in target_weekdays and now_local.time() < target_time:
        next_datetime_naive = datetime.datetime.combine(now_local.date(), target_time)
        return LOCAL_TZ.localize(next_datetime_naive)
    
    # If time has passed today, or today isn't a target day, find the next one
    next_day_weekday = None
    for day in sorted(target_weekdays):
        if day > today_weekday:
            next_day_weekday = day
            break
    
    days_to_add = 0
    if next_day_weekday is None:
        # Wrap to the first target day next week
        next_day_weekday = sorted(target_weekdays)[0]
        days_to_add = (next_day_weekday - today_weekday + 7) % 7
    else:
        # It's a day later this week
        days_to_add = (next_day_weekday - today_weekday)
        
    next_date = now_local.date() + datetime.timedelta(days=days_to_add)
    next_datetime_naive = datetime.datetime.combine(next_date, target_time)
    
    return LOCAL_TZ.localize(next_datetime_naive)

def calculate_next_from_rule(rule_str):
    """Calculates the next reminder time based on a rule string, starting from now."""
    try:
        # Rule: "WEEKLY:0,2,4:10:00"
        parts = rule_str.split(':')
        if parts[0] != 'WEEKLY' or len(parts) != 3:
            print(f"[Log] Invalid rule format: {rule_str}"); return None
        
        day_indices_str = parts[1]
        time_str = parts[2]
        
        target_weekdays = [int(d) for d in day_indices_str.split(',')]
        target_time = datetime.datetime.strptime(time_str, '%H:%M').time()
        
        now_local = datetime.datetime.now(LOCAL_TZ)
        
        # Pass 'now_local' to calculate *from this moment*
        return calculate_next_occurrence(now_local, target_weekdays, target_time)
    except Exception as e:
        print(f"[Log] Error parsing rule {rule_str}: {e}"); return None

# --- MODIFIED: Helper function to add reminders to DB ---
async def add_reminder_to_db(author_id, channel_id, remind_time, task, is_recurring=False, recurrence_rule=None):
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
        
        db_table.put_item(Item=item_to_put)
        print(f"[Log] Added {'RECURRING' if is_recurring else ''} reminder to DB. User: {author_id}, ID: {reminder_id}, Time: {remind_time_iso}")
        return True
    except Exception as e:
        print(f"[Log] ERROR adding reminder to DB: {e}"); return False

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})'); print('Bot is ready.')
    check_reminders.start(); check_followups.start()

@bot.event
async def on_message(message):
    if message.author == bot.user: return

    # --- Handle calendar file upload ---
    if message.attachments and message.content == "!importcalendar":
        if message.author.id not in ADMIN_USER_IDS: # <-- UPDATED CHECK
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
                            if dtstart.tzinfo: 
                                dtstart_local = dtstart.astimezone(LOCAL_TZ)
                            else: 
                                dtstart_local = LOCAL_TZ.localize(dtstart)
                        elif isinstance(dtstart, datetime.date):
                            dtstart_local = LOCAL_TZ.localize(datetime.datetime.combine(dtstart, datetime.time(23, 59, 59)))
                        else: continue
                        
                        remind_time = dtstart_local - datetime.timedelta(hours=24)
                        if remind_time > now_local: 
                            # Use default args for non-recurring
                            await add_reminder_to_db(message.author.id, message.channel.id, remind_time, f"(From Calendar) {summary}")
                            reminders_added += 1
                        else: reminders_past += 1
                print(f"[Log] Calendar processed. Added {reminders_added}, Skipped {reminders_past}.")
                await message.channel.send(f"✅ Calendar imported! I added **{reminders_added}** new reminders. I skipped {reminders_past} events in the past.")
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("✅")
            except Exception as e:
                print(f"[Log] FAILED to parse calendar: {e}"); await message.channel.send(f"❌ Error parsing `.ics` file. Error: {e}")
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("❌")
        else: await message.channel.send("That doesn't look like an `.ics` file. Please upload a valid calendar file.")
        return 

    # --- Handle CSV task file upload ---
    if message.attachments and message.content == "!importtasks":
        # # <-- To make this admin-only, uncomment the 3 lines below
        # if message.author.id not in ADMIN_USER_IDS: 
        #     await message.channel.send("Sorry, only bot admins can import tasks."); return
        
        attachment = message.attachments[0]
        if attachment.filename.endswith(".csv"):
            await message.add_reaction("🔄") 
            try:
                file_content_bytes = await attachment.read()
                file_content_string = file_content_bytes.decode('utf-8')
                csv_file = io.StringIO(file_content_string)
                
                reader = csv.DictReader(csv_file)
                
                reminders_added = 0
                reminders_past = 0
                errors_found = 0
                now_local = datetime.datetime.now(LOCAL_TZ) 

                for row in reader:
                    try:
                        task = row['Task']
                        course = row.get('Course', '') 
                        due_date = row['DueDate']
                        due_time = row['DueTime']
                        
                        datetime_str = f"{due_date} {due_time}"
                        
                        due_datetime = dateparser.parse(datetime_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})

                        if not due_datetime:
                            print(f"[Log] Failed to parse date: {datetime_str}"); errors_found += 1; continue
                            
                        remind_time = due_datetime - datetime.timedelta(hours=48)
                        
                        full_task_str = f"({course}) {task}" if course else task

                        if remind_time > now_local: 
                            # Use default args for non-recurring
                            await add_reminder_to_db(message.author.id, message.channel.id, remind_time, full_task_str)
                            reminders_added += 1
                        else:
                            reminders_past += 1
                    except KeyError as e:
                        print(f"[Log] CSV missing required column: {e} (Row: {row})"); errors_found += 1
                    except Exception as e:
                        print(f"[Log] Error processing CSV row: {e} (Row: {row})"); errors_found += 1

                print(f"[Log] CSV processed. Added {reminders_added}, Skipped {reminders_past}, Errors {errors_found}.")
                
                response_msg = f"✅ CSV imported! I added **{reminders_added}** new reminders."
                if reminders_past > 0:
                    response_msg += f" I skipped {reminders_past} events in the past."
                if errors_found > 0:
                     response_msg += f" I found **{errors_found} rows** I couldn't read (see my logs for details)."
                
                await message.channel.send(response_msg)
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("✅")
                
            except Exception as e:
                print(f"[Log] FAILED to parse CSV: {e}"); await message.channel.send(f"❌ Error parsing `.csv` file. Error: {e}")
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("❌")
        else:
            await message.channel.send("That doesn't look like a `.csv` file. Please upload a valid CSV.")
        return 

    # --- Existing DM Follow-up Logic ---
    if isinstance(message.channel, discord.DMChannel) and message.author.id in active_followups:
        user_id = message.author.id
        follow_up_data = active_followups[user_id]
        if follow_up_data["status"] == "WAITING_FOR_REPLY":
            user_reply = message.content
            async with message.channel.typing(): status = await get_task_status_from_ai(user_reply)
            if status == "[TASK_DONE]":
                await message.channel.send("Great job! Way to get it done. I'll check this off the list. ✅")
                del active_followups[user_id]
                print(f"[Log] Task complete for user {user_id}. Removed from active list.")
            else:
                await message.channel.send("Okay, no worries. I'll check in with you again in a bit!")
                
                random_minutes = random.randint(15, 180)
                
                next_remind_time = datetime.datetime.now(LOCAL_TZ) + datetime.timedelta(minutes=random_minutes)
                follow_up_data["status"] = "WAITING_TO_REMIND"; follow_up_data["next_remind_time"] = next_remind_time
                print(f"[Log] User {user_id} not done. Next check-in at {next_remind_time.isoformat()}")
        
        return

    # Only process commands if no attachment logic was triggered
    await bot.process_commands(message)

# --- Background Loops ---
@tasks.loop(seconds=15)
async def check_reminders():
    now_local_iso = datetime.datetime.now(LOCAL_TZ).isoformat()
    
    try:
        response = db_table.query(
            IndexName=DYNAMO_GSI_NAME,
            KeyConditionExpression='#s = :s AND remind_time_utc <= :now', 
            ExpressionAttributeNames={
                '#s': 'status'
            },
            ExpressionAttributeValues={
                ':s': 'PENDING',
                ':now': now_local_iso 
            }
        )
        due_reminders = response.get('Items', [])
        
        for reminder in due_reminders:
            task = reminder['task']
            author_id = int(reminder['user_id'])
            reminder_id = reminder['reminder_id']
            channel_id = int(reminder['channel_id'])
            sent_successfully = False
            
            print(f"\n[Log] Processing DB reminder for task: \"{task}\"")

            try:
                user = await bot.fetch_user(author_id)
                if user:
                    if author_id in active_followups and active_followups[author_id]['task'] == task:
                        print(f"[Log] User {author_id} already in active follow-up. Deleting duplicate DB entry.")
                        sent_successfully = True 
                    else:
                        try:
                            await user.send(f"Hey {user.mention}, this is your reminder to: **{task}**\n\nDid you get that done?")
                            sent_successfully = True
                            print(f"[Log] Sent reminder via DM to {author_id}")
                        
                        except discord.errors.Forbidden:
                            print(f"[Log] DM failed for {author_id}. User has DMs blocked. Attempting public fallback.")
                            try:
                                channel = await bot.fetch_channel(channel_id)
                                if channel:
                                    await channel.send(f"Hey {user.mention}, I tried to DM you this reminder but your DMs are off!\n\n**Task:** {task}\n\nDid you get that done?")
                                    sent_successfully = True
                                    print(f"[Log] Sent reminder publicly to channel {channel_id}")
                                else:
                                    print(f"[Log] Public fallback failed. Can't find channel {channel_id}.")
                            except (discord.errors.Forbidden, discord.errors.NotFound):
                                print(f"[Log] Public fallback failed. Bot can't see or post in channel {channel_id}.")
                            except Exception as e:
                                print(f"[Log] Unknown error in public fallback: {e}")
                        
                        except Exception as e:
                            print(f"[Log] Unknown error trying to DM user: {e}")
                
                if sent_successfully:
                    if not (author_id in active_followups and active_followups[author_id]['task'] == task):
                        active_followups[author_id] = {
                            "task": task, "status": "WAITING_FOR_REPLY", "next_remind_time": None
                        }
                        print(f"[Log] Added user {author_id} to active follow-up list.")
                    
                    # Delete the reminder *after* it's been sent
                    db_table.delete_item(Key={'user_id': str(author_id), 'reminder_id': reminder_id})
                    print(f"[Log] Deleted reminder {reminder_id} from DB.")

                    # --- NEW: Check for recurrence ---
                    if reminder.get('is_recurring', False):
                        rule = reminder.get('recurrence_rule')
                        if not rule or rule == 'NONE':
                            print(f"[Log] Reminder {reminder_id} was marked recurring but had no rule. Stopping.")
                            continue # Skip to next reminder in loop
                        
                        print(f"[Log] Rescheduling recurring reminder {reminder_id} with rule: {rule}")
                        try:
                            # Calculate next time FROM NOW
                            next_remind_time = calculate_next_from_rule(rule)
                            
                            if next_remind_time:
                                # Re-add the reminder to the DB with the new time
                                await add_reminder_to_db(
                                    author_id, channel_id, 
                                    next_remind_time, task, 
                                    is_recurring=True, recurrence_rule=rule
                                )
                                print(f"[Log] Successfully rescheduled {reminder_id}. Next at: {next_remind_time.isoformat()}")
                            else:
                                print(f"[Log] ERROR: Could not calculate next time for rule {rule}. Stopping recurrence.")
                        except Exception as e:
                            print(f"[Log] CRITICAL ERROR rescheduling reminder {reminder_id}: {e}")
                    # --- End of recurrence check ---

                else:
                    print(f"[Log] Failed to send reminder {reminder_id} by any method. Will retry next loop.")

            except Exception as e:
                print(f"[Log] CRITICAL error in check_reminders sub-loop: {e}")
                try:
                    db_table.delete_item(Key={'user_id': reminder['user_id'], 'reminder_id': reminder['reminder_id']})
                    print(f"[Log] Deleted erroring reminder {reminder['reminder_id']} to prevent loop.")
                except Exception as del_e:
                    print(f"[Log] FAILED to delete erroring reminder: {del_e}")

    except Exception as e:
        print(f"[Log] An unexpected error occurred querying DynamoDB: {e}")


@check_reminders.before_loop
async def before_check_reminders():
    await bot.wait_until_ready()
    print("Reminder check loop is starting.")

@tasks.loop(seconds=30)
async def check_followups():
    now = datetime.datetime.now(LOCAL_TZ) 
    for user_id, data in list(active_followups.items()):
        if data["status"] == "WAITING_TO_REMIND" and data["next_remind_time"] <= now:
            print(f"[Log] Re-reminding user {user_id} for task: {data['task']}")
            try:
                user = await bot.fetch_user(user_id)
                if user:
                    phrase = random.choice(RE_REMINDER_PHRASES); await user.send(f"Hey! Just checking in on that task: **{data['task']}**\n\n{phrase}")
                    data["status"] = "WAITING_FOR_REPLY"; data["next_remind_time"] = None
                    print(f"[Log] Re-reminder sent. User {user_id} is 'WAITING_FOR_REPLY'")
            except (discord.errors.Forbidden, discord.errors.NotFound):
                print(f"[Log] Could not find or DM user {user_id} for follow-up. Removing.")
                if user_id in active_followups: del active_followups[user_id]
            except Exception as e: print(f"[Log] Error in check_followups loop: {e}")

@check_followups.before_loop
async def before_check_followups():
    await bot.wait_until_ready()
    print("Follow-up check loop is starting.")

# --- Bot Commands (Refactored for DB) ---
@bot.command(name='listreminders', help='(Admin only) Lists all upcoming reminders from the database.')
async def listreminders(ctx):
    if ctx.author.id not in ADMIN_USER_IDS:
        await ctx.send("Sorry, this command is for bot admins only."); return
    try:
        # Querying only the admin's (own) reminders
        response = db_table.query(
            KeyConditionExpression='user_id = :uid',
            ExpressionAttributeValues={':uid': str(ctx.author.id)}
        )
        items = response.get('Items', [])
        
        # --- NEW: Also scan for reminders *set for others* by this admin (if you want this)
        # This is complex with DynamoDB. For now, we'll stick to just *your* reminders.
        # A full "list all" would require a Scan, which is inefficient.
        # A GSI on 'creator_id' could work if we add that field.
        
        if not items:
            await ctx.send("You have no reminders assigned to *you* in the database!"); return
        
        items.sort(key=lambda r: r['remind_time_utc'])
        response_message = f"**You have {len(items)} upcoming reminders in the DB:**\n\n"
        for i, item in enumerate(items):
            task = item['task']
            if len(task) > 50: task = task[:50] + "..."
            remind_time_obj = datetime.datetime.fromisoformat(item['remind_time_utc'])
            time_str = f"<t:{int(remind_time_obj.timestamp())}:f>"
            reminder_id_short = item['reminder_id'].split('-')[0]
            
            # Add a recurring symbol
            recur_str = " (🔄 Recurring)" if item.get('is_recurring', False) else ""
            
            response_message += f"**{i+1}.** {task}{recur_str}\n    *Due: {time_str}*\n    *ID: `{reminder_id_short}`*\n"
            if len(response_message) > 1800:
                await ctx.send(response_message); response_message = ""
        if response_message: await ctx.send(response_message)
    except Exception as e: await ctx.send(f"An error occurred while fetching reminders: {e}")

@bot.command(name='importcalendar', help='(Admin only) Upload your .ics calendar file to import all deadlines.')
async def importcalendar(ctx):
    if ctx.author.id not in ADMIN_USER_IDS:
        await ctx.send("Sorry, this command is for bot admins only."); return
    await ctx.send(f"Okay, {ctx.author.mention}! Please **drag and drop** your `.ics` file and **type `!importcalendar` in the comment**.")

@bot.command(name='importtasks', help='Upload your .csv task file to import all deadlines.')
async def importtasks(ctx):
    await ctx.send(f"Okay, {ctx.author.mention}! Please **drag and drop** your `.csv` file and **type `!importtasks` in the comment**.\n\nMake sure your file has columns: `Task`, `DueDate`, `DueTime`, and (optionally) `Course`.")

@bot.command(name='remindme', help='Sets a reminder. Usage: !remindme <minutes> <task>')
async def remindme(ctx, minutes: int, *, task: str):
    try:
        if minutes <= 0:
            await ctx.send("Please provide a positive number of minutes!"); return
        
        now = datetime.datetime.now(LOCAL_TZ) 
        remind_time = now + datetime.timedelta(minutes=minutes)
        
        if await add_reminder_to_db(ctx.author.id, ctx.channel.id, remind_time, task):
            await ctx.send(f"Okay, {ctx.author.mention}! I'll remind you to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
        else: await ctx.send("Sorry, I had an error saving that reminder to the database.")
    except ValueError: await ctx.send("Invalid number of minutes. Please enter a number.")
    except Exception as e: 
        print(f"[Log] ERROR in !remindme: {e}")
        await ctx.send(f"An error occurred: {e}")

@bot.command(name='remindat', help='Sets a reminder. Usage: !remindat "<time>" <task> (e.g., "10pm" or "in 2 hours")')
async def remindat(ctx, time_str: str, *, task: str):
    try:
        remind_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
        
        if not remind_time:
            await ctx.send(f'Sorry, I couldn\'t understand the time "{time_str}". Please try again.')
            return

        now = datetime.datetime.now(LOCAL_TZ)
        if remind_time <= now:
            await ctx.send(f"That time is in the past! (I understood that as: {remind_time.strftime('%Y-%m-%d %I:%M %p')}) Please provide a future time."); return
        
        if await add_reminder_to_db(ctx.author.id, ctx.channel.id, remind_time, task):
             await ctx.send(f"Got it, {ctx.author.mention}! I'll remind you to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
        else: await ctx.send("Sorry, I had an error saving that reminder to the database.")
    
    except Exception as e: 
        print(f"[Log] ERROR in !remindat: {e}")
        await ctx.send(f"An error occurred: {e}")

# --- MODIFIED: Admin Commands ---
@bot.command(name='setreminder', help='(Admin only) Sets a reminder for users. Usage: !setreminder <@user1 ...> "<time>" <task>')
async def setreminder(ctx, users: commands.Greedy[discord.User], time_str: str, *, task: str):
    if ctx.author.id not in ADMIN_USER_IDS:
        await ctx.send("Sorry, this command is for bot admins only."); return
    
    if not users:
        await ctx.send("You must specify at least one user! Usage: `!setreminder <@user> \"<time>\" <task>`"); return

    try:
        remind_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})

        if not remind_time:
            await ctx.send(f'Sorry, I couldn\'t understand the time "{time_str}". Please try again.')
            return
            
        now = datetime.datetime.now(LOCAL_TZ)
        if remind_time <= now:
            await ctx.send(f"That time is in the past! (I understood that as: {remind_time.strftime('%Y-%m-%d %I:%M %p')}) Please provide a future time."); return
        
        success_users = []
        fail_users = []
        
        for user in users:
            if await add_reminder_to_db(user.id, ctx.channel.id, remind_time, task):
                success_users.append(user.mention)
            else: 
                fail_users.append(user.mention)
        
        response_msg = ""
        if success_users:
            response_msg += f"✅ Got it! I'll remind {', '.join(success_users)} to **{task}** at <t:{int(remind_time.timestamp())}:f>.\n"
        if fail_users:
            response_msg += f"❌ I failed to set a reminder for {', '.join(fail_users)}."
            
        await ctx.send(response_msg)
    
    except Exception as e: 
        print(f"[Log] ERROR in !setreminder: {e}")
        await ctx.send(f"An error occurred: {e}")

# --- NEW: Routine Reminder Command ---
@bot.command(name='routinereminder', help='(Admin only) Sets a recurring reminder. Usage: !routinereminder <@user1 ...> "<days>" "<time>" <task>')
async def routinereminder(ctx, users: commands.Greedy[discord.User], days_str: str, time_str: str, *, task: str):
    if ctx.author.id not in ADMIN_USER_IDS:
        await ctx.send("Sorry, this command is for bot admins only."); return

    if not users:
        await ctx.send("You must specify at least one user!"); return

    try:
        # 1. Parse the days
        target_weekdays = parse_days_string(days_str)
        if not target_weekdays:
            await ctx.send(f"I couldn't understand the days: \"{days_str}\". Please use 'everyday' or 'Mon,Wed,Fri', 'Tues/Thurs', etc."); return
        
        # 2. Parse the time
        parsed_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
        if not parsed_time:
            await ctx.send(f"I couldn't understand the time: \"{time_str}\". Please use '10am' or '14:30'."); return
        target_time = parsed_time.time()

        # 3. Create the rule string
        rule_str = f"WEEKLY:{','.join(map(str, target_weekdays))}:{target_time.strftime('%H:%M')}"
        
        # 4. Find the *first* occurrence
        now_local = datetime.datetime.now(LOCAL_TZ)
        first_occurrence_time = calculate_next_occurrence(now_local, target_weekdays, target_time)
        
        # 5. Add to DB for each user
        success_users = []
        fail_users = []
        
        for user in users:
            if await add_reminder_to_db(user.id, ctx.channel.id, first_occurrence_time, task, is_recurring=True, recurrence_rule=rule_str):
                success_users.append(user.mention)
            else:
                fail_users.append(user.mention)
                
        response_msg = ""
        day_names = ['Mon', 'Tues', 'Wed', 'Thurs', 'Fri', 'Sat', 'Sun']
        human_days = ", ".join([day_names[d] for d in target_weekdays]) if len(target_weekdays) < 7 else "everyday"

        if success_users:
            response_msg += f"✅ Set recurring reminder for {', '.join(success_users)}: **{task}**\n"
            response_msg += f"   *When:* {human_days} at {target_time.strftime('%I:%M %p %Z')}\n"
            response_msg += f"   *First one is:* <t:{int(first_occurrence_time.timestamp())}:f>"
        if fail_users:
            response_msg += f"\n❌ I failed to set the recurring reminder for {', '.join(fail_users)}."
        
        await ctx.send(response_msg)

    except Exception as e:
        print(f"[Log] ERROR in !routinereminder: {e}")
        await ctx.send(f"An error occurred: {e}")


@bot.command(name='deletereminder', help='(Admin only) Deletes a reminder. Usage: !deletereminder <id>')
async def deletereminder(ctx, short_id: str):
    if ctx.author.id not in ADMIN_USER_IDS: return 
    
    # This now needs to scan *all* users' reminders, as an admin can delete anyone's
    response = db_table.scan(
        FilterExpression='begins_with(reminder_id, :sid)',
        ExpressionAttributeValues={':sid': short_id}
    )
    items = response.get('Items', [])
    if not items:
        await ctx.send(f"I couldn't find a reminder with an ID starting with `{short_id}`."); return
    if len(items) > 1:
        await ctx.send(f"That ID is ambiguous and matches {len(items)} reminders. Please be more specific."); return
    
    item = items[0]
    try:
        db_table.delete_item(Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']})
        await ctx.send(f"✅ Successfully deleted reminder: **{item['task']}** (for user <@{item['user_id']}>)")
    except Exception as e: await ctx.send(f"An error occurred while deleting: {e}")

@bot.command(name='updatetask', help='(Admin only) Updates a task. Usage: !updatetask <id> <new task>')
async def updatetask(ctx, short_id: str, *, new_task: str):
    if ctx.author.id not in ADMIN_USER_IDS: return 

    response = db_table.scan(
        FilterExpression='begins_with(reminder_id, :sid)',
        ExpressionAttributeValues={':sid': short_id}
    )
    items = response.get('Items', [])
    if not items:
        await ctx.send(f"I couldn't find a reminder with an ID starting with `{short_id}`."); return
    if len(items) > 1:
        await ctx.send(f"That ID is ambiguous and matches {len(items)} reminders. Please be more specific."); return
    
    item = items[0]
    try:
        db_table.update_item(
            Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']},
            UpdateExpression="set task = :t", ExpressionAttributeValues={':t': new_task}
        )
        await ctx.send(f"✅ Task updated for `{short_id}`!\n**Old:** {item['task']}\n**New:** {new_task}")
    except Exception as e: await ctx.send(f"An error occurred while updating: {e}")

@bot.command(name='updatetime', help='(Admin only) Updates time. Usage: !updatetime <id> "<time>"')
async def updatetime(ctx, short_id: str, time_str: str):
    if ctx.author.id not in ADMIN_USER_IDS: return 
    
    response = db_table.scan(
        FilterExpression='begins_with(reminder_id, :sid)',
        ExpressionAttributeValues={':sid': short_id}
    )
    items = response.get('Items', [])
    if not items:
        await ctx.send(f"I couldn't find a reminder with an ID starting with `{short_id}`."); return
    if len(items) > 1:
        await ctx.send(f"That ID is ambiguous and matches {len(items)} reminders. Please be more specific."); return
    
    item = items[0]
    try:
        new_remind_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})

        if not new_remind_time:
            await ctx.send(f'Sorry, I couldn\'t understand the time "{time_str}". Please try again.')
            return

        now = datetime.datetime.now(LOCAL_TZ)
        if new_remind_time <= now:
            await ctx.send(f"That time is in the past! (I understood that as: {new_remind_time.strftime('%Y-%m-%d %I:%M %p')}) Please provide a future time."); return

        new_remind_time_iso = new_remind_time.isoformat()
        
        # Update logic: Delete and replace to ensure GSI is updated
        # (This is safer, but update_item is also possible)
        db_table.delete_item(Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']})
        item['remind_time_utc'] = new_remind_time_iso 
        # CRITICAL: If this was a recurring reminder, updating the time *once*
        # breaks the recurrence. We should set is_recurring to False.
        if 'is_recurring' in item:
            item['is_recurring'] = False
            item['recurrence_rule'] = 'NONE' # Clear the rule
        
        db_table.put_item(Item=item)
        
        new_time_discord = f"<t:{int(new_remind_time.timestamp())}:f>"
        await ctx.send(f"✅ Time updated for **{item['task']}**!\n**New Time:** {new_time_discord}\n*(Note: This action made the reminder non-recurring.)*")
    
    except Exception as e: 
        print(f"[Log] ERROR in !updatetime: {e}")
        await ctx.send(f"An error occurred while updating: {e}")

# --- Run the Bot ---
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("="*50); print("ERROR: Invalid DISCORD_TOKEN."); print("="*50)
    except Exception as e:
        print(f"An error occurred while running the bot: {e}")
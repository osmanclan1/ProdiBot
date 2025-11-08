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
import dateparser
import csv
from dotenv import load_dotenv
import json

print(">>> LOCAL VERSION RUNNING <<<")
load_dotenv()

# Configuration
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
LOCAL_TZ = pytz.timezone('America/Chicago')
ADMIN_USER_IDS = [321078607772385280, 720677158736887808]

# DynamoDB Setup
try:
    session = boto3.Session(profile_name="asad")
    dynamodb = session.resource("dynamodb", region_name="us-east-1")
    db_table = dynamodb.Table("ProdibotDB")
    DYNAMO_GSI_NAME = "StatusandTime"
    print(f"Successfully connected to DynamoDB table: ProdibotDB")
except Exception as e:
    print(f"ERROR: Could not connect to DynamoDB. {e}"); exit()

if not DISCORD_TOKEN or not OPENAI_API_KEY:
    print("="*50); print("ERROR: DISCORD_TOKEN or OPENAI_API_KEY is missing."); print("="*50); exit()

try:
    client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    print(f"Error initializing OpenAI client: {e}"); exit()

intents = discord.Intents.default()
intents.message_content = True   # Required to read message text
intents.messages = True          # Enables message events (guild + DM)
intents.members = True
bot = commands.Bot(command_prefix="?", intents=intents)

# Memory
active_followups = {}
task_memory = {}
MAX_MEMORY_MESSAGES = 8
RE_REMINDER_PHRASES = ["Just a friendly nudge!", "How's that task coming along?", "Just checking in on this again.", "Hope you haven't forgotten about this!"]

def init_task_memory(user_id, instruction):
    task_memory[user_id] = {"instruction": instruction, "messages": []}

def add_memory_message(user_id, role, content):
    if user_id not in task_memory: return
    buffer = task_memory[user_id]["messages"]
    buffer.append({"role": role, "content": content})
    if len(buffer) > MAX_MEMORY_MESSAGES: buffer.pop(0)

def get_task_context(user_id):
    return task_memory.get(user_id, None)

def ensure_memory(user_id, task):
    if get_task_context(user_id) is None:
        init_task_memory(user_id, task)

# AI Functions
async def get_task_status_from_ai(user_message, user_id):
    context = get_task_context(user_id)
    instruction = context["instruction"] if context else ""
    history = context["messages"] if context else []
    history_json = json.dumps(history, ensure_ascii=False)
    
    system_prompt = "You are a classification bot. Determine if the task is complete.\n- If message implies completion (done, finished, etc) return: [TASK_DONE]\n- Otherwise return: [TASK_NOT_DONE]\nReturn ONLY one of those tokens."
    user_prompt = f"Task: {instruction}\n\nRecent messages (JSON list of role/content pairs):\n{history_json}\n\nUser now says: {user_message}"
    
    try:
        completion = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            max_tokens=5, temperature=0.0
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Log] ERROR calling OpenAI: {e}")
        return "[TASK_NOT_DONE]"

async def get_memory_chat_reply(user_id):
    context = get_task_context(user_id)
    if not context:
        print(f"[DEBUG] No context for user {user_id}")
        return None
    
    instruction = context.get("instruction", "")
    messages = context.get("messages", [])
    
    print(f"[DEBUG] get_memory_chat_reply - instruction: '{instruction}', messages count: {len(messages)}")
    
    if not instruction or instruction == "General conversation":
        print(f"[DEBUG] Invalid instruction: '{instruction}'")
        return None
    
    # Allow response even if no previous messages (first message)
    system_prompt = (
        f"You are a task manager checking on the user's progress for: {instruction}\n"
        "Your role is to:\n- Check if the task has been completed\n- Ask for status updates\n"
        "- Hold the user accountable\n- Redirect off-topic conversation back to completion status\n"
        "- Do NOT provide help, guidance, or advice - only check completion status\n"
        "Keep responses brief, direct, and focused on completion status. Be professional but firm."
    )
    
    openai_messages = [{"role": "system", "content": system_prompt}]
    # Include last 8 messages (or all if less than 8)
    openai_messages.extend([{"role": msg["role"], "content": msg["content"]} for msg in messages[-8:]])
    
    print(f"[DEBUG] Sending {len(openai_messages)} messages to OpenAI")
    
    try:
        completion = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini", messages=openai_messages, max_tokens=200, temperature=0.7
        )
        reply = completion.choices[0].message.content.strip()
        print(f"[DEBUG] OpenAI returned reply: {reply[:50]}...")
        return reply
    except Exception as e:
        print(f"[Log] ERROR calling OpenAI for chat reply: {e}")
        import traceback
        traceback.print_exc()
        return None

# Recurring reminder helpers
def parse_days_string(days_str):
    days_str = days_str.lower().strip()
    if days_str == 'everyday': return list(range(7))
    
    day_map = {'mon': 0, 'monday': 0, 'tue': 1, 'tues': 1, 'tuesday': 1, 'wed': 2, 'wednesday': 2,
               'thu': 3, 'thur': 3, 'thurs': 3, 'thursday': 3, 'fri': 4, 'friday': 4,
               'sat': 5, 'saturday': 5, 'sun': 6, 'sunday': 6}
    
    selected_days = set()
    parts = [p.strip() for p in days_str.replace('/', ',').replace(' ', ',').split(',') if p.strip()]
    
    for part in parts:
        if part in day_map:
            selected_days.add(day_map[part])
        elif 'm' in part and 'w' in part and 'f' in part:
            selected_days.update([0, 2, 4])
    
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
        return LOCAL_TZ.localize(datetime.datetime.combine(now_local.date(), target_time))
    
    next_day_weekday = None
    for day in sorted(target_weekdays):
        if day > today_weekday:
            next_day_weekday = day
            break
    
    if next_day_weekday is None:
        next_day_weekday = sorted(target_weekdays)[0]
        days_to_add = (next_day_weekday - today_weekday + 7) % 7
    else:
        days_to_add = (next_day_weekday - today_weekday)
    
    next_date = now_local.date() + datetime.timedelta(days=days_to_add)
    return LOCAL_TZ.localize(datetime.datetime.combine(next_date, target_time))

def calculate_next_from_rule(rule_str):
    try:
        parts = rule_str.split(':')
        if parts[0] != 'WEEKLY' or len(parts) != 3:
            print(f"[Log] Invalid rule format: {rule_str}"); return None
        
        target_weekdays = [int(d) for d in parts[1].split(',')]
        target_time = datetime.datetime.strptime(parts[2], '%H:%M').time()
        return calculate_next_occurrence(datetime.datetime.now(LOCAL_TZ), target_weekdays, target_time)
    except Exception as e:
        print(f"[Log] Error parsing rule {rule_str}: {e}"); return None

async def add_reminder_to_db(author_id, channel_id, remind_time, task, is_recurring=False, recurrence_rule=None):
    try:
        item = {
            'user_id': str(author_id), 'reminder_id': str(uuid.uuid4()),
            'channel_id': str(channel_id), 'remind_time_utc': remind_time.isoformat(),
            'task': task, 'status': 'PENDING'
        }
        if is_recurring:
            item['is_recurring'] = True
            item['recurrence_rule'] = recurrence_rule
        
        db_table.put_item(Item=item)
        print(f"[Log] Added {'RECURRING' if is_recurring else ''} reminder to DB. User: {author_id}")
        return True
    except Exception as e:
        print(f"[Log] ERROR adding reminder to DB: {e}"); return False

def parse_datetime_from_ical(dtstart):
    if isinstance(dtstart, datetime.datetime):
        return dtstart.astimezone(LOCAL_TZ) if dtstart.tzinfo else LOCAL_TZ.localize(dtstart)
    elif isinstance(dtstart, datetime.date):
        return LOCAL_TZ.localize(datetime.datetime.combine(dtstart, datetime.time(23, 59, 59)))
    return None

# Bot Events
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})'); print('Bot is ready.')
    check_reminders.start(); check_followups.start()

@bot.event
async def on_message(message):
    if message.author == bot.user: return
    print(f"[DEBUG] Received message from {message.author.id} in {type(message.channel).__name__}: {message.content[:50]}")
    
    # Log all DM messages to memory immediately
    if isinstance(message.channel, discord.DMChannel) and not message.content.startswith("?"):
        add_memory_message(message.author.id, "user", message.content)
        print(f"[DM USER] {message.author.id}: {message.content}")

    # Calendar import
    if message.attachments and message.content == "?importcalendar":
        if message.author.id not in ADMIN_USER_IDS:
            await message.channel.send("Sorry, only bot admins can import a calendar."); return
        attachment = message.attachments[0]
        if attachment.filename.endswith(".ics"):
            await message.add_reaction("🔄")
            try:
                gcal = Calendar.from_ical(await attachment.read())
                reminders_added = reminders_past = 0
                now_local = datetime.datetime.now(LOCAL_TZ)
                
                for component in gcal.walk():
                    if component.name == "VEVENT":
                        summary = str(component.get('summary'))
                        dtstart_local = parse_datetime_from_ical(component.get('dtstart').dt)
                        if not dtstart_local: continue
                        
                        remind_time = dtstart_local - datetime.timedelta(hours=24)
                        if remind_time > now_local:
                            if await add_reminder_to_db(message.author.id, message.channel.id, remind_time, f"(From Calendar) {summary}"):
                                ensure_memory(message.author.id, f"(From Calendar) {summary}")
                                reminders_added += 1
                        else:
                            reminders_past += 1
                
                print(f"[Log] Calendar processed. Added {reminders_added}, Skipped {reminders_past}.")
                await message.channel.send(f"✅ Calendar imported! I added **{reminders_added}** new reminders. I skipped {reminders_past} events in the past.")
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("✅")
            except Exception as e:
                print(f"[Log] FAILED to parse calendar: {e}")
                await message.channel.send(f"❌ Error parsing `.ics` file. Error: {e}")
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("❌")
        else:
            await message.channel.send("That doesn't look like an `.ics` file. Please upload a valid calendar file.")
        return

    # CSV import
    if message.attachments and message.content == "?importtasks":
        attachment = message.attachments[0]
        if attachment.filename.endswith(".csv"):
            await message.add_reaction("🔄")
            try:
                reader = csv.DictReader(io.StringIO((await attachment.read()).decode('utf-8')))
                reminders_added = reminders_past = errors_found = 0
                now_local = datetime.datetime.now(LOCAL_TZ)
                
                for row in reader:
                    try:
                        task = row['Task']
                        course = row.get('Course', '')
                        due_datetime = dateparser.parse(f"{row['DueDate']} {row['DueTime']}", settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
                        
                        if not due_datetime:
                            errors_found += 1; continue
                        
                        remind_time = due_datetime - datetime.timedelta(hours=48)
                        full_task_str = f"({course}) {task}" if course else task
                        
                        if remind_time > now_local:
                            if await add_reminder_to_db(message.author.id, message.channel.id, remind_time, full_task_str):
                                ensure_memory(message.author.id, full_task_str)
                                reminders_added += 1
                        else:
                            reminders_past += 1
                    except (KeyError, Exception) as e:
                        print(f"[Log] Error processing CSV row: {e}"); errors_found += 1
                
                response_msg = f"✅ CSV imported! I added **{reminders_added}** new reminders."
                if reminders_past > 0: response_msg += f" I skipped {reminders_past} events in the past."
                if errors_found > 0: response_msg += f" I found **{errors_found} rows** I couldn't read."
                
                await message.channel.send(response_msg)
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("✅")
            except Exception as e:
                print(f"[Log] FAILED to parse CSV: {e}")
                await message.channel.send(f"❌ Error parsing `.csv` file. Error: {e}")
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("❌")
        else:
            await message.channel.send("That doesn't look like a `.csv` file. Please upload a valid CSV.")
        return

    # DM Follow-up Logic (only for WAITING_FOR_REPLY status)
    if isinstance(message.channel, discord.DMChannel) and message.author.id in active_followups:
        user_id = message.author.id
        follow_up_data = active_followups[user_id]
        print(f"[DEBUG] User {user_id} in active_followups, status: {follow_up_data['status']}")
        if follow_up_data["status"] == "WAITING_FOR_REPLY":
            async with message.channel.typing():
                status = await get_task_status_from_ai(message.content, user_id)
            
            if status == "[TASK_DONE]":
                reply = "Great job! Way to get it done. I'll check this off the list. ✅"
                await message.channel.send(reply)
                add_memory_message(user_id, "assistant", reply)
                print(f"[DM BOT]: {reply}")
                del active_followups[user_id]
                if user_id in task_memory: del task_memory[user_id]
                print(f"[Log] Task complete for user {user_id}.")
            else:
                reply = "Okay, no worries. I'll check in with you again in a bit!"
                await message.channel.send(reply)
                add_memory_message(user_id, "assistant", reply)
                print(f"[DM BOT]: {reply}")
                random_minutes = random.randint(1, 2)
                follow_up_data["status"] = "WAITING_TO_REMIND"
                follow_up_data["next_remind_time"] = datetime.datetime.now(LOCAL_TZ) + datetime.timedelta(minutes=random_minutes)
                print(f"[Log] User {user_id} not done. Next check-in at {follow_up_data['next_remind_time'].isoformat()}")
            return
        # If status is WAITING_TO_REMIND, fall through to chatbot mode
        print(f"[DEBUG] Status is WAITING_TO_REMIND, falling through to chatbot mode")

    # Chatbot mode (works for all DMs that aren't commands and aren't actively waiting for reply)
    if isinstance(message.channel, discord.DMChannel) and not message.content.startswith("?"):
        print(f"[DEBUG] Entering chatbot mode for user {message.author.id}")
        user_id = message.author.id
        context = get_task_context(user_id)
        
        if not context or not context.get("instruction") or context.get("instruction") == "General conversation":
            print(f"[DEBUG] No valid context found. Context: {context}")
            await message.channel.send("I track task completion. To start, set a reminder using `?remindme` or `?remindat`.\n\nOnce you have an active task, I'll check on your progress and completion status.")
            return
        
        print(f"[DEBUG] Context found: instruction='{context.get('instruction')}', messages={len(context.get('messages', []))}")
        
        # Refresh context to get updated message count (message already logged at top)
        context = get_task_context(user_id)
        print(f"[DEBUG] Memory after adding user message: {len(context.get('messages', []))} messages")
        
        async with message.channel.typing():
            bot_reply = await get_memory_chat_reply(user_id)
        
        print(f"[DEBUG] Bot reply received: {bot_reply is not None}, reply: {bot_reply[:100] if bot_reply else 'None'}")
        
        if bot_reply:
            await message.channel.send(bot_reply)
            add_memory_message(user_id, "assistant", bot_reply)
            print(f"[DM BOT]: {bot_reply}")
            context = get_task_context(user_id)
            if context:
                messages_list = context.get('messages', [])
                print(f"[MEMORY UPDATED] Current buffer for {user_id} ({len(messages_list)} messages):\n{json.dumps(messages_list, indent=2, ensure_ascii=False)}")
        else:
            error_msg = "Sorry, I'm having trouble processing that right now. Please try again!"
            await message.channel.send(error_msg)
            print(f"[DM BOT]: {error_msg}")
            print(f"[DEBUG] get_memory_chat_reply returned None. Context: {context}")
        return

    await bot.process_commands(message)

# Background Loops
@tasks.loop(seconds=15)
async def check_reminders():
    try:
        response = db_table.query(
            IndexName=DYNAMO_GSI_NAME,
            KeyConditionExpression='#s = :s AND remind_time_utc <= :now',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':s': 'PENDING', ':now': datetime.datetime.now(LOCAL_TZ).isoformat()}
        )
        
        for reminder in response.get('Items', []):
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
                            ensure_memory(author_id, task)
                            reply = f"Hey {user.mention}, this is your reminder to: **{task}**\n\nDid you get that done?"
                            await user.send(reply)
                            add_memory_message(user.id, "assistant", reply)
                            print(f"[DM BOT]: {reply}")
                            sent_successfully = True
                            print(f"[Log] Sent reminder via DM to {author_id}")
                        except discord.errors.Forbidden:
                            print(f"[Log] DM failed for {author_id}. Attempting public fallback.")
                            try:
                                channel = await bot.fetch_channel(channel_id)
                                if channel:
                                    await channel.send(f"Hey {user.mention}, I tried to DM you this reminder but your DMs are off!\n\n**Task:** {task}\n\nDid you get that done?")
                                    sent_successfully = True
                                    print(f"[Log] Sent reminder publicly to channel {channel_id}")
                            except Exception:
                                print(f"[Log] Public fallback failed.")
                        except Exception as e:
                            print(f"[Log] Unknown error trying to DM user: {e}")
                
                if sent_successfully:
                    if not (author_id in active_followups and active_followups[author_id]['task'] == task):
                        active_followups[author_id] = {"task": task, "status": "WAITING_FOR_REPLY", "next_remind_time": None}
                        print(f"[Log] Added user {author_id} to active follow-up list.")
                    
                    db_table.delete_item(Key={'user_id': str(author_id), 'reminder_id': reminder_id})
                    print(f"[Log] Deleted reminder {reminder_id} from DB.")
                    
                    # Recurrence check
                    if reminder.get('is_recurring', False):
                        rule = reminder.get('recurrence_rule')
                        if rule and rule != 'NONE':
                            print(f"[Log] Rescheduling recurring reminder {reminder_id} with rule: {rule}")
                            next_remind_time = calculate_next_from_rule(rule)
                            if next_remind_time:
                                if await add_reminder_to_db(author_id, channel_id, next_remind_time, task, is_recurring=True, recurrence_rule=rule):
                                    ensure_memory(author_id, task)
                                print(f"[Log] Successfully rescheduled {reminder_id}. Next at: {next_remind_time.isoformat()}")
                else:
                    print(f"[Log] Failed to send reminder {reminder_id} by any method. Will retry next loop.")
            except Exception as e:
                print(f"[Log] CRITICAL error in check_reminders sub-loop: {e}")
                try:
                    db_table.delete_item(Key={'user_id': reminder['user_id'], 'reminder_id': reminder['reminder_id']})
                except Exception: pass
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
                    ensure_memory(user_id, data['task'])
                    phrase = random.choice(RE_REMINDER_PHRASES)
                    reply = f"Hey! Just checking in on that task: **{data['task']}**\n\n{phrase}"
                    await user.send(reply)
                    add_memory_message(user.id, "assistant", reply)
                    print(f"[DM BOT]: {reply}")
                    data["status"] = "WAITING_FOR_REPLY"
                    data["next_remind_time"] = None
                    print(f"[Log] Re-reminder sent. User {user_id} is 'WAITING_FOR_REPLY'")
            except (discord.errors.Forbidden, discord.errors.NotFound):
                print(f"[Log] Could not find or DM user {user_id} for follow-up. Removing.")
                if user_id in active_followups: del active_followups[user_id]
            except Exception as e:
                print(f"[Log] Error in check_followups loop: {e}")

@check_followups.before_loop
async def before_check_followups():
    await bot.wait_until_ready()
    print("Follow-up check loop is starting.")

# Helper decorator
def admin_only():
    def predicate(ctx):
        return ctx.author.id in ADMIN_USER_IDS
    return commands.check(predicate)

# Commands
@bot.command(name='listreminders', help='(Admin only) Lists all upcoming reminders from the database.')
@admin_only()
async def listreminders(ctx):
    try:
        response = db_table.query(
            KeyConditionExpression='user_id = :uid',
            ExpressionAttributeValues={':uid': str(ctx.author.id)}
        )
        items = response.get('Items', [])
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
            recur_str = " (🔄 Recurring)" if item.get('is_recurring', False) else ""
            response_message += f"**{i+1}.** {task}{recur_str}\n    *Due: {time_str}*\n    *ID: `{reminder_id_short}`*\n"
            if len(response_message) > 1800:
                await ctx.send(response_message); response_message = ""
        if response_message: await ctx.send(response_message)
    except Exception as e:
        await ctx.send(f"An error occurred while fetching reminders: {e}")

@bot.command(name='importcalendar', help='(Admin only) Upload your .ics calendar file to import all deadlines.')
@admin_only()
async def importcalendar(ctx):
    await ctx.send(f"Okay, {ctx.author.mention}! Please **drag and drop** your `.ics` file and **type `?importcalendar` in the comment**.")

@bot.command(name='importtasks', help='Upload your .csv task file to import all deadlines.')
async def importtasks(ctx):
    await ctx.send(f"Okay, {ctx.author.mention}! Please **drag and drop** your `.csv` file and **type `?importtasks` in the comment**.\n\nMake sure your file has columns: `Task`, `DueDate`, `DueTime`, and (optionally) `Course`.")

@bot.command(name='remindme', help='Sets a reminder. Usage: ?remindme <minutes> <task>')
async def remindme(ctx, minutes: int, *, task: str):
    try:
        if minutes <= 0:
            await ctx.send("Please provide a positive number of minutes!"); return
        
        remind_time = datetime.datetime.now(LOCAL_TZ) + datetime.timedelta(minutes=minutes)
        if await add_reminder_to_db(ctx.author.id, ctx.channel.id, remind_time, task):
            ensure_memory(ctx.author.id, task)
            await ctx.send(f"[LOCAL BOT] I will remind you to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
        else:
            await ctx.send("Sorry, I had an error saving that reminder to the database.")
    except ValueError:
        await ctx.send("Invalid number of minutes. Please enter a number.")
    except Exception as e:
        print(f"[Log] ERROR in ?remindme: {e}")
        await ctx.send(f"An error occurred: {e}")

@bot.command(name='remindat', help='Sets a reminder. Usage: ?remindat "<time>" <task>')
async def remindat(ctx, time_str: str, *, task: str):
    try:
        remind_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
        if not remind_time:
            await ctx.send(f'Sorry, I couldn\'t understand the time "{time_str}". Please try again.'); return
        
        now = datetime.datetime.now(LOCAL_TZ)
        if remind_time <= now:
            await ctx.send(f"That time is in the past! (I understood that as: {remind_time.strftime('%Y-%m-%d %I:%M %p')}) Please provide a future time."); return
        
        if await add_reminder_to_db(ctx.author.id, ctx.channel.id, remind_time, task):
            ensure_memory(ctx.author.id, task)
            await ctx.send(f"Got it, {ctx.author.mention}! I'll remind you to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
        else:
            await ctx.send("Sorry, I had an error saving that reminder to the database.")
    except Exception as e:
        print(f"[Log] ERROR in ?remindat: {e}")
        await ctx.send(f"An error occurred: {e}")

@bot.command(name='setreminder', help='(Admin only) Sets a reminder for users. Usage: ?setreminder <@user1 ...> "<time>" <task>')
@admin_only()
async def setreminder(ctx, users: commands.Greedy[discord.User], time_str: str, *, task: str):
    if not users:
        await ctx.send("You must specify at least one user! Usage: `?setreminder <@user> \"<time>\" <task>`"); return
    
    try:
        remind_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
        if not remind_time:
            await ctx.send(f'Sorry, I couldn\'t understand the time "{time_str}". Please try again.'); return
        
        now = datetime.datetime.now(LOCAL_TZ)
        if remind_time <= now:
            await ctx.send(f"That time is in the past! (I understood that as: {remind_time.strftime('%Y-%m-%d %I:%M %p')}) Please provide a future time."); return
        
        success_users = []
        fail_users = []
        for user in users:
            if await add_reminder_to_db(user.id, ctx.channel.id, remind_time, task):
                success_users.append(user.mention)
                ensure_memory(user.id, task)
            else:
                fail_users.append(user.mention)
        
        response_msg = ""
        if success_users:
            response_msg += f"✅ Got it! I'll remind {', '.join(success_users)} to **{task}** at <t:{int(remind_time.timestamp())}:f>.\n"
        if fail_users:
            response_msg += f"❌ I failed to set a reminder for {', '.join(fail_users)}."
        await ctx.send(response_msg)
    except Exception as e:
        print(f"[Log] ERROR in ?setreminder: {e}")
        await ctx.send(f"An error occurred: {e}")

@bot.command(name='routinereminder', help='(Admin only) Sets a recurring reminder. Usage: ?routinereminder <@user1 ...> "<days>" "<time>" <task>')
@admin_only()
async def routinereminder(ctx, users: commands.Greedy[discord.User], days_str: str, time_str: str, *, task: str):
    if not users:
        await ctx.send("You must specify at least one user!"); return
    
    try:
        target_weekdays = parse_days_string(days_str)
        if not target_weekdays:
            await ctx.send(f"I couldn't understand the days: \"{days_str}\". Please use 'everyday' or 'Mon,Wed,Fri', 'Tues/Thurs', etc."); return
        
        parsed_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
        if not parsed_time:
            await ctx.send(f"I couldn't understand the time: \"{time_str}\". Please use '10am' or '14:30'."); return
        
        target_time = parsed_time.time()
        rule_str = f"WEEKLY:{','.join(map(str, target_weekdays))}:{target_time.strftime('%H:%M')}"
        first_occurrence_time = calculate_next_occurrence(datetime.datetime.now(LOCAL_TZ), target_weekdays, target_time)
        
        success_users = []
        fail_users = []
        for user in users:
            if await add_reminder_to_db(user.id, ctx.channel.id, first_occurrence_time, task, is_recurring=True, recurrence_rule=rule_str):
                success_users.append(user.mention)
                ensure_memory(user.id, task)
            else:
                fail_users.append(user.mention)
        
        day_names = ['Mon', 'Tues', 'Wed', 'Thurs', 'Fri', 'Sat', 'Sun']
        human_days = ", ".join([day_names[d] for d in target_weekdays]) if len(target_weekdays) < 7 else "everyday"
        
        response_msg = ""
        if success_users:
            response_msg += f"✅ Set recurring reminder for {', '.join(success_users)}: **{task}**\n"
            response_msg += f"   *When:* {human_days} at {target_time.strftime('%I:%M %p %Z')}\n"
            response_msg += f"   *First one is:* <t:{int(first_occurrence_time.timestamp())}:f>"
        if fail_users:
            response_msg += f"\n❌ I failed to set the recurring reminder for {', '.join(fail_users)}."
        await ctx.send(response_msg)
    except Exception as e:
        print(f"[Log] ERROR in ?routinereminder: {e}")
        await ctx.send(f"An error occurred: {e}")

def find_reminder_by_id(short_id):
    response = db_table.scan(FilterExpression='begins_with(reminder_id, :sid)', ExpressionAttributeValues={':sid': short_id})
    items = response.get('Items', [])
    if not items:
        return None, f"I couldn't find a reminder with an ID starting with `{short_id}`."
    if len(items) > 1:
        return None, f"That ID is ambiguous and matches {len(items)} reminders. Please be more specific."
    return items[0], None

@bot.command(name='deletereminder', help='(Admin only) Deletes a reminder. Usage: ?deletereminder <id>')
@admin_only()
async def deletereminder(ctx, short_id: str):
    item, error = find_reminder_by_id(short_id)
    if error:
        await ctx.send(error); return
    try:
        db_table.delete_item(Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']})
        await ctx.send(f"✅ Successfully deleted reminder: **{item['task']}** (for user <@{item['user_id']}>)")
    except Exception as e:
        await ctx.send(f"An error occurred while deleting: {e}")

@bot.command(name='updatetask', help='(Admin only) Updates a task. Usage: ?updatetask <id> <new task>')
@admin_only()
async def updatetask(ctx, short_id: str, *, new_task: str):
    item, error = find_reminder_by_id(short_id)
    if error:
        await ctx.send(error); return
    try:
        db_table.update_item(
            Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']},
            UpdateExpression="set task = :t", ExpressionAttributeValues={':t': new_task}
        )
        await ctx.send(f"✅ Task updated for `{short_id}`!\n**Old:** {item['task']}\n**New:** {new_task}")
    except Exception as e:
        await ctx.send(f"An error occurred while updating: {e}")

@bot.command(name='updatetime', help='(Admin only) Updates time. Usage: ?updatetime <id> "<time>"')
@admin_only()
async def updatetime(ctx, short_id: str, time_str: str):
    item, error = find_reminder_by_id(short_id)
    if error:
        await ctx.send(error); return
    try:
        new_remind_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
        if not new_remind_time:
            await ctx.send(f'Sorry, I couldn\'t understand the time "{time_str}". Please try again.'); return
        
        now = datetime.datetime.now(LOCAL_TZ)
        if new_remind_time <= now:
            await ctx.send(f"That time is in the past! (I understood that as: {new_remind_time.strftime('%Y-%m-%d %I:%M %p')}) Please provide a future time."); return
        
        db_table.delete_item(Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']})
        item['remind_time_utc'] = new_remind_time.isoformat()
        if 'is_recurring' in item:
            item['is_recurring'] = False
            item['recurrence_rule'] = 'NONE'
        db_table.put_item(Item=item)
        await ctx.send(f"✅ Time updated for **{item['task']}**!\n**New Time:** <t:{int(new_remind_time.timestamp())}:f>\n*(Note: This action made the reminder non-recurring.)*")
    except Exception as e:
        print(f"[Log] ERROR in ?updatetime: {e}")
        await ctx.send(f"An error occurred while updating: {e}")

@bot.command(name='memdump', help='(Admin only) Shows memory for a user. Usage: ?memdump [@user]')
@admin_only()
async def memdump(ctx, user: discord.User = None):
    target_user = user if user else ctx.author
    user_id = target_user.id
    context = get_task_context(user_id)
    
    if not context:
        await ctx.send("No memory found for this user."); return
    
    instruction = context.get("instruction", "N/A")
    messages = context.get("messages", [])
    
    print("=" * 17 + " MEMORY DUMP " + "=" * 17)
    print(f"User: {user_id}\nInstruction: \"{instruction}\"\nMessages:")
    for i, msg in enumerate(messages, 1):
        print(f"{i}. {msg.get('role', 'unknown')}: \"{msg.get('content', '')}\"")
    print("=" * 50)
    
    discord_msg = f"**User:** {target_user.mention}\n**Instruction:** {instruction}\n\n**Memory (latest 8):**\n\n"
    for msg in messages:
        discord_msg += f"{msg.get('role', 'unknown')}: {msg.get('content', '')}\n\n"
    
    if len(discord_msg) > 1900:
        discord_msg = f"**User:** {target_user.mention}\n**Instruction:** {instruction}\n\n**Memory:**\n```json\n{json.dumps(messages, indent=2, ensure_ascii=False)}\n```"
    
    await ctx.send(discord_msg)

@bot.command(name='memclear', help='(Admin only) Clears memory for a user. Usage: ?memclear [@user]')
@admin_only()
async def memclear(ctx, user: discord.User = None):
    target_user = user if user else ctx.author
    user_id = target_user.id
    
    if user_id not in task_memory:
        await ctx.send("No memory found for this user."); return
    
    del task_memory[user_id]
    print(f"[MEMORY CLEARED] User {user_id} ({target_user.name}) memory has been cleared.")
    await ctx.send(f"✅ Memory cleared for {target_user.mention}.")

# Run the Bot
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("="*50); print("ERROR: Invalid DISCORD_TOKEN."); print("="*50)
    except Exception as e:
        print(f"An error occurred while running the bot: {e}")

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

# --- Configuration ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# --- Set our "home" timezone ---
LOCAL_TZ = pytz.timezone('America/Chicago')

# --- DynamoDB Setup ---
try:
    dynamodb = boto3.resource('dynamodb', region_name="us-east-1") 
    
    # --- OLD DB (We still use it for !remindme, etc.) ---
    DYNAMO_TABLE_NAME = 'ProdibotDB'
    DYNAMO_GSI_NAME = 'StatusandTime'
    db_table = dynamodb.Table(DYNAMO_TABLE_NAME)
    
    # --- NEW DB TABLES (For the new agent) ---
    IDENTITY_TABLE_NAME = "MiniBotIdentity"
    WAKEUP_TABLE_NAME   = "MiniBotWakeups"
    identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
    wakeup_table   = dynamodb.Table(WAKEUP_TABLE_NAME)
    
    print(f"Successfully connected to DynamoDB tables:")
    print(f"- {DYNAMO_TABLE_NAME}")
    print(f"- {IDENTITY_TABLE_NAME}")
    print(f"- {WAKEUP_TABLE_NAME}")
    
except Exception as e:
    print(f"ERROR: Could not connect to DynamoDB. {e}"); exit()

OWNER_USER_ID = 321078607772385280 

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

# --- Helper function to add reminders to DB (OLD) ---
async def add_reminder_to_db(author_id, channel_id, remind_time, task):
    try:
        reminder_id = str(uuid.uuid4())
        remind_time_iso = remind_time.isoformat()
        
        db_table.put_item(
            Item={
                'user_id': str(author_id), 'reminder_id': reminder_id,
                'channel_id': str(channel_id), 
                'remind_time_utc': remind_time_iso, 
                'task': task, 'status': 'PENDING'
            }
        )
        print(f"[Log] Added (OLD) reminder to DB. User: {author_id}, ID: {reminder_id}, Time: {remind_time_iso}")
        return True
    except Exception as e:
        print(f"[Log] ERROR adding (OLD) reminder to DB: {e}"); return False

# --- +++ NEW HELPER FUNCTION (Implements Tool Call 2) +++ ---
async def create_agent_reminder(user_id, channel_id, remind_time, task_goal, personality):
    """
    This function implements the logic from your friend's "TOOL CALL 2".
    It creates the Bot Identity and the initial Wakeup.
    """
    try:
        user_id_str = str(user_id)
        
        # 1. Create/Update the MiniBotIdentity item 
        print(f"[Log] Creating/Updating Bot Identity for user: {user_id_str}")
        identity_table.put_item(
            Item={
                'user_id': user_id_str,
                'status': 'ACTIVE', # 
                'goal': task_goal, # 
                'last_result': 'N/A', # 
                'notes': 'Task initiated by owner.', # 
                'personality': personality, # 
                'created_at': datetime.datetime.now(LOCAL_TZ).isoformat()
            }
        )
        
        # 2. Create the MiniBotWakeups item 
        print(f"[Log] Creating Wakeup for user: {user_id_str}")
        wakeup_id = str(uuid.uuid4())
        
        # --- This is the TTL (Time To Live) ---
        # DynamoDB needs this as a Unix timestamp (integer)
        wakeup_timestamp = int(remind_time.timestamp()) 
        
        wakeup_table.put_item(
            Item={
                'wakeup_id': wakeup_id,
                'user_id': user_id_str, # Store this so the Lambda knows *who* to wake up
                'channel_id': str(channel_id),
                'goal': task_goal,
                'wakeup_time': wakeup_timestamp #  This is the TTL field
            }
        )
        
        print(f"[Log] ✅ Agent reminder created. Wakeup at: {remind_time.isoformat()}")
        return True
        
    except Exception as e:
        print(f"[Log] ❌ ERROR in create_agent_reminder: {e}")
        return False
# --- +++ END NEW FUNCTION +++ ---


# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})'); print('Bot is ready.')
    check_reminders.start(); check_followups.start()

@bot.event
async def on_message(message):
    if message.author == bot.user: return

    # --- Handle calendar file upload (No changes here) ---
    if message.attachments and message.content == "!importcalendar":
        if message.author.id != OWNER_USER_ID:
             await message.channel.send("Sorry, only my owner can import a calendar."); return
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

    # --- Existing DM Follow-up Logic (No changes here yet) ---
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
                random_minutes = random.randint(15, 45)
                next_remind_time = datetime.datetime.now(LOCAL_TZ) + datetime.timedelta(minutes=random_minutes)
                follow_up_data["status"] = "WAITING_TO_REMIND"; follow_up_data["next_remind_time"] = next_remind_time
                print(f"[Log] User {user_id} not done. Next check-in at {next_remind_time.isoformat()}")
        
        return

    if not (message.attachments and message.content == "!importcalendar"):
        await bot.process_commands(message)

# --- Background Loops ---
# This loop handles the OLD reminders (from ProdibotDB)
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
            
            print(f"\n[Log] Processing (OLD) DB reminder for task: \"{task}\"")

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
                else:
                    print(f"[Log] Failed to send reminder {reminder_id} by any method. User or channel not found.")

                db_table.delete_item(Key={'user_id': str(author_id), 'reminder_id': reminder_id})
                print(f"[Log] Deleted (OLD) reminder {reminder_id} from DB.")

            except Exception as e:
                print(f"[Log] CRITICAL error in check_reminders sub-loop: {e}")
                try:
                    db_table.delete_item(Key={'user_id': reminder['user_id'], 'reminder_id': reminder['reminder_id']})
                    print(f"[Log] Deleted erroring (OLD) reminder {reminder['reminder_id']} to prevent loop.")
                except Exception as del_e:
                    print(f"[Log] FAILED to delete erroring (OLD) reminder: {del_e}")

    except Exception as e:
        print(f"[Log] An unexpected error occurred querying (OLD) DynamoDB: {e}")


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

# --- Bot Commands ---
@bot.command(name='listreminders', help='(Owner only) Lists all upcoming reminders from the (OLD) database.')
async def listreminders(ctx):
    if ctx.author.id != OWNER_USER_ID:
        await ctx.send("Sorry, this command is for the bot owner only."); return
    try:
        response = db_table.query(
            KeyConditionExpression='user_id = :uid',
            ExpressionAttributeValues={':uid': str(ctx.author.id)}
        )
        items = response.get('Items', [])
        if not items:
            await ctx.send("You have no (old) reminders in the database!"); return
        
        items.sort(key=lambda r: r['remind_time_utc'])
        response_message = f"**You have {len(items)} upcoming (old) reminders in the DB:**\n\n"
        for i, item in enumerate(items):
            task = item['task']
            if len(task) > 50: task = task[:50] + "..."
            remind_time_obj = datetime.datetime.fromisoformat(item['remind_time_utc'])
            time_str = f"<t:{int(remind_time_obj.timestamp())}:f>"
            reminder_id_short = item['reminder_id'].split('-')[0]
            response_message += f"**{i+1}.** {task}\n    *Due: {time_str}*\n    *ID: `{reminder_id_short}`*\n"
            if len(response_message) > 1800:
                await ctx.send(response_message); response_message = ""
        if response_message: await ctx.send(response_message)
    except Exception as e: await ctx.send(f"An error occurred while fetching reminders: {e}")

@bot.command(name='importcalendar', help='Upload your .ics calendar file to import all deadlines (to OLD DB).')
async def importcalendar(ctx):
    await ctx.send(f"Okay, {ctx.author.mention}! Please **drag and drop** your `.ics` file and **type `!importcalendar` in the comment**.")

@bot.command(name='remindme', help='Sets a reminder (to OLD DB). Usage: !remindme <minutes> <task>')
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

@bot.command(name='remindat', help='Sets a reminder (to OLD DB). Usage: !remindat "<time>" <task>')
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

# --- +++ UPDATED COMMAND +++ ---
@bot.command(name='setreminder', help='(Owner) Sets an AGENT reminder. Usage: !setreminder <@user> "<time>" <task>')
async def setreminder(ctx, user: discord.User, time_str: str, *, task: str):
    if ctx.author.id != OWNER_USER_ID:
        await ctx.send("Sorry, this command is for the bot owner only."); return
    
    try:
        # 1. Parse time (no change here)
        remind_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})

        if not remind_time:
            await ctx.send(f'Sorry, I couldn\'t understand the time "{time_str}". Please try again.')
            return
            
        now = datetime.datetime.now(LOCAL_TZ)
        if remind_time <= now:
            await ctx.send(f"That time is in the past! (I understood that as: {remind_time.strftime('%Y-%m-%d %I:%M %p')}) Please provide a future time."); return
        
        # 2. --- THIS IS THE CHANGE ---
        # Call the new helper function instead of the old one
        # We'll set a default personality for now
        default_personality = "You are a persistent but friendly reminder bot. Your goal is to make sure the user does their task."
        
        if await create_agent_reminder(user.id, ctx.channel.id, remind_time, task, default_personality):
             # This is the confirmation to you, the owner 
             await ctx.send(f"✅ Agent reminder set for {user.mention} to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
        else: 
            await ctx.send("Sorry, I had an error saving that AGENT reminder to the database.")
    
    except Exception as e: 
        print(f"[Log] ERROR in !setreminder: {e}")
        await ctx.send(f"An error occurred: {e}")
# --- +++ END UPDATED COMMAND +++ ---


# --- Admin Commands (No changes here, they still manage the OLD DB) ---
async def get_reminder_by_short_id(user_id, short_id):
    try:
        response = db_table.query(
            KeyConditionExpression='user_id = :uid',
            ExpressionAttributeValues={':uid': str(user_id)}
        )
        items = response.get('Items', [])
        for item in items:
            if item['reminder_id'].startswith(short_id): return item
        return None
    except Exception as e:
        print(f"[Log] Error in get_reminder_by_short_id: {e}"); return None

@bot.command(name='deletereminder', help='(Owner) Deletes an (OLD) reminder. Usage: !deletereminder <id>')
async def deletereminder(ctx, short_id: str):
    if ctx.author.id != OWNER_USER_ID: return
    
    response = db_table.scan(
        FilterExpression='begins_with(reminder_id, :sid)',
        ExpressionAttributeValues={':sid': short_id}
    )
    items = response.get('Items', [])
    if not items:
        await ctx.send(f"I couldn't find an (old) reminder with an ID starting with `{short_id}`."); return
    if len(items) > 1:
        await ctx.send(f"That ID is ambiguous and matches {len(items)} reminders. Please be more specific."); return
    
    item = items[0]
    try:
        db_table.delete_item(Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']})
        await ctx.send(f"✅ Successfully deleted (old) reminder: **{item['task']}** (for user <@{item['user_id']}>)")
    except Exception as e: await ctx.send(f"An error occurred while deleting: {e}")

@bot.command(name='updatetask', help='(Owner) Updates an (OLD) task. Usage: !updatetask <id> <new task>')
async def updatetask(ctx, short_id: str, *, new_task: str):
    if ctx.author.id != OWNER_USER_ID: return

    response = db_table.scan(
        FilterExpression='begins_with(reminder_id, :sid)',
        ExpressionAttributeValues={':sid': short_id}
    )
    items = response.get('Items', [])
    if not items:
        await ctx.send(f"I couldn't find an (old) reminder with an ID starting with `{short_id}`."); return
    if len(items) > 1:
        await ctx.send(f"That ID is ambiguous and matches {len(items)} reminders. Please be more specific."); return
    
    item = items[0]
    try:
        db_table.update_item(
            Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']},
            UpdateExpression="set task = :t", ExpressionAttributeValues={':t': new_task}
        )
        await ctx.send(f"✅ (Old) Task updated for `{short_id}`!\n**Old:** {item['task']}\n**New:** {new_task}")
    except Exception as e: await ctx.send(f"An error occurred while updating: {e}")

@bot.command(name='updatetime', help='(Owner) Updates an (OLD) time. Usage: !updatetime <id> "<time>"')
async def updatetime(ctx, short_id: str, time_str: str):
    if ctx.author.id != OWNER_USER_ID: return
    
    response = db_table.scan(
        FilterExpression='begins_with(reminder_id, :sid)',
        ExpressionAttributeValues={':sid': short_id}
    )
    items = response.get('Items', [])
    if not items:
        await ctx.send(f"I couldn't find an (old) reminder with an ID starting with `{short_id}`."); return
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
        
        db_table.delete_item(Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']})
        item['remind_time_utc'] = new_remind_time_iso 
        db_table.put_item(Item=item)
        
        new_time_discord = f"<t:{int(new_remind_time.timestamp())}:f>"
        await ctx.send(f"✅ (Old) Time updated for **{item['task']}**!\n**New Time:** {new_time_discord}")
    
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
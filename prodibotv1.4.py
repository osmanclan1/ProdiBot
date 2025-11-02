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
import boto3        # <-- NEW: AWS SDK
import uuid         # <-- NEW: To create unique reminder IDs
import shlex        # <-- NEW: To safely parse update commands

# --- Configuration ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# --- NEW: DynamoDB Setup ---
# Boto3 will automatically use the IAM Role from our EC2 instance
try:
    dynamodb = boto3.resource('dynamodb')
    DYNAMO_TABLE_NAME = 'ProdiBotReminders'
    DYNAMO_GSI_NAME = 'RemindersByStatusAndTime'
    db_table = dynamodb.Table(DYNAMO_TABLE_NAME)
    print(f"Successfully connected to DynamoDB table: {DYNAMO_TABLE_NAME}")
except Exception as e:
    print(f"ERROR: Could not connect to DynamoDB. {e}")
    print("       Did you attach the IAM Role to the EC2 instance?")
    exit()

OWNER_USER_ID = 321078607772385280 

if not DISCORD_TOKEN or not OPENAI_API_KEY:
    print("="*50)
    print("ERROR: DISCORD_TOKEN or OPENAI_API_KEY is missing.")
    print("       Make sure they are set as Environment Variables in your host.")
    print("="*50)
    exit()

try:
    client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    print(f"Error initializing OpenAI client: {e}")
    exit()

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- Bot's "Memory" ---
# REPLACED: The reminders = [] list is now GONE.
active_followups = {} # This is still in-memory, which is fine!
RE_REMINDER_PHRASES = [
    "Just a friendly nudge!",
    "How's that task coming along?",
    "Just checking in on this again.",
    "Hope you haven't forgotten about this!",
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
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            max_tokens=5,
            temperature=0.0
        )
        response_text = completion.choices[0].message.content.strip()
        
        if response_text == "[TASK_DONE]":
            print("[Log] AI classified as: [TASK_DONE]")
            return "[TASK_DONE]"
        else:
            print("[Log] AI classified as: [TASK_NOT_DONE]")
            return "[TASK_NOT_DONE]"
    except Exception as e:
        print(f"[Log] ERROR calling OpenAI for classification: {e}")
        return "[TASK_NOT_DONE]"

# --- Helper function to add reminders to DB ---
async def add_reminder_to_db(author_id, channel_id, remind_time, task):
    try:
        reminder_id = str(uuid.uuid4()) # Generate a unique ID
        remind_time_iso = remind_time.isoformat() # Convert time to string for DynamoDB

        db_table.put_item(
            Item={
                'user_id': str(author_id),
                'reminder_id': reminder_id,
                'channel_id': str(channel_id),
                'remind_time_utc': remind_time_iso,
                'task': task,
                'status': 'PENDING' # This is the GSI Partition Key!
            }
        )
        print(f"[Log] Added reminder to DB. User: {author_id}, ID: {reminder_id}")
        return True
    except Exception as e:
        print(f"[Log] ERROR adding reminder to DB: {e}")
        return False

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    print('Bot is ready to receive commands.')
    check_reminders.start()
    check_followups.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # --- Handle calendar file upload ---
    if message.attachments and message.content == "!importcalendar":
        if message.author.id != OWNER_USER_ID:
             await message.channel.send("Sorry, only my owner can import a calendar.")
             return
        attachment = message.attachments[0]
        if attachment.filename.endswith(".ics"):
            await message.add_reaction("🔄") 
            try:
                file_content = await attachment.read()
                gcal = Calendar.from_ical(file_content)
                reminders_added = 0
                reminders_past = 0
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                
                for component in gcal.walk():
                    if component.name == "VEVENT":
                        summary = str(component.get('summary'))
                        dtstart = component.get('dtstart').dt
                        if isinstance(dtstart, datetime.datetime):
                            if dtstart.tzinfo: dtstart_utc = dtstart.astimezone(pytz.utc)
                            else: dtstart_utc = dtstart.replace(tzinfo=pytz.utc)
                        elif isinstance(dtstart, datetime.date):
                            dtstart_utc = datetime.datetime.combine(dtstart, datetime.time(23, 59, 59), tzinfo=pytz.utc)
                        else: continue
                        
                        remind_time = dtstart_utc - datetime.timedelta(hours=24)
                        
                        if remind_time > now_utc:
                            # --- REFACTORED ---
                            await add_reminder_to_db(message.author.id, message.channel.id, remind_time, f"(From Calendar) {summary}")
                            reminders_added += 1
                        else:
                            reminders_past += 1
                
                print(f"[Log] Calendar processed for {message.author.name}. Added {reminders_added}, Skipped {reminders_past}.")
                await message.channel.send(f"✅ Calendar imported! I added **{reminders_added}** new reminders. I skipped {reminders_past} events in the past.")
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("✅")
            except Exception as e:
                print(f"[Log] FAILED to parse calendar: {e}"); await message.channel.send(f"❌ Error parsing `.ics` file. Error: {e}")
                await message.remove_reaction("🔄", bot.user); await message.add_reaction("❌")
        else: await message.channel.send("That doesn't look like an `.ics` file. Please upload a valid calendar file.")
        return 

    # --- Existing DM Follow-up Logic (No changes needed) ---
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
                random_minutes = random.randint(15, 45); next_remind_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=random_minutes)
                follow_up_data["status"] = "WAITING_TO_REMIND"; follow_up_data["next_remind_time"] = next_remind_time
                print(f"[Log] User {user_id} not done. Next check-in at {next_remind_time.isoformat()}")

    if not (message.attachments and message.content == "!importcalendar"):
        await bot.process_commands(message)

# --- Background Loops ---
@tasks.loop(seconds=15) # Check every 15s is fine now
async def check_reminders():
    now_utc_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    # --- REFACTORED: Query the GSI ---
    try:
        response = db_table.query(
            IndexName=DYNAMO_GSI_NAME,
            KeyConditionExpression='status = :s AND remind_time_utc <= :now',
            ExpressionAttributeValues={
                ':s': 'PENDING',
                ':now': now_utc_iso
            }
        )
        
        due_reminders = response.get('Items', [])
        
        for reminder in due_reminders:
            try:
                task = reminder['task']
                author_id = int(reminder['user_id'])
                reminder_id = reminder['reminder_id']
                
                print(f"\n[Log] Processing DB reminder for task: \"{task}\"")
                user = await bot.fetch_user(author_id)
                
                if user:
                    # Check for duplicates in *local memory* before sending DM
                    if author_id in active_followups and active_followups[author_id]['task'] == task:
                        print(f"[Log] User {author_id} already in active follow-up. Deleting duplicate DB entry.")
                    else:
                        # Send DM and add to local followup memory
                        await user.send(f"Hey {user.mention}, this is your reminder to: **{task}**\n\nDid you get that done?")
                        active_followups[author_id] = {
                            "task": task, "status": "WAITING_FOR_REPLY", "next_remind_time": None
                        }
                        print(f"[Log] Added user {author_id} to active follow-up list.")
                
                # --- Delete reminder from DB after processing ---
                db_table.delete_item(
                    Key={'user_id': str(author_id), 'reminder_id': reminder_id}
                )
                print(f"[Log] Deleted reminder {reminder_id} from DB.")

            except (discord.errors.Forbidden, discord.errors.NotFound):
                print(f"[Log] ERROR: Could not find or DM user {author_id}. Deleting from DB.")
                db_table.delete_item(Key={'user_id': reminder['user_id'], 'reminder_id': reminder['reminder_id']})
            except Exception as e:
                print(f"[Log] An unexpected error occurred in check_reminders sub-loop: {e}")
                # Still delete it to avoid error loops
                db_table.delete_item(Key={'user_id': reminder['user_id'], 'reminder_id': reminder['reminder_id']})

    except Exception as e:
        print(f"[Log] An unexpected error occurred querying DynamoDB: {e}")

@check_reminders.before_loop
async def before_check_reminders():
    await bot.wait_until_ready()
    print("Reminder check loop is starting.")

@tasks.loop(seconds=30)
async def check_followups():
    # This loop is unchanged, as active_followups is still in-memory
    now = datetime.datetime.now(datetime.timezone.utc)
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

@bot.command(name='listreminders', help='(Owner only) Lists all upcoming reminders from the database.')
async def listreminders(ctx):
    if ctx.author.id != OWNER_USER_ID:
        await ctx.send("Sorry, this command is for the bot owner only.")
        return
    
    try:
        # --- REFACTORED: Query DynamoDB ---
        response = db_table.query(
            KeyConditionExpression='user_id = :uid',
            ExpressionAttributeValues={':uid': str(ctx.author.id)}
        )
        items = response.get('Items', [])
        
        if not items:
            await ctx.send("You have no reminders in the database!")
            return
        
        # Sort by time (since they're no longer sorted in memory)
        items.sort(key=lambda r: r['remind_time_utc'])
        
        response_message = f"**You have {len(items)} upcoming reminders in the DB:**\n\n"
        
        for i, item in enumerate(items):
            task = item['task']
            if len(task) > 50: task = task[:50] + "..."
            
            # Convert ISO string time back to datetime object
            remind_time_obj = datetime.datetime.fromisoformat(item['remind_time_utc'])
            time_str = f"<t:{int(remind_time_obj.timestamp())}:f>"
            
            # --- NEW: Show the ID for editing ---
            reminder_id_short = item['reminder_id'].split('-')[0] # Show first part of UUID
            
            response_message += f"**{i+1}.** {task}\n    *Due: {time_str}*\n    *ID: `{reminder_id_short}`*\n"
            
            if len(response_message) > 1800:
                await ctx.send(response_message); response_message = ""
                
        if response_message:
            await ctx.send(response_message)
            
    except Exception as e:
        await ctx.send(f"An error occurred while fetching reminders: {e}")

@bot.command(name='importcalendar', help='Upload your .ics calendar file to import all deadlines.')
async def importcalendar(ctx):
    await ctx.send(f"Okay, {ctx.author.mention}! Please **drag and drop** your `.ics` file and **type `!importcalendar` in the comment**.")

@bot.command(name='remindme', help='Sets a reminder. Usage: !remindme <minutes> <task>')
async def remindme(ctx, minutes: int, *, task: str):
    try:
        if minutes <= 0:
            await ctx.send("Please provide a positive number of minutes!"); return
        now = datetime.datetime.now(datetime.timezone.utc)
        remind_time = now + datetime.timedelta(minutes=minutes)
        
        # --- REFACTORED ---
        if await add_reminder_to_db(ctx.author.id, ctx.channel.id, remind_time, task):
            await ctx.send(f"Okay, {ctx.author.mention}! I'll **DM you** to remind you to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
        else:
            await ctx.send("Sorry, I had an error saving that reminder to the database.")
            
    except ValueError: await ctx.send("Invalid number of minutes. Please enter a number.")
    except Exception as e: await ctx.send(f"An error occurred: {e}")

@bot.command(name='remindat', help='Sets a reminder. Usage: !remindat "<YYYY-MM-DD HH:MM>" <task>')
async def remindat(ctx, time_str: str, *, task: str):
    try:
        remind_time = datetime.datetime.strptime(time_str, "%Y-MM-DD %H:%M")
        remind_time = remind_time.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        if remind_time <= now:
            await ctx.send("That time is in the past!"); return
        
        # --- REFACTORED ---
        if await add_reminder_to_db(ctx.author.id, ctx.channel.id, remind_time, task):
             await ctx.send(f"Got it, {ctx.author.mention}! I'll **DM you** to remind you to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
        else:
            await ctx.send("Sorry, I had an error saving that reminder to the database.")

    except ValueError: await ctx.send('Invalid time format! Please use `"YYYY-MM-DD HH:MM"` (and make sure it\'s in UTC).')
    except Exception as e: await ctx.send(f"An error occurred: {e}")

# --- NEW COMMANDS ---

async def get_reminder_by_short_id(user_id, short_id):
    """Helper to find a reminder ID by its short prefix."""
    try:
        response = db_table.query(
            KeyConditionExpression='user_id = :uid',
            ExpressionAttributeValues={':uid': str(user_id)}
        )
        items = response.get('Items', [])
        for item in items:
            if item['reminder_id'].startswith(short_id):
                return item # Return the full item
        return None
    except Exception as e:
        print(f"[Log] Error in get_reminder_by_short_id: {e}")
        return None

@bot.command(name='deletereminder', help='Deletes a reminder. Usage: !deletereminder <id>')
async def deletereminder(ctx, short_id: str):
    if ctx.author.id != OWNER_USER_ID: return # Owner only
    
    item = await get_reminder_by_short_id(ctx.author.id, short_id)
    if not item:
        await ctx.send(f"I couldn't find a reminder with an ID starting with `{short_id}`. Use `!listreminders` to see IDs.")
        return
        
    try:
        db_table.delete_item(
            Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']}
        )
        await ctx.send(f"✅ Successfully deleted reminder: **{item['task']}**")
    except Exception as e:
        await ctx.send(f"An error occurred while deleting: {e}")

@bot.command(name='updatetask', help='Updates a reminder task. Usage: !updatetask <id> <new task>')
async def updatetask(ctx, short_id: str, *, new_task: str):
    if ctx.author.id != OWNER_USER_ID: return
    
    item = await get_reminder_by_short_id(ctx.author.id, short_id)
    if not item:
        await ctx.send(f"I couldn't find a reminder with an ID starting with `{short_id}`. Use `!listreminders` to see IDs.")
        return
        
    try:
        db_table.update_item(
            Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']},
            UpdateExpression="set task = :t",
            ExpressionAttributeValues={':t': new_task}
        )
        await ctx.send(f"✅ Task updated for `{short_id}`!\n**Old:** {item['task']}\n**New:** {new_task}")
    except Exception as e:
        await ctx.send(f"An error occurred while updating: {e}")

@bot.command(name='updatetime', help='Updates a reminder time. Usage: !updatetime <id> "<YYYY-MM-DD HH:MM>"')
async def updatetime(ctx, short_id: str, time_str: str):
    if ctx.author.id != OWNER_USER_ID: return
    
    item = await get_reminder_by_short_id(ctx.author.id, short_id)
    if not item:
        await ctx.send(f"I couldn't find a reminder with an ID starting with `{short_id}`. Use `!listreminders` to see IDs.")
        return
        
    try:
        new_remind_time = datetime.datetime.strptime(time_str, "%Y-m-%d %H:%M").replace(tzinfo=datetime.timezone.utc)
        new_remind_time_iso = new_remind_time.isoformat()
        
        # Easiest way to update a GSI key is to delete and re-create
        # 1. Delete the old item
        db_table.delete_item(
            Key={'user_id': item['user_id'], 'reminder_id': item['reminder_id']}
        )
        # 2. Create a new item (with the same ID)
        item['remind_time_utc'] = new_remind_time_iso # Update the time
        db_table.put_item(Item=item) # Put the whole item back
        
        new_time_discord = f"<t:{int(new_remind_time.timestamp())}:f>"
        await ctx.send(f"✅ Time updated for **{item['task']}**!\n**New Time:** {new_time_discord}")
        
    except ValueError:
        await ctx.send('Invalid time format! Please use `"YYYY-MM-DD HH:MM"` (and make sure it\'s in UTC).')
    except Exception as e:
        await ctx.send(f"An error occurred while updating: {e}")


# --- Run the Bot ---
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("="*50); print("ERROR: Invalid DISCORD_TOKEN."); print("="*50)
    except Exception as e:
        print(f"An error occurred while running the bot: {e}")

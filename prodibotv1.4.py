import discord
from discord.ext import tasks, commands
import datetime
import asyncio
import os
import random
from openai import OpenAI
from icalendar import Calendar  # <-- NEW: Import the calendar library
import pytz                     # <-- NEW: For handling timezones
import io                       # <-- NEW: To read the uploaded file

# --- Configuration ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

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
reminders = []
active_followups = {}
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

    # --- NEW: Handle calendar file upload ---
    # This checks if a message has an attachment AND the comment is "!importcalendar"
    if message.attachments and message.content == "!importcalendar":
        if message.author.id != 321078607772385280: # Optional: Lock to your user ID
             await message.channel.send("Sorry, only my owner can import a calendar.")
             return

        attachment = message.attachments[0]
        if attachment.filename.endswith(".ics"):
            await message.add_reaction("🔄") # Add a "processing" reaction
            
            try:
                file_content = await attachment.read()
                calendar_data = io.BytesIO(file_content)
                
                # Parse the calendar
                gcal = Calendar.from_ical(file_content)
                
                reminders_added = 0
                reminders_past = 0
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                
                for component in gcal.walk():
                    if component.name == "VEVENT":
                        summary = str(component.get('summary'))
                        dtstart = component.get('dtstart').dt

                        # Convert datetime to UTC
                        if isinstance(dtstart, datetime.datetime):
                            # If it's "timezone aware", convert to UTC
                            if dtstart.tzinfo:
                                dtstart_utc = dtstart.astimezone(pytz.utc)
                            else:
                                # If it's "naive", assume it's UTC (less common for .ics)
                                dtstart_utc = dtstart.replace(tzinfo=pytz.utc)
                        elif isinstance(dtstart, datetime.date):
                            # Handle "all-day" events, assume end of day in UTC
                            dtstart_utc = datetime.datetime.combine(dtstart, datetime.time(23, 59, 59), tzinfo=pytz.utc)
                        else:
                            continue

                        # Set reminder 24 hours BEFORE the deadline
                        remind_time = dtstart_utc - datetime.timedelta(hours=24)

                        # Only add reminders that are in the future
                        if remind_time > now_utc:
                            reminders.append({
                                'author_id': message.author.id,
                                'channel_id': message.channel.id, 
                                'time': remind_time,
                                'task': f"(From Calendar) {summary}"
                            })
                            reminders_added += 1
                        else:
                            reminders_past += 1

                print(f"[Log] Calendar processed for {message.author.name}.")
                print(f"[Log] Added {reminders_added} new reminders. Skipped {reminders_past} past reminders.")
                
                await message.channel.send(f"✅ Calendar imported! I added **{reminders_added}** new reminders. I skipped {reminders_past} events that were already in the past.")
                await message.remove_reaction("🔄", bot.user)
                await message.add_reaction("✅")
                
            except Exception as e:
                print(f"[Log] FAILED to parse calendar: {e}")
                await message.channel.send(f"❌ I couldn't read that `.ics` file. Something went wrong. Error: {e}")
                await message.remove_reaction("🔄", bot.user)
                await message.add_reaction("❌")
        else:
            await message.channel.send("That doesn't look like an `.ics` file. Please upload a valid calendar file.")
        return # Stop processing so it doesn't check for followups

    # --- Existing DM Follow-up Logic ---
    if isinstance(message.channel, discord.DMChannel) and message.author.id in active_followups:
        user_id = message.author.id
        follow_up_data = active_followups[user_id]

        if follow_up_data["status"] == "WAITING_FOR_REPLY":
            user_reply = message.content
            async with message.channel.typing():
                status = await get_task_status_from_ai(user_reply)
            
            if status == "[TASK_DONE]":
                await message.channel.send("Great job! Way to get it done. I'll check this off the list. ✅")
                del active_followups[user_id]
                print(f"[Log] Task complete for user {user_id}. Removed from active list.")
            else:
                await message.channel.send("Okay, no worries. I'll check in with you again in a bit!")
                random_minutes = random.randint(15, 45)
                next_remind_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=random_minutes)
                follow_up_data["status"] = "WAITING_TO_REMIND"
                follow_up_data["next_remind_time"] = next_remind_time
                print(f"[Log] User {user_id} has not completed task. Next check-in at {next_remind_time.isoformat()}")

    # Only process commands if it wasn't a calendar upload
    if not (message.attachments and message.content == "!importcalendar"):
        await bot.process_commands(message)

# --- Background Loops ---
@tasks.loop(seconds=1)
async def check_reminders():
    now = datetime.datetime.now(datetime.timezone.utc)
    for reminder in reminders[:]:
        if reminder['time'] <= now:
            try:
                print(f"\n[Log] Processing INITIAL reminder for task: \"{reminder['task']}\"")
                author_id = reminder['author_id']
                user = await bot.fetch_user(author_id)
                if user:
                    # Check if user is already in a followup for this *exact* task
                    if user.id in active_followups and active_followups[user.id]['task'] == reminder['task']:
                        print(f"[Log] User {user.id} is already in an active follow-up for this task. Skipping duplicate reminder.")
                        reminders.remove(reminder)
                        continue

                    await user.send(f"Hey {user.mention}, this is your reminder to: **{reminder['task']}**\n\nDid you get that done?")
                    active_followups[user.id] = {
                        "task": reminder['task'],
                        "status": "WAITING_FOR_REPLY",
                        "next_remind_time": None
                    }
                    print(f"[Log] Added user {user.id} to active follow-up list.")
                reminders.remove(reminder)
            except (discord.errors.Forbidden, discord.errors.NotFound):
                print(f"[Log] ERROR: Could not find or DM user {author_id}. Removing reminder.")
                if reminder in reminders:
                    reminders.remove(reminder)
            except Exception as e:
                print(f"[Log] An unexpected error occurred in check_reminders: {e}")
                if reminder in reminders:
                    reminders.remove(reminder)

@check_reminders.before_loop
async def before_check_reminders():
    await bot.wait_until_ready()
    print("Reminder check loop is starting.")

@tasks.loop(seconds=30)
async def check_followups():
    now = datetime.datetime.now(datetime.timezone.utc)
    for user_id, data in list(active_followups.items()):
        if data["status"] == "WAITING_TO_REMIND" and data["next_remind_time"] <= now:
            print(f"[Log] Re-reminding user {user_id} for task: {data['task']}")
            try:
                user = await bot.fetch_user(user_id)
                if user:
                    phrase = random.choice(RE_REMINDER_PHRASES)
                    await user.send(f"Hey! Just checking in on that task: **{data['task']}**\n\n{phrase}")
                    data["status"] = "WAITING_FOR_REPLY"
                    data["next_remind_time"] = None
                    print(f"[Log] Re-reminder sent. User {user_id} is now 'WAITING_FOR_REPLY'")
            except (discord.errors.Forbidden, discord.errors.NotFound):
                print(f"[Log] Could not find or DM user {user_id} for follow-up. Removing.")
                if user_id in active_followups:
                    del active_followups[user_id]
            except Exception as e:
                print(f"[Log] Error in check_followups loop: {e}")

@check_followups.before_loop
async def before_check_followups():
    await bot.wait_until_ready()
    print("Follow-up check loop is starting.")

# --- Bot Commands ---

# --- NEW CALENDAR COMMAND ---
@bot.command(name='importcalendar', help='Upload your .ics calendar file to import all deadlines.')
async def importcalendar(ctx):
    # This command now just prompts the user.
    # The actual logic is in the on_message event handler.
    await ctx.send(f"Okay, {ctx.author.mention}! Please **drag and drop** your `.ics` calendar file into this channel and **type `!importcalendar` in the comment box** when you upload it.")

@bot.command(name='remindme', help='Sets a reminder for a number of minutes from now. Usage: !remindme <minutes> <task>')
async def remindme(ctx, minutes: int, *, task: str):
    try:
        if minutes <= 0:
            await ctx.send("Please provide a positive number of minutes!")
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        remind_time = now + datetime.timedelta(minutes=minutes)
        reminders.append({
            'author_id': ctx.author.id,
            'channel_id': ctx.channel.id,
            'time': remind_time,
            'task': task
        })
        print(f"[Log] Reminder set for {ctx.author.name} at {remind_time.isoformat()}")
        await ctx.send(f"Okay, {ctx.author.mention}! I'll **DM you** to remind you to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
    except ValueError:
        await ctx.send("Invalid number of minutes. Please enter a number.")
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")

@bot.command(name='remindat', help='Sets a reminder at a specific time. Usage: !remindat "<YYYY-MM-DD HH:MM>" <task>')
async def remindat(ctx, time_str: str, *, task: str):
    try:
        remind_time = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        remind_time = remind_time.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        if remind_time <= now:
            await ctx.send("That time is in the past! Please provide a future time.")
            return
        reminders.append({
            'author_id': ctx.author.id,
            'channel_id': ctx.channel.id,
            'time': remind_time,
            'task': task
        })
        print(f"[Log] Reminder set for {ctx.author.name} at {remind_time.isoformat()}")
        await ctx.send(f"Got it, {ctx.author.mention}! I'll **DM you** to remind you to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
    except ValueError:
        await ctx.send('Invalid time format! Please use `"YYYY-MM-DD HH:MM"` (and make sure it\'s in UTC).')
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")

# --- Run the Bot ---
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("="*50)
        print("ERROR: Invalid DISCORD_TOKEN.")
        print("       Please check your token and make sure it is set in your host.")
        print("="*50)
    except Exception as e:
        print(f"An error occurred while running the bot: {e}")


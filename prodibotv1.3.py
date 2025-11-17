import discord
from discord.ext import tasks, commands
import datetime
import asyncio
import os      # <-- Import the Operating System library
import random
from openai import OpenAI

# --- Configuration ---
# NEW: Read keys safely from the environment.
# We will set these in the AWS server, NOT here.
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Check if the keys are missing
if not DISCORD_TOKEN or not OPENAI_API_KEY:
    print("="*50)
    print("ERROR: DISCORD_TOKEN or OPENAI_API_KEY is missing.")
    print("       Make sure they are set as Environment Variables in your host.")
    print("="*50)
    exit()

# --- Everything below this line is your bot code from v1.3 ---

# Setup OpenAI client
try:
    client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    print(f"Error initializing OpenAI client: {e}")
    exit()

# Setup Discord Bot
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


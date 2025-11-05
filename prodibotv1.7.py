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
import json

# --- Configuration ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OWNER_USER_ID = int(os.environ.get("OWNER_USER_ID", 0))

# --- AWS Configuration ---
LAMBDA_ARN = os.environ.get("LAMBDA_ARN")
LAMBDA_ROLE_ARN = os.environ.get("LAMBDA_ROLE_ARN")
OUTBOX_QUEUE_URL = os.environ.get("OUTBOX_QUEUE_URL")
MINI_INBOX_QUEUE_URL = os.environ.get("MINI_INBOX_QUEUE_URL")
AWS_REGION = "us-east-1" # Or your preferred region

# --- Set our "home" timezone ---
LOCAL_TZ = pytz.timezone('America/Chicago')

# --- Check for all required environment variables ---
if not all([DISCORD_TOKEN, OPENAI_API_KEY, OWNER_USER_ID, LAMBDA_ARN, LAMBDA_ROLE_ARN, OUTBOX_QUEUE_URL, MINI_INBOX_QUEUE_URL]):
    print("="*50)
    print("ERROR: A required environment variable is missing!")
    print("Check: DISCORD_TOKEN, OPENAI_API_KEY, OWNER_USER_ID, LAMBDA_ARN, LAMBDA_ROLE_ARN, OUTBOX_QUEUE_URL, MINI_INBOX_QUEUE_URL")
    print("="*50); exit()

# --- Initialize AWS Clients ---
try:
    dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
    scheduler = boto3.client('scheduler', region_name=AWS_REGION)
    sqs = boto3.client('sqs', region_name=AWS_REGION)
    
    IDENTITY_TABLE_NAME = "MiniBotIdentity"
    identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
    
    print("Successfully connected to DynamoDB, Scheduler, and SQS.")
except Exception as e:
    print(f"ERROR: Could not connect to AWS services. {e}"); exit()

# --- OpenAI Client ---
try:
    client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    print(f"Error initializing OpenAI client: {e}"); exit()

# --- Bot Setup ---
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True
intents.dm_messages = True # <-- Required for DM replies

bot = commands.Bot(command_prefix="!", intents=intents)

# --- +++ NEW HELPER FUNCTION (Creates Schedule) +++ ---
async def create_agent_reminder(user_id, channel_id, remind_time, task_goal, personality):
    """
    Uses EventBridge Scheduler to create a one-time schedule
    that will trigger our MiniControllerLambda.
    """
    try:
        user_id_str = str(user_id)
        
        # 1. Create/Update the MiniBotIdentity item
        print(f"[Log] Creating/Updating Bot Identity for user: {user_id_str}")
        identity_table.put_item(
            Item={
                'user_id': user_id_str,
                'status': 'ACTIVE',
                'goal': task_goal,
                'last_result': 'N/A',
                'notes': 'Task initiated by owner.',
                'personality': personality,
                'created_at': datetime.datetime.now(LOCAL_TZ).isoformat()
            }
        )
        
        # 2. Create the EventBridge Schedule
        print(f"[Log] Creating EventBridge Schedule for user: {user_id_str}")
        schedule_name = str(uuid.uuid4())
        
        # EventBridge needs time in UTC and in a specific format
        remind_time_utc = remind_time.astimezone(datetime.timezone.utc)
        schedule_expression = f"at({remind_time_utc.strftime('%Y-%m-%dT%H:%M:%S')})"

        # This is the JSON payload that will be sent to the Lambda
        lambda_input = json.dumps({
            "type": "WAKEUP", # This tells the Lambda it's a new task
            "user_id": user_id_str,
            "channel_id": str(channel_id),
            "goal": task_goal
        })

        # Use asyncio.to_thread to run the blocking boto3 call
        await asyncio.to_thread(
            scheduler.create_schedule,
            Name=schedule_name,
            ScheduleExpression=schedule_expression,
            ScheduleExpressionTimezone="UTC",
            ActionAfterCompletion="DELETE",  # One-time schedule
            Target={
                'Arn': LAMBDA_ARN,
                'RoleArn': LAMBDA_ROLE_ARN,
                'Input': lambda_input
            },
            FlexibleTimeWindow={'Mode': 'OFF'} # Precision
        )
        
        print(f"[Log] ✅ Agent reminder created. Schedule: {schedule_name} at: {remind_time.isoformat()}")
        return True
        
    except Exception as e:
        print(f"[Log] ❌ ERROR in create_agent_reminder: {e}")
        return False
# --- +++ END NEW FUNCTION +++ ---


# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})'); print('Bot is ready.')
    # --- +++ START NEW BACKGROUND LOOP +++ ---
    poll_outbox_queue.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # --- +++ NEW DM HANDLER +++ ---
    # Check if the message is a DM
    if isinstance(message.channel, discord.DMChannel):
        print(f"[Log] Received DM from {message.author.name} (ID: {message.author.id})")
        
        # This is a reply to our agent. Send it to the mini_inbox_queue
        try:
            message_body = json.dumps({
                "type": "REPLY", # Tells the Lambda this is a user reply
                "user_id": str(message.author.id),
                "message_content": message.content,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
            })
            
            await asyncio.to_thread(
                sqs.send_message,
                QueueUrl=MINI_INBOX_QUEUE_URL,
                MessageBody=message_body
            )
            print(f"[Log] Sent DM reply to mini_inbox_queue for user {message.author.id}")
        except Exception as e:
            print(f"[Log] ❌ ERROR sending DM to SQS: {e}")
            
        return # Stop processing, DMs shouldn't trigger commands

    # --- Process server commands ---
    await bot.process_commands(message)

# --- +++ NEW BACKGROUND TASK (Polls SQS Outbox) +++ ---
@tasks.loop(seconds=5) # Poll every 5 seconds
async def poll_outbox_queue():
    try:
        # Long poll SQS for up to 20 seconds
        response = await asyncio.to_thread(
            sqs.receive_message,
            QueueUrl=OUTBOX_QUEUE_URL,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=20
        )
        
        messages = response.get('Messages', [])
        if not messages:
            return # No messages, loop again
            
        print(f"[Log] Received {len(messages)} message(s) from outbox_queue.")
        
        for msg in messages:
            try:
                body = json.loads(msg['Body'])
                message_content = body['message']
                user_id = body.get('user_id') # Use .get() for safety
                channel_id = body.get('channel_id')
                
                # --- Message Sending Logic ---
                target = None
                if user_id: # Prioritize DMing the user
                    try:
                        target = await bot.fetch_user(int(user_id))
                    except (discord.NotFound, discord.Forbidden):
                         print(f"[Log] Could not fetch user {user_id}. Attempting channel fallback.")
                
                if target is None and channel_id: # Fallback to public channel
                    try:
                        target = await bot.fetch_channel(int(channel_id))
                    except (discord.NotFound, discord.Forbidden):
                        print(f"[Log] Could not fetch channel {channel_id}. Message lost.")
                        
                if target:
                    await target.send(message_content)
                    print(f"[Log] Sent message to {target.name}")
                
                # --- Delete message from SQS ---
                await asyncio.to_thread(
                    sqs.delete_message,
                    QueueUrl=OUTBOX_QUEUE_URL,
                    ReceiptHandle=msg['ReceiptHandle']
                )
                
            except Exception as e:
                print(f"[Log] ❌ ERROR processing message from outbox: {e}")
                # Don't delete the message, let it retry
                
    except Exception as e:
        print(f"[Log] ❌ ERROR in SQS poll loop: {e}")

@poll_outbox_queue.before_loop
async def before_poll_outbox_queue():
    await bot.wait_until_ready()
    print("SQS outbox polling loop is starting.")
# --- +++ END NEW BACKGROUND TASK +++ ---


# --- +++ UPDATED COMMAND +++ ---
@bot.command(name='setreminder', help='(Owner) Sets an AGENT reminder. Usage: !setreminder <@user> "<time>" <task>')
async def setreminder(ctx, user: discord.User, time_str: str, *, task: str):
    if ctx.author.id != OWNER_USER_ID:
        await ctx.send("Sorry, this command is for the bot owner only."); return
    
    try:
        # 1. Parse time
        remind_time = dateparser.parse(time_str, settings={'TIMEZONE': 'America/Chicago', 'RETURN_AS_TIMEZONE_AWARE': True})
        if not remind_time:
            await ctx.send(f'Sorry, I couldn\'t understand the time "{time_str}". Please try again.')
            return
            
        now = datetime.datetime.now(LOCAL_TZ)
        if remind_time <= now:
            await ctx.send(f"That time is in the past! Please provide a future time."); return
        
        # 2. Call the new helper function
        default_personality = "You are a persistent but friendly reminder bot. Your goal is to make sure the user does their task."
        
        if await create_agent_reminder(user.id, ctx.channel.id, remind_time, task, default_personality):
             await ctx.send(f"✅ Agent reminder set for {user.mention} to **{task}** at <t:{int(remind_time.timestamp())}:f>.")
        else: 
            await ctx.send("Sorry, I had an error saving that AGENT reminder to the database.")
    
    except Exception as e: 
        print(f"[Log] ❌ ERROR in !setreminder: {e}")
        await ctx.send(f"An error occurred: {e}")
# --- +++ END UPDATED COMMAND +C --


# --- DEPRECATED/OLD COMMANDS ---
# (You can leave these or remove them)
@bot.command(name='remindme', help='(OLD) Sets a simple reminder. Usage: !remindme <minutes> <task>')
async def remindme(ctx, minutes: int, *, task: str):
    await ctx.send("This command is deprecated. Please use `!setreminder`.")

@bot.command(name='remindat', help='(OLD) Sets a simple reminder. Usage: !remindat "<time>" <task>')
async def remindat(ctx, time_str: str, *, task: str):
    await ctx.send("This command is deprecated. Please use `!setreminder`.")
    
@bot.command(name='listreminders', help='(OLD) Lists old reminders.')
async def listreminders(ctx):
     await ctx.send("This command is deprecated.")

# ... (you can remove all the other old helper functions and commands) ...


# --- Run the Bot ---
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("="*50); print("ERROR: Invalid DISCORD_TOKEN."); print("="*50)
    except Exception as e:
        print(f"An error occurred while running the bot: {e}")
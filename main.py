import os
import asyncio
from threading import Thread
from flask import Flask

# --- Telegram Libraries ---
# python-telegram-bot for the Controller Bot
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# telethon for the Worker User-Bot
from telethon.sync import TelegramClient

# --- Environment Variable Loading ---
# We load the variables from Render's environment settings.
API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
SESSION_STRING = os.environ.get('SESSION_STRING')

# --- Part 1: The Keep-Alive Web Server (for Render) ---
# Render's free tier sleeps after 15 mins of inactivity.
# This simple Flask server responds to pings from UptimeRobot to keep the bot alive.
app = Flask('')

@app.route('/')
def home():
    return "I am awake!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()
    print("Keep-alive server started.")

# --- Part 2: The Worker User-Bot (Telethon) ---
# This is the client that will perform the actions (forwarding, deleting).
# It's initialized here but won't do anything until we build the task queue in Part 2.
try:
    user_bot = TelegramClient(
        "telegram_butler_user",  # A session name, can be anything
        int(API_ID),
        API_HASH,
        # The system_version is specified to avoid a warning on some platforms
        system_version="4.16.30-vx-amd64" 
    ).start(session=SESSION_STRING)
    print("User-Bot client started successfully.")
except Exception as e:
    print(f"Error starting User-Bot client: {e}")
    user_bot = None

# --- Part 3: The Controller Bot (python-telegram-bot) ---
# This is the bot you will interact with.

# Command handler for /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Butler is online and at your service!')

# Command handler for /ping - a test command
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if user_bot:
        try:
            # The user_bot sends a message to its own "Saved Messages"
            await user_bot.send_message('me', 'Pong!')
            await update.message.reply_text('Pong! Check your "Saved Messages".')
            print("Ping command successful.")
        except Exception as e:
            await update.message.reply_text(f'Error communicating with User-Bot: {e}')
            print(f"Ping command error: {e}")
    else:
        await update.message.reply_text('User-Bot client is not connected. Cannot ping.')
        print("Ping command failed: User-Bot not connected.")

# --- Main execution block ---
def main() -> None:
    """Start the bot."""
    # Start the keep-alive server
    keep_alive()

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).build()

    # Register the command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ping", ping))

    # Start the Bot
    print("Controller Bot polling started.")
    application.run_polling()

if __name__ == '__main__':
    main()

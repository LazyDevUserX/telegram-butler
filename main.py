import os
import asyncio
from threading import Thread
from flask import Flask
import logging

# --- Telegram Libraries ---
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telethon.sync import TelegramClient
# NEW: Import StringSession to handle the session string correctly
from telethon.sessions import StringSession

# --- Basic Logging Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variable Loading ---
API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
SESSION_STRING = os.environ.get('SESSION_STRING')

# --- Part 1: The Keep-Alive Web Server (for Render) ---
app = Flask('')
@app.route('/')
def home():
    return "I am awake!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()
    logger.info("Keep-alive server started.")

# --- Part 2: The Worker User-Bot (Telethon) ---
user_bot = None
if SESSION_STRING:
    try:
        # CORRECTED: The session string is now passed into a StringSession object,
        # which is given as the first argument to the client.
        user_bot = TelegramClient(
            StringSession(SESSION_STRING),
            int(API_ID),
            API_HASH,
            system_version="4.16.30-vx-amd64"
        )
        user_bot.start() # The .start() method is now called without arguments.
        logger.info("User-Bot client started successfully.")
    except Exception as e:
        logger.error(f"Error starting User-Bot client: {e}")
        user_bot = None
else:
    logger.error("SESSION_STRING is not set. User-Bot cannot start.")


# --- Part 3: The Controller Bot (python-telegram-bot) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Butler is online and at your service!')

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if user_bot and user_bot.is_connected():
        try:
            await user_bot.send_message('me', 'Pong!')
            await update.message.reply_text('Pong! Check your "Saved Messages".')
            logger.info("Ping command successful.")
        except Exception as e:
            await update.message.reply_text(f'Error communicating with User-Bot: {e}')
            logger.error(f"Ping command error: {e}")
    else:
        await update.message.reply_text('User-Bot client is not connected. Cannot ping.')
        logger.warning("Ping command failed: User-Bot not connected.")

# --- Main execution block ---
def main() -> None:
    keep_alive()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ping", ping))

    logger.info("Controller Bot polling started.")
    application.run_polling()

if __name__ == '__main__':
    main()


# --- main.py v2.1 (with enhanced logging) ---
# Adds more log messages to help us debug startup issues.

import os
import asyncio
import logging
from flask import Flask
from threading import Thread
from telethon.sync import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import FloodWaitError
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Configuration ---
API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
SESSION_STRING = os.environ.get('SESSION_STRING')

# --- Logging Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
LOGGER = logging.getLogger(__name__)

# --- Flask Web Server for Render Health Check ---
app = Flask('')
@app.route('/')
def home():
    return "I am awake!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()
    LOGGER.info("Keep-alive server started.")

# --- Task Queue and Worker Setup ---
task_queue = asyncio.Queue()

async def worker(user_bot):
    LOGGER.info("Worker started, waiting for tasks.")
    while True:
        try:
            task = await task_queue.get()
            chat_id = task['chat_id']
            source_id = task['source_id']
            dest_id = task['dest_id']
            start_id = task['start_id']
            end_id = task['end_id']
            bot = task['bot']

            LOGGER.info(f"Processing task: Forward {source_id}:{start_id}-{end_id} to {dest_id}.")
            
            message_ids = list(range(start_id, end_id + 1))
            
            try:
                await user_bot.forward_messages(
                    to_peer=dest_id,
                    from_peer=source_id,
                    message_ids=message_ids
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ Task completed: Forwarded messages from {start_id} to {end_id}."
                )
            except FloodWaitError as e:
                LOGGER.error(f"Flood wait error: sleeping for {e.seconds}s")
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Task paused due to Telegram limits. Will resume in {e.seconds} seconds."
                )
                await asyncio.sleep(e.seconds)
            except Exception as e:
                LOGGER.error(f"An error occurred during forwarding: {e}")
                await bot.send_message(chat_id=chat_id, text=f"❌ Error during forwarding: {e}")

        except Exception as e:
            LOGGER.error(f"Worker loop error: {e}")
        finally:
            task_queue.task_done()

# --- Telegram Bot (Controller) Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Butler is online and at your service!")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_bot_client = context.bot_data['user_bot']
    try:
        await user_bot_client.send_message("me", "Pong!")
        await update.message.reply_text("Pong! Check your \"Saved Messages\".")
    except Exception as e:
        await update.message.reply_text(f"Error pinging User-Bot: {e}")

async def forward_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.split()
        if len(parts) != 5:
            await update.message.reply_text(
                "Usage: /forward <source_id> <dest_id> <start_msg_id> <end_msg_id>"
            )
            return

        source_id = int(parts[1])
        dest_id = int(parts[2])
        start_id = int(parts[3])
        end_id = int(parts[4])

        if start_id > end_id:
            await update.message.reply_text("Error: Start message ID must be less than or equal to end ID.")
            return

        task = {
            'chat_id': update.effective_chat.id,
            'source_id': source_id,
            'dest_id': dest_id,
            'start_id': start_id,
            'end_id': end_id,
            'bot': context.bot
        }
        
        await task_queue.put(task)
        await update.message.reply_text("✅ Task added to the queue.")

    except (ValueError, IndexError):
        await update.message.reply_text(
            "Invalid input. Please ensure all IDs are integers.\n"
            "Usage: /forward <source_id> <dest_id> <start_msg_id> <end_msg_id>"
        )
    except Exception as e:
        LOGGER.error(f"Error in /forward command: {e}")
        await update.message.reply_text(f"An error occurred: {e}")

# --- Main Application Logic ---
async def main():
    user_bot = None
    try:
        LOGGER.info("Starting User-Bot client...")
        user_bot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await user_bot.start()
        LOGGER.info("User-Bot client started successfully.")

        LOGGER.info("Setting up Controller Bot...")
        ptb_app = Application.builder().token(BOT_TOKEN).build()
        ptb_app.bot_data['user_bot'] = user_bot

        LOGGER.info("Registering command handlers...")
        ptb_app.add_handler(CommandHandler("start", start))
        ptb_app.add_handler(CommandHandler("ping", ping))
        ptb_app.add_handler(CommandHandler("forward", forward_command))
        LOGGER.info("Command handlers registered.")
        
        LOGGER.info("Creating worker task...")
        asyncio.create_task(worker(user_bot))
        LOGGER.info("Worker task created.")
        
        LOGGER.info("Controller Bot polling started.")
        await ptb_app.run_polling()

    except Exception as e:
        LOGGER.error(f"An error occurred in the main function: {e}", exc_info=True)
    finally:
        if user_bot and user_bot.is_connected():
            await user_bot.disconnect()
            LOGGER.info("User-Bot client disconnected.")

if __name__ == "__main__":
    keep_alive()
    
    LOGGER.info("Application starting...")
    try:
        asyncio.run(main())
    except Exception as e:
        LOGGER.critical(f"Application failed to run: {e}", exc_info=True)

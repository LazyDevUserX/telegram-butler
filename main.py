# --- main.py v6 (Task Cancellation & Poll Forwarding Fix) ---
# Adds a /cancel command and correctly handles forwarding polls.

import os
import asyncio
import logging
import time
from flask import Flask
from threading import Thread
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import FloodWaitError, MessageIdInvalidError
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Configuration ---
API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
SESSION_STRING = os.environ.get('SESSION_STRING')
OWNER_ID = int(os.environ.get('OWNER_ID', 0))

# --- In-Memory Settings Store ---
user_settings = {
    "source_chat": None,
    "dest_chat": None,
    "forward_header": True, # Default to forwarding with header
    "delay_seconds": 1     # Default delay of 1 second
}

# --- Task Control ---
cancel_event = asyncio.Event()

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

# --- UI Helper Function ---
def create_progress_bar(progress, total, length=20):
    """Creates a text-based progress bar."""
    if total == 0: return '█' * length
    filled_length = int(length * progress // total)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return bar

# --- Task Queue and Worker Setup ---
task_queue = asyncio.Queue()

async def worker(user_bot, bot_app):
    LOGGER.info("Worker started, waiting for tasks.")
    while True:
        try:
            # Reset cancel event for the new task
            cancel_event.clear()
            
            task = await task_queue.get()
            task_type = task.get('type')
            
            if task_type == 'forward':
                await handle_forward_task(user_bot, bot_app, task)
            elif task_type == 'delete':
                await handle_delete_task(user_bot, bot_app, task)
            elif task_type == 'send':
                await handle_send_task(user_bot, bot_app, task)
                
        except Exception as e:
            LOGGER.error(f"Worker loop error: {e}", exc_info=True)
        finally:
            if not task_queue.empty():
                task_queue.task_done()

async def handle_send_task(user_bot, bot_app, task):
    """Handles sending a simple text message."""
    try:
        await user_bot.send_message(task['dest_id'], task['text'])
        await bot_app.bot.send_message(task['chat_id'], "✅ Message sent successfully.")
    except Exception as e:
        LOGGER.error(f"Error sending message: {e}")
        await bot_app.bot.send_message(task['chat_id'], f"❌ Error sending message: {e}")

async def handle_forward_task(user_bot, bot_app, task):
    """Handles the forwarding task with progress updates and cancellation."""
    chat_id, source_id, dest_id, start_id, end_id = [task[k] for k in ['chat_id', 'source_id', 'dest_id', 'start_id', 'end_id']]
    total_messages = (end_id - start_id) + 1
    processed_count = 0
    skipped_count = 0
    
    status_msg = await bot_app.bot.send_message(chat_id, "Starting forward task...")
    last_update_time = time.time()

    for msg_id in range(start_id, end_id + 1):
        if cancel_event.is_set():
            LOGGER.info("Cancel event set. Breaking from forward loop.")
            break

        try:
            original_message = await user_bot.get_messages(source_id, ids=msg_id)
            if not original_message:
                skipped_count += 1
                continue

            # If header is ON and it's NOT a poll, use the efficient forward_messages
            if user_settings["forward_header"] and not original_message.poll:
                await user_bot.forward_messages(entity=dest_id, messages=msg_id, from_peer=source_id)
            # Otherwise (header is OFF or it IS a poll), re-create the message
            else:
                # NEW: Built-in text replacement for polls and messages
                if original_message.poll and '[REMEDICS]' in original_message.poll.question:
                    original_message.poll.question = original_message.poll.question.replace('[REMEDICS]', '[MediX]')
                
                if original_message.text and '[REMEDICS]' in original_message.text:
                    original_message.text = original_message.text.replace('[REMEDICS]', '[MediX]')

                await user_bot.send_message(dest_id, original_message)

            processed_count += 1
            await asyncio.sleep(user_settings["delay_seconds"]) # Use configured delay
        except FloodWaitError as e:
            LOGGER.warning(f"Flood wait of {e.seconds}s. Pausing task.")
            await bot_app.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=f"⚠️ Flood wait. Pausing for {e.seconds} seconds...")
            await asyncio.sleep(e.seconds)
        except MessageIdInvalidError:
            LOGGER.warning(f"Message ID {msg_id} is invalid or deleted. Skipping.")
            skipped_count += 1
        except Exception as e:
            LOGGER.error(f"Error forwarding message {msg_id}: {e}")
            skipped_count += 1

        if time.time() - last_update_time > 2 or (processed_count + skipped_count) % 10 == 0:
            progress = processed_count + skipped_count
            bar = create_progress_bar(progress, total_messages)
            text = (f"**Forwarding in Progress...**\n\n"
                    f"`{bar}`\n\n"
                    f"**Processed:** {processed_count}/{total_messages}\n"
                    f"**Skipped:** {skipped_count}\n"
                    f"**Task:** `Forwarding`")
            try:
                await bot_app.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=text, parse_mode='Markdown')
                last_update_time = time.time()
            except Exception: pass
    
    if cancel_event.is_set():
        final_text = (f"**Task Cancelled!**\n\n"
                      f"**Forwarded:** {processed_count} messages.\n"
                      f"**Skipped:** {skipped_count} messages.")
    else:
        final_text = (f"**Task Completed!**\n\n"
                      f"**Forwarded:** {processed_count} messages.\n"
                      f"**Skipped:** {skipped_count} messages.")
    await bot_app.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=final_text, parse_mode='Markdown')


async def handle_delete_task(user_bot, bot_app, task):
    """Handles the deletion task with progress updates and cancellation."""
    chat_id, dest_id, start_id, end_id = [task[k] for k in ['chat_id', 'dest_id', 'start_id', 'end_id']]
    total_messages = (end_id - start_id) + 1
    deleted_count = 0
    
    status_msg = await bot_app.bot.send_message(chat_id, "Starting delete task...")
    last_update_time = time.time()

    message_ids_to_delete = list(range(start_id, end_id + 1))
    
    for i in range(0, len(message_ids_to_delete), 100):
        if cancel_event.is_set():
            LOGGER.info("Cancel event set. Breaking from delete loop.")
            break
        
        chunk = message_ids_to_delete[i:i+100]
        try:
            await user_bot.delete_messages(entity=dest_id, message_ids=chunk)
            deleted_count += len(chunk)
            await asyncio.sleep(1)
        except FloodWaitError as e:
            LOGGER.warning(f"Flood wait of {e.seconds}s on delete. Pausing task.")
            await bot_app.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=f"⚠️ Flood wait. Pausing for {e.seconds} seconds...")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            LOGGER.error(f"Error deleting chunk: {e}")
            
        if time.time() - last_update_time > 2:
            bar = create_progress_bar(deleted_count, total_messages)
            text = (f"**Deletion in Progress...**\n\n"
                    f"`{bar}`\n\n"
                    f"**Deleted:** {deleted_count}/{total_messages}\n"
                    f"**Task:** `Deleting`")
            try:
                await bot_app.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=text, parse_mode='Markdown')
                last_update_time = time.time()
            except Exception: pass

    if cancel_event.is_set():
        final_text = f"**Task Cancelled!**\n\n**Deleted:** {deleted_count} messages."
    else:
        final_text = f"**Task Completed!**\n\n**Deleted:** {deleted_count} messages."
    await bot_app.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=final_text, parse_mode='Markdown')


# --- Telegram Bot (Controller) Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Butler is online. Use /help to see available commands.")

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
        source_id = user_settings["source_chat"]
        dest_id = user_settings["dest_chat"]
        start_id, end_id = None, None

        if len(parts) == 3:
            start_id, end_id = int(parts[1]), int(parts[2])
            if not source_id or not dest_id:
                await update.message.reply_text("Please set source and destination chats first using /set.")
                return
        elif len(parts) == 5:
            source_id, dest_id, start_id, end_id = map(int, parts[1:])
        else:
            await update.message.reply_text("Usage:\n`/forward <start> <end>`\n`/forward <source> <dest> <start> <end>`", parse_mode='Markdown')
            return

        task = {'type': 'forward', 'chat_id': update.effective_chat.id, 'source_id': source_id, 'dest_id': dest_id, 'start_id': start_id, 'end_id': end_id}
        if task['start_id'] > task['end_id']:
            await update.message.reply_text("Error: Start ID must be less than or equal to end ID.")
            return
        await task_queue.put(task)
        await update.message.reply_text("✅ Forward task added to the queue.")
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid input. Please ensure all IDs are integers.")
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}")

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.split()
        if len(parts) != 4:
            await update.message.reply_text("Usage: /delete <channel_id> <start> <end>")
            return
        task = {'type': 'delete', 'chat_id': update.effective_chat.id, 'dest_id': int(parts[1]), 'start_id': int(parts[2]), 'end_id': int(parts[3])}
        if task['start_id'] > task['end_id']:
            await update.message.reply_text("Error: Start ID must be less than or equal to end ID.")
            return
        await task_queue.put(task)
        await update.message.reply_text("✅ Delete task added to the queue.")
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid input. Please ensure all IDs are integers.")
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}")
        
async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("Usage: /send <channel_id> <message_text>")
            return
        task = {'type': 'send', 'chat_id': update.effective_chat.id, 'dest_id': int(parts[1]), 'text': parts[2]}
        await task_queue.put(task)
        await update.message.reply_text("✅ Send task added to the queue.")
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid input. Channel ID must be an integer.")
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}")

async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.split()
        if len(parts) != 3:
            await update.message.reply_text("Usage: /set <source|dest> <chat_id>")
            return
        setting_type, chat_id = parts[1].lower(), int(parts[2])
        if setting_type == "source": user_settings["source_chat"] = chat_id
        elif setting_type == "dest": user_settings["dest_chat"] = chat_id
        else:
            await update.message.reply_text("Invalid setting. Use 'source' or 'dest'.")
            return
        await update.message.reply_text(f"✅ Default {setting_type} chat set to: `{chat_id}`", parse_mode='Markdown')
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid command format or Chat ID.")

async def header_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        status = update.message.text.split()[1].lower()
        if status == "on":
            user_settings["forward_header"] = True
            await update.message.reply_text("✅ Forwarding with header is now **ON**.", parse_mode='Markdown')
        elif status == "off":
            user_settings["forward_header"] = False
            await update.message.reply_text("✅ Forwarding with header is now **OFF** (anonymous).", parse_mode='Markdown')
        else:
            await update.message.reply_text("Invalid status. Use 'on' or 'off'.")
    except IndexError:
        await update.message.reply_text("Usage: /header <on|off>")

async def delay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        delay = float(update.message.text.split()[1])
        if 0 <= delay <= 60:
            user_settings["delay_seconds"] = delay
            await update.message.reply_text(f"✅ Delay between messages set to **{delay}** seconds.", parse_mode='Markdown')
        else:
            await update.message.reply_text("Please choose a delay between 0 and 60 seconds.")
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid delay. Please enter a number.")

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = user_settings['source_chat'] or 'Not Set'
    dest = user_settings['dest_chat'] or 'Not Set'
    header = "On" if user_settings['forward_header'] else "Off"
    delay = user_settings['delay_seconds']
    text = (f"**Current Butler Settings**\n\n"
            f"**Default Source:** `{source}`\n"
            f"**Default Destination:** `{dest}`\n"
            f"**Forward with Header:** `{header}`\n"
            f"**Message Delay:** `{delay}s`")
    await update.message.reply_text(text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "**Telegram Butler Help**\n\n"
        "**Core Commands:**\n"
        "`/forward <start> <end>`\n_› Forwards using saved chats._\n\n"
        "`/forward <src> <dest> <start> <end>`\n_› Forwards using specified chats._\n\n"
        "`/delete <chat_id> <start> <end>`\n_› Deletes messages in a range._\n\n"
        "`/send <chat_id> <text>`\n_› Sends a message to a chat._\n\n"
        "**Configuration:**\n"
        "`/set <source|dest> <chat_id>`\n_› Saves default source or destination._\n\n"
        "`/header <on|off>`\n_› Toggle forwarding with sender name._\n\n"
        "`/delay <seconds>`\n_› Set delay between forwards._\n\n"
        "**Utilities:**\n"
        "`/cancel`\n_› Stops the current task._\n\n"
        "`/settings`\n_› Shows current settings._\n\n"
        "`/ping`\n_› Checks if the user-bot is alive._"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the current task and clears the queue."""
    if task_queue.empty():
        await update.message.reply_text("No active task to cancel.")
        return
        
    cancel_event.set()
    
    # Clear any pending tasks in the queue
    while not task_queue.empty():
        try:
            task_queue.get_nowait()
            task_queue.task_done()
        except asyncio.QueueEmpty:
            break
            
    await update.message.reply_text("🛑 Task cancellation requested. The current operation will stop shortly.")


# --- Main Application Logic ---
async def main():
    LOGGER.info("Setting up Controller Bot...")
    ptb_app = Application.builder().token(BOT_TOKEN).build()

    LOGGER.info("Starting User-Bot client...")
    user_bot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await user_bot.start()
    LOGGER.info("User-Bot client started successfully.")

    ptb_app.bot_data['user_bot'] = user_bot

    LOGGER.info("Registering command handlers...")
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("ping", ping))
    ptb_app.add_handler(CommandHandler("forward", forward_command))
    ptb_app.add_handler(CommandHandler("delete", delete_command))
    ptb_app.add_handler(CommandHandler("send", send_command))
    ptb_app.add_handler(CommandHandler("set", set_command))
    ptb_app.add_handler(CommandHandler("header", header_command))
    ptb_app.add_handler(CommandHandler("delay", delay_command))
    ptb_app.add_handler(CommandHandler("settings", settings_command))
    ptb_app.add_handler(CommandHandler("help", help_command))
    ptb_app.add_handler(CommandHandler("cancel", cancel_command)) # New
    LOGGER.info("Command handlers registered.")
    
    async with ptb_app:
        LOGGER.info("Starting controller bot polling...")
        await ptb_app.start()
        await ptb_app.updater.start_polling()
        
        if OWNER_ID:
            try:
                await ptb_app.bot.send_message(chat_id=OWNER_ID, text="✅ Butler is updated and online!")
                LOGGER.info(f"Sent startup notification to OWNER_ID: {OWNER_ID}")
            except Exception as e:
                LOGGER.error(f"Failed to send startup notification: {e}")

        LOGGER.info("Starting worker...")
        worker_task = asyncio.create_task(worker(user_bot, ptb_app))
        
        LOGGER.info("Application is now running. Waiting for commands.")
        await asyncio.Event().wait()
        
        LOGGER.info("Shutting down...")
        await ptb_app.updater.stop()
        await ptb_app.stop()
        worker_task.cancel()
        await user_bot.disconnect()

if __name__ == "__main__":
    keep_alive()
    LOGGER.info("Application starting...")
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        LOGGER.info("Application stopped cleanly.")
    except Exception as e:
        LOGGER.critical(f"Application failed to run: {e}", exc_info=True)

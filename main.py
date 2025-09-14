# --- main.py v7 (Unified Telethon Engine) ---
# This is a major rewrite for stability and to fix poll forwarding.
# It now uses only the Telethon library for both the user and bot clients.

import os
import asyncio
import logging
import time
import json
from flask import Flask
from threading import Thread

from telethon.sync import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import FloodWaitError, MessageIdInvalidError

# --- Configuration ---
API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
SESSION_STRING = os.environ.get('SESSION_STRING')
OWNER_ID = int(os.environ.get('OWNER_ID', 0))

# --- In-Memory Settings Store with a new section for replacement rules ---
user_settings = {
    "source_chat": None,
    "dest_chat": None,
    "forward_header": True,
    "delay_seconds": 1,
    "replace_enabled": False, # Off by default as requested
    "replace_rules": {}       # Stores "find": "replace" pairs
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
def home(): return "I am awake!"
def run_flask(): app.run(host='0.0.0.0', port=8080)
def keep_alive():
    t = Thread(target=run_flask)
    t.start()
    LOGGER.info("Keep-alive server started.")

# --- UI Helper ---
def create_progress_bar(progress, total, length=20):
    if total == 0: return '█' * length
    filled_length = int(length * progress // total)
    return '█' * filled_length + '░' * (length - filled_length)

# --- Task Queue and Worker ---
task_queue = asyncio.Queue()

async def worker(user_bot, bot_client):
    LOGGER.info("Worker started.")
    while True:
        try:
            cancel_event.clear()
            task = await task_queue.get()
            task_type = task.get('type')
            
            if task_type == 'forward':
                await handle_forward_task(user_bot, bot_client, task)
            elif task_type == 'delete':
                await handle_delete_task(user_bot, bot_client, task)
            elif task_type == 'send':
                await user_bot.send_message(task['dest_id'], task['text'])
                await bot_client.send_message(task['chat_id'], "✅ Message sent successfully.")
                
        except Exception as e:
            LOGGER.error(f"Worker loop error: {e}", exc_info=True)
        finally:
            task_queue.task_done()

# --- Task Handlers ---

def apply_replacements(text):
    """Applies all user-defined replacement rules to a string."""
    for find, replace in user_settings["replace_rules"].items():
        text = text.replace(find, replace)
    return text

async def handle_forward_task(user_bot, bot_client, task):
    chat_id, source_id, dest_id, start_id, end_id = [task[k] for k in ['chat_id', 'source_id', 'dest_id', 'start_id', 'end_id']]
    total_messages = (end_id - start_id) + 1
    processed, skipped = 0, 0
    
    status_msg = await bot_client.send_message(chat_id, "Starting forward task...")
    last_update = time.time()

    for msg_id in range(start_id, end_id + 1):
        if cancel_event.is_set():
            LOGGER.info("Cancellation detected.")
            break
        try:
            message = await user_bot.get_messages(source_id, ids=msg_id)
            if not message:
                skipped += 1
                continue
            
            # THE CORE FIX: Inspired by your script
            # If replacement is OFF, use the reliable direct forward method
            if not user_settings["replace_enabled"]:
                await message.forward_to(dest_id)
            # If replacement is ON, we must rebuild the message
            else:
                new_text = message.text
                if new_text:
                    new_text = apply_replacements(str(new_text))
                
                # Handle polls separately
                if message.media and hasattr(message.media, 'poll'):
                    poll_question = str(message.media.poll.question)
                    poll_question = apply_replacements(poll_question)
                    
                    # We need to construct a new poll to send it
                    await user_bot.send_message(
                        dest_id,
                        file=message.media,
                        text=new_text
                    )
                else: # Handle regular messages
                    await user_bot.send_message(
                        dest_id,
                        message=new_text or "",
                        file=message.file or None
                    )

            processed += 1
            await asyncio.sleep(user_settings["delay_seconds"])
        except FloodWaitError as e:
            LOGGER.warning(f"Flood wait: {e.seconds}s.")
            await status_msg.edit(text=f"⚠️ Flood wait. Pausing for {e.seconds}s...")
            await asyncio.sleep(e.seconds)
        except MessageIdInvalidError:
            skipped += 1
        except Exception as e:
            LOGGER.error(f"Error on msg {msg_id}: {e}")
            skipped += 1
            
        # UI Update
        if time.time() - last_update > 2:
            progress = processed + skipped
            bar = create_progress_bar(progress, total_messages)
            text = (f"**Forwarding...**\n`{bar}`\n"
                    f"**Processed:** {processed}/{total_messages}\n"
                    f"**Skipped:** {skipped}")
            await status_msg.edit(text)
            last_update = time.time()

    final_text_verb = "Cancelled" if cancel_event.is_set() else "Completed"
    final_text = (f"**Task {final_text_verb}!**\n"
                  f"**Forwarded:** {processed}\n"
                  f"**Skipped:** {skipped}")
    await status_msg.edit(final_text)

async def handle_delete_task(user_bot, bot_client, task):
    # This function remains largely the same logic
    chat_id, dest_id, start_id, end_id = [task[k] for k in ['chat_id', 'dest_id', 'start_id', 'end_id']]
    total = (end_id - start_id) + 1
    deleted_count = 0
    
    status_msg = await bot_client.send_message(chat_id, "Starting delete task...")
    last_update = time.time()

    msg_ids = list(range(start_id, end_id + 1))
    
    for i in range(0, len(msg_ids), 100):
        if cancel_event.is_set(): break
        chunk = msg_ids[i:i+100]
        try:
            await user_bot.delete_messages(entity=dest_id, message_ids=chunk)
            deleted_count += len(chunk)
            await asyncio.sleep(1)
        except Exception as e: LOGGER.error(f"Error deleting chunk: {e}")
            
        if time.time() - last_update > 2:
            bar = create_progress_bar(deleted_count, total)
            text = f"**Deleting...**\n`{bar}`\n**Deleted:** {deleted_count}/{total}"
            await status_msg.edit(text)
            last_update = time.time()

    final_verb = "Cancelled" if cancel_event.is_set() else "Completed"
    final_text = f"**Task {final_verb}!**\n**Deleted:** {deleted_count} messages."
    await status_msg.edit(final_text)

# --- Main Bot Client and Command Handlers (All Telethon now) ---
user_bot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
bot = TelegramClient('bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

@bot.on(events.NewMessage(pattern='/start', from_users=OWNER_ID))
async def start_handler(event):
    await event.respond("Butler is online. Use /help to see available commands.")

@bot.on(events.NewMessage(pattern='/ping', from_users=OWNER_ID))
async def ping_handler(event):
    await user_bot.send_message("me", "Pong!")
    await event.respond("Pong! Check your \"Saved Messages\".")

@bot.on(events.NewMessage(pattern='/forward', from_users=OWNER_ID))
async def forward_handler(event):
    parts = event.raw_text.split()
    source, dest = user_settings["source_chat"], user_settings["dest_chat"]
    
    try:
        if len(parts) == 3:
            start, end = int(parts[1]), int(parts[2])
            if not all([source, dest]):
                await event.respond("Source/Destination not set. Use `/set`.")
                return
        elif len(parts) == 5:
            source, dest, start, end = map(int, parts[1:])
        else:
            await event.respond("Usage:\n`/forward <start> <end>`\n`/forward <src> <dest> <start> <end>`")
            return
        
        task = {'type': 'forward', 'chat_id': event.chat_id, 'source_id': source, 'dest_id': dest, 'start_id': start, 'end_id': end}
        await task_queue.put(task)
        await event.respond("✅ Forward task added to queue.")
    except (ValueError, IndexError):
        await event.respond("Invalid command format. IDs must be numbers.")

@bot.on(events.NewMessage(pattern='/delete', from_users=OWNER_ID))
async def delete_handler(event):
    try:
        _, dest, start, end = map(int, event.raw_text.split())
        task = {'type': 'delete', 'chat_id': event.chat_id, 'dest_id': dest, 'start_id': start, 'end_id': end}
        await task_queue.put(task)
        await event.respond("✅ Delete task added to queue.")
    except (ValueError, IndexError):
        await event.respond("Usage: `/delete <chat_id> <start_id> <end_id>`")

@bot.on(events.NewMessage(pattern='/send', from_users=OWNER_ID))
async def send_handler(event):
    try:
        parts = event.raw_text.split(maxsplit=2)
        dest, text = int(parts[1]), parts[2]
        task = {'type': 'send', 'chat_id': event.chat_id, 'dest_id': dest, 'text': text}
        await task_queue.put(task)
        await event.respond("✅ Send task added to queue.")
    except (ValueError, IndexError):
        await event.respond("Usage: `/send <chat_id> <message>`")

@bot.on(events.NewMessage(pattern='/set', from_users=OWNER_ID))
async def set_handler(event):
    try:
        _, key, value = event.raw_text.split()
        key = key.lower()
        if key in ["source", "dest"]:
            user_settings[f"{key}_chat"] = int(value)
            await event.respond(f"✅ Default {key} chat set to `{value}`.")
        else:
            await event.respond("Invalid key. Use `source` or `dest`.")
    except (ValueError, IndexError):
        await event.respond("Usage: `/set <source|dest> <chat_id>`")

@bot.on(events.NewMessage(pattern='/header', from_users=OWNER_ID))
async def header_handler(event):
    try:
        status = event.raw_text.split()[1].lower()
        if status == "on": user_settings["forward_header"] = True
        elif status == "off": user_settings["forward_header"] = False
        else:
            await event.respond("Use `on` or `off`.")
            return
        await event.respond(f"✅ Forwarding with header is now **{status.upper()}**.")
    except IndexError:
        await event.respond("Usage: `/header <on|off>`")

@bot.on(events.NewMessage(pattern='/delay', from_users=OWNER_ID))
async def delay_handler(event):
    try:
        delay = float(event.raw_text.split()[1])
        user_settings["delay_seconds"] = delay
        await event.respond(f"✅ Message delay set to `{delay}` seconds.")
    except (ValueError, IndexError):
        await event.respond("Usage: `/delay <seconds>`")

# --- New Replace Commands ---
@bot.on(events.NewMessage(pattern='/replace', from_users=OWNER_ID))
async def replace_toggle_handler(event):
    try:
        status = event.raw_text.split()[1].lower()
        if status == "on": user_settings["replace_enabled"] = True
        elif status == "off": user_settings["replace_enabled"] = False
        else:
            await event.respond("Use `on` or `off`.")
            return
        await event.respond(f"✅ Text replacement is now **{status.upper()}**.")
    except IndexError:
        await event.respond("Usage: `/replace <on|off>`")

@bot.on(events.NewMessage(pattern='/add_replace', from_users=OWNER_ID))
async def add_replace_handler(event):
    try:
        # Using json to parse arguments allows for spaces
        args_json = event.raw_text.split(maxsplit=1)[1]
        args = json.loads(args_json)
        if len(args) != 2: raise ValueError
        find_text, replace_text = args[0], args[1]
        user_settings["replace_rules"][find_text] = replace_text
        await event.respond(f"✅ Replacement rule added:\n`{find_text}` -> `{replace_text}`")
    except Exception:
        await event.respond('Usage: `/add_replace ["find text", "replace text"]`\n(Note: Use double quotes)')

@bot.on(events.NewMessage(pattern='/clear_replace', from_users=OWNER_ID))
async def clear_replace_handler(event):
    user_settings["replace_rules"].clear()
    await event.respond("✅ All replacement rules have been cleared.")

@bot.on(events.NewMessage(pattern='/settings', from_users=OWNER_ID))
async def settings_handler(event):
    rules = "\n".join([f"`{k}` -> `{v}`" for k, v in user_settings['replace_rules'].items()]) or "`None`"
    text = (
        "**Current Butler Settings**\n\n"
        f"**Source:** `{user_settings['source_chat'] or 'Not Set'}`\n"
        f"**Destination:** `{user_settings['dest_chat'] or 'Not Set'}`\n"
        f"**Header:** `{ 'On' if user_settings['forward_header'] else 'Off'}`\n"
        f"**Delay:** `{user_settings['delay_seconds']}s`\n"
        f"**Replacement:** `{'On' if user_settings['replace_enabled'] else 'Off'}`\n"
        f"**Rules:**\n{rules}"
    )
    await event.respond(text)

@bot.on(events.NewMessage(pattern='/help', from_users=OWNER_ID))
async def help_handler(event):
    text = (
        "**Telegram Butler Help**\n\n"
        "**Core:**\n"
        "`/forward <start> <end>`\n"
        "`/forward <src> <dest> <start> <end>`\n"
        "`/delete <chat> <start> <end>`\n"
        "`/send <chat> <text>`\n\n"
        "**Configuration:**\n"
        "`/set <source|dest> <chat_id>`\n"
        "`/header <on|off>`\n"
        "`/delay <seconds>`\n\n"
        "**Text Replacement:**\n"
        "`/replace <on|off>` (Default: off)\n"
        '`/add_replace ["find", "new"]`\n'
        "`/clear_replace`\n\n"
        "**Utilities:**\n"
        "`/cancel` | `/settings` | `/ping`"
    )
    await event.respond(text)

@bot.on(events.NewMessage(pattern='/cancel', from_users=OWNER_ID))
async def cancel_handler(event):
    if task_queue.empty():
        await event.respond("No active task to cancel.")
        return
    cancel_event.set()
    while not task_queue.empty():
        try:
            task_queue.get_nowait()
            task_queue.task_done()
        except asyncio.QueueEmpty: break
    await event.respond("🛑 Cancellation requested. Current task will stop, queue cleared.")


# --- Main Application Start ---
async def main():
    await user_bot.start()
    LOGGER.info("User-Bot client started.")
    
    # Start the worker task
    asyncio.create_task(worker(user_bot, bot))
    
    # Send startup message
    if OWNER_ID:
        await bot.send_message(OWNER_ID, "✅ Butler is updated and online!")
    
    LOGGER.info("Application is now running.")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    keep_alive()
    LOGGER.info("Application starting...")
    # Telethon's event loop management is more direct
    user_bot.loop.run_until_complete(main())


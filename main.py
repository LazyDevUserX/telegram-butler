# --- main.py v7.2 (Stable Startup & Cleanup) ---
# Final rewrite of the core application logic for stability.
# This version fixes the startup crash and cleans up the code structure.

import os
import asyncio
import logging
import time
import json
from flask import Flask
from threading import Thread

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors.rpcerrorlist import FloodWaitError, MessageIdInvalidError

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
    "forward_header": True,
    "delay_seconds": 1,
    "replace_enabled": False,
    "replace_rules": {}
}

# --- Task Control & Logging ---
cancel_event = asyncio.Event()
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
LOGGER = logging.getLogger(__name__)

# --- Flask Keep-Alive Server ---
app = Flask('')
@app.route('/')
def home(): return "I am awake!"
def run_flask(): app.run(host='0.0.0.0', port=8080)
def keep_alive():
    Thread(target=run_flask, daemon=True).start()
    LOGGER.info("Keep-alive server started.")

# --- UI Helper ---
def create_progress_bar(progress, total, length=20):
    if total == 0: return '█' * length
    filled_length = int(length * progress // total)
    return '█' * filled_length + '░' * (length - filled_length)

# --- Task Queue & Worker ---
task_queue = asyncio.Queue()

async def worker(user_bot, bot_client):
    LOGGER.info("Worker started.")
    while True:
        try:
            cancel_event.clear()
            task = await task_queue.get()
            if task.get('type') == 'forward':
                await handle_forward_task(user_bot, bot_client, task)
            elif task.get('type') == 'delete':
                await handle_delete_task(user_bot, bot_client, task)
            elif task.get('type') == 'send':
                await user_bot.send_message(task['dest_id'], task['text'])
                await bot_client.send_message(task['chat_id'], "✅ Message sent successfully.")
        except Exception as e:
            LOGGER.error(f"Worker loop error: {e}", exc_info=True)
        finally:
            task_queue.task_done()

# --- Task Handlers ---
def apply_replacements(text):
    for find, replace in user_settings["replace_rules"].items():
        text = text.replace(find, replace)
    return text

async def handle_forward_task(user_bot, bot_client, task):
    chat_id, source, dest, start, end = [task[k] for k in ['chat_id', 'source_id', 'dest_id', 'start_id', 'end_id']]
    total = (end - start) + 1
    processed, skipped = 0, 0
    status_msg = await bot_client.send_message(chat_id, "Starting forward task...")
    last_update = time.time()

    for msg_id in range(start, end + 1):
        if cancel_event.is_set(): break
        try:
            message = await user_bot.get_messages(source, ids=msg_id)
            if not message:
                skipped += 1
                continue
            
            # Simplified & Robust Forwarding Logic
            if user_settings["replace_enabled"] and message.text:
                new_text = apply_replacements(str(message.text))
                await user_bot.send_message(dest, message=new_text, file=message.file or None)
            else:
                # This is the most reliable method for all message types, including polls.
                await message.forward_to(dest)
                
            processed += 1
            await asyncio.sleep(user_settings["delay_seconds"])
        except FloodWaitError as e:
            LOGGER.warning(f"Flood wait: {e.seconds}s.")
            await status_msg.edit(f"⚠️ Flood wait. Pausing for {e.seconds}s...")
            await asyncio.sleep(e.seconds)
        except MessageIdInvalidError:
            skipped += 1
        except Exception as e:
            LOGGER.error(f"Error on msg {msg_id}: {e}")
            skipped += 1
        
        if time.time() - last_update > 2:
            try:
                progress = processed + skipped
                bar = create_progress_bar(progress, total)
                await status_msg.edit(f"**Forwarding...**\n`{bar}`\n**Processed:** {processed}/{total}\n**Skipped:** {skipped}")
                last_update = time.time()
            except Exception: pass

    final_verb = "Cancelled" if cancel_event.is_set() else "Completed"
    await status_msg.edit(f"**Task {final_verb}!**\n**Forwarded:** {processed}\n**Skipped:** {skipped}")

async def handle_delete_task(user_bot, bot_client, task):
    chat_id, dest, start, end = [task[k] for k in ['chat_id', 'dest_id', 'start_id', 'end_id']]
    total = (end - start) + 1
    deleted = 0
    status_msg = await bot_client.send_message(chat_id, "Starting delete task...")
    
    msg_ids = list(range(start, end + 1))
    for i in range(0, len(msg_ids), 100):
        if cancel_event.is_set(): break
        chunk = msg_ids[i:i+100]
        try:
            await user_bot.delete_messages(entity=dest, message_ids=chunk)
            deleted += len(chunk)
            await asyncio.sleep(1)
        except Exception as e: LOGGER.error(f"Error deleting chunk: {e}")
    
    final_verb = "Cancelled" if cancel_event.is_set() else "Completed"
    await status_msg.edit(f"**Task {final_verb}!**\n**Deleted:** {deleted} messages.")

# --- Main Bot Client and Command Handlers ---
async def register_handlers(bot, user_bot):
    @bot.on(events.NewMessage(pattern='/start', from_users=OWNER_ID))
    async def start(event): await event.respond("Butler is online. Use /help.")

    @bot.on(events.NewMessage(pattern='/ping', from_users=OWNER_ID))
    async def ping(event):
        await user_bot.send_message("me", "Pong!")
        await event.respond("Pong! Check your \"Saved Messages\".")

    @bot.on(events.NewMessage(pattern='/forward', from_users=OWNER_ID))
    async def forward(event):
        parts = event.raw_text.split()
        source, dest = user_settings["source_chat"], user_settings["dest_chat"]
        try:
            if len(parts) == 3:
                start, end = int(parts[1]), int(parts[2])
                if not all([source, dest]):
                    return await event.respond("Source/Destination not set. Use `/set`.")
            elif len(parts) == 5:
                source, dest, start, end = map(int, parts[1:])
            else:
                return await event.respond("Usage:\n`/forward <start> <end>`\n`/forward <src> <dest> <start> <end>`")
            
            await task_queue.put({'type': 'forward', 'chat_id': event.chat_id, 'source_id': source, 'dest_id': dest, 'start_id': start, 'end_id': end})
            await event.respond("✅ Forward task added to queue.")
        except (ValueError, IndexError):
            await event.respond("Invalid command format.")

    @bot.on(events.NewMessage(pattern='/delete', from_users=OWNER_ID))
    async def delete(event):
        try:
            _, dest, start, end = map(int, event.raw_text.split())
            await task_queue.put({'type': 'delete', 'chat_id': event.chat_id, 'dest_id': dest, 'start_id': start, 'end_id': end})
            await event.respond("✅ Delete task added to queue.")
        except (ValueError, IndexError):
            await event.respond("Usage: `/delete <chat_id> <start> <end>`")

    @bot.on(events.NewMessage(pattern='/send', from_users=OWNER_ID))
    async def send(event):
        try:
            _, dest_str, text = event.raw_text.split(maxsplit=2)
            await task_queue.put({'type': 'send', 'chat_id': event.chat_id, 'dest_id': int(dest_str), 'text': text})
            await event.respond("✅ Send task added to queue.")
        except (ValueError, IndexError):
            await event.respond("Usage: `/send <chat_id> <message>`")

    @bot.on(events.NewMessage(pattern='/set', from_users=OWNER_ID))
    async def set_settings(event):
        try:
            _, key, value = event.raw_text.split()
            if key.lower() in ["source", "dest"]:
                user_settings[f"{key.lower()}_chat"] = int(value)
                await event.respond(f"✅ Default {key.lower()} chat set to `{value}`.")
            else: await event.respond("Invalid key. Use `source` or `dest`.")
        except (ValueError, IndexError):
            await event.respond("Usage: `/set <source|dest> <chat_id>`")
    
    @bot.on(events.NewMessage(pattern='/(header|replace)', from_users=OWNER_ID))
    async def toggle_settings(event):
        try:
            command = event.pattern_match.group(1)
            status = event.raw_text.split()[1].lower()
            key = "forward_header" if command == "header" else "replace_enabled"
            
            if status == "on": user_settings[key] = True
            elif status == "off": user_settings[key] = False
            else: return await event.respond("Use `on` or `off`.")
            
            await event.respond(f"✅ {command.capitalize()} is now **{status.upper()}**.")
        except IndexError:
            await event.respond(f"Usage: `/{event.pattern_match.group(1)} <on|off>`")

    @bot.on(events.NewMessage(pattern='/delay', from_users=OWNER_ID))
    async def delay(event):
        try:
            delay_val = float(event.raw_text.split()[1])
            user_settings["delay_seconds"] = delay_val
            await event.respond(f"✅ Message delay set to `{delay_val}` seconds.")
        except (ValueError, IndexError):
            await event.respond("Usage: `/delay <seconds>`")

    @bot.on(events.NewMessage(pattern='/add_replace', from_users=OWNER_ID))
    async def add_replace(event):
        try:
            args = json.loads(event.raw_text.split(maxsplit=1)[1])
            find, replace = args[0], args[1]
            user_settings["replace_rules"][find] = replace
            await event.respond(f"✅ Rule added: `{find}` -> `{replace}`")
        except Exception:
            await event.respond('Usage: `/add_replace ["find this", "replace with this"]`')

    @bot.on(events.NewMessage(pattern='/clear_replace', from_users=OWNER_ID))
    async def clear_replace(event):
        user_settings["replace_rules"].clear()
        await event.respond("✅ All replacement rules cleared.")

    @bot.on(events.NewMessage(pattern='/settings', from_users=OWNER_ID))
    async def settings(event):
        rules = "\n".join([f"`{k}` -> `{v}`" for k, v in user_settings['replace_rules'].items()]) or "`None`"
        text = (f"**Butler Settings**\n\n"
                f"**Source:** `{user_settings['source_chat'] or 'Not Set'}`\n"
                f"**Destination:** `{user_settings['dest_chat'] or 'Not Set'}`\n"
                f"**Header:** `{'On' if user_settings['forward_header'] else 'Off'}`\n"
                f"**Delay:** `{user_settings['delay_seconds']}s`\n"
                f"**Replacement:** `{'On' if user_settings['replace_enabled'] else 'Off'}`\n"
                f"**Rules:**\n{rules}")
        await event.respond(text)

    @bot.on(events.NewMessage(pattern='/help', from_users=OWNER_ID))
    async def help(event):
        text = ("**Telegram Butler Help**\n\n"
                "`/forward <start> <end>`\n"
                "`/forward <src> <dest> <start> <end>`\n"
                "`/delete <chat> <start> <end>`\n"
                "`/send <chat> <text>`\n\n"
                "**Config:**\n"
                "`/set <source|dest> <id>` | `/delay <sec>`\n"
                "`/header <on|off>` | `/replace <on|off>`\n"
                '`/add_replace ["find", "new"]`\n"
                "`/clear_replace`\n\n"
                "**Utils:** `/cancel` | `/settings` | `/ping`")
        await event.respond(text)

    @bot.on(events.NewMessage(pattern='/cancel', from_users=OWNER_ID))
    async def cancel(event):
        if task_queue.empty():
            return await event.respond("No active task to cancel.")
        cancel_event.set()
        while not task_queue.empty():
            try: task_queue.get_nowait(); task_queue.task_done()
            except asyncio.QueueEmpty: break
        await event.respond("🛑 Cancellation requested. Task will stop; queue cleared.")

# --- Main Application Start ---
async def main():
    user_bot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    bot = TelegramClient('bot_session', API_ID, API_HASH)
    
    await register_handlers(bot, user_bot)
    
    worker_task = asyncio.create_task(worker(user_bot, bot))

    # Correct and stable startup sequence
    await asyncio.gather(
        user_bot.start(),
        bot.start(bot_token=BOT_TOKEN)
    )
    
    LOGGER.info("All clients started successfully.")

    if OWNER_ID:
        try:
            await bot.send_message(OWNER_ID, "✅ Butler is updated and online!")
            LOGGER.info(f"Sent startup notification to OWNER_ID.")
        except Exception as e:
            LOGGER.error(f"Failed to send startup notification: {e}")

    LOGGER.info("Application is now running.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    keep_alive()
    LOGGER.info("Application starting...")
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        LOGGER.info("Application stopped cleanly.")
    except Exception as e:
        LOGGER.critical(f"Application failed to run: {e}", exc_info=True)

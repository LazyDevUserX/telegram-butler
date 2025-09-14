import os
import re
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta

from telethon import TelegramClient, events, functions, types
from telethon.sessions import StringSession
from telethon.errors import MessageIdInvalidError, FloodWaitError, ChatAdminRequiredError
from telethon.tl.types import PeerUser, PeerChat, PeerChannel

# --- Configuration ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
SESSION_STRING = os.environ.get('SESSION_STRING')
OWNER_ID = int(os.environ.get('OWNER_ID'))

# --- In-Memory State & Settings ---
class BotState:
    def __init__(self):
        self.task_queue = deque()
        self.is_running_task = False
        self.cancel_requested = False
        self.delay = 1.0
        self.filters = set()
        self.default_source = None
        self.default_dest = None
        self.forward_header = True

state = BotState()

# --- Initialize Clients ---
user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
bot_client = TelegramClient('bot', API_ID, API_HASH)

# --- Helper Functions ---
def owner_only(func):
    async def wrapper(event):
        if event.sender_id != OWNER_ID:
            await event.respond("🚫 You are not authorized to use this bot.")
            return
        await func(event)
    return wrapper

async def parse_message_url(url):
    match = re.match(r'https://t.me/(c/)?([\w\d_]+)/(\d+)', url)
    if not match: return None, None
    is_private = bool(match.group(1))
    identifier, msg_id = match.group(2), int(match.group(3))
    try:
        if is_private: return await user_client.get_entity(PeerChannel(int(identifier))), msg_id
        else: return await user_client.get_entity(identifier), msg_id
    except Exception: return None, None

def format_progress_bar(progress, total, elapsed_time):
    if total == 0: return "[---] 0/0 (0.00%) | ETA: N/A"
    percentage = (progress / total) * 100
    bar_length = 10
    filled_length = int(bar_length * progress // total)
    bar = '█' * filled_length + '░' * (bar_length - filled_length)
    eta = "N/A"
    if progress > 0:
        time_per_item = elapsed_time / progress
        remaining_items = total - progress
        eta_seconds = remaining_items * time_per_item
        eta = str(timedelta(seconds=int(eta_seconds)))
    return f"[{bar}] {progress}/{total} ({percentage:.2f}%)\n**ETA:** {eta}"

async def get_url_from_entity_id(entity_id, msg_id):
    try:
        entity = await user_client.get_entity(entity_id)
        if hasattr(entity, 'username') and entity.username:
            return f"https://t.me/{entity.username}/{msg_id}"
        elif isinstance(entity, types.Channel):
            return f"https://t.me/c/{str(entity.id).replace('-100', '')}/{msg_id}"
    except Exception: return None

# --- Core Task Processing Logic ---
async def worker():
    if state.is_running_task: return
    state.is_running_task = True
    while state.task_queue:
        task = state.task_queue[0]
        try:
            if task['type'] == 'forward': await process_forward_task(task)
            elif task['type'] == 'delete': await process_delete_task(task)
            elif task['type'] == 'send_text': await process_send_text_task(task)
        except Exception as e:
            logger.error(f"Error processing task {task}: {e}", exc_info=True)
            try: await bot_client.send_message(task['chat_id'], f"🚨 **Task Failed!**\n**Reason:** `{e}`")
            except Exception as e2: logger.error(f"Failed to send error message: {e2}")
        finally:
            if state.task_queue: state.task_queue.popleft()
    state.is_running_task = False
    state.cancel_requested = False

async def process_forward_task(task):
    chat_id = task['chat_id']
    status_msg = await bot_client.send_message(chat_id, "🚀 Starting forward task...")
    source_entity, start_id = await parse_message_url(task['start_url'])
    _, end_id = await parse_message_url(task['end_url'])
    dest_entity, _ = await parse_message_url(task['dest_url'])

    if not all([source_entity, start_id, end_id, dest_entity]):
        await status_msg.edit("❌ **Error:** Invalid URL provided. Could not resolve entities.")
        return

    skipped, processed_count = [], 0
    message_ids = list(range(start_id, end_id + 1))
    total_count = len(message_ids)
    start_time = time.time()
    
    for i, msg_id in enumerate(message_ids):
        if state.cancel_requested:
            await status_msg.edit("🛑 **Task Canceled!** The queue has been cleared."); return
        try:
            message = await user_client.get_messages(source_entity, ids=msg_id)
            if not message:
                skipped.append(f"`{msg_id}`: Deleted/inaccessible."); continue
            
            msg_type = "text"
            if message.photo: msg_type = "photo"
            elif message.video or message.gif: msg_type = "video"
            elif message.sticker: msg_type = "sticker"
            elif message.document: msg_type = "document"
            elif message.poll: msg_type = "poll"
            if msg_type in state.filters:
                skipped.append(f"`{msg_id}`: Skipped (type: `{msg_type}`)."); continue
            
            if message.poll:
                poll = message.poll.poll
                quiz = poll.quiz
                
                # --- FINAL FIX IS HERE ---
                # Initialize with an empty list instead of None to prevent TypeError
                correct_answers_data = [] 
                solution, solution_entities = None, None

                if quiz and message.media and hasattr(message.media, 'results') and message.media.results:
                    solution = message.media.results.solution
                    solution_entities = message.media.results.solution_entities
                    correct_answers_data = [res.option for res in message.media.results.results if res.correct]
                
                await user_client.send_message(
                    dest_entity,
                    file=types.InputMediaPoll(
                        poll=types.Poll(
                            id=poll.id,
                            question=poll.question,
                            answers=[types.PollAnswer(ans.text, ans.option) for ans in poll.answers],
                            quiz=quiz,
                        ),
                        correct_answers=correct_answers_data,
                        solution=solution,
                        solution_entities=solution_entities
                    )
                )
            else:
                if state.forward_header:
                    await user_client.forward_messages(dest_entity, msg_id, source_entity)
                else:
                    await user_client.send_message(dest_entity, message)
            
            processed_count += 1
        except FloodWaitError as fwe:
            await status_msg.edit(f"⏳ **Flood Wait:** Pausing for {fwe.seconds}s."); await asyncio.sleep(fwe.seconds); continue
        except Exception as e:
            skipped.append(f"`{msg_id}`: Failed ({type(e).__name__}: {e})")
        
        if i % 5 == 0 or i == total_count - 1:
            elapsed = time.time() - start_time
            await status_msg.edit(f"**Forwarding in Progress...**\n\n{format_progress_bar(i + 1, total_count, elapsed)}")
        await asyncio.sleep(state.delay)

    summary = f"✅ **Forwarding Complete!**\n\n**Processed:** {processed_count}/{total_count} messages."
    if skipped:
        summary += "\n\n**Skipped Messages Report:**\n" + "\n".join(skipped[:15])
        if len(skipped) > 15: summary += f"\n...and {len(skipped) - 15} more."
    await status_msg.edit(summary)

async def process_delete_task(task):
    chat_id = task['chat_id']
    status_msg = await bot_client.send_message(chat_id, "🗑️ Starting delete task...")
    source_entity, start_id = await parse_message_url(task['start_url'])
    _, end_id = await parse_message_url(task['end_url'])

    if not all([source_entity, start_id, end_id]):
        await status_msg.edit("❌ **Error:** Invalid URL provided."); return
    try:
        await user_client.delete_messages(source_entity, [start_id])
    except ChatAdminRequiredError:
        await status_msg.edit("❌ **Permission Denied!** I need 'Delete Messages' rights."); return
    except MessageIdInvalidError: pass
    except Exception as e:
        await status_msg.edit(f"❌ **Error:** `{e}`"); return

    message_ids = list(range(start_id, end_id + 1))
    total_count, deleted_count = len(message_ids), 0
    start_time = time.time()
    
    for i in range(0, total_count, 100):
        if state.cancel_requested:
            await status_msg.edit("🛑 **Task Canceled!**"); return
        chunk = message_ids[i:i + 100]
        try:
            await user_client.delete_messages(source_entity, chunk)
            deleted_count += len(chunk)
        except Exception as e:
            await status_msg.edit(f"⚠️ **Warning:** Could not delete a batch. Reason: `{e}`. Stopping."); break
        elapsed = time.time() - start_time
        await status_msg.edit(f"**Deleting in Progress...**\n\n{format_progress_bar(deleted_count, total_count, elapsed)}")
        await asyncio.sleep(1)

    await status_msg.edit(f"✅ **Deletion Complete!**\nAttempted to delete: {deleted_count}/{total_count}.")

async def process_send_text_task(task):
    dest_entity, _ = await parse_message_url(task['dest_url'])
    if dest_entity:
        await user_client.send_message(dest_entity, task['text'])
        await bot_client.send_message(task['chat_id'], f"✅ Text message sent.")
    else:
        await bot_client.send_message(task['chat_id'], f"❌ Invalid destination for `/send_text`.")

# --- Bot Command Handlers ---
@bot_client.on(events.NewMessage(pattern='/start', from_users=OWNER_ID))
async def start_handler(event):
    await event.respond("👋 **Welcome to your Userbot Controller!**\nUse /help to see all commands.")

@bot_client.on(events.NewMessage(pattern='/help', from_users=OWNER_ID))
@owner_only
async def help_handler(event):
    help_text = """
    **🤖 Userbot Command Center**

    **Core Commands:**
    `/forward <start_url> <end_url> <dest_url>`
    Forwards messages. Header style is controlled by `/header`.
    *Shorthand:* `/forward <start_id> <end_id>` if defaults are set.

    `/delete <start_url> <end_url>`
    Deletes messages. Requires admin rights.
    *Shorthand:* `/delete <start_id> <end_id>` if default source is set.

    `/send_text <dest_url> "Your message"`
    Sends a text message.
    *Shorthand:* `/send_text "message"` if default destination is set.

    **Task Management:**
    `/cancel` - Stops the current task and clears the queue.
    `/status` - Shows current settings and queue status.

    **Configuration:**
    `/header <on/off>` - Toggle the 'Forwarded from' header.
    `/set_source <@username or ID>` - Sets the default source.
    `/set_dest <@username or ID>` - Sets the default destination.
    `/set_delay <seconds>` - Sets delay between messages.
    `/filter <type...>` - Excludes message types (e.g. `photo video`).
    `/filters` - Shows current content filters.
    """
    await event.respond(help_text, link_preview=False)

@bot_client.on(events.NewMessage(pattern='/forward', from_users=OWNER_ID))
@owner_only
async def forward_command_handler(event):
    parts = event.text.split()
    start_url, end_url, dest_url = None, None, None
    args = parts[1:]

    try:
        if len(args) == 3:
            start_url, end_url, dest_url = args
        elif len(args) == 2 and state.default_source and state.default_dest:
            start_url = await get_url_from_entity_id(state.default_source, args[0])
            end_url = await get_url_from_entity_id(state.default_source, args[1])
            dest_url = await get_url_from_entity_id(state.default_dest, 1)
        else:
            await event.respond("Invalid syntax. See /help."); return

        if not all([start_url, end_url, dest_url]):
             await event.respond("❌ Could not construct valid URLs. Check inputs/defaults."); return
    except IndexError:
        await event.respond("Invalid syntax. See /help."); return

    task = {'type': 'forward', 'chat_id': event.chat_id, 'start_url': start_url, 'end_url': end_url, 'dest_url': dest_url}
    state.task_queue.append(task)
    await event.respond(f"✅ **Forward Task Queued!** Position: `#{len(state.task_queue)}`.")
    if not state.is_running_task: asyncio.create_task(worker())

@bot_client.on(events.NewMessage(pattern='/delete', from_users=OWNER_ID))
@owner_only
async def delete_command_handler(event):
    parts, args = event.text.split(), event.text.split()[1:]
    start_url, end_url = None, None

    if len(args) == 2:
        if args[0].isdigit() and args[1].isdigit() and state.default_source:
             start_url = await get_url_from_entity_id(state.default_source, args[0])
             end_url = await get_url_from_entity_id(state.default_source, args[1])
        else: start_url, end_url = args
    else:
        await event.respond("Usage: `/delete <start_url> <end_url>` or set a default and use `/delete <start_id> <end_id>`."); return
    
    if not all([start_url, end_url]):
        await event.respond("❌ Could not construct valid URLs from inputs/defaults."); return

    task = {'type': 'delete', 'chat_id': event.chat_id, 'start_url': start_url, 'end_url': end_url}
    state.task_queue.append(task)
    await event.respond(f"✅ **Delete Task Queued!** Position: `#{len(state.task_queue)}`.")
    if not state.is_running_task: asyncio.create_task(worker())

@bot_client.on(events.NewMessage(pattern='/send_text', from_users=OWNER_ID))
@owner_only
async def send_text_handler(event):
    match = re.match(r'/send_text\s+("([^"]+)"|(\S+)\s+"([^"]+)")', event.text)
    dest_url, text = None, None

    if match:
        if match.group(2) and state.default_dest:
            text, dest_url = match.group(2), await get_url_from_entity_id(state.default_dest, 1)
        elif match.group(3) and match.group(4):
            dest_url, text = match.group(3), match.group(4)
            if '/t.me/' in dest_url and len(dest_url.split('/')) < 5: dest_url += "/1"
    
    if not all([dest_url, text]):
        await event.respond('Usage: `/send_text <dest> "msg"` or set default and use `/send_text "msg"`'); return

    task = {'type': 'send_text', 'chat_id': event.chat_id, 'dest_url': dest_url, 'text': text}
    state.task_queue.append(task)
    await event.respond(f"✅ **Text Message Queued!** Position: `#{len(state.task_queue)}`.")
    if not state.is_running_task: asyncio.create_task(worker())

@bot_client.on(events.NewMessage(pattern='/header', from_users=OWNER_ID))
@owner_only
async def header_handler(event):
    try:
        setting = event.text.split(maxsplit=1)[1].lower()
        if setting == "on":
            state.forward_header = True; await event.respond("✅ **Header Enabled.**")
        elif setting == "off":
            state.forward_header = False; await event.respond("✅ **Header Disabled.**")
        else:
            await event.respond("⚠️ Invalid option. Use `/header on` or `/header off`.")
    except IndexError:
        await event.respond(f"ℹ️ Header status: `{'ON' if state.forward_header else 'OFF'}`.")

@bot_client.on(events.NewMessage(pattern='/set_source|/set_dest', from_users=OWNER_ID))
@owner_only
async def set_default_handler(event):
    command = event.pattern_match.string.split()[0]
    try:
        identifier = event.text.split(maxsplit=1)[1]
        try:
            entity = await user_client.get_entity(int(identifier)); final_id = int(identifier)
        except ValueError:
            entity = await user_client.get_entity(identifier); final_id = entity.id

        if command == '/set_source': state.default_source = final_id
        else: state.default_dest = final_id
        await event.respond(f"✅ Default {command.split('_')[1]} set to: `{final_id}`")

    except IndexError: await event.respond(f"Usage: `{command} <@username or channel_id>`")
    except Exception as e: await event.respond(f"❌ Error: Could not find channel. (`{e}`)")

@bot_client.on(events.NewMessage(pattern='/cancel', from_users=OWNER_ID))
@owner_only
async def cancel_handler(event):
    if not state.is_running_task and not state.task_queue:
        await event.respond("🤷 No tasks are running or queued."); return
    state.cancel_requested = True; state.task_queue.clear()
    await event.respond("🛑 **Cancellation Requested!** Queue cleared.")

@bot_client.on(events.NewMessage(pattern='/set_delay', from_users=OWNER_ID))
@owner_only
async def set_delay_handler(event):
    try:
        delay = float(event.text.split()[1])
        if delay < 0: raise ValueError
        state.delay = delay; await event.respond(f"✅ Delay set to `{delay}` seconds.")
    except (ValueError, IndexError): await event.respond("⚠️ Invalid value. Use a non-negative number.")

@bot_client.on(events.NewMessage(pattern='/filter', from_users=OWNER_ID))
@owner_only
async def filter_handler(event):
    parts = event.text.split()[1:]
    valid_types = {'photo', 'video', 'sticker', 'gif', 'document', 'poll', 'text'}
    if not parts: await event.respond("Usage: `/filter <type1>...` or `/filter clear`"); return
    if parts[0].lower() == 'clear':
        state.filters.clear(); await event.respond("✅ All content filters cleared."); return
    added = [t for t in parts if t in valid_types]; invalid = [t for t in parts if t not in valid_types]
    for f_type in added: state.filters.add(f_type)
    response = f"✅ Filters updated: `{', '.join(sorted(list(state.filters)))}`\n" if added else ""
    if invalid: response += f"⚠️ Invalid types ignored: `{', '.join(invalid)}`"
    await event.respond(response.strip())

@bot_client.on(events.NewMessage(pattern='/filters', from_users=OWNER_ID))
@owner_only
async def show_filters_handler(event):
    await event.respond(f"Filtering types: `{', '.join(sorted(list(state.filters))) or 'None'}`")
    
@bot_client.on(events.NewMessage(pattern='/status', from_users=OWNER_ID))
@owner_only
async def status_handler(event):
    header_status = "ON" if state.forward_header else "OFF"
    status_text = f"""
**⚙️ Bot Status & Configuration**

**Task Status:** {'Running' if state.is_running_task else 'Idle'}
**Tasks in Queue:** {len(state.task_queue)}

**Forward Header:** `{header_status}`
**Message Delay:** `{state.delay}` seconds
**Filtered Types:** `{', '.join(sorted(list(state.filters))) or 'None'}`
**Default Source:** `{state.default_source or 'Not Set'}`
**Default Destination:** `{state.default_dest or 'Not Set'}`
"""
    await event.respond(status_text)

async def main():
    await bot_client.start(bot_token=BOT_TOKEN)
    logger.info("Bot client started.")
    await user_client.start()
    me = await user_client.get_me()
    logger.info(f"User client started as {me.first_name}.")
    logger.info("Bot is running...")
    await bot_client.run_until_disconnected()

if __name__ == '__main__':
    if not all([API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, OWNER_ID]):
        raise RuntimeError("Missing one or more environment variables.")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

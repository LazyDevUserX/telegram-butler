import os
import re
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta

from telethon import TelegramClient, events, types
from telethon.sessions import StringSession
from telethon.errors import MessageIdInvalidError, FloodWaitError, ChatAdminRequiredError, rpcerrorlist

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
        if is_private: return await user_client.get_entity(types.PeerChannel(int(identifier))), msg_id
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
        eta_seconds = (total - progress) * time_per_item
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

# --- NEW: Poll Re-creation Logic Extracted to a Helper Function ---
async def _recreate_poll(message, destination_entity):
    """Internal function to build and send a new poll from an old one."""
    poll = message.poll.poll
    quiz = poll.quiz
    correct_answers_data = []
    solution, solution_entities = None, None

    if quiz and message.media and hasattr(message.media, 'results') and message.media.results:
        solution = message.media.results.solution
        solution_entities = message.media.results.solution_entities
        correct_answers_data = [res.option for res in message.media.results.results if res.correct]
    
    await user_client.send_message(
        destination_entity,
        file=types.InputMediaPoll(
            poll=types.Poll(
                id=types.utils.get_random_id(),
                question=poll.question,
                answers=[types.PollAnswer(ans.text, ans.option) for ans in poll.answers],
                quiz=quiz,
            ),
            correct_answers=correct_answers_data,
            solution=solution,
            solution_entities=solution_entities
        )
    )
    logger.info(f"Successfully re-created poll from message {message.id}.")

# --- MODIFIED: Smart Poll Forwarding with Admin Check ---
async def forward_poll_smartly(message, destination_entity, header_off=False):
    """
    Handles poll forwarding with refined logic.
    - If header is off and user is admin in source, it forces re-creation.
    - Otherwise, it tries a native forward and falls back to re-creation.
    """
    if header_off:
        try:
            me = await user_client.get_me()
            perms = await user_client.get_permissions(message.chat_id, me)
            if perms and (perms.is_admin or perms.is_creator):
                logger.info(f"Admin in source channel {message.chat_id} and header is off. Forcing poll re-creation.")
                await _recreate_poll(message, destination_entity)
                return # Skip the rest of the function
        except Exception as e:
            logger.warning(f"Could not check admin permissions for {message.chat_id}. Proceeding with default logic. Error: {e}")

    # Default "Smart Forward" logic
    try:
        await user_client.forward_messages(destination_entity, message)
        logger.info(f"Successfully forwarded poll {message.id} via native forward.")
    except (rpcerrorlist.MsgIdInvalidError, rpcerrorlist.PollVoteRequiredError, rpcerrorlist.ChatAdminRequiredError) as e:
        logger.warning(f"Native forward failed for poll {message.id} (Reason: {type(e).__name__}). Falling back to re-creation.")
        await _recreate_poll(message, destination_entity)


# --- Core Task Processing Logic ---
async def worker():
    if state.is_running_task: return
    state.is_running_task = True
    while state.task_queue:
        task = state.task_queue.popleft()
        try:
            if task['type'] == 'forward': await process_forward_task(task)
            # Other tasks...
        except Exception as e:
            logger.error(f"Worker failed on task {task}: {e}", exc_info=True)
            try: await bot_client.send_message(task['chat_id'], f"🚨 **Task Failed!**\n**Reason:** `{e}`")
            except Exception as e2: logger.error(f"Failed to send error message: {e2}")
    state.is_running_task = False
    state.cancel_requested = False

async def process_forward_task(task):
    chat_id = task['chat_id']
    status_msg = await bot_client.send_message(chat_id, "🚀 Starting forward task...")
    source_entity, start_id = await parse_message_url(task['start_url'])
    _, end_id = await parse_message_url(task['end_url'])
    dest_entity, _ = await parse_message_url(task['dest_url'])

    if not all([source_entity, start_id, end_id, dest_entity]):
        await status_msg.edit("❌ **Error:** Invalid URL provided."); return

    skipped, processed_count = [], 0
    message_ids = list(range(start_id, end_id + 1))
    total_count = len(message_ids)
    start_time = time.time()
    
    for i, msg_id in enumerate(message_ids):
        if state.cancel_requested:
            await status_msg.edit("🛑 **Task Canceled!**"); return
        try:
            message = await user_client.get_messages(source_entity, ids=msg_id)
            if not message:
                skipped.append(f"`{msg_id}`: Deleted/inaccessible."); continue
            
            msg_type = "text"
            if message.photo: msg_type = "photo"
            elif message.video or message.gif: msg_type = "video"
            elif message.document: msg_type = "document"
            elif message.poll: msg_type = "poll"
            if msg_type in state.filters:
                skipped.append(f"`{msg_id}`: Skipped (type: `{msg_type}`)."); continue
            
            # --- MODIFIED: Logic now checks for `force_recreate` flag ---
            if message.poll:
                if task.get('force_recreate', False):
                    await _recreate_poll(message, dest_entity)
                else:
                    await forward_poll_smartly(message, dest_entity, header_off=(not state.forward_header))
            else:
                if state.forward_header:
                    await user_client.forward_messages(dest_entity, message)
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

# --- Other functions (delete, send_text) remain the same ---
async def process_delete_task(task):
    # ... (code is unchanged)
    pass
async def process_send_text_task(task):
    # ... (code is unchanged)
    pass

# --- Bot Command Handlers ---
@bot_client.on(events.NewMessage(pattern='/start', from_users=OWNER_ID))
async def start_handler(event):
    await event.respond("👋 **Welcome to your Userbot Controller!**\nUse /help to see all commands.")

@bot_client.on(events.NewMessage(pattern='/help', from_users=OWNER_ID))
@owner_only
async def help_handler(event):
    # --- MODIFIED: Help text updated with new command ---
    help_text = """
    **🤖 Userbot Command Center**

    **Core Commands:**
    `/forward <start_url> <end_url> <dest_url>`
    Forwards messages using the primary "Smart Forward" logic.
    *Shorthand:* `/forward <start_id> <end_id>` if defaults are set.

    `/force_forward <start_url> <end_url> <dest_url>`
    *Experimental:* Attempts to forward polls by re-creating them, which can bypass some forwarding restrictions and remove the header.

    `/delete <start_url> <end_url>`
    Deletes messages. Requires admin rights.

    `/send_text <dest_url> "Your message"`
    Sends a text message.

    **Task Management:**
    `/cancel` - Stops the current task and clears the queue.
    `/status` - Shows current settings and queue status.

    **Configuration:**
    `/header <on/off>` - Toggle the 'Forwarded from' header. This now has special logic for polls from channels you admin.
    `/set_source <@username or ID>` - Sets the default source.
    `/set_dest <@username or ID>` - Sets the default destination.
    `/set_delay <seconds>` - Sets delay between messages.
    `/filter <type...>` - Excludes message types (e.g. `photo video`).
    `/filters` - Shows current content filters.
    """
    await event.respond(help_text, link_preview=False)

def _parse_forward_args(args):
    """Helper to parse arguments for forward commands."""
    start_url, end_url, dest_url = None, None, None
    if len(args) == 3:
        start_url, end_url, dest_url = args
    elif len(args) == 2 and state.default_source and state.default_dest:
        # This part requires async call, so it's handled in the main handler
        pass 
    return start_url, end_url, dest_url

@bot_client.on(events.NewMessage(pattern=r'/forward|/force_forward', from_users=OWNER_ID))
@owner_only
async def any_forward_command_handler(event):
    command = event.pattern_match.string.split()[0]
    is_force_forward = (command == '/force_forward')
    
    args = event.text.split()[1:]
    start_url, end_url, dest_url = None, None, None

    if len(args) == 3:
        start_url, end_url, dest_url = args
    elif len(args) == 2 and state.default_source and state.default_dest:
        start_url = await get_url_from_entity_id(state.default_source, args[0])
        end_url = await get_url_from_entity_id(state.default_source, args[1])
        dest_url = await get_url_from_entity_id(state.default_dest, 1)
    else:
        await event.respond(f"**Invalid Syntax.**\n**Usage:** `{command} <start_url> <end_url> <dest_url>`\nOr set defaults and use `{command} <start_id> <end_id>`")
        return

    if not all([start_url, end_url, dest_url]):
         await event.respond("❌ Could not construct valid URLs. Check inputs/defaults."); return

    task = {
        'type': 'forward', 
        'chat_id': event.chat_id, 
        'start_url': start_url, 
        'end_url': end_url, 
        'dest_url': dest_url,
        'force_recreate': is_force_forward # NEW flag for the worker
    }
    
    command_name = "Force Forward" if is_force_forward else "Forward"
    await event.respond(f"✅ **{command_name} Task Queued!** Position: `#{len(state.task_queue) + 1}`.")
    state.task_queue.append(task)
    if not state.is_running_task: asyncio.create_task(worker())


# ... (All other handlers: /delete, /send_text, /header, /set_source, etc. remain unchanged) ...
@bot_client.on(events.NewMessage(pattern='/delete', from_users=OWNER_ID))
@owner_only
async def delete_command_handler(event):
    # ... code unchanged
    pass

@bot_client.on(events.NewMessage(pattern='/send_text', from_users=OWNER_ID))
@owner_only
async def send_text_handler(event):
    # ... code unchanged
    pass
    
@bot_client.on(events.NewMessage(pattern='/header', from_users=OWNER_ID))
@owner_only
async def header_handler(event):
    # ... code unchanged
    pass

@bot_client.on(events.NewMessage(pattern='/set_source|/set_dest', from_users=OWNER_ID))
@owner_only
async def set_default_handler(event):
    # ... code unchanged
    pass

@bot_client.on(events.NewMessage(pattern='/cancel', from_users=OWNER_ID))
@owner_only
async def cancel_handler(event):
    # ... code unchanged
    pass

@bot_client.on(events.NewMessage(pattern='/set_delay', from_users=OWNER_ID))
@owner_only
async def set_delay_handler(event):
    # ... code unchanged
    pass
    
@bot_client.on(events.NewMessage(pattern='/filter', from_users=OWNER_ID))
@owner_only
async def filter_handler(event):
    # ... code unchanged
    pass
    
@bot_client.on(events.NewMessage(pattern='/filters', from_users=OWNER_ID))
@owner_only
async def show_filters_handler(event):
    # ... code unchanged
    pass
    
@bot_client.on(events.NewMessage(pattern='/status', from_users=OWNER_ID))
@owner_only
async def status_handler(event):
    # ... code unchanged
    pass

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
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    loop.run_until_complete(main())

import os
import re
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta

from telethon import TelegramClient, events, types, utils ## --- IMPORT ADDED ---
from telethon.sessions import StringSession
from telethon.errors import MessageIdInvalidError, FloodWaitError, ChatAdminRequiredError, rpcerrorlist
from telethon.tl.types import PeerChannel

# --- Configuration ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
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
    """Decorator to restrict command usage to the OWNER_ID."""
    async def wrapper(event):
        if event.sender_id != OWNER_ID:
            await event.respond("🚫 You are not authorized to use this bot.")
            return
        await func(event)
    return wrapper

async def parse_message_url(url):
    """Parses a t.me link to get the entity and message ID."""
    match = re.match(r'https://t.me/(c/)?([\w\d_]+)/(\d+)', url)
    if not match: return None, None
    try:
        if match.group(1):
            entity = await user_client.get_entity(PeerChannel(int(match.group(2))))
        else:
            entity = await user_client.get_entity(match.group(2))
        msg_id = int(match.group(3))
        return entity, msg_id
    except (ValueError, TypeError, Exception) as e:
        logger.error(f"Error parsing URL {url}: {e}")
        return None, None

def format_progress_bar(progress, total, elapsed_time):
    """Formats a progress bar string."""
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

## --- RE-INTRODUCED & FIXED: Poll Recreation Logic ---
async def _recreate_poll(message, destination_entity):
    """Builds and sends a new poll based on an existing one."""
    if not message.poll: return
    
    poll = message.poll.poll
    quiz = poll.quiz
    correct_answers_data = []
    solution, solution_entities = None, None

    if quiz and message.media and hasattr(message.media, 'results') and message.media.results:
        solution = message.media.results.solution
        solution_entities = message.media.results.solution_entities
        if message.media.results.results:
             correct_answers_data = [res.option for res in message.media.results.results if res.correct]

    await user_client.send_message(
        destination_entity,
        file=types.InputMediaPoll(
            poll=types.Poll(
                id=utils.get_random_id(), # Fixed the call to get_random_id
                question=poll.question,
                answers=[types.PollAnswer(ans.text, ans.option) for ans in poll.answers],
                quiz=quiz,
            ),
            correct_answers=correct_answers_data if correct_answers_data else None,
            solution=solution,
            solution_entities=solution_entities
        )
    )
    logger.info(f"Successfully re-created poll from message {message.id}.")

# --- Core Task Processing Logic ---
async def worker():
    if state.is_running_task: return
    state.is_running_task = True
    while state.task_queue:
        if state.cancel_requested:
            state.task_queue.clear()
            break
        
        task = state.task_queue.popleft()
        try:
            if task['type'] == 'forward':
                await process_forward_task(task)
            elif task['type'] == 'delete':
                await process_delete_task(task)
        except Exception as e:
            logger.error(f"Worker failed on task {task}: {e}", exc_info=True)
            try: await bot_client.send_message(task['chat_id'], f"🚨 **Task Failed!**\n**Reason:** `{e}`")
            except Exception as e2: logger.error(f"Failed to send error message: {e2}")

    state.is_running_task = False
    state.cancel_requested = False
    logger.info("Worker finished all tasks.")

async def process_forward_task(task):
    chat_id = task['chat_id']
    status_msg = await bot_client.send_message(chat_id, "🚀 Starting forward task...")

    try:
        source_entity = await user_client.get_entity(task['source_id'])
        dest_entity = await user_client.get_entity(task['dest_id'])
        start_id = task['start_id']
        end_id = task['end_id']
    except Exception as e:
        await status_msg.edit(f"❌ **Error:** Could not find one of the channels/chats. Details: `{e}`")
        return

    skipped, processed_count = [], 0
    message_ids = list(range(start_id, end_id + 1))
    total_count = len(message_ids)
    start_time = time.time()

    i = 0
    while i < len(message_ids):
        if state.cancel_requested:
            await status_msg.edit("🛑 **Task Canceled by User!**"); return
        
        msg_id = message_ids[i]

        try:
            message = await user_client.get_messages(source_entity, ids=msg_id)
            if not message:
                skipped.append(f"`{msg_id}`: Deleted or inaccessible.")
                i += 1
                continue

            msg_type = "text"
            if message.photo: msg_type = "photo"
            elif message.video or message.gif: msg_type = "video"
            elif message.document: msg_type = "document"
            elif message.poll: msg_type = "poll"

            if msg_type in state.filters:
                skipped.append(f"`{msg_id}`: Skipped (type: `{msg_type}`).")
                i += 1
                continue

            ## --- UPDATED LOGIC: Smart header handling ---
            if state.forward_header:
                # Standard forward with "Forwarded from" header
                await user_client.forward_messages(dest_entity, message)
            else:
                # Special handling for headerless forwarding
                if message.poll:
                    # Polls must be explicitly re-created
                    await _recreate_poll(message, dest_entity)
                else:
                    # Other types can be "copied" by sending the message object
                    await user_client.send_message(dest_entity, message)
            
            processed_count += 1
        except FloodWaitError as fwe:
            logger.warning(f"Flood wait of {fwe.seconds} seconds. Retrying message {msg_id}.")
            await status_msg.edit(f"⏳ **Flood Wait:** Pausing for {fwe.seconds}s. Will retry automatically.")
            await asyncio.sleep(fwe.seconds)
            continue
        except Exception as e:
            skipped.append(f"`{msg_id}`: Failed ({type(e).__name__})")
            logger.error(f"Failed to process message {msg_id}: {e}", exc_info=True) # Log full traceback for this error
        
        if i % 5 == 0 or i == total_count - 1:
            elapsed = time.time() - start_time
            try:
                await status_msg.edit(f"**Forwarding in Progress...**\n\n{format_progress_bar(i + 1, total_count, elapsed)}")
            except MessageIdInvalidError:
                break
        
        await asyncio.sleep(0.5) 
        i += 1

    summary = f"✅ **Forwarding Complete!**\n\n**Processed:** {processed_count}/{total_count} messages."
    if skipped:
        summary += "\n\n**Skipped/Failed Messages Report:**\n" + "\n".join(skipped[:15])
        if len(skipped) > 15: summary += f"\n...and {len(skipped) - 15} more."
    
    try: await status_msg.edit(summary)
    except MessageIdInvalidError: pass

async def process_delete_task(task):
    chat_id = task['chat_id']
    status_msg = await bot_client.send_message(chat_id, "🚀 Starting delete task...")

    try:
        target_entity = await user_client.get_entity(task['target_id'])
        start_id = task['start_id']
        end_id = task['end_id']
    except Exception as e:
        await status_msg.edit(f"❌ **Error:** Could not find the target channel. Details: `{e}`")
        return
        
    all_message_ids = list(range(start_id, end_id + 1))
    total_count = len(all_message_ids)
    deleted_count = 0
    start_time = time.time()
    CHUNK_SIZE = 100

    for i in range(0, total_count, CHUNK_SIZE):
        if state.cancel_requested:
            await status_msg.edit("🛑 **Task Canceled by User!**"); return
            
        chunk = all_message_ids[i:i + CHUNK_SIZE]
        
        try:
            await user_client.delete_messages(target_entity, chunk)
            deleted_count += len(chunk)
        except ChatAdminRequiredError:
            await status_msg.edit("❌ **Deletion Failed!**\nReason: Admin permissions are required.")
            return
        except FloodWaitError as fwe:
            logger.warning(f"Flood wait of {fwe.seconds} seconds during deletion.")
            await status_msg.edit(f"⏳ **Flood Wait:** Pausing for {fwe.seconds}s.")
            await asyncio.sleep(fwe.seconds)
            try:
                await user_client.delete_messages(target_entity, chunk)
                deleted_count += len(chunk)
            except Exception as e:
                logger.error(f"Failed to delete chunk after flood wait: {e}")
                await status_msg.edit(f"🚨 **Error after Flood Wait!** Task aborted. Reason: `{e}`")
                return
        except Exception as e:
            logger.error(f"An error occurred during deletion: {e}")
            await status_msg.edit(f"🚨 **An unexpected error occurred!** Task aborted. Reason: `{e}`")
            return

        elapsed = time.time() - start_time
        try:
            await status_msg.edit(f"**Deletion in Progress...**\n\n{format_progress_bar(deleted_count, total_count, elapsed)}")
        except MessageIdInvalidError:
            break
        
        await asyncio.sleep(state.delay)

    summary = f"✅ **Deletion Complete!**\n\n**Deleted:** {deleted_count}/{total_count} messages."
    try: await status_msg.edit(summary)
    except MessageIdInvalidError: pass


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
    Forwards messages. Use `/header off` to copy instead of forward.
    *Shorthand:* `/forward <start_id> <end_id>`

    `/delete <start_url> <end_url>`
    Deletes all messages in the specified range. Requires admin rights.

    **Task Management:**
    `/cancel` - Stops the current task and clears the queue.
    `/status` - Shows current settings and queue status.

    **Configuration:**
    `/header <on/off>` - Toggle the 'Forwarded from' header.
    `/set_source <@username or chat_id>` - Sets the default source.
    `/set_dest <@username or chat_id>` - Sets the default destination.
    `/set_delay <seconds>` - Sets delay between batches (default: 1.0s).
    `/filter <type...>` - Excludes message types from forwarding.
    `/filters` - Shows current content filters.
    """
    await event.respond(help_text, link_preview=False)

# ... (All other handlers: /forward, /delete, /set_source, etc. remain unchanged) ...

@bot_client.on(events.NewMessage(pattern=r'/forward', from_users=OWNER_ID))
@owner_only
async def forward_command_handler(event):
    args = event.text.split()[1:]
    
    source_entity, dest_entity, start_id, end_id = None, None, None, None

    try:
        if len(args) == 3:
            source_entity, start_id = await parse_message_url(args[0])
            _, end_id = await parse_message_url(args[1])
            dest_entity, _ = await parse_message_url(args[2])
        elif len(args) == 2 and state.default_source and state.default_dest:
            source_entity = await user_client.get_entity(state.default_source)
            dest_entity = await user_client.get_entity(state.default_dest)
            start_id, end_id = int(args[0]), int(args[1])
        else:
            await event.respond(f"**Invalid Syntax.**\n**Usage:** `/forward <start_url> <end_url> <dest_url>`\nOr set defaults and use `/forward <start_id> <end_id>`")
            return
    except (ValueError, TypeError):
        await event.respond("❌ Invalid message IDs. Please provide numbers.")
        return
    except Exception as e:
        await event.respond(f"❌ Error parsing inputs: `{e}`")
        return

    if not all([source_entity, dest_entity, start_id, end_id]):
        await event.respond("❌ Could not process inputs. Check your URLs or default settings."); return

    if end_id < start_id:
        await event.respond("❌ Error: The end message ID must be greater than the start ID.")
        return

    task = {
        'type': 'forward',
        'chat_id': event.chat_id,
        'source_id': source_entity.id,
        'dest_id': dest_entity.id,
        'start_id': start_id,
        'end_id': end_id,
    }

    await event.respond(f"✅ **Forward Task Queued!** Position: `#{len(state.task_queue) + 1}`.")
    state.task_queue.append(task)
    if not state.is_running_task:
        asyncio.create_task(worker())

@bot_client.on(events.NewMessage(pattern=r'/delete', from_users=OWNER_ID))
@owner_only
async def delete_command_handler(event):
    args = event.text.split()[1:]

    if len(args) != 2:
        await event.respond("**Invalid Syntax.**\n**Usage:** `/delete <start_message_url> <end_message_url>`")
        return

    try:
        start_entity, start_id = await parse_message_url(args[0])
        end_entity, end_id = await parse_message_url(args[1])
    except Exception as e:
        await event.respond(f"❌ Error parsing URLs: `{e}`")
        return

    if not all([start_entity, start_id, end_entity, end_id]):
        await event.respond("❌ Could not parse one or both of the message URLs.")
        return

    if start_entity.id != end_entity.id:
        await event.respond("❌ Error: The start and end message links must be from the same chat.")
        return

    if end_id < start_id:
        await event.respond("❌ Error: The end message ID must be greater than the start ID.")
        return

    task = {
        'type': 'delete',
        'chat_id': event.chat_id,
        'target_id': start_entity.id,
        'start_id': start_id,
        'end_id': end_id,
    }
    
    await event.respond(f"✅ **Delete Task Queued!** Position: `#{len(state.task_queue) + 1}`.")
    state.task_queue.append(task)
    if not state.is_running_task:
        asyncio.create_task(worker())


@bot_client.on(events.NewMessage(pattern=r'/set_source|/set_dest', from_users=OWNER_ID))
@owner_only
async def set_default_handler(event):
    command = event.pattern_match.string.split()[0]
    is_source = (command == '/set_source')
    
    try:
        entity_identifier = event.text.split(maxsplit=1)[1]
        try: entity_identifier = int(entity_identifier)
        except ValueError: pass 

        entity = await user_client.get_entity(entity_identifier)
        entity_name = entity.title if hasattr(entity, 'title') else entity.first_name
        
        if is_source:
            state.default_source = entity.id
            await event.respond(f"✅ **Default source set to:** `{entity_name}`")
        else:
            state.default_dest = entity.id
            await event.respond(f"✅ **Default destination set to:** `{entity_name}`")

    except IndexError:
        await event.respond(f"**Usage:** `{command} <@username or chat_id>`")
    except Exception as e:
        await event.respond(f"❌ **Error:** Could not find entity. `{e}`")


@bot_client.on(events.NewMessage(pattern='/header', from_users=OWNER_ID))
@owner_only
async def header_handler(event):
    try:
        arg = event.text.split(maxsplit=1)[1].lower()
        if arg == 'on':
            state.forward_header = True
            await event.respond("✅ **Forward header is now ON.**")
        elif arg == 'off':
            state.forward_header = False
            await event.respond("✅ **Forward header is now OFF.**")
        else:
            await event.respond("Usage: `/header <on/off>`")
    except IndexError:
        await event.respond(f"Header is currently **{'ON' if state.forward_header else 'OFF'}**.")


@bot_client.on(events.NewMessage(pattern='/cancel', from_users=OWNER_ID))
@owner_only
async def cancel_handler(event):
    if not state.is_running_task and not state.task_queue:
        await event.respond("🤷‍♂️ Nothing to cancel. The queue is empty.")
        return
    state.cancel_requested = True
    state.task_queue.clear()
    await event.respond("🛑 **Cancel request received!** The current task will stop, and the queue has been cleared.")


@bot_client.on(events.NewMessage(pattern='/set_delay', from_users=OWNER_ID))
@owner_only
async def set_delay_handler(event):
    try:
        delay = float(event.text.split(maxsplit=1)[1])
        if delay < 0:
             await event.respond("❌ Delay cannot be negative.")
             return
        state.delay = delay
        await event.respond(f"✅ **Delay between batches set to {state.delay} seconds.**")
    except (IndexError, ValueError):
        await event.respond(f"Usage: `/set_delay <seconds>`. Current: `{state.delay}`s.")


@bot_client.on(events.NewMessage(pattern='/filter', from_users=OWNER_ID))
@owner_only
async def filter_handler(event):
    args = event.text.split()[1:]
    if not args:
        state.filters.clear()
        await event.respond("✅ **All content filters cleared.**")
        return
    
    valid_filters = {'photo', 'video', 'document', 'poll'}
    added = set(arg for arg in args if arg in valid_filters)
    state.filters.update(added)
    if not added and args:
        await event.respond("No valid filter types provided.")
    else:
        await event.respond(f"✅ **Filters updated.** Current filters: `{', '.join(state.filters) or 'None'}`")


@bot_client.on(events.NewMessage(pattern='/filters', from_users=OWNER_ID))
@owner_only
async def show_filters_handler(event):
    if not state.filters:
        await event.respond("No content filters are active.")
    else:
        await event.respond(f"**Active Content Filters:**\n- `{'`\n- `'.join(state.filters)}`")


@bot_client.on(events.NewMessage(pattern='/status', from_users=OWNER_ID))
@owner_only
async def status_handler(event):
    source_name, dest_name = "Not Set", "Not Set"
    if state.default_source:
        try: 
            entity = await user_client.get_entity(state.default_source)
            source_name = entity.title if hasattr(entity, 'title') else entity.first_name
        except: source_name = f"ID: {state.default_source} (Inaccessible)"
    if state.default_dest:
        try: 
            entity = await user_client.get_entity(state.default_dest)
            dest_name = entity.title if hasattr(entity, 'title') else entity.first_name
        except: dest_name = f"ID: {state.default_dest} (Inaccessible)"

    status_text = f"""
    **📊 Bot Status & Configuration**

    **Task Queue:** `{len(state.task_queue)}` pending tasks.
    **Worker Status:** `{'Running' if state.is_running_task else 'Idle'}`

    **Settings:**
    - **Header:** `{'ON (Standard Forward)' if state.forward_header else 'OFF (Copy Message)'}`
    - **Delay:** `{state.delay}` seconds
    - **Default Source:** `{source_name}`
    - **Default Destination:** `{dest_name}`
    - **Active Filters:** `{', '.join(state.filters) or 'None'}`
    """
    await event.respond(status_text)


async def main():
    await bot_client.start(bot_token=BOT_TOKEN)
    logger.info("Bot client started.")
    await user_client.start()
    me = await user_client.get_me()
    logger.info(f"User client started as {me.first_name}.")
    await bot_client.send_message(OWNER_ID, "✅ **Bot is online and ready!**")
    logger.info("Bot is running...")
    await bot_client.run_until_disconnected()

if __name__ == '__main__':
    if not all([API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, OWNER_ID]):
        raise RuntimeError("Missing one or more required environment variables.")
    
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.run_until_complete(main())

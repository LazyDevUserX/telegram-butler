import os
import re
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta

from telethon import TelegramClient, events, functions, types
from telethon.sessions import StringSession
from telethon.errors import MessageIdInvalidError, FloodWaitError, UserIsAdminInChatError, ChatAdminRequiredError

# --- Configuration ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
SESSION_STRING = os.environ.get('SESSION_STRING')
OWNER_ID = int(os.environ.get('OWNER_ID'))

# --- In-Memory State & Settings (Render Workaround for No DB) ---
class BotState:
    def __init__(self):
        self.task_queue = deque()
        self.is_running_task = False
        self.cancel_requested = False
        # Default settings
        self.delay = 1.0
        self.filters = set()
        self.default_source = None
        self.default_dest = None

state = BotState()

# --- Initialize Clients ---
user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
bot_client = TelegramClient('bot', API_ID, API_HASH)

# --- Helper Functions ---

def owner_only(func):
    """Decorator to restrict command usage to the bot owner."""
    async def wrapper(event):
        if event.sender_id != OWNER_ID:
            await event.respond("🚫 You are not authorized to use this bot.")
            return
        await func(event)
    return wrapper

async def parse_message_url(url):
    """Parses a Telegram message URL and returns the entity and message ID."""
    match = re.match(r'https://t.me/(c/)?(\w+)/(\d+)', url)
    if not match:
        return None, None
    
    is_private_channel = bool(match.group(1))
    identifier = match.group(2)
    msg_id = int(match.group(3))

    if is_private_channel:
        try:
            # For private channels, the identifier is the chat ID
            return await user_client.get_entity(types.PeerChannel(int(identifier))), msg_id
        except (ValueError, TypeError):
             return None, None
    else:
        return await user_client.get_entity(identifier), msg_id

def format_progress_bar(progress, total, elapsed_time):
    """Formats the progress bar string with ETA."""
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

# --- Core Task Processing Logic ---

async def worker():
    """The main worker function that processes tasks from the queue."""
    if state.is_running_task:
        return

    state.is_running_task = True
    while state.task_queue:
        task = state.task_queue[0]  # Peek at the task
        
        try:
            if task['type'] == 'forward':
                await process_forward_task(task)
            elif task['type'] == 'delete':
                await process_delete_task(task)
            elif task['type'] == 'send_text':
                await process_send_text_task(task)
        except Exception as e:
            logger.error(f"Error processing task {task}: {e}", exc_info=True)
            try:
                await bot_client.send_message(task['chat_id'], f"🚨 **Task Failed!**\n\n**Reason:** `{e}`")
            except Exception as e2:
                logger.error(f"Failed to even send the error message: {e2}")
        finally:
            if state.task_queue:
                state.task_queue.popleft() # Remove the processed task
    
    state.is_running_task = False
    state.cancel_requested = False


async def process_forward_task(task):
    """Handles the logic for a single forwarding task."""
    chat_id = task['chat_id']
    status_msg = await bot_client.send_message(chat_id, "🚀 Starting forward task...")
    
    source_entity, start_id = await parse_message_url(task['start_url'])
    _, end_id = await parse_message_url(task['end_url'])
    dest_entity, _ = await parse_message_url(task['dest_url'])

    if not all([source_entity, start_id, end_id, dest_entity]):
        await status_msg.edit("❌ **Error:** Invalid URL provided. Please check and try again.")
        return

    skipped_messages = []
    processed_count = 0
    message_ids = list(range(start_id, end_id + 1))
    total_count = len(message_ids)
    start_time = time.time()
    
    for i, msg_id in enumerate(message_ids):
        if state.cancel_requested:
            await status_msg.edit("🛑 **Task Canceled!** The queue has been cleared.")
            return

        try:
            message = await user_client.get_messages(source_entity, ids=msg_id)
            if not message:
                skipped_messages.append(f"`{msg_id}`: Original message was deleted or inaccessible.")
                continue

            # Content Filtering
            msg_type = "text"
            if message.photo: msg_type = "photo"
            elif message.video or message.gif: msg_type = "video" # Grouping these
            elif message.sticker: msg_type = "sticker"
            elif message.document: msg_type = "document"
            elif message.poll: msg_type = "poll"

            if msg_type in state.filters:
                skipped_messages.append(f"`{msg_id}`: Skipped due to `{msg_type}` filter.")
                continue
            
            # Special Poll Handling
            if message.poll:
                await user_client.send_message(
                    dest_entity,
                    file=types.InputMediaPoll(
                        poll=types.Poll(
                            id=message.poll.poll.id,
                            question=message.poll.poll.question,
                            answers=[types.PollAnswer(ans.text, ans.option) for ans in message.poll.poll.answers]
                        )
                    )
                )
            # Regular Forwarding
            else:
                if task['no_header']:
                    await user_client.send_message(dest_entity, message)
                else:
                    await user_client.forward_messages(dest_entity, msg_id, source_entity)
            
            processed_count += 1
        except FloodWaitError as fwe:
            await status_msg.edit(f"⏳ **Flood Wait:** Pausing for {fwe.seconds} seconds as requested by Telegram.")
            await asyncio.sleep(fwe.seconds)
            # Retry the same message
            continue
        except Exception as e:
            skipped_messages.append(f"`{msg_id}`: Failed with error: `{e}`")
        
        # Update progress
        if i % 5 == 0 or i == total_count - 1: # Update every 5 messages or on the last one
            elapsed = time.time() - start_time
            progress_text = format_progress_bar(i + 1, total_count, elapsed)
            await status_msg.edit(f"**Forwarding in Progress...**\n\n{progress_text}")
        
        await asyncio.sleep(state.delay)

    summary = f"✅ **Forwarding Complete!**\n\n**Processed:** {processed_count}/{total_count} messages."
    if skipped_messages:
        summary += "\n\n**Skipped Messages Report:**\n" + "\n".join(skipped_messages[:10]) # Show first 10 skips
        if len(skipped_messages) > 10:
            summary += f"\n...and {len(skipped_messages) - 10} more."
    await status_msg.edit(summary)


async def process_delete_task(task):
    """Handles the logic for a single deletion task."""
    chat_id = task['chat_id']
    status_msg = await bot_client.send_message(chat_id, "🗑️ Starting delete task...")

    source_entity, start_id = await parse_message_url(task['start_url'])
    _, end_id = await parse_message_url(task['end_url'])

    if not all([source_entity, start_id, end_id]):
        await status_msg.edit("❌ **Error:** Invalid URL provided.")
        return
        
    try:
        # Check permissions first by trying to delete one message
        await user_client.delete_messages(source_entity, [start_id])
    except ChatAdminRequiredError:
        await status_msg.edit("❌ **Permission Denied!** I need to be an admin with 'Delete Messages' rights in that channel.")
        return
    except MessageIdInvalidError:
        # This is okay, the message might have been deleted already. Continue.
        pass
    except Exception as e:
        await status_msg.edit(f"❌ **Error:** An unexpected error occurred: `{e}`")
        return

    message_ids = list(range(start_id, end_id + 1))
    total_count = len(message_ids)
    start_time = time.time()
    
    # Process in batches of 100 (Telegram API limit)
    deleted_count = 0
    for i in range(0, total_count, 100):
        if state.cancel_requested:
            await status_msg.edit("🛑 **Task Canceled!**")
            return
            
        chunk = message_ids[i:i + 100]
        try:
            await user_client.delete_messages(source_entity, chunk)
            deleted_count += len(chunk)
        except Exception as e:
            await status_msg.edit(f"⚠️ **Warning:** Could not delete a batch of messages. Reason: `{e}`. Stopping.")
            break
        
        elapsed = time.time() - start_time
        progress_text = format_progress_bar(deleted_count, total_count, elapsed)
        await status_msg.edit(f"**Deleting in Progress...**\n\n{progress_text}")
        await asyncio.sleep(1) # Small delay between batches

    await status_msg.edit(f"✅ **Deletion Complete!**\n\n**Attempted to delete:** {deleted_count}/{total_count} messages.")

async def process_send_text_task(task):
    """Handles sending a simple text message."""
    dest_entity, _ = await parse_message_url(task['dest_url'])
    if dest_entity:
        await user_client.send_message(dest_entity, task['text'])
        await bot_client.send_message(task['chat_id'], f"✅ Text message sent to `{task['dest_url']}`.")
    else:
        await bot_client.send_message(task['chat_id'], f"❌ Invalid destination URL for `/send_text`.")


# --- Bot Command Handlers ---

@bot_client.on(events.NewMessage(pattern='/start', from_users=OWNER_ID))
async def start_handler(event):
    await event.respond("👋 **Welcome to your Userbot Controller!**\n\nI'm ready to manage your messages. Use /help to see all available commands.")

@bot_client.on(events.NewMessage(pattern='/help', from_users=OWNER_ID))
async def help_handler(event):
    help_text = """
    **🤖 Userbot Command Center**

    **Core Commands:**
    `/forward <start_url> <end_url> <dest_url> [-noheader]`
    Forwards messages in a range. `-noheader` is optional.
    
    `/delete <start_url> <end_url>`
    Deletes messages in a range from a channel where you are an admin.

    `/send_text <dest_url> "Your message"`
    Sends a text message to a channel.

    **Task Management:**
    `/cancel`
    Stops the current task and clears the entire queue.

    **Configuration:**
    `/set_delay <seconds>`
    Sets a delay between each forwarded message (e.g., `/set_delay 1.5`).

    `/filter <type1> <type2> ...`
    Excludes message types from forwarding.
    Types: `photo`, `video`, `sticker`, `gif`, `document`, `poll`, `text`.
    Use `/filter clear` to remove all filters.

    `/filters`
    Shows the current list of filtered types.

    `/set_source <channel_url>`
    Sets a default source channel for forwarding.

    `/set_dest <channel_url>`
    Sets a default destination channel for forwarding.

    **Info:**
    `/status`
    Shows current settings and queue status.
    """
    await event.respond(help_text, link_preview=False)

@bot_client.on(events.NewMessage(pattern='/forward', from_users=OWNER_ID))
@owner_only
async def forward_command_handler(event):
    parts = event.text.split()
    no_header = "-noheader" in parts
    if no_header:
        parts.remove("-noheader")

    start_url, end_url, dest_url = None, None, None

    # Flexible argument parsing
    if len(parts) == 4:
        _, start_url, end_url, dest_url = parts
    elif len(parts) == 3 and state.default_dest:
        _, start_url, end_url = parts
        dest_url = state.default_dest
    elif len(parts) == 2 and state.default_source and state.default_dest:
        _, start_id_str = parts
        end_id_str = start_id_str # For single message
        start_url = f"{state.default_source}/{start_id_str}"
        end_url = f"{state.default_source}/{end_id_str}"
        dest_url = state.default_dest
    else:
        await event.respond("**Usage:** `/forward <start_url> <end_url> <dest_url> [-noheader]`\nOr set default channels with `/set_source` and `/set_dest`.")
        return

    task = {
        'type': 'forward',
        'chat_id': event.chat_id,
        'start_url': start_url,
        'end_url': end_url,
        'dest_url': dest_url,
        'no_header': no_header
    }
    state.task_queue.append(task)
    await event.respond(f"✅ **Task Queued!** Position: `#{len(state.task_queue)}`.")
    
    if not state.is_running_task:
        asyncio.create_task(worker())

@bot_client.on(events.NewMessage(pattern='/delete', from_users=OWNER_ID))
@owner_only
async def delete_command_handler(event):
    parts = event.text.split()
    if len(parts) != 3:
        await event.respond("**Usage:** `/delete <start_url> <end_url>`")
        return

    task = {
        'type': 'delete',
        'chat_id': event.chat_id,
        'start_url': parts[1],
        'end_url': parts[2],
    }
    state.task_queue.append(task)
    await event.respond(f"✅ **Task Queued!** Position: `#{len(state.task_queue)}`.")
    
    if not state.is_running_task:
        asyncio.create_task(worker())

@bot_client.on(events.NewMessage(pattern='/send_text', from_users=OWNER_ID))
@owner_only
async def send_text_handler(event):
    match = re.match(r'/send_text\s+(\S+)\s+"([^"]+)"', event.text)
    if not match:
        await event.respond('**Usage:** `/send_text <destination_url> "Your message here"`')
        return

    dest_url = match.group(1)
    text = match.group(2)

    task = {
        'type': 'send_text',
        'chat_id': event.chat_id,
        'dest_url': dest_url,
        'text': text
    }
    state.task_queue.append(task)
    await event.respond(f"✅ **Task Queued!** Position: `#{len(state.task_queue)}`.")
    
    if not state.is_running_task:
        asyncio.create_task(worker())
        
@bot_client.on(events.NewMessage(pattern='/cancel', from_users=OWNER_ID))
@owner_only
async def cancel_handler(event):
    if not state.is_running_task and not state.task_queue:
        await event.respond("🤷 No tasks are currently running or queued.")
        return
        
    state.cancel_requested = True
    state.task_queue.clear()
    await event.respond("🛑 **Cancellation Requested!**\nThe current task will stop after its current message, and the queue has been cleared.")

@bot_client.on(events.NewMessage(pattern='/set_delay', from_users=OWNER_ID))
@owner_only
async def set_delay_handler(event):
    try:
        delay = float(event.text.split()[1])
        if delay < 0:
            raise ValueError
        state.delay = delay
        await event.respond(f"✅ Delay between messages set to `{delay}` seconds.")
    except (ValueError, IndexError):
        await event.respond("⚠️ **Invalid value.** Please provide a non-negative number.\n**Usage:** `/set_delay 1.5`")

@bot_client.on(events.NewMessage(pattern='/filter', from_users=OWNER_ID))
@owner_only
async def filter_handler(event):
    parts = event.text.split()[1:]
    valid_types = {'photo', 'video', 'sticker', 'gif', 'document', 'poll', 'text'}
    
    if not parts:
        await event.respond("**Usage:** `/filter <type1> <type2>...` or `/filter clear`")
        return

    if parts[0].lower() == 'clear':
        state.filters.clear()
        await event.respond("✅ All content filters have been cleared.")
        return

    added = []
    invalid = []
    for f_type in parts:
        if f_type in valid_types:
            state.filters.add(f_type)
            added.append(f_type)
        else:
            invalid.append(f_type)

    response = ""
    if added:
        response += f"✅ Filters updated. Now filtering: `{', '.join(sorted(list(state.filters)))}`\n"
    if invalid:
        response += f"⚠️ Invalid filter types ignored: `{', '.join(invalid)}`"
    await event.respond(response.strip())

@bot_client.on(events.NewMessage(pattern='/filters', from_users=OWNER_ID))
@owner_only
async def show_filters_handler(event):
    if not state.filters:
        await event.respond("ℹ️ No content filters are currently active.")
    else:
        await event.respond(f"Filtering these types: `{', '.join(sorted(list(state.filters)))}`")

@bot_client.on(events.NewMessage(pattern='/set_source', from_users=OWNER_ID))
@owner_only
async def set_source_handler(event):
    try:
        url = event.text.split()[1]
        entity, _ = await parse_message_url(url + "/1") # Test parse with a dummy ID
        if not entity: raise ValueError
        state.default_source = url.rsplit('/', 1)[0]
        await event.respond(f"✅ Default source channel set to: `{state.default_source}`")
    except (IndexError, ValueError):
        await event.respond("⚠️ **Invalid URL.** Please provide a valid channel URL.\n**Usage:** `/set_source https://t.me/channel_name`")

@bot_client.on(events.NewMessage(pattern='/set_dest', from_users=OWNER_ID))
@owner_only
async def set_dest_handler(event):
    try:
        url = event.text.split()[1]
        entity, _ = await parse_message_url(url + "/1") # Test parse with a dummy ID
        if not entity: raise ValueError
        state.default_dest = url.rsplit('/', 1)[0]
        await event.respond(f"✅ Default destination channel set to: `{state.default_dest}`")
    except (IndexError, ValueError):
        await event.respond("⚠️ **Invalid URL.** Please provide a valid channel URL.\n**Usage:** `/set_dest https://t.me/channel_name`")

@bot_client.on(events.NewMessage(pattern='/status', from_users=OWNER_ID))
@owner_only
async def status_handler(event):
    status_text = "**⚙️ Bot Status & Configuration**\n\n"
    status_text += f"**Task Status:** {'Running' if state.is_running_task else 'Idle'}\n"
    status_text += f"**Tasks in Queue:** {len(state.task_queue)}\n\n"
    status_text += f"**Message Delay:** `{state.delay}` seconds\n"
    status_text += f"**Filtered Types:** `{', '.join(sorted(list(state.filters))) or 'None'}`\n"
    status_text += f"**Default Source:** `{state.default_source or 'Not Set'}`\n"
    status_text += f"**Default Destination:** `{state.default_dest or 'Not Set'}`\n"
    await event.respond(status_text)

async def main():
    """Main function to start both clients."""
    # Start the bot client first
    await bot_client.start(bot_token=BOT_TOKEN)
    logger.info("Bot client started.")
    
    # Start the user client
    await user_client.start()
    me = await user_client.get_me()
    logger.info(f"User client started as {me.first_name}.")
    
    logger.info("Bot is running...")
    await bot_client.run_until_disconnected()

if __name__ == '__main__':
    # Sanity checks for environment variables
    if not all([API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, OWNER_ID]):
        raise RuntimeError("One or more environment variables are missing. Please check your configuration.")
        
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

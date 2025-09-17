import asyncio
import os
import json
import logging
from telethon import TelegramClient, events, types
from telethon.errors import BadRequestError
from telethon.sessions import StringSession

# ---------------------------
# Logging setup
# ---------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------
# Load settings
# ---------------------------
SETTINGS_FILE = "settings.json"
if os.path.exists(SETTINGS_FILE):
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        settings = json.load(f)
else:
    settings = {"replace": True, "src": "source_chat", "dst": "target_chat"}
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

# ---------------------------
# Telegram API setup
# ---------------------------
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))

# Initialize both clients
user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
bot_client = TelegramClient('bot', API_ID, API_HASH)

# ---------------------------
# Poll copy helper
# ---------------------------
async def copy_poll(message, dst_chat):
    """Safely rebuild and send a poll from a message object."""
    try:
        orig = message.poll.poll

        new_poll = types.Poll(
            id=0,
            question=types.TextWithEntities(text=orig.question.text, entities=orig.question.entities or []),
            answers=[
                types.PollAnswer(
                    text=types.TextWithEntities(text=ans.text.text, entities=ans.text.entities or []),
                    option=ans.option
                )
                for ans in orig.answers
            ],
            multiple_choice=orig.multiple_choice,
            quiz=orig.quiz
        )

        solution = None
        solution_entities = None
        if message.media.results:
            solution = message.media.results.solution
            solution_entities = message.media.results.solution_entities

        media = types.InputMediaPoll(
            poll=new_poll,
            correct_answers=message.media.results.correct_answers if message.media.results else None,
            solution=solution,
            solution_entities=solution_entities
        )

        await user_client.send_file(dst_chat, file=media)
        logger.info(f"✅ Poll from message {message.id} copied successfully")
        return True
    except Exception as e:
        logger.exception(f"⚠️ Poll copy for message {message.id} failed: {e}")
        return False

# ---------------------------
# /forward COMMAND
# ---------------------------
@bot_client.on(events.NewMessage(pattern=r'/forward (\d+) (\d+)', from_users=OWNER_ID))
async def forward_range_handler(event):
    start_id = int(event.pattern_match.group(1))
    end_id = int(event.pattern_match.group(2))

    if start_id > end_id:
        await event.respond("❌ **Error:** Start ID must be less than or equal to End ID.")
        return

    status_msg = await event.respond(f"🚀 **Starting task for range {start_id}-{end_id}...**")

    try:
        # --- IMPROVEMENT: Fetch entities before the loop to warm up the cache ---
        src_chat_id = int(settings["src"])
        dst_chat_id = int(settings["dst"])
        
        await status_msg.edit("Verifying channels...")
        source_entity = await user_client.get_entity(src_chat_id)
        destination_entity = await user_client.get_entity(dst_chat_id)
        logger.info(f"Source '{source_entity.title}' and Destination '{destination_entity.title}' verified.")

    except Exception as e:
        await status_msg.edit(f"❌ **Error:** Could not access channels. Make sure the user is a member of both and the IDs in `settings.json` are correct.\n**Details:** `{e}`")
        return
    
    await status_msg.edit(f"🚀 **Processing range {start_id}-{end_id}...**")
    
    processed = 0
    failed = 0
    total = (end_id - start_id) + 1

    for i, msg_id in enumerate(range(start_id, end_id + 1)):
        try:
            # --- Use the full entity object for reliability ---
            message = await user_client.get_messages(source_entity, ids=msg_id)
            if not message:
                failed += 1
                continue

            if message.poll:
                if await copy_poll(message, destination_entity):
                    processed += 1
                else:
                    failed += 1
            else:
                if message.text and settings["replace"]:
                    new_text = message.text.replace("[REMEDICS]", "[MediX]")
                    await user_client.send_message(destination_entity, new_text, file=message.media)
                else:
                    await message.forward_to(destination_entity)
                processed += 1
            
            if (i + 1) % 10 == 0 or (i + 1) == total:
                 await status_msg.edit(f"⚙️ **In Progress...**\nProcessed: {processed}/{total}\nFailed: {failed}")

            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"❌ Failed to process message {msg_id}: {e}")
            failed += 1

    await status_msg.edit(f"✅ **Task Complete!**\n\nProcessed: {processed}\nFailed: {failed}")

# ---------------------------
# Automatic Forward handler for NEW messages
# ---------------------------
@user_client.on(events.NewMessage)
async def handler_forward_new(event):
    # This handler is simple and relies on Telethon's automatic caching from recent events
    try:
        src_chat_id = int(settings["src"])
        dst_chat_id = int(settings["dst"])
    
        if event.chat_id != src_chat_id:
            return

        if event.poll:
            await copy_poll(event.message, dst_chat_id)
            return

        msg = event.message
        if msg.text and settings["replace"]:
            new_text = msg.text.replace("[REMEDICS]", "[MediX]")
            await user_client.send_message(dst_chat_id, new_text, file=msg.media)
        else:
            await msg.forward_to(dst_chat_id)

        logger.info("✅ New message forwarded automatically")

    except Exception as e:
        logger.exception(f"❌ Unexpected error in auto-forwarder: {e}")

# ---------------------------
# Main entrypoint
# ---------------------------
async def main():
    await asyncio.gather(
        user_client.start(),
        bot_client.start(bot_token=BOT_TOKEN)
    )
    logger.info("✅ Both userbot and bot are logged in and running...")
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == "__main__":
    asyncio.run(main())

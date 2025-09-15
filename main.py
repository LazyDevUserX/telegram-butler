import asyncio
import nest_asyncio
import os
import json
import random
import logging
from telethon import TelegramClient, events, types
from telethon.errors import BadRequestError
from telethon.helpers import escape_markdown

# Allow nested asyncio (needed for some environments)
nest_asyncio.apply()

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load settings
SETTINGS_FILE = "settings.json"
if os.path.exists(SETTINGS_FILE):
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        settings = json.load(f)
else:
    settings = {"replace": True, "src": "source_chat", "dst": "target_chat"}
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

# Telegram API setup
API_ID = int(os.getenv("API_ID", "123456"))  # replace with your api_id
API_HASH = os.getenv("API_HASH", "your_api_hash")  # replace with your api_hash
SESSION = "userbot"

client = TelegramClient(SESSION, API_ID, API_HASH)


async def copy_poll(event, dst):
    """Safely rebuild and forward a poll without Telethon crashes."""
    try:
        orig = event.poll

        # Build a new poll object
        new_poll = types.Poll(
            id=0,  # ✅ reset ID so Telegram generates it
            question=types.TextWithEntities(text=orig.question, entities=[]),
            answers=[
                types.PollAnswer(
                    text=types.TextWithEntities(text=ans.text, entities=[]),
                    option=ans.option
                )
                for ans in orig.answers
            ],
            multiple_choice=orig.multiple_choice,
            quiz=orig.quiz
        )

        # Optional solution
        solution = None
        if getattr(orig, "solution", None):
            solution = types.TextWithEntities(
                text=orig.solution,
                entities=[]
            )

        # Wrap into InputMediaPoll
        media = types.InputMediaPoll(
            poll=new_poll,
            correct_answers=orig.correct_answers or [],
            solution=solution
        )

        # Send fresh poll
        await event.client.send_file(dst, file=media)
        logger.info("✅ Poll copied successfully")
    except Exception as e:
        logger.exception("⚠️ Poll copy failed: %s", e)
        await event.respond(f"⚠️ Poll skipped: {e}")


@client.on(events.NewMessage)
async def handler_forward(event):
    src = settings["src"]
    dst = settings["dst"]

    try:
        if event.is_private or str(event.chat_id) != str(src):
            return

        if event.poll:
            # Special handling for polls
            await copy_poll(event, dst)
            return

        # Normal messages (text, media, etc.)
        if event.message:
            msg = event.message

            if msg.text and settings["replace"]:
                # Replace brand name in text
                new_text = msg.text.replace("[REMEDICS]", "[MediX]")
                await client.send_message(dst, new_text, file=msg.media)
            else:
                await msg.forward_to(dst)

            logger.info("✅ Message forwarded")

    except BadRequestError as e:
        logger.error("❌ Telegram API error: %s", e)
        await event.respond(f"⚠️ Failed to forward: {e}")
    except Exception as e:
        logger.exception("❌ Unexpected error: %s", e)
        await event.respond(f"⚠️ Error: {e}")


async def main():
    await client.start()
    logger.info("✅ Userbot logged in and running...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

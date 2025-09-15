import os
import re
import asyncio
import logging
from telethon import TelegramClient, events, types

# --- Basic Configuration ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
SESSION_STRING = os.environ.get('SESSION_STRING')
OWNER_ID = int(os.environ.get('OWNER_ID'))

# --- Simple In-Memory Settings ---
# Using a dictionary for simplicity instead of a class
settings = {
    'default_source': None,
    'default_dest': None,
    'header_on': True,
    'replace_on': False
}

# --- Initialize Clients ---
bot = TelegramClient('bot', API_ID, API_HASH)
user = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# --- Command Handlers ---

@bot.on(events.NewMessage(pattern='/set_source', from_users=OWNER_ID))
async def set_source_handler(event):
    try:
        entity_id = event.text.split(maxsplit=1)[1]
        entity = await user.get_entity(entity_id)
        settings['default_source'] = entity.id
        await event.respond(f"✅ **Default source set to:** `{getattr(entity, 'title', entity.first_name)}`")
    except Exception as e:
        await event.respond(f"❌ **Error:** Could not find entity. `{e}`")

@bot.on(events.NewMessage(pattern='/set_dest', from_users=OWNER_ID))
async def set_dest_handler(event):
    try:
        entity_id = event.text.split(maxsplit=1)[1]
        entity = await user.get_entity(entity_id)
        settings['default_dest'] = entity.id
        await event.respond(f"✅ **Default destination set to:** `{getattr(entity, 'title', entity.first_name)}`")
    except Exception as e:
        await event.respond(f"❌ **Error:** Could not find entity. `{e}`")

@bot.on(events.NewMessage(pattern='/header', from_users=OWNER_ID))
async def header_handler(event):
    arg = event.text.split(maxsplit=1)[1].lower()
    if arg == 'on':
        settings['header_on'] = True
        await event.respond("✅ Header is **ON** (Standard Forward).")
    elif arg == 'off':
        settings['header_on'] = False
        await event.respond("✅ Header is **OFF** (Copy/Re-create Mode).")

@bot.on(events.NewMessage(pattern='/replace', from_users=OWNER_ID))
async def replace_handler(event):
    arg = event.text.split(maxsplit=1)[1].lower()
    if arg == 'on':
        settings['replace_on'] = True
        await event.respond("✅ Text replacement `[REMEDICS]` -> `[MediX]` is **ON**.")
    elif arg == 'off':
        settings['replace_on'] = False
        await event.respond("✅ Text replacement is **OFF**.")

@bot.on(events.NewMessage(pattern='/forward', from_users=OWNER_ID))
async def forward_handler(event):
    if not settings['default_source'] or not settings['default_dest']:
        await event.respond("❌ **Error:** Please set a source and destination first using `/set_source` and `/set_dest`.")
        return

    try:
        args = event.text.split()
        start_id = int(args[1])
        end_id = int(args[2])
        if start_id > end_id:
            await event.respond("❌ **Error:** Start ID must be less than or equal to End ID.")
            return
    except (IndexError, ValueError):
        await event.respond("**Usage:** `/forward <start_id> <end_id>`")
        return

    status_msg = await event.respond(f"🚀 **Processing range {start_id}-{end_id}...**")
    
    source = await user.get_entity(settings['default_source'])
    dest = await user.get_entity(settings['default_dest'])
    processed_count = 0
    skipped_count = 0

    for msg_id in range(start_id, end_id + 1):
        try:
            message = await user.get_messages(source, ids=msg_id)
            if not message:
                skipped_count += 1
                continue

            if settings['header_on']:
                await message.forward_to(dest)
            else: # Header OFF - Copy/Re-create Logic
                if message.poll:
                    # --- Experimental Poll Logic ---
                    try:
                        logger.info(f"Voting on poll {msg_id} to access results...")
                        # 1. Vote on the first option to get results
                        vote_option = message.poll.poll.answers[0].option
                        await user.send_vote(source, message_id=message.id, options=[vote_option])
                        
                        # 2. Re-fetch the message to get updated media with results
                        await asyncio.sleep(1) # Give Telegram a moment to process the vote
                        updated_message = await user.get_messages(source, ids=msg_id)

                        poll = updated_message.poll.poll
                        results = updated_message.media.results

                        # 3. Extract all data
                        question = poll.question
                        answers = [types.PollAnswer(ans.text, ans.option) for ans in poll.answers]
                        correct_answers = []
                        solution = None
                        if results:
                            correct_answers = [res.option for res in results.results if res.correct]
                            solution = results.solution
                        
                        # 4. (Optional) Replace text
                        if settings['replace_on']:
                            question = question.replace("[REMEDICS]", "[MediX]")
                            if solution:
                                solution = solution.replace("[REMEDICS]", "[MediX]")

                        # 5. Re-create and send the poll
                        await user.send_message(
                            dest,
                            file=types.InputMediaPoll(
                                poll=types.Poll(id=message.id, question=question, answers=answers, quiz=poll.quiz),
                                correct_answers=correct_answers,
                                solution=solution
                            )
                        )
                        logger.info(f"Successfully re-created poll from message {msg_id}.")

                    except Exception as e:
                        logger.error(f"Could not re-create poll {msg_id}: {e}. Skipping.")
                        skipped_count += 1
                        continue # Skip to next message if poll recreation fails
                else:
                    # For non-poll messages, just copy them
                    await user.send_message(dest, message)
            
            processed_count += 1
            await asyncio.sleep(1.5) # Delay to avoid flood errors

        except Exception as e:
            logger.error(f"Failed to process message {msg_id}: {e}")
            skipped_count += 1
            await asyncio.sleep(2) # Longer delay on error

    await status_msg.edit(f"✅ **Task Complete!**\n\n**Processed:** {processed_count}\n**Skipped/Failed:** {skipped_count}")


async def main():
    """Main function to start both clients."""
    await user.start()
    logger.info("User client started.")
    await bot.start(bot_token=BOT_TOKEN)
    logger.info("Bot client started.")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    if not all([API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, OWNER_ID]):
        raise RuntimeError("One or more environment variables are missing.")
    asyncio.run(main())
                        

import os
import asyncio
import random
import logging
from telethon import TelegramClient, events, types
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendVoteRequest

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Env Vars ---
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION_STRING = os.environ["SESSION_STRING"]
OWNER_ID = int(os.environ["OWNER_ID"])

# --- Clients ---
bot = TelegramClient('bot', API_ID, API_HASH)
user = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# --- Settings ---
settings = {
    'source': None,
    'dest': None,
    'header': True,
    'replace': False
}

# --- Commands ---
@bot.on(events.NewMessage(pattern='/set_source', from_users=OWNER_ID))
async def set_source(event):
    arg = event.text.split(maxsplit=1)[1]
    entity = await user.get_entity(arg)
    settings['source'] = entity.id
    await event.respond(f"✅ Source set: {getattr(entity,'title', getattr(entity,'first_name','?'))}")

@bot.on(events.NewMessage(pattern='/set_dest', from_users=OWNER_ID))
async def set_dest(event):
    arg = event.text.split(maxsplit=1)[1]
    entity = await user.get_entity(arg)
    settings['dest'] = entity.id
    await event.respond(f"✅ Destination set: {getattr(entity,'title', getattr(entity,'first_name','?'))}")

@bot.on(events.NewMessage(pattern='/header', from_users=OWNER_ID))
async def toggle_header(event):
    try:
        arg = event.text.split(maxsplit=1)[1].lower()
        settings['header'] = (arg == "on")
    except:
        pass
    await event.respond(f"Header: {'ON' if settings['header'] else 'OFF'}")

@bot.on(events.NewMessage(pattern='/replace', from_users=OWNER_ID))
async def toggle_replace(event):
    try:
        arg = event.text.split(maxsplit=1)[1].lower()
        settings['replace'] = (arg == "on")
    except:
        pass
    await event.respond(f"Replace: {'ON' if settings['replace'] else 'OFF'}")

@bot.on(events.NewMessage(pattern='/forward', from_users=OWNER_ID))
async def forward_range(event):
    if not settings['source'] or not settings['dest']:
        await event.respond("❌ Set source and destination first.")
        return

    try:
        _, start_id, end_id = event.text.split()
        start_id, end_id = int(start_id), int(end_id)
    except:
        await event.respond("Usage: /forward <start_id> <end_id>")
        return

    source = await user.get_entity(settings['source'])
    dest = await user.get_entity(settings['dest'])

    processed, skipped, failed = 0, 0, 0
    status_msg = await event.respond(f"🚀 Starting forward {start_id} → {end_id}...")

    for msg_id in range(start_id, end_id + 1):
        try:
            msg = await user.get_messages(source, ids=msg_id)
            if not msg:
                skipped += 1
                continue

            if settings['header']:
                await msg.forward_to(dest)
            else:
                if msg.poll:
                    try:
                        # vote to unlock
                        opt = msg.poll.poll.answers[0].option
                        await user(SendVoteRequest(source, msg.id, [opt]))
                        await asyncio.sleep(1)
                        upd = await user.get_messages(source, ids=msg_id)
                        poll = upd.poll.poll
                        results = upd.media.results

                        q = poll.question
                        ans = [types.PollAnswer(a.text, a.option) for a in poll.answers]
                        correct = [r.option for r in results.results if r.correct] if results and results.results else []
                        solution = results.solution if results and results.solution else None
                        entities = results.solution_entities if results and results.solution_entities else None

                        # normalize
                        if not solution:
                            solution = None
                            entities = None
                        elif not entities:
                            entities = []

                        if settings['replace']:
                            q = q.replace("[REMEDICS]", "[MediX]")
                            if solution: solution = solution.replace("[REMEDICS]", "[MediX]")

                        await user.send_message(
                            dest,
                            file=types.InputMediaPoll(
                                poll=types.Poll(
                                    id=random.getrandbits(64),
                                    question=q,
                                    answers=ans,
                                    quiz=poll.quiz
                                ),
                                correct_answers=correct,
                                solution=solution,
                                solution_entities=entities
                            )
                        )
                        processed += 1
                        logger.info(f"✅ Poll {msg_id} recreated")
                    except Exception as e:
                        logger.error(f"⚠️ Poll {msg_id} failed: {e}")
                        failed += 1
                else:
                    text = msg.message
                    if text and settings['replace']:
                        text = text.replace("[REMEDICS]", "[MediX]")
                    await user.send_message(dest, text or None)
                    processed += 1

            await asyncio.sleep(1.5)

        except Exception as e:
            logger.error(f"❌ Message {msg_id} failed: {e}")
            failed += 1
            await asyncio.sleep(2)

    await status_msg.edit(
        f"✅ Done!\n\n"
        f"Processed: {processed}\n"
        f"Skipped: {skipped}\n"
        f"Failed: {failed}"
    )

# --- Run ---
async def main():
    await user.start()
    logger.info("User client started.")
    await bot.start(bot_token=BOT_TOKEN)
    logger.info("Bot client started.")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())

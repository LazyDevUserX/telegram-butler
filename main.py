# main.py -- experimental poll-forwarder (fixed + status + confirmations)
import os
import re
import asyncio
import random
import logging
from telethon import TelegramClient, events, types
from telethon.sessions import StringSession

# -------- Logging --------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------- Env Vars (must be set in Render) --------
try:
    API_ID = int(os.environ["API_ID"])
    API_HASH = os.environ["API_HASH"]
    BOT_TOKEN = os.environ["BOT_TOKEN"]
    SESSION_STRING = os.environ["SESSION_STRING"]
    OWNER_ID = int(os.environ["OWNER_ID"])
except KeyError as ke:
    logger.exception("Missing environment variable: %s", ke)
    raise

# -------- Clients --------
bot = TelegramClient("bot", API_ID, API_HASH)
user = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# -------- In-memory settings --------
settings = {
    "source": None,
    "dest": None,
    "header": True,
    "replace": False,
}

# -------- Utilities --------
def is_numeric_id(s: str) -> bool:
    return bool(re.match(r"^-?\d+$", s.strip()))

def nice_name(entity):
    if entity is None:
        return "Not set"
    return getattr(entity, "title", None) or getattr(entity, "username", None) or getattr(entity, "first_name", None) or f"id:{getattr(entity,'id', '?')}"

async def resolve_entity(arg: str):
    arg = arg.strip()
    if not arg:
        raise ValueError("No target provided.")
    if is_numeric_id(arg):
        try:
            return await user.get_entity(int(arg))
        except Exception:
            pass
    try:
        return await user.get_entity(arg)
    except Exception as e:
        raise ValueError(f"Cannot find or access entity '{arg}'. Make sure your user account is a member.") from e

async def require_owner(event):
    sender = await event.get_sender()
    if sender is None or sender.id != OWNER_ID:
        await event.respond("⛔ You are not authorized to use this bot.")
        return False
    return True

# -------- Command handlers --------
@bot.on(events.NewMessage(pattern=r'(?i)^/set[_ ]source\b'))
async def handler_set_source(event):
    if not await require_owner(event): return
    m = re.match(r'(?i)^/set[_ ]source(?:\s+(.+))?$', event.raw_text.strip())
    if not m or not m.group(1):
        await event.respond("Usage: `/set source <chat_id or @username>`")
        return
    arg = m.group(1).strip()
    try:
        entity = await resolve_entity(arg)
        settings["source"] = entity
        await event.respond(f"✅ Source set to: **{nice_name(entity)}** (id: `{entity.id}`)")
        logger.info("Source set: %s (%s)", nice_name(entity), entity.id)
    except ValueError as ve:
        await event.respond(f"❌ Could not set source: {ve}")

@bot.on(events.NewMessage(pattern=r'(?i)^/set[_ ]dest\b'))
async def handler_set_dest(event):
    if not await require_owner(event): return
    m = re.match(r'(?i)^/set[_ ]dest(?:\s+(.+))?$', event.raw_text.strip())
    if not m or not m.group(1):
        await event.respond("Usage: `/set dest <chat_id or @username>`")
        return
    arg = m.group(1).strip()
    try:
        entity = await resolve_entity(arg)
        settings["dest"] = entity
        await event.respond(f"✅ Destination set to: **{nice_name(entity)}** (id: `{entity.id}`)")
        logger.info("Dest set: %s (%s)", nice_name(entity), entity.id)
    except ValueError as ve:
        await event.respond(f"❌ Could not set destination: {ve}")

@bot.on(events.NewMessage(pattern=r'(?i)^/header\b'))
async def handler_header(event):
    if not await require_owner(event): return
    parts = event.raw_text.split()
    if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
        settings["header"] = (parts[1].lower() == "on")
        await event.respond(f"✅ Header set to **{'ON (forward)' if settings['header'] else 'OFF (recreate mode)'}**")
    else:
        await event.respond(f"Header is currently **{'ON' if settings['header'] else 'OFF'}**. Use `/header on` or `/header off`.")

@bot.on(events.NewMessage(pattern=r'(?i)^/replace\b'))
async def handler_replace(event):
    if not await require_owner(event): return
    parts = event.raw_text.split()
    if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
        settings["replace"] = (parts[1].lower() == "on")
        await event.respond(f"✅ Replace `[REMEDICS]` -> `[MediX]` is **{'ON' if settings['replace'] else 'OFF'}**.")
    else:
        await event.respond(f"Replacement is currently **{'ON' if settings['replace'] else 'OFF'}**. Use `/replace on` or `/replace off`.")

@bot.on(events.NewMessage(pattern=r'(?i)^/status\b'))
async def handler_status(event):
    if not await require_owner(event): return
    src = nice_name(settings["source"])
    dst = nice_name(settings["dest"])
    header = "ON" if settings["header"] else "OFF"
    repl = "ON" if settings["replace"] else "OFF"
    await event.respond(
        f"🔎 **Status**\n\n• Source: **{src}**\n• Destination: **{dst}**\n• Header: **{header}**\n• Replace: **{repl}**"
    )

@bot.on(events.NewMessage(pattern=r'(?i)^/forward\b'))
async def handler_forward(event):
    if not await require_owner(event): return
    if not settings["source"] or not settings["dest"]:
        await event.respond("❌ Set source and destination first.")
        return

    parts = event.raw_text.split()
    if len(parts) < 3:
        await event.respond("Usage: `/forward <start_id> <end_id>`")
        return

    try:
        start_id = int(parts[1])
        end_id = int(parts[2])
    except ValueError:
        await event.respond("Start and end must be integers.")
        return
    if start_id > end_id:
        await event.respond("Start ID must be <= End ID.")
        return

    src = settings["source"]
    dst = settings["dest"]
    processed = skipped = failed = 0
    status_msg = await event.respond(f"🚀 Forwarding {start_id} → {end_id} ...")

    for msg_id in range(start_id, end_id + 1):
        try:
            msg = await user.get_messages(src, ids=msg_id)
            if not msg:
                skipped += 1
                continue

            # -------- Forward or Recreate --------
            if settings["header"]:
                await msg.forward_to(dst)
            else:
                if msg.poll:
                    # --- Copy poll/quizzes safely ---
                    try:
                        poll_obj = msg.poll.poll
                        # ✅ Extract text if it's TextWithEntities
                        question = getattr(poll_obj.question, "text", poll_obj.question)

                        if settings["replace"] and isinstance(question, str):
                            question = question.replace("[REMEDICS]", "[MediX]")

                        answers = [
                            types.PollAnswer(a.text, a.option)
                            for a in poll_obj.answers
                        ]

                        correct_answers = []
                        solution = None
                        solution_entities = []

                        if getattr(poll_obj, "quiz", False):
                            results = getattr(msg.poll, "results", None)
                            if results and getattr(results, "results", None):
                                for r in results.results:
                                    if getattr(r, "correct", False):
                                        correct_answers.append(r.option)

                            solution = getattr(results, "solution", None)
                            if solution:
                                solution = getattr(solution, "text", solution)
                                if settings["replace"] and isinstance(solution, str):
                                    solution = solution.replace("[REMEDICS]", "[MediX]")

                            solution_entities = getattr(results, "solution_entities", []) or []

                        await user.send_message(
                            dst,
                            file=types.InputMediaPoll(
                                poll=types.Poll(
                                    id=random.getrandbits(64),
                                    question=question,
                                    answers=answers,
                                    quiz=getattr(poll_obj, "quiz", False),
                                ),
                                correct_answers=correct_answers,
                                solution=solution,
                                solution_entities=solution_entities,
                            ),
                        )
                    except Exception as e_poll:
                        failed += 1
                        logger.exception("Poll %s copy failed: %s", msg_id, e_poll)
                        await event.respond(f"⚠️ Poll {msg_id} skipped: {e_poll}")
                        continue
                else:
                    # --- Copy text/media ---
                    text = msg.message
                    if text and settings["replace"]:
                        text = text.replace("[REMEDICS]", "[MediX]")
                    if msg.media and not text:
                        await user.send_file(dst, msg.media, caption=text)
                    else:
                        await user.send_message(dst, text or None)

            processed += 1
            await asyncio.sleep(1.5)

            if (msg_id - start_id + 1) % 5 == 0:
                try:
                    await status_msg.edit(f"🚀 Progress: processed={processed} skipped={skipped} failed={failed} (at msg {msg_id})")
                except Exception:
                    pass

        except Exception as e_outer:
            failed += 1
            logger.exception("Failed processing message %s: %s", msg_id, e_outer)
            await event.respond(f"❌ Error on {msg_id}: {e_outer}")
            await asyncio.sleep(2)

    try:
        await status_msg.edit(f"✅ Done!\nProcessed: {processed}\nSkipped: {skipped}\nFailed: {failed}")
    except Exception:
        await event.respond(f"✅ Done! Processed: {processed} Skipped: {skipped} Failed: {failed}")

# -------- Start both clients --------
async def main():
    await user.start()
    logger.info("User client started.")
    await bot.start(bot_token=BOT_TOKEN)
    logger.info("Bot client started.")
    logger.info("Bot is ready. OWNER_ID=%s. Use /status to check settings.", OWNER_ID)
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())

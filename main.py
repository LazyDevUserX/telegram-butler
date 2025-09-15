# main.py -- experimental poll-forwarder (fixed + status + confirmations)
import os
import re
import asyncio
import random
import logging
from telethon import TelegramClient, events, types
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendVoteRequest

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
    # store entity objects for reliability
    "source": None,    # will contain a Telethon entity (Channel/Chat)
    "dest": None,      # will contain a Telethon entity
    "header": True,    # True = forward with header, False = recreate/copy
    "replace": False,  # simple [REMEDICS] -> [MediX] replacement toggle
}

# -------- Utilities --------
def is_numeric_id(s: str) -> bool:
    return bool(re.match(r"^-?\d+$", s.strip()))

def nice_name(entity):
    if entity is None:
        return "Not set"
    # Channels have .title, users have .first_name/.username
    return getattr(entity, "title", None) or getattr(entity, "username", None) or getattr(entity, "first_name", None) or f"id:{getattr(entity,'id', '?')}"

async def resolve_entity(arg: str):
    """
    Try to resolve an entity string to an entity via the user client.
    Accepts:
      - numeric ids (like -1001234567890)
      - @username
      - channel/chat title (if resolvable)
    Returns the entity on success or raises ValueError with a helpful message.
    """
    arg = arg.strip()
    if not arg:
        raise ValueError("No target provided.")

    # numeric id case
    if is_numeric_id(arg):
        try:
            entity = await user.get_entity(int(arg))
            return entity
        except Exception as e_int:
            # fall through to try as username (some channels require peer access)
            logger.debug("get_entity(int) failed for %s: %s", arg, e_int)
            # we'll try the raw string next (username)
    # username / other
    try:
        entity = await user.get_entity(arg)
        return entity
    except Exception as e:
        # if both numeric->int and username failed, tell the caller we couldn't resolve
        raise ValueError(f"Cannot find or access entity '{arg}'. Make sure your *user account* (SESSION_STRING) is a member of the chat.") from e

# -------- Authorization helper (gives useful reply instead of silent ignore) --------
async def require_owner(event):
    sender = await event.get_sender()
    if sender is None or sender.id != OWNER_ID:
        await event.respond("⛔ You are not authorized to use this bot. (This bot is restricted to the configured OWNER_ID.)")
        return False
    return True

# -------- Command handlers --------

# Accept both /set source and /set_source (case-insensitive)
@bot.on(events.NewMessage(pattern=r'(?i)^/set[_ ]source\b'))
async def handler_set_source(event):
    if not await require_owner(event):
        return

    # try to extract the argument (the chat id or @username)
    m = re.match(r'(?i)^/set[_ ]source(?:\s+(.+))?$', event.raw_text.strip())
    if not m or not m.group(1):
        await event.respond("Usage: `/set source <chat_id or @username>`\nExample: `/set source -1002912581857` or `/set source @medixpoll2`")
        return

    arg = m.group(1).strip()
    try:
        entity = await resolve_entity(arg)
        settings["source"] = entity
        await event.respond(f"✅ Source set to: **{nice_name(entity)}** (id: `{entity.id}`)")
        logger.info("Source set: %s (%s)", nice_name(entity), entity.id)
    except ValueError as ve:
        logger.exception("set_source failed")
        await event.respond(f"❌ Could not set source: {ve}")

@bot.on(events.NewMessage(pattern=r'(?i)^/set[_ ]dest\b'))
async def handler_set_dest(event):
    if not await require_owner(event):
        return

    m = re.match(r'(?i)^/set[_ ]dest(?:\s+(.+))?$', event.raw_text.strip())
    if not m or not m.group(1):
        await event.respond("Usage: `/set dest <chat_id or @username>`\nExample: `/set dest -1003045624674` or `/set dest @my_channel`")
        return

    arg = m.group(1).strip()
    try:
        entity = await resolve_entity(arg)
        settings["dest"] = entity
        await event.respond(f"✅ Destination set to: **{nice_name(entity)}** (id: `{entity.id}`)")
        logger.info("Dest set: %s (%s)", nice_name(entity), entity.id)
    except ValueError as ve:
        logger.exception("set_dest failed")
        await event.respond(f"❌ Could not set destination: {ve}")

@bot.on(events.NewMessage(pattern=r'(?i)^/header\b'))
async def handler_header(event):
    if not await require_owner(event):
        return

    parts = event.raw_text.split()
    if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
        settings["header"] = (parts[1].lower() == "on")
        await event.respond(f"✅ Header set to **{'ON (standard forward)' if settings['header'] else 'OFF (recreate mode)'}**")
    else:
        await event.respond(f"Header is currently **{'ON' if settings['header'] else 'OFF'}**. Use `/header on` or `/header off`.")

@bot.on(events.NewMessage(pattern=r'(?i)^/replace\b'))
async def handler_replace(event):
    if not await require_owner(event):
        return

    parts = event.raw_text.split()
    if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
        settings["replace"] = (parts[1].lower() == "on")
        await event.respond(f"✅ Replace `[REMEDICS]` -> `[MediX]` is **{'ON' if settings['replace'] else 'OFF'}**.")
    else:
        await event.respond(f"Replacement is currently **{'ON' if settings['replace'] else 'OFF'}**. Use `/replace on` or `/replace off`.")

@bot.on(events.NewMessage(pattern=r'(?i)^/status\b'))
async def handler_status(event):
    if not await require_owner(event):
        return

    src = nice_name(settings["source"])
    dst = nice_name(settings["dest"])
    header = "ON" if settings["header"] else "OFF"
    repl = "ON" if settings["replace"] else "OFF"
    await event.respond(
        f"🔎 **Status**\n\n"
        f"• Source: **{src}**\n"
        f"• Destination: **{dst}**\n"
        f"• Header: **{header}**\n"
        f"• Replace: **{repl}**"
    )

@bot.on(events.NewMessage(pattern=r'(?i)^/forward\b'))
async def handler_forward(event):
    if not await require_owner(event):
        return

    if not settings["source"] or not settings["dest"]:
        await event.respond("❌ Set source and destination first with `/set source` and `/set dest`.")
        return

    parts = event.raw_text.split()
    if len(parts) < 3:
        await event.respond("Usage: `/forward <start_id> <end_id>`\nExample: `/forward 5571 5585`")
        return

    try:
        start_id = int(parts[1])
        end_id = int(parts[2])
    except ValueError:
        await event.respond("Start and end must be integers. Example: `/forward 5571 5585`")
        return

    if start_id > end_id:
        await event.respond("Start ID must be <= End ID.")
        return

    source_entity = settings["source"]
    dest_entity = settings["dest"]

    processed = skipped = failed = 0
    status_msg = await event.respond(f"🚀 Starting forward {start_id} → {end_id} ...")

    for msg_id in range(start_id, end_id + 1):
        try:
            # get message from source as the user client (must be member)
            msg = await user.get_messages(source_entity, ids=msg_id)
            if not msg:
                skipped += 1
                logger.info("Message %s not found, skipping", msg_id)
                continue

            if settings["header"]:
                # simple forward with header
                await msg.forward_to(dest_entity)
                processed += 1
                logger.info("Forwarded message %s", msg_id)
            else:
                # recreate/copy mode
                if msg.poll:
                    # poll recreation flow
                    try:
                        # attempt to vote to unlock poll results (vote first option)
                        first_opt = None
                        try:
                            first_opt = msg.poll.poll.answers[0].option
                        except Exception:
                            # if structure differs, skip
                            logger.warning("Poll %s has no accessible answers structure", msg_id)
                            raise

                        await user(SendVoteRequest(peer=source_entity, msg_id=msg.id, options=[first_opt]))
                        await asyncio.sleep(1)  # let Telegram update

                        upd = await user.get_messages(source_entity, ids=msg_id)
                        # pick updated poll object if present
                        poll_obj = None
                        results = None

                        if getattr(upd, "poll", None) and getattr(upd.poll, "poll", None):
                            poll_obj = upd.poll.poll
                        elif getattr(msg, "poll", None) and getattr(msg.poll, "poll", None):
                            poll_obj = msg.poll.poll

                        if getattr(upd, "media", None) and hasattr(upd.media, "results"):
                            results = upd.media.results
                        elif getattr(msg, "media", None) and hasattr(msg.media, "results"):
                            results = msg.media.results

                        if not poll_obj:
                            raise ValueError("Could not parse poll object after voting.")

                        question = poll_obj.question
                        answers = [types.PollAnswer(a.text, a.option) for a in poll_obj.answers]

                        correct_answers = []
                        if results and getattr(results, "results", None):
                            for r in results.results:
                                if getattr(r, "correct", False):
                                    correct_answers.append(r.option)

                        solution = getattr(results, "solution", None) if results else None
                        solution_entities = getattr(results, "solution_entities", None) if results else None

                        # normalize solution/solution_entities so they are either both truthy or both falsy
                        if not solution:
                            solution = None
                            solution_entities = None
                        elif not solution_entities:
                            solution_entities = []

                        if settings["replace"]:
                            question = question.replace("[REMEDICS]", "[MediX]")
                            if solution:
                                solution = solution.replace("[REMEDICS]", "[MediX]")

                        await user.send_message(
                            dest_entity,
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
                        processed += 1
                        logger.info("Recreated poll %s -> success", msg_id)

                    except Exception as e_poll:
                        failed += 1
                        logger.exception("Poll %s recreation failed: %s", msg_id, e_poll)
                        # send a short message to owner so you see problem in chat
                        await event.respond(f"⚠️ Poll {msg_id} skipped: {e_poll}")

                else:
                    # not a poll -> copy text/media simply (replace text if needed)
                    text = msg.message
                    if text and settings["replace"]:
                        text = text.replace("[REMEDICS]", "[MediX]")
                    # send either text or forward non-poll media as new message
                    if msg.media and not text:
                        # send the media directly (non-forward)
                        await user.send_file(dest_entity, msg.media, caption=text)
                    else:
                        await user.send_message(dest_entity, text or None)
                    processed += 1
                    logger.info("Copied message %s", msg_id)

            # optional small delay to avoid hitting rate limits
            await asyncio.sleep(1.5)

            # update brief progress every 5 messages (edits status message)
            if (msg_id - start_id + 1) % 5 == 0:
                try:
                    await status_msg.edit(f"🚀 Progress: processed={processed} skipped={skipped} failed={failed} (at msg {msg_id})")
                except Exception:
                    pass

        except Exception as e_outer:
            failed += 1
            logger.exception("Failed processing message %s: %s", msg_id, e_outer)
            # show the error inline for quicker debugging
            await event.respond(f"❌ Error on {msg_id}: {e_outer}")
            await asyncio.sleep(2)

    # final status edit
    try:
        await status_msg.edit(
            f"✅ Done!\n\nProcessed: {processed}\nSkipped: {skipped}\nFailed: {failed}"
        )
    except Exception:
        await event.respond(f"✅ Done! Processed: {processed} Skipped: {skipped} Failed: {failed}")

# -------- start both clients --------
async def main():
    await user.start()
    logger.info("User client started.")
    await bot.start(bot_token=BOT_TOKEN)
    logger.info("Bot client started.")
    # helpful start message in logs
    logger.info("Bot is ready. OWNER_ID=%s. Use /status to check settings.", OWNER_ID)
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())

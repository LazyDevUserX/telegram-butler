#!/usr/bin/env python3
"""
Telegram Userbot controller using Telethon (user account) + python-telegram-bot (controller bot).
Features:
 - URL based operations (t.me links)
 - Forward ranges, delete ranges
 - Poll recreation when forwarding
 - Filters, per-user settings persisted to settings.json (works on Render with persistent disk)
 - Task queue with sequential processing, progress updates, cancellation
 - Owner-only commands (USER_ID env var)
 - /send_text command (queue-able)
"""

import os
import re
import json
import time
import math
import asyncio
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

# Telethon
from telethon import TelegramClient, errors
from telethon.tl.types import Message, PeerChannel, PeerChat, InputPeerChannel
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.errors.rpcerrorlist import ChatAdminRequiredError, MessageDeleteForbiddenError

# python-telegram-bot v20 (async)
from telegram import Update, ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# -- Config / Env vars
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "user_session")
USER_ID = int(os.environ.get("USER_ID", "0"))  # only this Telegram user can control the bot

SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "settings.json")
DEFAULT_DELAY = 1.0

# -- Globals
app_loop = asyncio.get_event_loop()
tele_client: TelegramClient = None  # will be set in main
task_queue = deque()
task_running = False
task_cancel_requested = False

# Regex to parse t.me links: https://t.me/<channel_or_user>/<msg_id>
TME_MSG_RE = re.compile(r"(?:https?://)?t\.me/(?P<chan>[-\w]+)/(?P<msg_id>\d+)")
# also support t.me/c/<internal_id>/<msg_id> (for private groups/channels)
TME_C_RE = re.compile(r"(?:https?://)?t\.me/c/(?P<int_id>\d+)/(?P<msg_id>\d+)")

# load/save settings (JSON)
def load_settings() -> Dict[str, Any]:
    if not os.path.exists(SETTINGS_FILE):
        default = {
            "default_source": None,
            "default_dest": None,
            "delay": DEFAULT_DELAY,
            "filters": [],  # list of types to skip: photo,video,sticker,gif,document,poll,text
        }
        save_settings(default)
        return default
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_settings(d: Dict[str, Any]):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

settings = load_settings()

# --------------------------
# Utilities: parse t.me links
# --------------------------
async def resolve_url_to_entity_and_msg(client: TelegramClient, url: str):
    """
    Accepts a t.me URL and returns (entity, message_id) where entity is Telethon entity.
    Supports:
      - https://t.me/channel/123
      - https://t.me/c/123/456  (private channel, internal id)
    """
    url = url.strip()
    m = TME_MSG_RE.search(url)
    if m:
        chan = m.group("chan")
        msg_id = int(m.group("msg_id"))
        # Resolve channel username to entity
        try:
            entity = await client.get_entity(chan)
        except Exception as e:
            raise ValueError(f"Could not resolve channel '{chan}': {e}")
        return entity, msg_id

    m2 = TME_C_RE.search(url)
    if m2:
        int_id = int(m2.group("int_id"))
        msg_id = int(m2.group("msg_id"))
        # Telethon identifies these as -100<id>
        peer_id = -1000000000000 + int_id if int_id < 1000000000000 else -1000000000000 + int_id
        # Simplify: try to fetch by peer id
        try:
            entity = await client.get_entity(peer_id)
        except Exception:
            # fallback: try get_dialogs and match by id
            raise ValueError(f"Could not resolve private chat with internal id {int_id}")
        return entity, msg_id

    raise ValueError("Invalid t.me message URL. Expected forms like https://t.me/channel/123 or https://t.me/c/123/456")

# --------------------------
# Task definitions
# --------------------------
class TaskItem:
    def __init__(self, task_type: str, params: dict, requested_by: int):
        self.task_type = task_type  # 'forward_range', 'delete_range', 'send_text'
        self.params = params
        self.requested_by = requested_by
        self.created_at = datetime.utcnow().isoformat()

# queue helpers
def enqueue_task(task: TaskItem) -> int:
    task_queue.append(task)
    return len(task_queue)

def dequeue_task() -> Optional[TaskItem]:
    if task_queue:
        return task_queue.popleft()
    return None

def clear_queue():
    task_queue.clear()

# --------------------------
# Progress utilities
# --------------------------
def render_progress(processed: int, total: int, start_time: float) -> str:
    if total == 0:
        pct = 100.0
    else:
        pct = processed / total * 100.0
    bars = 20
    filled = int(bars * (pct / 100.0))
    bar_str = "█" * filled + "░" * (bars - filled)
    elapsed = time.time() - start_time
    if processed == 0:
        eta = "Unknown"
    else:
        rate = elapsed / max(processed, 1)
        remaining = (total - processed) * rate
        eta_td = timedelta(seconds=int(remaining))
        eta = str(eta_td)
    return f"[{bar_str}] {pct:.1f}% — {processed}/{total} processed — ETA {eta}"

# --------------------------
# Core operations (telethon)
# --------------------------
async def recreate_poll_and_send(client: TelegramClient, dest_entity, original_msg: Message):
    """
    Recreate a poll in destination. Returns new message or raises.
    """
    poll = original_msg.poll
    if not poll:
        raise ValueError("Original message has no poll")
    q = poll.poll.question if hasattr(poll, "poll") else getattr(poll, "question", None)
    # Telethon's Message.poll might have different structure; try fallback
    question = getattr(poll, "question", None) or (getattr(poll, "poll", None) and getattr(poll.poll, "question", None))
    options_raw = getattr(poll, "options", None) or (getattr(poll, "poll", None) and getattr(poll.poll, "options", None))
    if not question or not options_raw:
        # fallback: try to read from message.media.poll if possible
        raise ValueError("Could not parse poll data")
    # extract option texts
    opts = []
    try:
        for o in options_raw:
            txt = getattr(o, "text", None) or getattr(o, "option", None)
            if txt is None:
                txt = str(o)
            opts.append(txt)
    except Exception:
        # as a last resort, forward normally
        raise

    # Telethon has send_poll method:
    # client.send_poll(entity, question, options, multiple=False, quiz=False, correct_option_id=None, is_anonymous=True)
    try:
        new_msg = await client.send_poll(dest_entity, question=question, options=opts, multiple=False, quiz=False)
        return new_msg
    except Exception as e:
        raise

async def forward_message_preserve(client: TelegramClient, src_entity, msg_id: int, dest_entity, with_header=True):
    """
    Forward a single message, optionally removing forwarded header (via copy).
    If with_header True: forward (preserves forwarded from)
    If with_header False: use message.copy() to copy without forwarded header (Telethon: client.forward_messages has flag as_copy?)
    """
    try:
        # Telethon's forward_messages signature:
        # await client.forward_messages(entity=dest, messages=msg_id, from_peer=src_entity, as_copy=not with_header)
        # Use as_copy parameter to avoid forwarded header when True -> as_copy=True removes header
        as_copy = not with_header
        res = await client.forward_messages(dest_entity, messages=msg_id, from_peer=src_entity, as_copy=as_copy)
        return res
    except Exception as e:
        raise

async def delete_message_safe(client: TelegramClient, src_entity, msg_id: int):
    try:
        await client.delete_messages(src_entity, msg_id)
        return True, None
    except MessageDeleteForbiddenError as e:
        return False, "Bot does not have permission to delete messages in target"
    except Exception as e:
        return False, str(e)

# --------------------------
# Worker that processes queued tasks
# --------------------------
async def worker_process():
    global task_running, task_cancel_requested
    while True:
        if not task_queue:
            await asyncio.sleep(1)
            continue
        if task_running:
            await asyncio.sleep(0.5)
            continue
        task_running = True
        task = dequeue_task()
        if not task:
            task_running = False
            continue
        try:
            await process_task_item(task)
        except Exception as e:
            # send critical error message to owner (via controller bot)
            try:
                await notify_owner_text(f"Critical error processing task {task.task_type}: {e}")
            except Exception:
                pass
        finally:
            task_running = False
            task_cancel_requested = False
        await asyncio.sleep(0.2)

# --------------------------
# Notify owner helper (controller bot)
# --------------------------
async def notify_owner_text(text: str):
    """Sends a message to the owner via controller bot. Uses python-telegram-bot Application context stored on loop."""
    try:
        # we assume ptb_app is attached to loop as attribute
        ptb_app = app_loop._ptb_app  # set in main
        await ptb_app.bot.send_message(chat_id=USER_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print("Failed to notify owner:", e)

# --------------------------
# Process individual task
# --------------------------
async def process_task_item(task: TaskItem):
    """
    Process TaskItem:
      - forward_range: params: start_url, end_url, dest_url (optional if default), noheader (bool)
      - delete_range: params: start_url, end_url
      - send_text: params: dest_url, text
    The function will send progress updates to the owner by editing a status message in the controller-bot chat.
    """
    global tele_client, settings, task_cancel_requested

    # create an initial status message to edit
    status_msg = await tele_client.loop.run_in_executor(None, lambda: None)  # dummy to get into coroutine
    # Instead of mixing Telethon and PTB editing, we will use ptb bot to send/edit the status message.
    ptb_app = app_loop._ptb_app
    status = await ptb_app.bot.send_message(chat_id=task.requested_by, text=f"Started task {task.task_type} ...")
    start_time = time.time()

    try:
        if task.task_type == "forward_range":
            p = task.params
            start_url = p.get("start_url")
            end_url = p.get("end_url")
            dest_url = p.get("dest_url") or settings.get("default_dest")
            noheader = p.get("noheader", False)
            if not dest_url:
                await status.edit_text("Destination not specified and no default dest set. Aborting.")
                return

            # resolve start and end
            try:
                src_entity_start, start_id = await resolve_url_to_entity_and_msg(tele_client, start_url)
                src_entity_end, end_id = await resolve_url_to_entity_and_msg(tele_client, end_url)
            except Exception as e:
                await status.edit_text(f"Error resolving source URLs: {e}")
                return

            # ensure both source entities are same
            if src_entity_start.id != src_entity_end.id:
                await status.edit_text("Start and end URLs refer to different chats/channels. Aborting.")
                return
            src_entity = src_entity_start
            # resolve dest
            try:
                dest_entity, _tmp = await resolve_url_to_entity_and_msg(tele_client, dest_url + "/1") if dest_url.endswith("/") is False else await resolve_url_to_entity_and_msg(tele_client, dest_url + "1")
            except Exception:
                # try resolve as channel username only
                try:
                    dest_entity = await tele_client.get_entity(dest_url.replace("https://t.me/", "").strip("/"))
                except Exception as e:
                    await status.edit_text(f"Error resolving destination: {e}")
                    return

            # Determine iteration direction
            start_id, end_id = int(start_id), int(end_id)
            if start_id <= end_id:
                msg_ids = list(range(start_id, end_id + 1))
            else:
                msg_ids = list(range(start_id, end_id -1, -1))

            total = len(msg_ids)
            processed = 0
            skipped = []
            for mid in msg_ids:
                if task_cancel_requested:
                    await status.edit_text(f"Task cancelled by user. Processed {processed}/{total}.")
                    return
                # fetch message
                try:
                    orig_msg = await tele_client.get_messages(src_entity, ids=mid)
                    if not orig_msg:
                        skipped.append((mid, "Original message not found/deleted"))
                        processed += 1
                        await status.edit_text(render_progress(processed, total, start_time))
                        await asyncio.sleep(settings.get("delay", DEFAULT_DELAY))
                        continue
                except Exception as e:
                    skipped.append((mid, f"Failed to fetch message: {e}"))
                    processed += 1
                    await status.edit_text(render_progress(processed, total, start_time))
                    await asyncio.sleep(settings.get("delay", DEFAULT_DELAY))
                    continue

                # check filter types
                filtered = False
                for ftype in settings.get("filters", []):
                    if ftype == "photo" and orig_msg.photo:
                        filtered = True
                    if ftype == "video" and orig_msg.video:
                        filtered = True
                    if ftype == "sticker" and orig_msg.sticker:
                        filtered = True
                    if ftype == "gif" and getattr(orig_msg, "giphy", False):
                        filtered = True
                    if ftype == "document" and orig_msg.document:
                        filtered = True
                    if ftype == "poll" and orig_msg.poll:
                        filtered = True
                    if ftype == "text" and orig_msg.raw_text and not getattr(orig_msg, "media", None):
                        filtered = True
                if filtered:
                    skipped.append((mid, "Filtered by user settings"))
                    processed += 1
                    await status.edit_text(render_progress(processed, total, start_time))
                    await asyncio.sleep(settings.get("delay", DEFAULT_DELAY))
                    continue

                # Poll handling: recreate poll instead of forwarding
                try:
                    if orig_msg.poll:
                        try:
                            await recreate_poll_and_send(tele_client, dest_entity, orig_msg)
                        except Exception as e:
                            # fallback: try forwarding
                            try:
                                await forward_message_preserve(tele_client, src_entity, mid, dest_entity, with_header=not noheader)
                            except Exception as e2:
                                skipped.append((mid, f"Poll recreate failed and forward failed: {e2}"))
                    else:
                        # normal forward or copy
                        try:
                            await forward_message_preserve(tele_client, src_entity, mid, dest_entity, with_header=not noheader)
                        except errors.FloodWaitError as fw:
                            await status.edit_text(f"Flood wait of {fw.seconds} seconds — pausing")
                            await asyncio.sleep(fw.seconds + 2)
                            # retry once
                            try:
                                await forward_message_preserve(tele_client, src_entity, mid, dest_entity, with_header=not noheader)
                            except Exception as e:
                                skipped.append((mid, f"Forward failed after flood wait: {e}"))
                        except Exception as e:
                            skipped.append((mid, f"Forward failed: {e}"))
                except Exception as e:
                    skipped.append((mid, f"General handling error: {e}"))

                processed += 1
                # update progress message every message
                await status.edit_text(render_progress(processed, total, start_time))
                await asyncio.sleep(settings.get("delay", DEFAULT_DELAY))

            # finished
            report_lines = [f"Task completed: forwarded range {start_url} -> {end_url} to {dest_url}"]
            if skipped:
                report_lines.append(f"Skipped {len(skipped)} messages:")
                for mid, reason in skipped:
                    report_lines.append(f"- {mid}: {reason}")
            await status.edit_text("\n".join(report_lines))

        elif task.task_type == "delete_range":
            p = task.params
            start_url = p.get("start_url")
            end_url = p.get("end_url")

            try:
                src_entity_start, start_id = await resolve_url_to_entity_and_msg(tele_client, start_url)
                src_entity_end, end_id = await resolve_url_to_entity_and_msg(tele_client, end_url)
            except Exception as e:
                await status.edit_text(f"Error resolving URLs: {e}")
                return

            if src_entity_start.id != src_entity_end.id:
                await status.edit_text("Start and end URLs are from different chats. Aborting.")
                return
            src_entity = src_entity_start
            start_id, end_id = int(start_id), int(end_id)
            if start_id <= end_id:
                msg_ids = list(range(start_id, end_id + 1))
            else:
                msg_ids = list(range(start_id, end_id -1, -1))

            total = len(msg_ids)
            processed = 0
            skipped = []
            # check admin permissions: ensure tele_client has rights to delete in src_entity
            try:
                # quick try delete a non-existent msg id with allow? Instead try GetParticipant for channels to check admin?
                # Simpler: attempt to delete the first message inside try and handle permission error
                pass
            except Exception:
                pass

            for mid in msg_ids:
                if task_cancel_requested:
                    await status.edit_text(f"Task cancelled by user. Processed {processed}/{total}.")
                    return

                try:
                    ok, reason = await delete_message_safe(tele_client, src_entity, mid)
                    if not ok:
                        skipped.append((mid, reason or "Unknown"))
                except errors.FloodWaitError as fw:
                    await status.edit_text(f"Flood wait of {fw.seconds} seconds — pausing")
                    await asyncio.sleep(fw.seconds + 2)
                    try:
                        ok, reason = await delete_message_safe(tele_client, src_entity, mid)
                        if not ok:
                            skipped.append((mid, reason or "Unknown after retry"))
                    except Exception as e:
                        skipped.append((mid, f"Delete failed after flood: {e}"))
                except Exception as e:
                    skipped.append((mid, f"Delete failed: {e}"))

                processed += 1
                await status.edit_text(render_progress(processed, total, start_time))
                await asyncio.sleep(settings.get("delay", DEFAULT_DELAY))

            report_lines = [f"Delete task finished for range {start_url} -> {end_url}"]
            if skipped:
                report_lines.append(f"Skipped {len(skipped)} messages:")
                for mid, reason in skipped:
                    report_lines.append(f"- {mid}: {reason}")
            await status.edit_text("\n".join(report_lines))

        elif task.task_type == "send_text":
            p = task.params
            dest_url = p.get("dest_url") or settings.get("default_dest")
            text = p.get("text", "")
            if not dest_url:
                await status.edit_text("Destination not specified and no default dest set. Aborting.")
                return
            try:
                dest_entity, _ = await resolve_url_to_entity_and_msg(tele_client, dest_url + "/1") if dest_url.endswith("/") is False else await resolve_url_to_entity_and_msg(tele_client, dest_url + "1")
            except Exception:
                try:
                    dest_entity = await tele_client.get_entity(dest_url.replace("https://t.me/", "").strip("/"))
                except Exception as e:
                    await status.edit_text(f"Error resolving destination: {e}")
                    return
            # send message
            try:
                await tele_client.send_message(dest_entity, text)
                await status.edit_text("Text sent successfully.")
            except Exception as e:
                await status.edit_text(f"Failed to send text: {e}")
        else:
            await status.edit_text(f"Unknown task type: {task.task_type}")
    finally:
        # ensure final save of settings
        save_settings(settings)

# --------------------------
# Controller bot command handlers (python-telegram-bot)
# --------------------------
def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if uid != USER_ID:
            await update.message.reply_text("Unauthorized.")
            return
        return await func(update, context)
    return wrapper

@owner_only
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>Available commands (owner only)</b>\n\n"
        "/help - show this message\n"
        "/ping - check bot\n"
        "/set_source <t.me/channel> - set default source channel\n"
        "/set_dest <t.me/channel> - set default destination channel\n        "/set_delay <seconds> - set delay between messages (float)\n"
        "/filter <type1> <type2> ... - set filters (photo video sticker gif document poll text)\n"
        "/filters - show current filters\n"
        "/forward <start_url> <end_url> <dest_url (optional)> [-noheader] - forward range (queued)\n"
        "/delete <start_url> <end_url> - delete range (queued)\n"
        "/send_text <dest_url> \"Your message here\" - send text to dest (queued)\n"
        "/status - show queue and current settings\n"
        "/cancel - cancel current task and clear queue\n"
    )
    await update.message.reply_html(help_text)

@owner_only
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("PONG")

@owner_only
async def set_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /set_source <t.me/channel_url>")
        return
    url = context.args[0]
    settings["default_source"] = url
    save_settings(settings)
    await update.message.reply_text(f"Default source set to {url}")

@owner_only
async def set_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /set_dest <t.me/channel_url>")
        return
    url = context.args[0]
    settings["default_dest"] = url
    save_settings(settings)
    await update.message.reply_text(f"Default destination set to {url}")

@owner_only
async def set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /set_delay <seconds>")
        return
    try:
        v = float(context.args[0])
        settings["delay"] = v
        save_settings(settings)
        await update.message.reply_text(f"Delay set to {v} seconds")
    except Exception:
        await update.message.reply_text("Invalid number")

@owner_only
async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /filter <type1> <type2> ...")
        return
    types = [a.lower() for a in context.args]
    allowed = {"photo","video","sticker","gif","document","poll","text"}
    invalid = [t for t in types if t not in allowed]
    if invalid:
        await update.message.reply_text(f"Invalid filter types: {invalid}. Allowed: {', '.join(sorted(allowed))}")
        return
    settings["filters"] = types
    save_settings(settings)
    await update.message.reply_text(f"Filters set: {', '.join(types)}")

@owner_only
async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fl = settings.get("filters", [])
    await update.message.reply_text("Current filters: " + (", ".join(fl) if fl else "(none)"))

@owner_only
async def forward_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /forward <start_url> <end_url> <dest_url optional> [-noheader]
    """
    text = update.message.text or ""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /forward <start_url> <end_url> <dest_url optional> [-noheader]")
        return
    start_url = args[0]
    end_url = args[1]
    dest_url = None
    noheader = False
    if len(args) >= 3:
        if args[2].startswith("-"):
            # maybe flag
            if args[2] == "-noheader":
                noheader = True
        else:
            dest_url = args[2]
    # check flags at end
    if "-noheader" in text:
        noheader = True

    task = TaskItem("forward_range", {"start_url": start_url, "end_url": end_url, "dest_url": dest_url, "noheader": noheader}, requested_by=update.effective_user.id)
    pos = enqueue_task(task)
    if pos == 1 and not task_running:
        await update.message.reply_text(f"Task queued and will start immediately (Position: {pos}).")
    else:
        await update.message.reply_text(f"Task queued (Position: {pos}).")

@owner_only
async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /delete <start_url> <end_url>")
        return
    start_url = context.args[0]
    end_url = context.args[1]
    task = TaskItem("delete_range", {"start_url": start_url, "end_url": end_url}, requested_by=update.effective_user.id)
    pos = enqueue_task(task)
    await update.message.reply_text(f"Delete task queued (Position: {pos}).")

@owner_only
async def send_text_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Expect: /send_text <dest_url> "Your message here"
    if len(context.args) < 2:
        await update.message.reply_text('Usage: /send_text <dest_url> "Your message here"')
        return
    dest_url = context.args[0]
    # join remaining args
    text = update.message.text
    # naive extraction: everything after the dest_url
    idx = text.find(dest_url)
    if idx == -1:
        await update.message.reply_text("Could not parse message text.")
        return
    remaining = text[idx + len(dest_url):].strip()
    if remaining.startswith('"') and remaining.endswith('"'):
        body = remaining.strip('"')
    else:
        # allow unquoted
        body = remaining
    task = TaskItem("send_text", {"dest_url": dest_url, "text": body}, requested_by=update.effective_user.id)
    pos = enqueue_task(task)
    await update.message.reply_text(f"Send_text queued (Position: {pos}).")

@owner_only
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qlen = len(task_queue)
    current = "Running" if task_running else "Idle"
    st = (
        f"Status: {current}\n"
        f"Queue length: {qlen}\n"
        f"Default source: {settings.get('default_source')}\n"
        f"Default dest: {settings.get('default_dest')}\n"
        f"Delay: {settings.get('delay')}s\n"
        f"Filters: {', '.join(settings.get('filters', [])) or '(none)'}\n"
    )
    await update.message.reply_text(st)

@owner_only
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global task_cancel_requested
    if task_running:
        task_cancel_requested = True
        clear_queue()
        await update.message.reply_text("Cancellation requested. Current task will stop as soon as possible and queue cleared.")
    else:
        clear_queue()
        await update.message.reply_text("Queue cleared. No task was running.")

# --------------------------
# Initialization & main
# --------------------------
async def start_background_workers():
    # start telethon client if not started
    global tele_client
    # start worker coroutine
    asyncio.create_task(worker_process())

async def main_async():
    global tele_client, app_loop
    # Telethon client (userbot)
    tele_client = TelegramClient(SESSION_STRING, API_ID, API_HASH)
    await tele_client.start()  # this will use SESSION_STRING (string or file) — ensure proper config on Render
    print("Telethon client started")

    # keep telethon client running in background
    # nothing else — tele_client will be used by worker

    # start background worker
    asyncio.create_task(worker_process())

def build_ptb_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # attach commands
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("set_source", set_source))
    app.add_handler(CommandHandler("set_dest", set_dest))
    app.add_handler(CommandHandler("set_delay", set_delay))
    app.add_handler(CommandHandler("filter", cmd_filter))
    app.add_handler(CommandHandler("filters", cmd_filters))
    app.add_handler(CommandHandler("forward", forward_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("send_text", send_text_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    # helper: set reply_markup? not necessary
    return app

def run():
    if not API_ID or not API_HASH or not BOT_TOKEN or not SESSION_STRING or not USER_ID:
        print("Missing one of API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, USER_ID env vars.")
        return
    # create PTB app and store it onto event loop so worker can access it
    ptb_app = build_ptb_app()
    # store onto loop for global access
    app_loop._ptb_app = ptb_app
    # start telethon and ptb concurrently
    async def runner():
        # start telethon
        global tele_client
        tele_client = TelegramClient(SESSION_STRING, API_ID, API_HASH, loop=asyncio.get_event_loop())
        await tele_client.start()
        print("Telethon (user) client started")
        # start worker
        asyncio.create_task(worker_process())
        # start ptb app (this will run forever)
        await ptb_app.initialize()
        await ptb_app.start()
        print("Controller bot started")
        # keep running until cancelled
        try:
            await ptb_app.updater.start_polling()  # ensures polling started; different in v20 but start does polling too
            # block forever
            while True:
                await asyncio.sleep(3600)
        finally:
            await ptb_app.stop()
            await ptb_app.shutdown()
            await tele_client.disconnect()

    try:
        app_loop.run_until_complete(runner())
    except KeyboardInterrupt:
        print("Shutting down...")
    except Exception as e:
        print("Fatal error:", e)

if __name__ == "__main__":
    run()

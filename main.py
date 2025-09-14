import os
import re
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import MessageIdInvalidError
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- ENV Vars ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# --- URL Parser ---
URL_REGEX = r"https://t\.me/(?P<chat>[\w\d_]+)/(?P<msg_id>\d+)"

def parse_msg_url(url: str):
    match = re.match(URL_REGEX, url)
    if not match:
        raise ValueError("Invalid Telegram message URL")
    return match.group("chat"), int(match.group("msg_id"))

# --- Forwarding Logic ---
async def forward_message(userbot, source_url, dest_url):
    src_chat, src_id = parse_msg_url(source_url)
    dest_chat, _ = parse_msg_url(dest_url)  # msg_id ignored for dest

    msg = await userbot.get_messages(src_chat, ids=src_id)
    if not msg:
        raise MessageIdInvalidError("Message not found")

    # Handle polls specially
    if msg.poll:
        await userbot.send_poll(
            dest_chat,
            question=msg.poll.question,
            options=[o.text for o in msg.poll.options]
        )
    else:
        await userbot.forward_messages(dest_chat, msg)

# --- Bot Handlers ---
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def forward_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Usage: /forward_one <source_url> <dest_url>")

    source_url, dest_url = context.args[0], context.args[1]
    try:
        await forward_message(context.userbot, source_url, dest_url)
        await update.message.reply_text(f"✅ Forwarded from {source_url} → {dest_url}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# --- Main Startup ---
async def main():
    # Start Telethon userbot
    userbot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await userbot.start()
    print("✅ Userbot logged in")

    # Start control bot
    app = Application.builder().token(BOT_TOKEN).build()
    app.userbot = userbot

    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("forward_one", forward_one))

    await app.initialize()
    await app.start()
    print("🤖 Control bot started")
    await app.updater.start_polling()

if __name__ == "__main__":
    asyncio.run(main())

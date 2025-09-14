import os
import asyncio
from pyrogram import Client
from telegram.ext import Application, CommandHandler

# =====================
# ENVIRONMENT VARIABLES
# =====================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")  # Updated to match your env
BOT_TOKEN = os.getenv("BOT_TOKEN")

# =====================
# USERBOT (Pyrogram)
# =====================
userbot = Client(
    SESSION_STRING,
    api_id=API_ID,
    api_hash=API_HASH
)

# =====================
# TELEGRAM BOT (PTB v20+)
# =====================
async def start(update, context):
    await update.message.reply_text("🤖 Hello! Bot is running on Render 🚀")

async def help_command(update, context):
    await update.message.reply_text("ℹ️ Available commands: /start /help")

async def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    print("✅ Bot logged in")
    await app.run_polling()

# =====================
# MAIN ENTRY POINT
# =====================
async def main():
    # Start both userbot and bot concurrently
    async with userbot:
        print("✅ Userbot logged in")
        await asyncio.gather(
            run_bot(),
        )

if __name__ == "__main__":
    asyncio.run(main())

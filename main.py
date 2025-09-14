import asyncio
import json
import os
from telethon import TelegramClient
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

# --- Load env vars ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")   # Exported from Telethon
BOT_TOKEN = os.getenv("BOT_TOKEN")

# --- Telethon Userbot ---
userbot = TelegramClient("userbot", API_ID, API_HASH)
if SESSION_STRING:
    userbot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# --- Aiogram Bot ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# --- Config placeholder (JSON file) ---
CONFIG_FILE = "config.json"
if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "w") as f:
        json.dump({}, f)


# --- /status command ---
@dp.message_handler(commands=["status"])
async def status_cmd(message: types.Message):
    try:
        me = await userbot.get_me()
        await message.reply(
            f"✅ Bot is running\n👤 Userbot logged in as: {me.first_name} (@{me.username})"
        )
    except Exception as e:
        await message.reply(f"❌ Userbot error: {e}")


# --- Main entrypoint ---
async def main():
    # Start both userbot & aiogram bot
    await userbot.start()
    print("Userbot connected ✅")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

# file: telegram_bot.py

import asyncio
import httpx
import os
import tempfile
import time
import re
import configparser
import sys

from telegram import Update, Message
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import libtorrent as lt

from download_torrent import download_with_progress

# --- CONFIGURATION & HELPERS ---
DOWNLOAD_SAVE_PATH = "C:\\Users\\Ryan\\Desktop\\Telegram Downloads"

def escape_markdown(text: str) -> str:
    """Helper function to escape telegram's special characters."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def get_bot_token() -> str:
    config = configparser.ConfigParser()
    config_path = 'bot_token.ini'
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file '{config_path}' not found. Please create it.")
    config.read(config_path)
    token = config.get('telegram', 'token', fallback=None)
    if not token or token == "YOUR_SECRET_TOKEN_HERE":
        raise ValueError(f"Bot token not found or not set in '{config_path}'.")
    return token

# --- BOT HANDLER FUNCTIONS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    await update.message.reply_text("Hello! I am your personal torrent downloader.\nSend me a direct URL to a .torrent file to begin, or use /cancel to stop the current download.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    await update.message.reply_text("Send a URL ending in .torrent to start a download.\nUse the /cancel command to stop the current download for your chat.")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.message.chat_id
    if chat_id in context.bot_data:
        task: asyncio.Task = context.bot_data[chat_id]
        task.cancel()
        await update.message.reply_text("✅ Cancellation request sent.")
    else:
        await update.message.reply_text("ℹ️ There are no active downloads for you to cancel.")


async def download_task_wrapper(url: str, message: Message, context: ContextTypes.DEFAULT_TYPE):
    """A wrapper function that contains the main download logic."""
    progress_message = await message.reply_text("✅ URL received. Downloading .torrent file...")
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, follow_redirects=True, timeout=30)
            response.raise_for_status()
    except httpx.RequestError as e:
        safe_error = escape_markdown(str(e))
        await progress_message.edit_text(f"❌ *Error:* Failed to download from URL\.\n`{safe_error}`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    
    with tempfile.TemporaryDirectory() as temp_dir:
        original_filename = url.split('/')[-1]
        temp_torrent_path = os.path.join(temp_dir, original_filename)

        with open(temp_torrent_path, 'wb') as f: f.write(response.content)

        safe_filename = escape_markdown(original_filename)
        await progress_message.edit_text(f"⏳ \\.torrent file `{safe_filename}` downloaded\\. Starting content download\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)

        last_update_time = 0
        async def report_progress(status: lt.torrent_status):   #type:ignore
            nonlocal last_update_time
            state_str = status.state.name
            progress_percent = status.progress * 100
            speed_mbps = status.download_rate / 1024 / 1024
            
            print(f"[LOG] {original_filename}: {progress_percent:.2f}% | Peers: {status.num_peers} | Speed: {speed_mbps:.2f} MB/s")
            
            current_time = time.monotonic()
            if current_time - last_update_time > 5:
                last_update_time = current_time
                
                # --- THE PRIMARY FIX IS HERE ---
                progress_str = escape_markdown(f"{progress_percent:.2f}")
                speed_str = escape_markdown(f"{speed_mbps:.2f}")
                safe_state_str = escape_markdown(state_str) # Escape the state string

                telegram_message = (
                    f"⬇️ *Downloading:* `{safe_filename}`\n"
                    f"*Progress:* {progress_str}%\n"
                    f"*Peers:* {status.num_peers}\n"
                    f"*Speed:* {speed_str} MB/s\n"
                    f"*State:* {safe_state_str}" # Use the escaped state string
                )
                try:
                    await progress_message.edit_text(telegram_message, parse_mode=ParseMode.MARKDOWN_V2)
                except Exception as e:
                    print(f"[WARN] Could not edit Telegram message: {e}")

        try:
            success = await download_with_progress(temp_torrent_path, DOWNLOAD_SAVE_PATH, report_progress)
            if success:
                await progress_message.edit_text("✅ *Success\!* The download is complete\.", parse_mode=ParseMode.MARKDOWN_V2)
        except asyncio.CancelledError:
            await progress_message.edit_text("⏹️ *Cancelled:* The download was successfully stopped\.", parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            safe_error = escape_markdown(str(e))
            await progress_message.edit_text(f"❌ *Error:* An unexpected error occurred during download\.\n`{safe_error}`", parse_mode=ParseMode.MARKDOWN_V2)
        finally:
            if message.chat_id in context.bot_data:
                del context.bot_data[message.chat_id]

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    chat_id = update.message.chat_id
    url = update.message.text
    
    if not (url.startswith(('http://', 'https://')) and url.endswith('.torrent')):
        await update.message.reply_text("This doesn't look like a valid .torrent URL.")
        return

    if chat_id in context.bot_data:
        await update.message.reply_text("ℹ️ You already have a download in progress. Please /cancel it before starting a new one.")
        return

    task = asyncio.create_task(download_task_wrapper(url, update.message, context))
    context.bot_data[chat_id] = task

# --- MAIN SCRIPT EXECUTION ---
if __name__ == '__main__':
    if not os.path.exists(DOWNLOAD_SAVE_PATH): os.makedirs(DOWNLOAD_SAVE_PATH)
    try:
        BOT_TOKEN = get_bot_token()
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)

    print("Starting bot...")
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    
    application.run_polling()
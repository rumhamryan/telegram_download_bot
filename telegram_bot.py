# file: telegram_bot.py
import asyncio
import httpx
import os
import tempfile
import time
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import libtorrent as lt

# --- IMPORT OUR LIBRARY FUNCTION ---
from download_torrent import download_with_progress

# --- CONFIGURATION ---
BOT_TOKEN = "7551344874:AAFWEtjEHQ-VD4E9Z2tc4Z4eZdpUNCJRV7Q" 
DOWNLOAD_SAVE_PATH = "C:\\Users\\Ryan\\Desktop\\Telegram Downloads"

# --- HELPER FUNCTION ---
def escape_markdown(text: str) -> str:
    """Helper function to escape telegram's special characters."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# --- BOT HANDLER FUNCTIONS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    # No markdown here, so no changes needed.
    await update.message.reply_text("Hello! I am your personal torrent downloader.\nSend me a direct URL to a .torrent file to begin.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    # No markdown here, so no changes needed.
    await update.message.reply_text("Simply send a message containing a URL that ends in .torrent.")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    url = update.message.text
    
    if not (url.startswith(('http://', 'https://')) and url.endswith('.torrent')):
        await update.message.reply_text("This doesn't look like a valid .torrent URL.")
        return

    progress_message = await update.message.reply_text("✅ URL received. Downloading .torrent file...")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, follow_redirects=True, timeout=30)
            response.raise_for_status()
        except httpx.RequestError as e:
            # Escape the error message just in case it contains special characters
            safe_error = escape_markdown(str(e))
            await progress_message.edit_text(f"❌ *Error:* Failed to download from URL.\n`{safe_error}`", parse_mode=ParseMode.MARKDOWN_V2)
            return

    with tempfile.TemporaryDirectory() as temp_dir:
        original_filename = url.split('/')[-1]
        temp_torrent_path = os.path.join(temp_dir, original_filename)

        with open(temp_torrent_path, 'wb') as f:
            f.write(response.content)

        # Escape the filename for safe display in Telegram
        safe_filename = escape_markdown(original_filename)
        
        # --- THE FIRST FIX IS HERE ---
        # Manually escape the literal '.' characters in the string and use backticks.
        await progress_message.edit_text(
            f"⏳ \\.torrent file `{safe_filename}` downloaded\\. Starting content download\\.\\.", 
            parse_mode=ParseMode.MARKDOWN_V2
        )

        last_update_time = 0
        
        async def report_progress(status: lt.torrent_status): # type:ignore
            nonlocal last_update_time
            state_str = status.state.name
            progress_percent = status.progress * 100
            speed_mbps = status.download_rate / 1024 / 1024
            
            print(f"[LOG] {original_filename}: {progress_percent:.2f}% | Peers: {status.num_peers} | Speed: {speed_mbps:.2f} MB/s")
            
            current_time = time.monotonic()
            if current_time - last_update_time > 5:
                last_update_time = current_time
                
                progress_str = escape_markdown(f"{progress_percent:.2f}")
                speed_str = escape_markdown(f"{speed_mbps:.2f}")
                
                telegram_message = (
                    f"⬇️ *Downloading:* `{safe_filename}`\n"
                    f"*Progress:* {progress_str}%\n"
                    f"*Peers:* {status.num_peers}\n"
                    f"*Speed:* {speed_str} MB/s\n"
                    f"*State:* {state_str}"
                )
                try:
                    await progress_message.edit_text(telegram_message, parse_mode=ParseMode.MARKDOWN_V2)
                except Exception as e:
                    print(f"[WARN] Could not edit Telegram message: {e}")

        success = await download_with_progress(temp_torrent_path, DOWNLOAD_SAVE_PATH, report_progress)

        if success:
            await progress_message.edit_text("✅ *Success \n!* The download is complete\n.", parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await progress_message.edit_text("❌ *Error:* The torrent download failed\n.", parse_mode=ParseMode.MARKDOWN_V2)

# --- MAIN SCRIPT EXECUTION ---
if __name__ == '__main__':
    if not os.path.exists(DOWNLOAD_SAVE_PATH): os.makedirs(DOWNLOAD_SAVE_PATH)
    print("Starting bot...")
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.run_polling()
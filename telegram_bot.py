# file: telegram_bot.py
import asyncio
import httpx
import os
import tempfile
import time
import re
import configparser
import sys
import math

from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
import libtorrent as lt

from download_torrent import download_with_progress

# --- CONFIGURATION & HELPERS ---
DOWNLOAD_SAVE_PATH = "C:\\Users\\Ryan\\Desktop\\Telegram Downloads"

def escape_markdown(text: str) -> str:
    """Helper function to escape telegram's special characters."""
    # Using a raw string for the regex pattern is a best practice.
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(rf'([{re.escape(escape_chars)}])', r'\\\1', text)

def get_bot_token() -> str:
    """Reads the bot token from the bot_token.ini file."""
    config = configparser.ConfigParser()
    config_path = 'bot_token.ini'
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file '{config_path}' not found. Please create it.")
    
    config.read(config_path)
    token = config.get('telegram', 'token', fallback=None)
    
    if not token or token == "YOUR_SECRET_TOKEN_HERE":
        raise ValueError(f"Bot token not found or not set in '{config_path}'. Please add '[telegram]' section with 'token = YOUR_TOKEN'.")
    return token

def format_bytes(size_bytes: int) -> str:
    """Formats a size in bytes into a human-readable string (KB, MB, GB, etc.)."""
    if size_bytes == 0: return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

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
        print(f"[INFO] Received /cancel command from chat_id {chat_id}. Attempting to cancel active task.")
        task: asyncio.Task = context.bot_data[chat_id]
        task.cancel()
        await update.message.reply_text("✅ Cancellation request sent.")
    else:
        print(f"[INFO] Received /cancel command from chat_id {chat_id}, but no active task was found.")
        await update.message.reply_text("ℹ️ There are no active downloads for you to cancel.")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles a URL, downloads the .torrent file, and presents metadata for confirmation.
    """
    if not update.message or not update.message.text: return
    chat_id = update.message.chat_id
    url = update.message.text
    
    print(f"[INFO] Received message from chat_id {chat_id}.")

    if not (url.startswith(('http://', 'https://')) and url.endswith('.torrent')):
        print(f"[INFO] Message from {chat_id} was not a valid .torrent URL.")
        await update.message.reply_text("This does not appear to be a valid .torrent URL.")
        return
        
    if chat_id in context.bot_data:
        print(f"[WARN] User {chat_id} tried to start a new download while one is active.")
        await update.message.reply_text("ℹ️ You already have a download in progress. Please /cancel it before starting a new one.")
        return

    progress_message = await update.message.reply_text("✅ URL received. Analyzing .torrent file...")
    print(f"[INFO] Downloading .torrent file from URL: {url}")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, follow_redirects=True, timeout=30)
            response.raise_for_status()
    except httpx.RequestError as e:
        print(f"[ERROR] Failed to download .torrent from URL for chat_id {chat_id}. Reason: {e}")
        safe_error = escape_markdown(str(e))
        await progress_message.edit_text(rf"❌ *Error:* Failed to download from URL\." + f"\n`{safe_error}`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    with tempfile.NamedTemporaryFile(delete=False, suffix=".torrent") as temp_file:
        temp_file.write(response.content)
        temp_torrent_path = temp_file.name

    print(f"[SUCCESS] Downloaded and saved .torrent to temporary path: {temp_torrent_path}")

    try:
        ti = lt.torrent_info(temp_torrent_path)  # type: ignore
    except RuntimeError:
        print(f"[ERROR] Failed to parse .torrent file for chat_id {chat_id}.")
        await progress_message.edit_text(r"❌ *Error:* The provided file is not a valid torrent\.", parse_mode=ParseMode.MARKDOWN_V2)
        os.remove(temp_torrent_path)
        return

    files = ti.files()
    num_files = files.num_files()

    file_list = [
        rf"`{escape_markdown(files.file_path(i))}` \({escape_markdown(format_bytes(files.file_size(i)))}\)"
        for i in range(num_files)
    ]
    
    if len(file_list) > 10:
        file_list = file_list[:10] + ["`...and more`"]
    
    total_size_str = escape_markdown(format_bytes(ti.total_size()))
    
    confirmation_text = (
        f"🔎 *Torrent Details*\n\n"
        f"*Name:* `{escape_markdown(ti.name())}`\n"
        f"*Total Size:* {total_size_str}\n"
        f"*Files \({num_files}\):*\n" + "\n".join(file_list) +
        f"\n\nDo you want to start this download?"
    )

    keyboard = [[
        InlineKeyboardButton("✅ Confirm Download", callback_data="confirm_download"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await progress_message.edit_text(confirmation_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    print(f"[INFO] Sent confirmation prompt to chat_id {chat_id} for torrent '{ti.name()}'.")
    
    if context.user_data is None:
        print("[ERROR] context.user_data was None for chat_id {chat_id}. This is unexpected. Aborting operation.")
        await progress_message.edit_text(r"❌ *Error:* Could not access user session data\. Please try again\.", parse_mode=ParseMode.MARKDOWN_V2)
        if os.path.exists(temp_torrent_path):
            os.remove(temp_torrent_path)
        return

    context.user_data['pending_torrent_path'] = temp_torrent_path

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user pressing an inline keyboard button (Confirm or Cancel)."""
    query = update.callback_query
    if not query: return
    await query.answer()

    # Log the button press event
    print(f"[INFO] Received button press from user {query.from_user.id}: '{query.data}'")

    if not context.user_data:
        print(f"[WARN] Button press from user {query.from_user.id} ignored: No user_data found.")
        await query.edit_message_text("This action has expired (no user data).")
        return

    if 'pending_torrent_path' not in context.user_data:
        print(f"[WARN] Button press from user {query.from_user.id} ignored: No pending torrent found.")
        await query.edit_message_text("This action has already been completed or has expired.")
        return

    message = query.message
    if not isinstance(message, Message):
        print(f"[WARN] Button press from user {query.from_user.id} ignored: Original message is inaccessible.")
        await query.edit_message_text("This action is no longer valid as the original message is gone.")
        context.user_data.pop('pending_torrent_path', None)
        return

    temp_torrent_path = context.user_data.pop('pending_torrent_path')
    
    if query.data == "confirm_download":
        print(f"[SUCCESS] Download confirmed by user {message.chat_id}. Queuing download task.")
        await query.edit_message_text("✅ Confirmation received. Your download has been queued.")
        task = asyncio.create_task(download_task_wrapper(temp_torrent_path, message, context))
        context.bot_data[message.chat_id] = task

    elif query.data == "cancel_operation":
        print(f"[CANCEL] Operation cancelled by user {message.chat_id} via button.")
        await query.edit_message_text("❌ Operation cancelled by user.")
        if os.path.exists(temp_torrent_path):
            os.remove(temp_torrent_path)

async def download_task_wrapper(torrent_path: str, message: Message, context: ContextTypes.DEFAULT_TYPE):
    """A wrapper function that contains the main download logic."""
    progress_message = message
    original_filename = os.path.basename(torrent_path)
    safe_filename = escape_markdown(original_filename)

    print(f"[INFO] Starting download task for '{original_filename}' for chat_id {message.chat_id}.")
    await progress_message.edit_text(rf"⏳ \.torrent file `{safe_filename}` downloaded\. Starting content download\.\.", parse_mode=ParseMode.MARKDOWN_V2)

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
            progress_str = escape_markdown(f"{progress_percent:.2f}")
            speed_str = escape_markdown(f"{speed_mbps:.2f}")
            safe_state_str = escape_markdown(state_str)
            telegram_message = (f"⬇️ *Downloading:* `{safe_filename}`\n*Progress:* {progress_str}%\n*State:* {safe_state_str}\n*Peers:* {status.num_peers}\n*Speed:* {speed_str} MB/s")
            try:
                await progress_message.edit_text(telegram_message, parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as e:
                print(f"[WARN] Could not edit Telegram message: {e}")

    try:
        success = await download_with_progress(torrent_path, DOWNLOAD_SAVE_PATH, report_progress)
        if success:
            print(f"[SUCCESS] Download task for '{original_filename}' completed.")
            await progress_message.edit_text(r"✅ *Success\!* The download is complete\.", parse_mode=ParseMode.MARKDOWN_V2)
    except asyncio.CancelledError:
        print(f"[CANCEL] Download task for '{original_filename}' was cancelled by user {message.chat_id}.")
        await progress_message.edit_text(r"⏹️ *Cancelled:* The download was successfully stopped\.", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        print(f"[ERROR] An unexpected exception occurred in download task for '{original_filename}': {e}")
        safe_error = escape_markdown(str(e))
        await progress_message.edit_text(rf"❌ *Error:* An unexpected error occurred\." + f"\n`{safe_error}`", parse_mode=ParseMode.MARKDOWN_V2)
    finally:
        print(f"[INFO] Cleaning up resources for task '{original_filename}' for chat_id {message.chat_id}.")
        if message.chat_id in context.bot_data:
            del context.bot_data[message.chat_id]
        if os.path.exists(torrent_path):
            os.remove(torrent_path)

# --- MAIN SCRIPT EXECUTION ---
if __name__ == '__main__':
    if not os.path.exists(DOWNLOAD_SAVE_PATH):
        os.makedirs(DOWNLOAD_SAVE_PATH)
    try:
        BOT_TOKEN = get_bot_token()
    except (FileNotFoundError, ValueError) as e:
        print(f"CRITICAL ERROR: {e}")
        sys.exit(1)

    print("Starting bot...")
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    application.run_polling()
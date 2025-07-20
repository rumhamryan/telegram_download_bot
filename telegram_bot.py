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

def escape_markdown(text: str) -> str:
    """Helper function to escape telegram's special characters."""
    # Using a raw string for the regex pattern is a best practice.
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(rf'([{re.escape(escape_chars)}])', r'\\\1', text)

def get_configuration() -> tuple[str, str]:
    """Reads bot token and save path from the bot_token.ini file."""
    config = configparser.ConfigParser()
    config_path = 'bot_token.ini'
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file '{config_path}' not found. Please create it.")
    
    config.read(config_path)
    
    # Read the bot token
    token = config.get('telegram', 'token', fallback=None)
    if not token or token == "YOUR_SECRET_TOKEN_HERE":
        raise ValueError(f"Bot token not found or not set in '{config_path}'. Please add '[telegram]' section with 'token = YOUR_TOKEN'.")
        
    # Read the save path
    save_path = config.get('telegram', 'save_path', fallback=None)
    if not save_path:
        raise ValueError(f"Download 'save_path' not found or not set in '{config_path}'. Please add it under the '[telegram]' section.")

    return token, save_path

def clean_filename(name: str) -> str:
    """
    Cleans a torrent name by extracting the movie title and year,
    and discarding all other tags and metadata.
    """
    # 1. Replace all dots and underscores with spaces.
    # This makes the string easier to parse.
    cleaned_name = re.sub(r'[\._]', ' ', name)

    # 2. Find the year (a 4-digit number between 1900 and 2099).
    # This is our primary anchor point.
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', cleaned_name)

    if year_match:
        # If a year is found, everything before it is the title.
        year = year_match.group(1)
        title = cleaned_name[:year_match.start()].strip()
        
        # 3. Further clean the title by removing any leftover junk in parentheses
        # that isn't part of the title itself.
        title = re.sub(r'\s*\(.*?\)\s*', '', title).strip()

        # 4. Reconstruct the string in the desired "Title (Year)" format.
        return f"{title} ({year})"
    else:
        # If no year is found, we do our best with a simpler cleaning method.
        # This part handles filenames that don't follow the "Title Year" pattern.
        # It removes content in brackets and common keywords.
        tags_to_remove = [
            r'\[.*?\]',  # Tags in square brackets
            r'\(.*?\)',  # Tags in parentheses
            r'\b(1080p|720p|480p|x264|x265|hevc|BluRay|WEB-DL|AAC|DTS|HDTV|RM4k|CC|10bit|commentary|HeVK)\b'
        ]
        regex_pattern = '|'.join(tags_to_remove)
        
        # Remove the file extension at the very end
        no_ext = os.path.splitext(cleaned_name)[0]
        cleaned_name = re.sub(regex_pattern, '', no_ext, flags=re.I)
        
        # Collapse multiple spaces into one.
        return re.sub(r'\s+', ' ', cleaned_name).strip()

def get_dominant_file_type(files: lt.file_storage) -> str: # type: ignore
    """
    Finds the file extension of the largest file in the torrent,
    which is likely the main content file.
    """
    if files.num_files() == 0:
        return "N/A"

    largest_file_index = -1
    max_size = -1

    for i in range(files.num_files()):
        if files.file_size(i) > max_size:
            max_size = files.file_size(i)
            largest_file_index = i
            
    # Get the filename of the largest file
    largest_filename = files.file_path(largest_file_index)
    
    # Extract the extension
    _, extension = os.path.splitext(largest_filename)
    
    if extension:
        return extension[1:].upper() # Return 'MP4', 'MKV', etc.
    return "N/A"

def format_bytes(size_bytes: int) -> str:
    """Formats a size in bytes into a human-readable string (KB, MB, GB, etc.)."""
    if size_bytes <= 0: return "0B" # Changed to handle zero and negative
    size_name = ("B", "KB", "MB", "GB", "TB")
    try:
        i = int(math.floor(math.log(size_bytes, 1024)))
    except ValueError:
        i = 0 # Handle cases where size_bytes is 0
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
    
    if not (url.startswith(('http://', 'https://')) and url.endswith('.torrent')):
        await update.message.reply_text("This does not appear to be a valid .torrent URL.")
        return
        
    if chat_id in context.bot_data:
        await update.message.reply_text("ℹ️ You already have a download in progress. Please /cancel it before starting a new one.")
        return

    progress_message = await update.message.reply_text("✅ URL received. Analyzing .torrent file...")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, follow_redirects=True, timeout=30)
            response.raise_for_status()
    except httpx.RequestError as e:
        safe_error = escape_markdown(str(e))
        await progress_message.edit_text(rf"❌ *Error:* Failed to download from URL\." + f"\n`{safe_error}`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    with tempfile.NamedTemporaryFile(delete=False, suffix=".torrent") as temp_file:
        temp_file.write(response.content)
        temp_torrent_path = temp_file.name

    try:
        ti = lt.torrent_info(temp_torrent_path)  # type: ignore
    except RuntimeError:
        print(f"[ERROR] Failed to parse .torrent file for chat_id {chat_id}.")
        await progress_message.edit_text(r"❌ *Error:* The provided file is not a valid torrent\.", parse_mode=ParseMode.MARKDOWN_V2)
        os.remove(temp_torrent_path)
        return
    
    files = ti.files()
    
    # Use our new helper functions to get cleaned-up data
    cleaned_name_str = clean_filename(ti.name())
    file_type_str = get_dominant_file_type(files)
    total_size_str = format_bytes(ti.total_size())

    # Escape all strings for safe display in Markdown
    safe_name = escape_markdown(cleaned_name_str)
    safe_file_type = escape_markdown(file_type_str)
    safe_total_size = escape_markdown(total_size_str)
    
    # Build the new, cleaner confirmation message
    confirmation_text = (
        f"🔎 *Torrent Details*\n\n"
        f"*Name:* {safe_name}\n"
        f"*File Type:* {safe_file_type}\n"
        f"*Total Size:* {safe_total_size}\n\n"
        f"Do you want to start this download?"
    )

    keyboard = [[
        InlineKeyboardButton("✅ Confirm Download", callback_data="confirm_download"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await progress_message.edit_text(confirmation_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    print(f"[INFO] Sent confirmation prompt to chat_id {chat_id} for torrent '{cleaned_name_str}'.")
    
    if context.user_data is None:
        print(f"[ERROR] context.user_data was None for chat_id {chat_id}. This is unexpected. Aborting operation.")
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
        
        # CORRECTED WAY: Retrieve the save path from bot_data dictionary.
        download_path = context.bot_data["DOWNLOAD_SAVE_PATH"]
        
        task = asyncio.create_task(download_task_wrapper(temp_torrent_path, message, context, download_path))
        context.bot_data[message.chat_id] = task

    elif query.data == "cancel_operation":
        print(f"[CANCEL] Operation cancelled by user {message.chat_id} via button.")
        await query.edit_message_text("❌ Operation cancelled by user.")
        if os.path.exists(temp_torrent_path):
            os.remove(temp_torrent_path)

async def download_task_wrapper(torrent_path: str, message: Message, context: ContextTypes.DEFAULT_TYPE, save_path: str):
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
        success = await download_with_progress(torrent_path, save_path, report_progress)
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
    try:
        # Read both configuration values at once.
        BOT_TOKEN, DOWNLOAD_SAVE_PATH = get_configuration()
    except (FileNotFoundError, ValueError) as e:
        # The same error handling now catches issues with the token OR the save_path.
        print(f"CRITICAL ERROR: {e}")
        sys.exit(1)

    # Ensure the configured download directory exists.
    if not os.path.exists(DOWNLOAD_SAVE_PATH):
        print(f"INFO: Download path '{DOWNLOAD_SAVE_PATH}' not found. Creating it.")
        os.makedirs(DOWNLOAD_SAVE_PATH)

    print("Starting bot...")
    
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # CORRECTED WAY: Store the save path in the bot_data dictionary.
    # This dictionary is designed for storing global bot-related data.
    application.bot_data["DOWNLOAD_SAVE_PATH"] = DOWNLOAD_SAVE_PATH
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    application.run_polling()
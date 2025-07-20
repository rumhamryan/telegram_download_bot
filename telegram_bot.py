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
from typing import Optional, Dict

from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from telegram.error import BadRequest
import libtorrent as lt

from download_torrent import download_with_progress

# --- CONFIGURATION & NEW CONSTANTS ---
MAX_TORRENT_SIZE_GB = 10
MAX_TORRENT_SIZE_BYTES = MAX_TORRENT_SIZE_GB * (1024**3)
ALLOWED_EXTENSIONS = ['.mkv', '.mp4']

def escape_markdown(text: str) -> str:
    """Helper function to escape telegram's special characters."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(rf'([{re.escape(escape_chars)}])', r'\\\1', text)

def get_configuration() -> tuple[str, str]:
    """Reads bot token and save path from the bot_token.ini file."""
    config = configparser.ConfigParser()
    config_path = 'bot_token.ini'
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file '{config_path}' not found. Please create it.")
    
    config.read(config_path)
    
    token = config.get('telegram', 'token', fallback=None)
    if not token or token == "YOUR_SECRET_TOKEN_HERE":
        raise ValueError(f"Bot token not found or not set in '{config_path}'. Please add '[telegram]' section with 'token = YOUR_TOKEN'.")
        
    save_path = config.get('telegram', 'save_path', fallback=None)
    if not save_path:
        raise ValueError(f"Download 'save_path' not found or not set in '{config_path}'. Please add it under the '[telegram]' section.")

    return token, save_path

def clean_filename(name: str) -> str:
    cleaned_name = re.sub(r'[\._]', ' ', name)
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', cleaned_name)

    if year_match:
        year = year_match.group(1)
        title = cleaned_name[:year_match.start()].strip()
        title = re.sub(r'\s*\(.*?\)\s*', '', title).strip()
        return f"{title} ({year})"
    else:
        tags_to_remove = [
            r'\[.*?\]', r'\(.*?\)',
            r'\b(1080p|720p|480p|x264|x265|hevc|BluRay|WEB-DL|AAC|DTS|HDTV|RM4k|CC|10bit|commentary|HeVK)\b'
        ]
        regex_pattern = '|'.join(tags_to_remove)
        no_ext = os.path.splitext(cleaned_name)[0]
        cleaned_name = re.sub(regex_pattern, '', no_ext, flags=re.I)
        return re.sub(r'\s+', ' ', cleaned_name).strip()

def get_dominant_file_type(files: lt.file_storage) -> str: # type: ignore
    if files.num_files() == 0: return "N/A"
    largest_file_index = max(range(files.num_files()), key=files.file_size)
    largest_filename = files.file_path(largest_file_index)
    _, extension = os.path.splitext(largest_filename)
    return extension[1:].upper() if extension else "N/A"

def format_bytes(size_bytes: int) -> str:
    if size_bytes <= 0: return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024))) if size_bytes > 0 else 0
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

async def fetch_metadata_from_magnet(magnet_link: str, progress_message: Message) -> Optional[lt.torrent_info]: # type: ignore
    """Creates a temporary session to fetch metadata from a magnet link."""
    await progress_message.edit_text("🔗 Magnet link detected. Fetching metadata... (This may take up to 60s)")
    
    settings = {
        'enable_dht': True,
        'listen_interfaces': '0.0.0.0:6881',
        'dht_bootstrap_nodes': 'router.utorrent.com:6881,router.bittorrent.com:6881,dht.transmissionbt.com:6881'
    }
    ses = lt.session(settings) # type: ignore

    params = lt.parse_magnet_uri(magnet_link) # type: ignore
    params.save_path = tempfile.gettempdir() 
    params.upload_mode = True 
    handle = ses.add_torrent(params)

    for i in range(60): # 60 second timeout
        if not handle.is_valid():
            print("[ERROR] Magnet link handle became invalid during metadata fetch.")
            return None

        if handle.status().has_metadata:
            print(f"[INFO] Metadata fetched successfully for magnet after {i}s.")
            # --- FIX for DeprecationWarning ---
            # The modern way to get torrent_info is via torrent_file()
            info = handle.torrent_file() 
            ses.remove_torrent(handle)
            return info
        await asyncio.sleep(1)

    print("[ERROR] Timed out fetching metadata from magnet link.")
    ses.remove_torrent(handle)
    return None

def validate_torrent_files(ti: lt.torrent_info) -> Optional[str]: # type: ignore
    """Checks if the torrent's files are of an allowed type."""
    files = ti.files()
    if files.num_files() == 0:
        return "the torrent contains no files."
        
    large_files_exist = False
    for i in range(files.num_files()):
        file_path = files.file_path(i)
        file_size = files.file_size(i)
        
        if file_size > 10 * 1024 * 1024:
            large_files_exist = True
            _, ext = os.path.splitext(file_path)
            if ext.lower() not in ALLOWED_EXTENSIONS:
                return f"contains an unsupported file type ('{ext}'). I can only download .mkv and .mp4 files."
    
    if not large_files_exist:
        largest_file_idx = max(range(files.num_files()), key=files.file_size)
        file_path = files.file_path(largest_file_idx)
        _, ext = os.path.splitext(file_path)
        if ext.lower() not in ALLOWED_EXTENSIONS:
             return f"contains an unsupported file type ('{ext}'). I can only download .mkv and .mp4 files."

    return None

# --- BOT HANDLER FUNCTIONS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    await update.message.reply_text("Hello! Send me a direct URL to a .torrent file or a magnet link to begin.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    await update.message.reply_text("Send a URL ending in .torrent or a magnet link to start a download.\nUse /cancel to stop your current download.")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.message.chat_id
    
    active_downloads = context.bot_data.get('active_downloads', {})
    if chat_id in active_downloads:
        print(f"[INFO] Received /cancel command from chat_id {chat_id}. Attempting to cancel active task.")
        task: asyncio.Task = active_downloads[chat_id]
        task.cancel()
        await update.message.reply_text("✅ Cancellation request sent.")
    else:
        print(f"[INFO] Received /cancel command from chat_id {chat_id}, but no active task was found.")
        await update.message.reply_text("ℹ️ There are no active downloads for you to cancel.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    chat_id = update.message.chat_id
    text = update.message.text.strip()
    
    if chat_id in context.bot_data.get('active_downloads', {}):
        await update.message.reply_text("ℹ️ You already have a download in progress. Please /cancel it before starting a new one.")
        return

    progress_message = await update.message.reply_text("✅ Input received. Analyzing...")

    source_value: Optional[str] = None
    source_type: Optional[str] = None
    temp_torrent_path: Optional[str] = None
    ti: Optional[lt.torrent_info] = None # type: ignore

    if text.startswith('magnet:?xt=urn:btih:'):
        source_type = 'magnet'
        source_value = text
        ti = await fetch_metadata_from_magnet(text, progress_message)
        if not ti:
            await progress_message.edit_text(r"❌ *Error:* Could not fetch metadata from the magnet link\. It might be inactive or invalid\.", parse_mode=ParseMode.MARKDOWN_V2)
            return

    elif text.startswith(('http://', 'https://')) and text.endswith('.torrent'):
        source_type = 'file'
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(text, follow_redirects=True, timeout=30)
                response.raise_for_status()
        except httpx.RequestError as e:
            await progress_message.edit_text(rf"❌ *Error:* Failed to download from URL\." + f"\n`{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        with tempfile.NamedTemporaryFile(delete=False, suffix=".torrent") as temp_file:
            temp_file.write(response.content)
            temp_torrent_path = temp_file.name
        
        source_value = temp_torrent_path
        try:
            ti = lt.torrent_info(temp_torrent_path) # type: ignore
        except RuntimeError:
            print(f"[ERROR] Failed to parse .torrent file for chat_id {chat_id}.")
            await progress_message.edit_text(r"❌ *Error:* The provided file is not a valid torrent\.", parse_mode=ParseMode.MARKDOWN_V2)
            os.remove(temp_torrent_path)
            return
    else:
        await progress_message.edit_text("This does not look like a valid .torrent URL or magnet link.")
        return

    if not ti:
        await progress_message.edit_text("❌ *Error:* Could not analyze the torrent content.", parse_mode=ParseMode.MARKDOWN_V2)
        if temp_torrent_path and os.path.exists(temp_torrent_path): os.remove(temp_torrent_path)
        return

    if ti.total_size() > MAX_TORRENT_SIZE_BYTES:
        error_msg = f"This torrent is *{format_bytes(ti.total_size())}*, which is larger than the *{MAX_TORRENT_SIZE_GB} GB* limit."
        await progress_message.edit_text(f"❌ *Size Limit Exceeded*\n\n{error_msg}", parse_mode=ParseMode.MARKDOWN_V2)
        if temp_torrent_path and os.path.exists(temp_torrent_path): os.remove(temp_torrent_path)
        return

    validation_error = validate_torrent_files(ti)
    if validation_error:
        error_msg = f"This torrent {validation_error}"
        await progress_message.edit_text(f"❌ *Unsupported File Type*\n\n{error_msg}", parse_mode=ParseMode.MARKDOWN_V2)
        if temp_torrent_path and os.path.exists(temp_torrent_path): os.remove(temp_torrent_path)
        return

    cleaned_name_str = clean_filename(ti.name())
    file_type_str = get_dominant_file_type(ti.files())
    total_size_str = format_bytes(ti.total_size())
    
    confirmation_text = (
        f"✅ *Validation Passed*\n\n"
        f"*Name:* {escape_markdown(cleaned_name_str)}\n"
        f"*File Type:* {escape_markdown(file_type_str)}\n"
        f"*Size:* {escape_markdown(total_size_str)}\n\n"
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
        print(f"[ERROR] context.user_data was None for chat_id {chat_id}. Aborting operation.")
        await progress_message.edit_text(r"❌ *Error:* Could not access user session data\. Please try again\.", parse_mode=ParseMode.MARKDOWN_V2)
        if temp_torrent_path and os.path.exists(temp_torrent_path):
            os.remove(temp_torrent_path)
        return
        
    # --- CHANGE IS HERE ---
    # Store the cleaned name along with the other data
    context.user_data['pending_torrent'] = {
        'type': source_type, 
        'value': source_value, 
        'clean_name': cleaned_name_str
    }

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: return
    await query.answer()

    message = query.message
    if not isinstance(message, Message): return

    print(f"[INFO] Received button press from user {query.from_user.id}: '{query.data}'")

    if not context.user_data:
        print(f"[WARN] Button press from user {query.from_user.id} ignored: No user_data found (session likely expired).")
        try:
            await query.edit_message_text("This action has expired. Please send the link again.")
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
        return

    if 'pending_torrent' not in context.user_data:
        print(f"[WARN] Button press from user {query.from_user.id} ignored: No pending torrent found.")
        try:
            await query.edit_message_text("This action has already been completed or has expired.")
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
        return

    pending_torrent = context.user_data.pop('pending_torrent')
    
    if query.data == "confirm_download":
        print(f"[SUCCESS] Download confirmed by user {query.from_user.id}. Queuing download task.")
        try:
            await query.edit_message_text("✅ Confirmation received. Your download has been queued.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

        download_path = context.bot_data["DOWNLOAD_SAVE_PATH"]
        task = asyncio.create_task(download_task_wrapper(pending_torrent, message, context, download_path))
        
        if 'active_downloads' not in context.bot_data:
            context.bot_data['active_downloads'] = {}
        context.bot_data['active_downloads'][message.chat_id] = task

    elif query.data == "cancel_operation":
        print(f"[CANCEL] Operation cancelled by user {query.from_user.id} via button.")
        try:
            await query.edit_message_text("❌ Operation cancelled by user.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
            
        if pending_torrent.get('type') == 'file' and pending_torrent.get('value') and os.path.exists(pending_torrent.get('value')):
            os.remove(pending_torrent.get('value'))

async def download_task_wrapper(source_dict: Dict, message: Message, context: ContextTypes.DEFAULT_TYPE, save_path: str):
    source_value = source_dict['value']
    source_type = source_dict['type']
    # --- CHANGE IS HERE ---
    # Retrieve the clean name, with a fallback just in case.
    clean_name = source_dict.get('clean_name', "Download")
    
    print(f"[INFO] Starting download task for '{clean_name}' for chat_id {message.chat_id}.")
    safe_display_name = escape_markdown(clean_name)
    await message.edit_text(rf"⏳ Starting content download for `{safe_display_name}`\.\.", parse_mode=ParseMode.MARKDOWN_V2)

    last_update_time = 0
    async def report_progress(status: lt.torrent_status):   #type:ignore
        nonlocal last_update_time
        
        # Use the raw metadata name for detailed console logging
        log_name = status.name if status.name else clean_name
        progress_percent = status.progress * 100
        speed_mbps = status.download_rate / 1024 / 1024
        print(f"[LOG] {log_name}: {progress_percent:.2f}% | Peers: {status.num_peers} | Speed: {speed_mbps:.2f} MB/s")

        current_time = time.monotonic()
        if current_time - last_update_time > 5:
            last_update_time = current_time
            
            # --- CHANGE IS HERE ---
            # Use the consistent, cleaned name for the user-facing message
            name_str = escape_markdown(clean_name[:35] + '...' if len(clean_name) > 35 else clean_name)
            progress_str = escape_markdown(f"{progress_percent:.2f}")
            speed_str = escape_markdown(f"{speed_mbps:.2f}")
            state_str = escape_markdown(status.state.name)
            
            telegram_message = (f"⬇️ *Downloading:* `{name_str}`\n*Progress:* {progress_str}%\n*State:* {state_str}\n*Peers:* {status.num_peers}\n*Speed:* {speed_str} MB/s")
            try:
                await message.edit_text(telegram_message, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest as e:
                # Silently ignore "not modified" errors, but log others.
                if "Message is not modified" not in str(e):
                    print(f"[WARN] Could not edit Telegram message: {e}")

    try:
        success = await download_with_progress(source_value, save_path, report_progress)
        if success:
            print(f"[SUCCESS] Download task for '{clean_name}' completed.")
            await message.edit_text(r"✅ *Success\!* The download is complete\.", parse_mode=ParseMode.MARKDOWN_V2)
    except asyncio.CancelledError:
        print(f"[CANCEL] Download task for '{clean_name}' was cancelled by user {message.chat_id}.")
        await message.edit_text(r"⏹️ *Cancelled:* The download was successfully stopped\.", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        print(f"[ERROR] An unexpected exception occurred in download task for '{clean_name}': {e}")
        safe_error = escape_markdown(str(e))
        await message.edit_text(rf"❌ *Error:* An unexpected error occurred\." + f"\n`{safe_error}`", parse_mode=ParseMode.MARKDOWN_V2)
    finally:
        print(f"[INFO] Cleaning up resources for task '{clean_name}' for chat_id {message.chat_id}.")
        active_downloads = context.bot_data.get('active_downloads', {})
        if message.chat_id in active_downloads:
            del active_downloads[message.chat_id]
        if source_type == 'file' and os.path.exists(source_value):
            os.remove(source_value)

# --- MAIN SCRIPT EXECUTION ---
if __name__ == '__main__':
    try:
        BOT_TOKEN, DOWNLOAD_SAVE_PATH = get_configuration()
    except (FileNotFoundError, ValueError) as e:
        print(f"CRITICAL ERROR: {e}")
        sys.exit(1)

    if not os.path.exists(DOWNLOAD_SAVE_PATH):
        print(f"INFO: Download path '{DOWNLOAD_SAVE_PATH}' not found. Creating it.")
        os.makedirs(DOWNLOAD_SAVE_PATH)

    print("Starting bot...")
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.bot_data["DOWNLOAD_SAVE_PATH"] = DOWNLOAD_SAVE_PATH
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    application.run_polling()
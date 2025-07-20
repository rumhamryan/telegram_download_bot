# file: telegram_bot.py

import wikipedia
from bs4 import BeautifulSoup
from bs4.element import Tag
import asyncio
import httpx
import json
import os
import tempfile
import time
import re
import configparser
import sys
import math
from typing import Optional, Dict
import shutil

from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
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

def get_configuration() -> tuple[str, dict, list[int]]:
    """Reads bot token, save paths, and allowed user IDs from the bot_token.ini file."""
    config = configparser.ConfigParser()
    config_path = 'bot_token.ini'
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file '{config_path}' not found. Please create it.")
    
    config.read(config_path)
    
    # --- Read Bot Token ---
    token = config.get('telegram', 'token', fallback=None)
    if not token or token == "YOUR_SECRET_TOKEN_HERE":
        raise ValueError(f"Bot token not found or not set in '{config_path}'.")
        
    # --- Read and Validate Paths ---
    paths = {
        'default': config.get('telegram', 'default_save_path', fallback=None),
        'movies': config.get('telegram', 'movies_save_path', fallback=None),
        'tv_shows': config.get('telegram', 'tv_shows_save_path', fallback=None)
    }

    if not paths['default']:
        raise ValueError("'default_save_path' is mandatory and was not found in the config file.")

    # Use default path as fallback for optional paths
    if not paths['movies']:
        print("[INFO] 'movies_save_path' not set. Falling back to default path for movies.")
        paths['movies'] = paths['default']
    if not paths['tv_shows']:
        print("[INFO] 'tv_shows_save_path' not set. Falling back to default path for TV shows.")
        paths['tv_shows'] = paths['default']
    
    # Ensure all configured directories exist
    for path_type, path_value in paths.items():
        # --- THE FIX: Add a check to ensure path_value is not None ---
        # This satisfies the IDE's static type checker.
        if path_value is not None:
            if not os.path.exists(path_value):
                print(f"INFO: {path_type.capitalize()} path '{path_value}' not found. Creating it.")
                os.makedirs(path_value)

    # --- Read Allowed User IDs ---
    allowed_ids_str = config.get('telegram', 'allowed_user_ids', fallback='')
    allowed_ids = []
    if not allowed_ids_str:
        print("[WARN] 'allowed_user_ids' is empty. The bot will be accessible to everyone.")
    else:
        try:
            allowed_ids = [int(id.strip()) for id in allowed_ids_str.split(',') if id.strip()]
            print(f"[INFO] Bot access is restricted to the following User IDs: {allowed_ids}")
        except ValueError:
            raise ValueError("Invalid entry in 'allowed_user_ids'.")

    return token, paths, allowed_ids

def parse_torrent_name(name: str) -> dict:
    """
    Parses a torrent name to identify if it's a movie or a TV show
    and extracts relevant metadata.
    """
    # Normalize by replacing dots and underscores with spaces
    cleaned_name = re.sub(r'[\._]', ' ', name)
    
    # --- TV Show Detection ---
    # Patterns: S01E02, s01e02, 1x02, etc. Case-insensitive.
    tv_match = re.search(r'(?i)\b(S(\d{1,2})E(\d{1,2})|(\d{1,2})x(\d{1,2}))\b', cleaned_name)
    if tv_match:
        # The text before the season/episode marker is the title
        title = cleaned_name[:tv_match.start()].strip()
        
        # Extract season and episode from the correct regex capture groups
        if tv_match.group(2) is not None: # Matched SXXEXX
            season = int(tv_match.group(2))
            episode = int(tv_match.group(3))
        else: # Matched XxXX
            season = int(tv_match.group(4))
            episode = int(tv_match.group(5))
            
        # Clean up trailing characters from the title
        title = re.sub(r'[\s-]+$', '', title).strip()
        
        return {'type': 'tv', 'title': title, 'season': season, 'episode': episode}

    # --- Movie Detection ---
    # Pattern: A four-digit year (19xx or 20xx)
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', cleaned_name)
    if year_match:
        year = year_match.group(1)
        # The text before the year is the title
        title = cleaned_name[:year_match.start()].strip()
        
        # Clean up title by removing any surrounding brackets
        title = re.sub(r'^\s*\(|\)\s*$', '', title).strip()

        return {'type': 'movie', 'title': title, 'year': year}

    # --- Fallback for names that don't match movie/TV patterns ---
    tags_to_remove = [
        r'\[.*?\]', r'\(.*?\)',
        r'\b(1080p|720p|480p|x264|x265|hevc|BluRay|WEB-DL|AAC|DTS|HDTV|RM4k|CC|10bit|commentary|HeVK)\b'
    ]
    regex_pattern = '|'.join(tags_to_remove)
    no_ext = os.path.splitext(cleaned_name)[0]
    title = re.sub(regex_pattern, '', no_ext, flags=re.I)
    title = re.sub(r'\s+', ' ', title).strip()
    return {'type': 'unknown', 'title': title}

def generate_plex_filename(parsed_info: dict, original_extension: str) -> str:
    """Generates a clean, Plex-friendly filename from the parsed info."""
    title = parsed_info.get('title', 'Unknown Title')
    
    # Sanitize title to remove characters invalid for filenames
    invalid_chars = r'<>:"/\|?*'
    safe_title = "".join(c for c in title if c not in invalid_chars)

    if parsed_info.get('type') == 'movie':
        year = parsed_info.get('year', 'Unknown Year')
        return f"{safe_title} ({year}){original_extension}"
    
    elif parsed_info.get('type') == 'tv':
        season = parsed_info.get('season', 0)
        episode = parsed_info.get('episode', 0)
        episode_title = parsed_info.get('episode_title')
        
        safe_episode_title = ""
        if episode_title:
            safe_episode_title = " - " + "".join(c for c in episode_title if c not in invalid_chars)
            
        # MODIFIED: Return format is now "sXXeXX - Episode Title.ext"
        return f"s{season:02d}e{episode:02d}{safe_episode_title}{original_extension}"
        
    else: # Fallback for 'unknown' type
        return f"{safe_title}{original_extension}"

async def fetch_episode_title_from_wikipedia(show_title: str, season: int, episode: int) -> Optional[str]:
    """
    Fetches an episode title from Wikipedia with a robust, multi-stage search strategy.
    """
    html_to_scrape = None

    # --- Attempt 1: Direct search for a dedicated "List of..." page ---
    direct_search_query = f"List of {show_title} episodes"
    print(f"[INFO] Searching Wikipedia for: '{direct_search_query}'")
    try:
        page = wikipedia.page(direct_search_query, auto_suggest=False, redirect=True)
        html_to_scrape = page.html()
        print(f"[INFO] Successfully found dedicated episode page.")
    except wikipedia.exceptions.PageError:
        print(f"[WARN] No dedicated 'List of episodes' page found. Trying fallback search on main page.")
        
        # --- Attempt 2: Search for the main show page ---
        try:
            main_page = wikipedia.page(show_title, auto_suggest=True, redirect=True)
            main_html = main_page.html()
            soup = BeautifulSoup(main_html, 'lxml')

            if soup.find('table', class_='wikitable'):
                print("[INFO] Found episode table directly on the main show page. Using this page.")
                html_to_scrape = main_html
            else:
                print("[WARN] No table found on main page. Looking for a link to an episode list.")
                episode_link_tag = soup.find('a', href=re.compile(r'/wiki/Episodes', re.IGNORECASE))
                if isinstance(episode_link_tag, Tag) and episode_link_tag.get('href'):
                    link_target = episode_link_tag.get('href')
                    print(f"[INFO] Found link to separate page: '{link_target}'. Fetching it now.")
                    new_page = wikipedia.page(link_target, auto_suggest=False)
                    html_to_scrape = new_page.html()

        except wikipedia.exceptions.PageError:
            print(f"[ERROR] Main page for '{show_title}' could not be found.")
        except Exception as e:
            print(f"[ERROR] An unexpected error occurred during fallback search: {e}")

    except Exception as e:
        print(f"[ERROR] An unexpected error occurred during direct Wikipedia search: {e}")

    # --- If any attempt succeeded, scrape the resulting HTML ---
    if not html_to_scrape:
        print(f"[ERROR] All search attempts failed. Could not find any page with episode info for '{show_title}'.")
        return None

    soup = BeautifulSoup(html_to_scrape, 'lxml')
    tables = soup.find_all('table', class_='wikitable')

    if not tables:
        print(f"[WARN] No 'wikitable' found on the retrieved Wikipedia page.")
        return None

    for table in tables:
        if not isinstance(table, Tag): continue
        rows = table.find_all('tr')
        for row in rows[1:]:
            if not isinstance(row, Tag): continue
            cells = row.find_all(['td', 'th'])
            # Ensure there are at least 2 columns now, since we need index 1
            if len(cells) < 2: continue

            try:
                cell_texts = [c.get_text(strip=True) for c in cells]
                
                match_found = False
                # 1. Strict match (for multi-season shows)
                row_text_for_match = ' '.join(cell_texts[:2])
                if re.search(fr'\b{season}\b.*\b{episode}\b', row_text_for_match):
                    match_found = True
                
                # 2. Lenient match (for single-season/limited series)
                elif season == 1 and re.fullmatch(str(episode), cell_texts[0]):
                    match_found = True

                if match_found:
                    # --- THE FIX: Look in the second column (index 1) for the title ---
                    title_cell = cells[1] 
                    if not isinstance(title_cell, Tag): continue
                    
                    found_text_element = title_cell.find(string=re.compile(r'"([^"]+)"'))
                    if found_text_element:
                        title_str = str(found_text_element)
                        cleaned_title = title_str.strip().strip('"')
                        print(f"[INFO] Wikipedia: Found episode title: '{cleaned_title}'")
                        return cleaned_title
            except (ValueError, IndexError):
                continue
    
    print(f"[WARN] Wikipedia: Found page but could not find S{season:02d}E{episode:02d} in any table.")
    return None

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
    await progress_message.edit_text("🔗 Magnet link detected. Fetching metadata...")
    
    settings = { 'enable_dht': True, 'listen_interfaces': '0.0.0.0:6881', 'dht_bootstrap_nodes': 'router.utorrent.com:6881,router.bittorrent.com:6881,dht.transmissionbt.com:6881' }
    ses = lt.session(settings) # type: ignore

    params = lt.parse_magnet_uri(magnet_link) # type: ignore
    params.save_path = tempfile.gettempdir() 
    params.upload_mode = True 
    handle = ses.add_torrent(params)

    for i in range(60): # 60 second timeout
        if handle.status().has_metadata:
            print(f"[INFO] Metadata fetched successfully for magnet after {i}s.")
            info = handle.torrent_file() 
            ses.remove_torrent(handle)
            return info
        
        # --- THE FIX: Lowered update interval from 10 to 3 seconds ---
        if i > 0 and i % 3 == 0:
            s = handle.status()
            peer_count = s.num_peers
            print(f"[LOG] Magnet metadata fetch progress: {peer_count} peers found.")
            try:
                await progress_message.edit_text(f"🔗 Fetching metadata... (may take up to 60s)\nPeers found: {peer_count}")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    print(f"[WARN] Could not edit progress message for magnet fetch: {e}")
        
        await asyncio.sleep(1)

    print("[ERROR] Timed out fetching metadata from magnet link.")
    await progress_message.edit_text("❌ *Error:* Timed out fetching metadata from the magnet link. It might be inactive.", parse_mode=ParseMode.MARKDOWN_V2)
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

async def is_user_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Checks if the user sending the update is in the allowed list.
    Returns True if authorized, False otherwise.
    """
    allowed_user_ids = context.bot_data.get('ALLOWED_USER_IDS', [])
    
    # If the allowlist is empty, everyone is authorized.
    if not allowed_user_ids:
        return True

    user = update.effective_user
    if not user or user.id not in allowed_user_ids:
        if user:
            print(f"[ACCESS DENIED] User {user.id} ({user.username}) attempted to use the bot.")
        else:
            print("[ACCESS DENIED] An update with no user was received.")
        return False
    
    # User is in the list, so they are authorized.
    return True

# --- PERSISTENCE FUNCTIONS ---

def save_active_downloads(file_path: str, active_downloads: Dict):
    """Saves the state of active downloads to a JSON file."""
    # We only want to save the data needed to restart the task, not the live task object itself.
    data_to_save = {}
    for chat_id, download_data in active_downloads.items():
        # Create a copy and remove the non-serializable asyncio.Task
        serializable_data = download_data.copy()
        serializable_data.pop('task', None) 
        data_to_save[chat_id] = serializable_data

    try:
        with open(file_path, 'w') as f:
            json.dump(data_to_save, f, indent=4)
        print(f"[INFO] Saved {len(data_to_save)} active download(s) to {file_path}")
    except Exception as e:
        print(f"[ERROR] Could not save persistence file: {e}")

def load_active_downloads(file_path: str) -> Dict:
    """Loads the state of active downloads from a JSON file."""
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
            print(f"[INFO] Loaded {len(data)} active download(s) from {file_path}")
            return data
    except (json.JSONDecodeError, IOError) as e:
        print(f"[ERROR] Could not read or parse persistence file '{file_path}': {e}. Starting fresh.")
        return {}
    
async def post_init(application: Application):
    """Resumes any active downloads after the bot has been initialized."""
    print("--- Resuming active downloads ---")
    persistence_file = application.bot_data['persistence_file']
    active_downloads = load_active_downloads(persistence_file)
    
    if active_downloads:
        for chat_id_str, download_data in active_downloads.items():
            print(f"Resuming download for chat_id {chat_id_str}...")
            task = asyncio.create_task(download_task_wrapper(download_data, application))
            download_data['task'] = task # Add the live task object back
    
    # Store the potentially updated dict back into bot_data
    application.bot_data['active_downloads'] = active_downloads
    print("--- Resume process finished ---")

async def post_shutdown(application: Application):
    """Gracefully signals tasks to stop and preserves the persistence file."""
    print("--- Shutting down: Signalling active tasks to stop ---")
    
    # --- SOLUTION: Set a flag before cancelling ---
    # This tells the task wrappers that this is a shutdown, not a user cancellation.
    application.bot_data['is_shutting_down'] = True
    
    active_downloads = application.bot_data.get('active_downloads', {})
    
    tasks_to_cancel = [
        download_data['task'] 
        for download_data in active_downloads.values() 
        if 'task' in download_data and not download_data['task'].done()
    ]
    
    if not tasks_to_cancel:
        print("No active tasks to stop.")
        return

    for task in tasks_to_cancel:
        task.cancel()
    
    await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
    print("--- All active tasks stopped. Shutdown complete. ---")

# --- BOT HANDLER FUNCTIONS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_authorized(update, context):
        return
    if not update.message: return
    await update.message.reply_text("Hello! Send me a direct URL to a .torrent file or a magnet link to begin.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_authorized(update, context):
        return
    if not update.message: return
    await update.message.reply_text("Send a URL ending in .torrent or a magnet link to start a download.\nUse /cancel to stop your current download.")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_authorized(update, context):
        return
        
    if not update.message: return
    chat_id = update.message.chat_id
    
    active_downloads = context.bot_data.get('active_downloads', {})
    chat_id_str = str(chat_id)
    
    if chat_id_str in active_downloads:
        download_data = active_downloads[chat_id_str]
        clean_name = download_data.get('source_dict', {}).get('clean_name', 'your download')
        
        print(f"[INFO] Received /cancel command from chat_id {chat_id} for '{clean_name}'.")
        
        if 'task' in download_data and not download_data['task'].done():
            task: asyncio.Task = download_data['task']
            task.cancel()
            
            # --- THE FIX: Delete the user's "/cancel" message to prevent clutter ---
            # No reply is needed because the download_task_wrapper will edit the original message.
            try:
                await update.message.delete()
            except BadRequest as e:
                print(f"[WARN] Could not delete /cancel command message: {e}")

        else:
            print(f"[WARN] /cancel command for chat_id {chat_id} found a record but no active task object.")
            await update.message.reply_text("⚠️ Found a record of your download, but the task is not running. It may be in a stalled state.")
    else:
        print(f"[INFO] Received /cancel command from chat_id {chat_id}, but no active task was found.")
        await update.message.reply_text("ℹ️ There are no active downloads for you to cancel.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_authorized(update, context):
        return
        
    if not update.message or not update.message.text: return
    chat_id = update.message.chat_id
    text = update.message.text.strip()
    
    # --- NEW: We'll delete the user's message later, so we need a reference ---
    user_message_to_delete = update.message

    if str(chat_id) in context.bot_data.get('active_downloads', {}):
        await update.message.reply_text("ℹ️ You already have a download in progress. Please /cancel it before starting a new one.")
        return

    progress_message = await update.message.reply_text("✅ Input received. Analyzing...")

    source_value: Optional[str] = None
    source_type: Optional[str] = None
    ti: Optional[lt.torrent_info] = None # type: ignore

    # ... (The logic for handling magnets and torrent files remains identical) ...
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
            torrent_content = response.content
        except httpx.RequestError as e:
            await progress_message.edit_text(rf"❌ *Error:* Failed to download from URL\." + f"\n`{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        try:
            ti = lt.torrent_info(torrent_content) # type: ignore
            info_hash = str(ti.info_hashes().v1) # type: ignore
            torrents_dir = ".torrents"
            os.makedirs(torrents_dir, exist_ok=True)
            
            source_value = os.path.join(torrents_dir, f"{info_hash}.torrent")
            with open(source_value, "wb") as f:
                f.write(torrent_content)
            print(f"[INFO] Persistently saved .torrent file to '{source_value}'")

        except RuntimeError:
            print(f"[ERROR] Failed to parse .torrent file for chat_id {chat_id}.")
            await progress_message.edit_text(r"❌ *Error:* The provided file is not a valid torrent\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
    else:
        await progress_message.edit_text("This does not look like a valid .torrent URL or magnet link.")
        return

    if not ti:
        await progress_message.edit_text("❌ *Error:* Could not analyze the torrent content.", parse_mode=ParseMode.MARKDOWN_V2)
        if source_type == 'file' and source_value and os.path.exists(source_value): os.remove(source_value)
        return

    if ti.total_size() > MAX_TORRENT_SIZE_BYTES:
        error_msg = f"This torrent is *{format_bytes(ti.total_size())}*, which is larger than the *{MAX_TORRENT_SIZE_GB} GB* limit."
        await progress_message.edit_text(f"❌ *Size Limit Exceeded*\n\n{error_msg}", parse_mode=ParseMode.MARKDOWN_V2)
        if source_type == 'file' and source_value and os.path.exists(source_value): os.remove(source_value)
        return

    validation_error = validate_torrent_files(ti)
    if validation_error:
        error_msg = f"This torrent {validation_error}"
        await progress_message.edit_text(f"❌ *Unsupported File Type*\n\n{error_msg}", parse_mode=ParseMode.MARKDOWN_V2)
        if source_type == 'file' and source_value and os.path.exists(source_value): os.remove(source_value)
        return
    
    # ... (The parsing and display name logic remains identical) ...
    parsed_info = parse_torrent_name(ti.name())
    display_name = ""

    if parsed_info['type'] == 'movie':
        display_name = f"{parsed_info['title']} ({parsed_info['year']})"
    
    elif parsed_info['type'] == 'tv':
        await progress_message.edit_text("📺 TV show detected. Searching Wikipedia for episode title...")
        
        episode_title = await fetch_episode_title_from_wikipedia(
            show_title=parsed_info['title'],
            season=parsed_info['season'],
            episode=parsed_info['episode']
        )
        parsed_info['episode_title'] = episode_title
        
        base_name = f"{parsed_info['title']} - S{parsed_info['season']:02d}E{parsed_info['episode']:02d}"
        display_name = f"{base_name} - {episode_title}" if episode_title else base_name

    else: # type is 'unknown'
        display_name = parsed_info['title']


    file_type_str = get_dominant_file_type(ti.files())
    total_size_str = format_bytes(ti.total_size())
    
    confirmation_text = (
        f"✅ *Validation Passed*\n\n"
        f"*Name:* {escape_markdown(display_name)}\n"
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
    print(f"[INFO] Sent confirmation prompt to chat_id {chat_id} for torrent '{display_name}'.")

    # --- NEW: Delete the user's original message containing the link ---
    try:
        await user_message_to_delete.delete()
        print(f"[INFO] Deleted original link message from user {chat_id}.")
    except BadRequest as e:
        # This can happen if the bot lacks permissions or the message is too old.
        if "Message to delete not found" in str(e) or "not enough rights" in str(e):
            print(f"[WARN] Could not delete user's message. Reason: {e}")
        else:
            raise

    if context.user_data is None:
        print(f"[ERROR] context.user_data was None for chat_id {chat_id}. Aborting operation.")
        await progress_message.edit_text(r"❌ *Error:* Could not access user session data\. Please try again\.", parse_mode=ParseMode.MARKDOWN_V2)
        if source_type == 'file' and source_value and os.path.exists(source_value):
            os.remove(source_value)
        return
        
    context.user_data['pending_torrent'] = {
        'type': source_type, 
        'value': source_value, 
        'clean_name': display_name,
        'parsed_info': parsed_info
    }

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_authorized(update, context):
        return

    query = update.callback_query
    if not query: return
    await query.answer()

    message = query.message
    if not isinstance(message, Message): return

    print(f"[INFO] Received button press from user {query.from_user.id}: '{query.data}'")

    if not context.user_data or 'pending_torrent' not in context.user_data:
        print(f"[WARN] Button press from user {query.from_user.id} ignored: No pending torrent found (session likely expired).")
        try:
            await query.edit_message_text("This action has expired. Please send the link again.")
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
        return

    pending_torrent = context.user_data.pop('pending_torrent')
    
    if query.data == "confirm_download":
        print(f"[SUCCESS] Download confirmed by user {query.from_user.id}. Queuing download task.")
        try:
            await query.edit_message_text("✅ Confirmation received. Your download has been queued.")
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise

        # --- NEW: Path selection logic ---
        save_paths = context.bot_data["SAVE_PATHS"]
        parsed_info = pending_torrent.get('parsed_info', {})
        torrent_type = parsed_info.get('type')

        final_save_path = save_paths['default'] # Start with the default
        if torrent_type == 'movie':
            final_save_path = save_paths['movies']
            print(f"[INFO] Torrent identified as a movie. Saving to: {final_save_path}")
        elif torrent_type == 'tv':
            final_save_path = save_paths['tv_shows']
            print(f"[INFO] Torrent identified as a TV show. Saving to: {final_save_path}")
        else:
            print(f"[INFO] Torrent type is unknown. Saving to default path: {final_save_path}")
        # --- End of new logic ---

        active_downloads = context.bot_data.get('active_downloads', {})
        
        download_data = {
            'source_dict': pending_torrent,
            'chat_id': message.chat_id,
            'message_id': message.message_id,
            'save_path': final_save_path # Use the selected path
        }
        
        task = asyncio.create_task(download_task_wrapper(download_data, context.application))
        download_data['task'] = task
        active_downloads[str(message.chat_id)] = download_data
        
        save_active_downloads(context.bot_data['persistence_file'], active_downloads)

    elif query.data == "cancel_operation":
        print(f"[CANCEL] Operation cancelled by user {query.from_user.id} via button.")
        try:
            await query.edit_message_text("❌ Operation cancelled by user.")
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
        if pending_torrent.get('type') == 'file' and pending_torrent.get('value') and os.path.exists(pending_torrent.get('value')):
            os.remove(pending_torrent.get('value'))

async def download_task_wrapper(download_data: Dict, application: Application):
    source_dict = download_data['source_dict']
    chat_id = download_data['chat_id']
    message_id = download_data['message_id']
    base_save_path = download_data['save_path']
    
    source_value = source_dict['value']
    source_type = source_dict['type']
    clean_name = source_dict.get('clean_name', "Download")
    parsed_info = source_dict.get('parsed_info', {})
    
    print(f"[INFO] Starting/Resuming download task for '{clean_name}' for chat_id {chat_id}.")
    
    last_update_time = 0
    async def report_progress(status: lt.torrent_status): #type: ignore
        nonlocal last_update_time
        log_name = status.name if status.name else clean_name
        progress_percent = status.progress * 100
        speed_mbps = status.download_rate / 1024 / 1024
        print(f"[LOG] {log_name}: {progress_percent:.2f}% | Peers: {status.num_peers} | Speed: {speed_mbps:.2f} MB/s")

        current_time = time.monotonic()
        if current_time - last_update_time > 5:
            last_update_time = current_time
            name_str = escape_markdown(clean_name[:35] + '...' if len(clean_name) > 35 else clean_name)
            progress_str = escape_markdown(f"{progress_percent:.2f}")
            speed_str = escape_markdown(f"{speed_mbps:.2f}")
            state_str = escape_markdown(status.state.name)
            
            telegram_message = (f"⬇️ *Downloading:*\n{name_str}\n*Progress:* {progress_str}%\n*State:* {state_str}\n*Peers:* {status.num_peers}\n*Speed:* {speed_str} MB/s")
            try:
                await application.bot.edit_message_text(text=telegram_message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest as e:
                if "Message is not modified" not in str(e): print(f"[WARN] Could not edit Telegram message: {e}")

    try:
        success, ti = await download_with_progress(
            source=source_value, 
            save_path=base_save_path,
            status_callback=report_progress,
            bot_data=application.bot_data,
            allowed_extensions=ALLOWED_EXTENSIONS
        )
        if success and ti:
            print(f"[SUCCESS] Download task for '{clean_name}' completed. Starting post-processing.")
            
            try:
                files = ti.files()
                target_file_path_in_torrent = None
                original_extension = ".mkv"
                for i in range(files.num_files()):
                    _, ext = os.path.splitext(files.file_path(i))
                    if ext.lower() in ALLOWED_EXTENSIONS:
                        target_file_path_in_torrent = files.file_path(i)
                        original_extension = ext
                        break
                
                if target_file_path_in_torrent:
                    final_filename = generate_plex_filename(parsed_info, original_extension)
                    
                    destination_directory = base_save_path
                    if parsed_info.get('type') == 'tv':
                        show_title = parsed_info.get('title', 'Unknown Show')
                        season_num = parsed_info.get('season', 0)
                        
                        invalid_chars = r'<>:"/\|?*'
                        safe_show_title = "".join(c for c in show_title if c not in invalid_chars)

                        destination_directory = os.path.join(
                            base_save_path, 
                            safe_show_title, 
                            f"Season {season_num:02d}"
                        )
                        print(f"[INFO] TV Show detected. Target directory set to: {destination_directory}")

                    os.makedirs(destination_directory, exist_ok=True)
                    
                    current_path = os.path.join(base_save_path, target_file_path_in_torrent)
                    new_path = os.path.join(destination_directory, final_filename)
                    
                    print(f"[MOVE] From: {current_path}\n[MOVE] To:   {new_path}")
                    shutil.move(current_path, new_path)
                    
                    original_top_level_dir = os.path.join(base_save_path, target_file_path_in_torrent.split(os.path.sep)[0])
                    if os.path.isdir(original_top_level_dir) and not os.listdir(original_top_level_dir):
                         print(f"[CLEANUP] Deleting empty original directory: {original_top_level_dir}")
                         shutil.rmtree(original_top_level_dir)
                    elif os.path.isfile(original_top_level_dir):
                        pass

            except Exception as e:
                print(f"[ERROR] Post-processing failed: {e}")

            # CORRECTED: Use f-string instead of rf-string for proper newline handling
            final_message = (
                f"✅ *Success\!*\n" #type: ignore
                f"Renamed and moved to Plex Server:\n"
                f"`{escape_markdown(clean_name)}`"
            )
            await application.bot.edit_message_text(text=final_message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN_V2)

    except asyncio.CancelledError:
        if application.bot_data.get('is_shutting_down', False):
            print(f"[INFO] Task for '{clean_name}' paused due to bot shutdown.")
            raise
        
        print(f"[CANCEL] Download task for '{clean_name}' was cancelled by user {chat_id}.")
        # CORRECTED: Use f-string instead of rf-string for proper newline handling
        final_message = (
            f"⏹️ *Cancelled*\n"
            f"Download has been stopped for:\n"
            f"`{escape_markdown(clean_name)}`"
        )
        try:
            await application.bot.edit_message_text(text=final_message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
    except Exception as e:
        print(f"[ERROR] An unexpected exception occurred in download task for '{clean_name}': {e}")
        safe_error = escape_markdown(str(e))
        # CORRECTED: Use f-string instead of rf-string for proper newline handling
        final_message = (
            f"❌ *Error*\n"
            f"An unexpected error occurred:\n"
            f"`{safe_error}`"
        )
        try:
            await application.bot.edit_message_text(text=final_message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
    finally:
        if not application.bot_data.get('is_shutting_down', False):
            print(f"[INFO] Cleaning up resources for task '{clean_name}' for chat_id {chat_id}.")
            active_downloads = application.bot_data.get('active_downloads', {})
            if str(chat_id) in active_downloads:
                del active_downloads[str(chat_id)]
                save_active_downloads(application.bot_data['persistence_file'], active_downloads)

            if source_type == 'file' and source_value and os.path.exists(source_value):
                os.remove(source_value)

            print(f"[CLEANUP] Scanning '{base_save_path}' for leftover .parts files...")
            try:
                for filename in os.listdir(base_save_path):
                    if filename.endswith(".parts"):
                        parts_file_path = os.path.join(base_save_path, filename)
                        print(f"[CLEANUP] Found and deleting leftover parts file: {parts_file_path}")
                        os.remove(parts_file_path)
            except Exception as e:
                print(f"[ERROR] Could not perform .parts file cleanup: {e}")

# --- MAIN SCRIPT EXECUTION ---
if __name__ == '__main__':
    PERSISTENCE_FILE = 'persistence.json'

    try:
        # --- UPDATED to receive the paths dictionary ---
        BOT_TOKEN, SAVE_PATHS, ALLOWED_USER_IDS = get_configuration()
    except (FileNotFoundError, ValueError) as e:
        print(f"CRITICAL ERROR: {e}")
        sys.exit(1)

    print("Starting bot...")
    
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    
    # Initialize bot_data dictionaries
    # --- NEW: Store the entire paths dictionary in the bot's global context ---
    application.bot_data["SAVE_PATHS"] = SAVE_PATHS
    application.bot_data["persistence_file"] = PERSISTENCE_FILE
    application.bot_data["ALLOWED_USER_IDS"] = ALLOWED_USER_IDS
    application.bot_data.setdefault('active_downloads', {})
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    application.run_polling()
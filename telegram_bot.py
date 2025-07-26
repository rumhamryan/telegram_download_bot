# file: telegram_bot.py

import datetime
import wikipedia
from bs4 import BeautifulSoup, Tag
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
import urllib.parse
import math
from typing import Optional, Dict, Tuple, List, Set
import shutil
import subprocess
import platform

from plexapi.server import PlexServer
from plexapi.exceptions import NotFound, Unauthorized

from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ApplicationBuilder, CallbackContext, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
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

def get_configuration() -> tuple[str, dict, list[int], dict]:
    """
    Reads bot token, paths, allowed IDs, and Plex config from the config.ini file.
    """
    config = configparser.ConfigParser()
    config_path = 'config.ini'
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file '{config_path}' not found. Please create it.")
    
    config.read(config_path)
    
    token = config.get('telegram', 'bot_token', fallback=None)
    if not token or token == "PLACE_TOKEN_HERE":
        raise ValueError(f"Bot token not found or not set in '{config_path}'.")
        
    paths = {
        'default': config.get('host', 'default_save_path', fallback=None),
        'movies': config.get('host', 'movies_save_path', fallback=None),
        'tv_shows': config.get('host', 'tv_shows_save_path', fallback=None)
    }

    if not paths['default']:
        raise ValueError("'default_save_path' is mandatory and was not found in the config file.")

    if not paths['movies']:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] 'movies_save_path' not set. Falling back to default path for movies.")
        paths['movies'] = paths['default']
    if not paths['tv_shows']:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] 'tv_shows_save_path' not set. Falling back to default path for TV shows.")
        paths['tv_shows'] = paths['default']
    
    for path_type, path_value in paths.items():
        if path_value is not None:
            if not os.path.exists(path_value):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO: {path_type.capitalize()} path '{path_value}' not found. Creating it.")
                os.makedirs(path_value)

    allowed_ids_str = config.get('telegram', 'allowed_user_ids', fallback='')
    allowed_ids = []
    if not allowed_ids_str:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] 'allowed_user_ids' is empty. The bot will be accessible to everyone.")
    else:
        try:
            allowed_ids = [int(id.strip()) for id in allowed_ids_str.split(',') if id.strip()]
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Bot access is restricted to the following User IDs: {allowed_ids}")
        except ValueError:
            raise ValueError("Invalid entry in 'allowed_user_ids'.")

    plex_config = {}
    if config.has_section('plex'):
        plex_url = config.get('plex', 'plex_url', fallback=None)
        plex_token = config.get('plex', 'plex_token', fallback=None)
        if plex_url and plex_token and plex_token != "YOUR_PEX_TOKEN_HERE":
            plex_config = {'url': plex_url, 'token': plex_token}
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Plex configuration loaded successfully.")
        else:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Plex section found, but URL or token is missing or default. Plex scanning will be disabled.")
    else:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] No [plex] section in config file. Plex scanning will be disabled.")

    return token, paths, allowed_ids, plex_config

def parse_torrent_name(name: str) -> dict:
    """
    Parses a torrent name to identify if it's a movie or a TV show
    and extracts relevant metadata.
    """
    # Normalize by replacing dots and underscores with spaces
    cleaned_name = re.sub(r'[\._]', ' ', name)
    
    # --- TV Show Detection (unchanged) ---
    tv_match = re.search(r'(?i)\b(S(\d{1,2})E(\d{1,2})|(\d{1,2})x(\d{1,2}))\b', cleaned_name)
    if tv_match:
        title = cleaned_name[:tv_match.start()].strip()
        if tv_match.group(2) is not None:
            season = int(tv_match.group(2))
            episode = int(tv_match.group(3))
        else:
            season = int(tv_match.group(4))
            episode = int(tv_match.group(5))
        title = re.sub(r'[\s-]+$', '', title).strip()
        return {'type': 'tv', 'title': title, 'season': season, 'episode': episode}

    # --- Movie Detection ---
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', cleaned_name)
    if year_match:
        year = year_match.group(1)
        title = cleaned_name[:year_match.start()].strip()
        
        # --- FIX STARTS HERE ---
        # Remove any trailing spaces, parentheses, or hyphens from the title
        title = re.sub(r'[\s(\)-]+$', '', title).strip()
        # --- FIX ENDS HERE ---

        return {'type': 'movie', 'title': title, 'year': year}

    # --- Fallback for names that don't match movie/TV patterns (unchanged) ---
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
    
def _extract_first_int(text: str) -> Optional[int]:
    """Safely extracts the first integer from a string, ignoring trailing characters."""
    if not text:
        return None
    match = re.search(r'\d+', text.strip()) # Changed from re.match to re.search
    if match:
        return int(match.group(0))
    return None

async def _parse_dedicated_episode_page(soup: BeautifulSoup, season: int, episode: int) -> Optional[str]:
    """
    (Primary Strategy - DEFINITIVE)
    Parses a dedicated 'List of...' page by using the 'Series overview'
    table to calculate the exact index of the target season's table.
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [WIKI] Trying Primary Strategy: Index Calculation via Overview Table")

    all_tables = soup.find_all('table', class_='wikitable')
    if not all_tables:
        return None

    # --- Step 1: Find the "Series overview" table to use as an index ---
    index_table = None
    first_table = all_tables[0]
    if isinstance(first_table, Tag):
        first_row = first_table.find('tr')
        if isinstance(first_row, Tag):
            headers = [th.get_text(strip=True) for th in first_row.find_all('th')]
            if headers and headers[0] == 'Season':
                index_table = first_table

    if not index_table or not isinstance(index_table, Tag):
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Could not find 'Series overview' table. Aborting Primary Strategy.")
        return None

    # --- Step 2: Calculate the target table's actual index ---
    target_table_index = -1
    # The counter starts at 1, representing the index of the first table *after* the overview table.
    current_table_index_counter = 0
    
    rows = index_table.find_all('tr')[1:] # Skip header
    for row in rows:
        if not isinstance(row, Tag): continue
        cells = row.find_all(['th', 'td'])
        if not cells: continue
        
        season_num_from_cell = _extract_first_int(cells[0].get_text(strip=True))
        
        if season_num_from_cell == season:
            target_table_index = current_table_index_counter
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Match for Season {season} found. Calculated target table index: {target_table_index}")
            break
        
        # IMPORTANT: Increment the counter *after* the check.
        current_table_index_counter += 1

    if target_table_index == -1:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Could not find Season {season} in the index table.")
        return None

    if target_table_index >= len(all_tables):
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI ERROR] Calculated index {target_table_index} is out of bounds (Total tables: {len(all_tables)}).")
        return None

    # --- Step 3: Parse the correct table using the calculated index ---
    target_table = all_tables[target_table_index]
    if not isinstance(target_table, Tag): return None

    for row in target_table.find_all('tr')[1:]:
        if not isinstance(row, Tag): continue
        cells = row.find_all(['td', 'th'])
        # A valid row must have at least 3 columns for this page type
        if len(cells) < 3: continue

        try:
            # The episode number is in the second column (index 1) of season tables
            episode_num_from_cell = _extract_first_int(cells[1].get_text(strip=True))

            if episode_num_from_cell == episode:
                # The title is in the third column (index 2)
                title_cell = cells[2]
                if not isinstance(title_cell, Tag): continue
                
                found_text = title_cell.find(string=re.compile(r'"([^"]+)"'))
                if found_text:
                    cleaned_title = str(found_text).strip().strip('"')
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SUCCESS] Found title via Primary Strategy: '{cleaned_title}'")
                    return cleaned_title
                else:
                    cleaned_title = title_cell.get_text(strip=True)
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not find title in quotes, using full cell text: '{cleaned_title}'")
                    return cleaned_title
        except (ValueError, IndexError):
            continue
            
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Primary Strategy failed to find the episode in the correct table.")
    return None

async def _parse_embedded_episode_page(soup: BeautifulSoup, season: int, episode: int) -> Optional[str]:
    """
    (Fallback Strategy - HEAVY DEBUGGING & TYPE SAFE)
    Parses a page using proven logic for embedded episode lists.
    """
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI DEBUG] === Trying Fallback Strategy: Flexible Row Search ===")
    
    tables = soup.find_all('table', class_='wikitable')
    for table_idx, table in enumerate(tables):
        if not isinstance(table, Tag): continue
        
        # --- FIX: Safely find headers to prevent IDE errors ---
        headers = []
        first_row = table.find('tr')
        if isinstance(first_row, Tag):
            headers = [th.get_text(strip=True) for th in first_row.find_all('th')]
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI INSPECT] Table {table_idx+1}/{len(tables)} Headers: {headers}")

        rows = table.find_all('tr')
        for row in rows[1:]:
            if not isinstance(row, Tag): continue
            cells = row.find_all(['td', 'th'])
            if len(cells) < 2: continue

            try:
                cell_texts = [c.get_text(strip=True) for c in cells]
                match_found = False
                row_text_for_match = ' '.join(cell_texts[:2])
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI DEBUG]   Searching row text: '{row_text_for_match}'")

                if re.search(fr'\b{season}\b.*\b{episode}\b', row_text_for_match):
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI DEBUG]     Heuristic 1 MATCH on row.")
                    match_found = True
                
                elif season == 1 and re.fullmatch(str(episode), cell_texts[0]):
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI DEBUG]     Heuristic 2 MATCH on row.")
                    match_found = True

                if match_found:
                    title_cell = cells[1] 
                    if not isinstance(title_cell, Tag): continue
                    
                    found_text_element = title_cell.find(string=re.compile(r'"([^"]+)"'))
                    if found_text_element:
                        cleaned_title = str(found_text_element).strip().strip('"')
                        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SUCCESS] Found title via Fallback Strategy: '{cleaned_title}'")
                        return cleaned_title
            except (ValueError, IndexError):
                continue
                
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Fallback Strategy failed.")
    return None

async def fetch_episode_title_from_wikipedia(show_title: str, season: int, episode: int) -> Tuple[Optional[str], Optional[str]]:
    """
    (Coordinator - MODIFIED)
    Fetches an episode title from Wikipedia.
    Returns a tuple: (episode_title, corrected_show_title).
    'corrected_show_title' will be the new name if a redirect occurred on fallback,
    otherwise it will be None.
    """
    html_to_scrape = None
    corrected_show_title: Optional[str] = None
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # --- Step 1: Find the correct Wikipedia page ---
    try:
        direct_search_query = f"List of {show_title} episodes"
        print(f"[{ts}] [INFO] Attempting to find dedicated episode page: '{direct_search_query}'")
        page = await asyncio.to_thread(
            wikipedia.page, direct_search_query, auto_suggest=False, redirect=True
        )
        html_to_scrape = await asyncio.to_thread(page.html)
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Successfully found dedicated episode page with original title.")
    
    except wikipedia.exceptions.PageError:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] No dedicated page found. Falling back to main show page search for '{show_title}'.")
        try:
            main_page = await asyncio.to_thread(
                wikipedia.page, show_title, auto_suggest=True, redirect=True
            )
            html_to_scrape = await asyncio.to_thread(main_page.html)
            
            # --- KEY CHANGE: Check for and store a corrected title ---
            if main_page.title != show_title:
                corrected_show_title = main_page.title
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Fallback successful. Show title was corrected: '{show_title}' -> '{corrected_show_title}'")
                try:
                    direct_search_query = f"List of {corrected_show_title} episodes"
                    print(f"[{ts}] [INFO] Attempting to find dedicated episode page: '{direct_search_query}'")
                    page = await asyncio.to_thread(
                        wikipedia.page, direct_search_query, auto_suggest=False, redirect=True
                    )
                    html_to_scrape = await asyncio.to_thread(page.html)
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Successfully found dedicated episode page with original title.")
                    
                except wikipedia.exceptions.PageError:
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] No dedicated page found. Falling back to main show page search for '{show_title}'.")
            else:
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Successfully found main show page with original title.")
            # --- END OF KEY CHANGE ---

        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] An unexpected error occurred during fallback page search: {e}")
            return None, None
            
    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] An unexpected error occurred during direct Wikipedia search: {e}")
        return None, None

    if not html_to_scrape:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] All page search attempts failed.")
        return None, None

    # --- Step 2: Orchestrate the parsing strategies ---
    soup = BeautifulSoup(html_to_scrape, 'lxml')
    
    episode_title = await _parse_dedicated_episode_page(soup, season, episode)
    
    if not episode_title:
        episode_title = await _parse_embedded_episode_page(soup, season, episode)

    if not episode_title:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Both parsing strategies failed to find S{season:02d}E{episode:02d}.")

    return episode_title, corrected_show_title

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

def _blocking_fetch_metadata(ses: lt.session, magnet_link: str) -> Optional[bytes]: #type: ignore
    """
    (PRODUCTION VERSION)
    Uses a long-lived session provided by the main application. It only
    creates and destroys a temporary handle. This function is synchronous and
    is intended to be run in a separate thread.
    """
    try:
        params = lt.parse_magnet_uri(magnet_link) #type: ignore
        params.save_path = tempfile.gettempdir()
        params.upload_mode = True
        handle = ses.add_torrent(params)

        start_time = time.monotonic()
        timeout_seconds = 30

        while time.monotonic() - start_time < timeout_seconds:
            if handle.status().has_metadata:
                ti = handle.torrent_file()
                creator = lt.create_torrent(ti) #type: ignore
                torrent_dict = creator.generate()
                bencoded_metadata = lt.bencode(torrent_dict) #type: ignore
                
                ses.remove_torrent(handle) # Clean up the handle
                return bencoded_metadata
            
            time.sleep(0.5)
    
    except Exception as e:
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] [ERROR] An exceptio`n occurred in the metadata worker thread: {e}")

    # This part is reached on timeout or error
    if 'handle' in locals() and handle.is_valid(): #type: ignore
        ses.remove_torrent(handle) #type: ignore
        
    return None

async def _update_fetch_timer(progress_message: Message, timeout: int, cancel_event: asyncio.Event):
    """(Helper) Updates a message with a simple elapsed time counter."""
    start_time = time.monotonic()
    while not cancel_event.is_set():
        elapsed = int(time.monotonic() - start_time)
        if elapsed > timeout:
            break
            
        text = (
            f"⬇️ *Fetching Metadata...*\n"
            f"`Magnet Link`\n\n"
            f"*Please wait, this can be slow.*\n"
            f"*The bot is NOT frozen.*\n\n"
            f"Elapsed Time: `{elapsed}s`"
        )
        try:
            await progress_message.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest:
            pass # Ignore "message not modified"
            
        try:
            # Wait for 1 second, but break immediately if cancelled.
            await asyncio.wait_for(cancel_event.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass # This is expected.

async def fetch_metadata_from_magnet(magnet_link: str, progress_message: Message, context: ContextTypes.DEFAULT_TYPE) -> Optional[lt.torrent_info]: #type: ignore
    """
    (Coordinator) Fetches metadata by running the blocking libtorrent code in a
    separate thread, while running a responsive UI timer in the main thread.
    """
    cancel_timer = asyncio.Event()
    timer_task = asyncio.create_task(
        _update_fetch_timer(progress_message, 120, cancel_timer)
    )

    # Get the global session from the application context
    ses = context.bot_data["TORRENT_SESSION"]
    
    # Run the blocking code in a worker thread, passing the global session
    bencoded_metadata = await asyncio.to_thread(_blocking_fetch_metadata, ses, magnet_link)
    
    # Signal the timer to stop and wait for it to finish.
    cancel_timer.set()
    await timer_task

    if bencoded_metadata:
        # Reconstruct the torrent_info object from the bytes in the main thread
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Reconstructing torrent_info object from bencoded data.")
        ti = lt.torrent_info(bencoded_metadata) #type: ignore
        return ti
    else:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Metadata fetch failed or timed out.")
        error_message_text = "Timed out fetching metadata from the magnet link. It might be inactive or poorly seeded."
        await progress_message.edit_text(f"❌ *Error:* {escape_markdown(error_message_text)}", parse_mode=ParseMode.MARKDOWN_V2)
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
    
    if not allowed_user_ids:
        return True

    user = update.effective_user
    if not user or user.id not in allowed_user_ids:
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if user:
            print(f"[{ts}] [ACCESS DENIED] User {user.id} ({user.username}) attempted to use the bot.")
        else:
            print(f"[{ts}] [ACCESS DENIED] An update with no user was received.")
        return False
    
    return True

# Define a custom filter class for waiting_for_delete_input
class WaitingForDeleteInputFilter(filters.BaseFilter):
    """
    A filter that returns True if the bot is expecting
    user input for a delete operation, False otherwise.
    """
    def filter(self, update: Update, context: CallbackContext) -> bool:
        if update.effective_user and context.user_data:
            return context.user_data.get('waiting_for_delete_input', False)
        return False

# Create an instance of the custom filter. This instance is what you'll use in MessageHandler.
waiting_for_delete_input_filter_instance = WaitingForDeleteInputFilter()

# --- PERSISTENCE FUNCTIONS ---

def save_active_downloads(file_path: str, active_downloads: Dict):
    """Saves the state of active downloads to a JSON file."""
    data_to_save = {}
    for chat_id, download_data in active_downloads.items():
        serializable_data = download_data.copy()
        serializable_data.pop('task', None) 
        data_to_save[chat_id] = serializable_data

    try:
        with open(file_path, 'w') as f:
            json.dump(data_to_save, f, indent=4)
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Saved {len(data_to_save)} active download(s) to {file_path}")
    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Could not save persistence file: {e}")

def load_active_downloads(file_path: str) -> Dict:
    """Loads the state of active downloads from a JSON file."""
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Loaded {len(data)} active download(s) from {file_path}")
            return data
    except (json.JSONDecodeError, IOError) as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Could not read or parse persistence file '{file_path}': {e}. Starting fresh.")
        return {}
    
async def post_init(application: Application):
    """Resumes any active downloads after the bot has been initialized."""
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] --- Resuming active downloads ---")
    persistence_file = application.bot_data['persistence_file']
    active_downloads = load_active_downloads(persistence_file)
    
    if active_downloads:
        for chat_id_str, download_data in active_downloads.items():
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Resuming download for chat_id {chat_id_str}...")
            task = asyncio.create_task(download_task_wrapper(download_data, application))
            download_data['task'] = task
    
    application.bot_data['active_downloads'] = active_downloads
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- Resume process finished ---")

async def post_shutdown(application: Application):
    """Gracefully signals tasks to stop and preserves the persistence file."""
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] --- Shutting down: Signalling active tasks to stop ---")
    
    application.bot_data['is_shutting_down'] = True
    
    active_downloads = application.bot_data.get('active_downloads', {})
    
    tasks_to_cancel = [
        download_data['task'] 
        for download_data in active_downloads.values() 
        if 'task' in download_data and not download_data['task'].done()
    ]
    
    if not tasks_to_cancel:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] No active tasks to stop.")
        return

    for task in tasks_to_cancel:
        task.cancel()
    
    await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- All active tasks stopped. Shutdown complete. ---")

# --- BOT HANDLER FUNCTIONS ---

from telegram import Update
from telegram.ext import CallbackContext

# ... (other imports and your existing bot code) ...

async def start_command(update: Update, context: CallbackContext) -> None:
    """Sends a message with instructions and torrent site links when the /start command is issued."""
    # Ensure update.message is not None before trying to use it.
    # This check satisfies the type checker and adds robustness.
    if update.message is None:
        # In this specific context (CommandHandler for /start), this case is highly unlikely,
        # but adding it makes the type checker happy and provides a fallback.
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Hello! Please send /start again." # A simpler fallback
            )
        return

    welcome_message = """
Send me a .torrent or .magnet link!

For Movies:
https://yts.mx/
https://1337x.to/
https://thepiratebay.org/

For TV Shows:
https://eztvx.to/
https://1337x.to/
"""
    await update.message.reply_text(welcome_message)

# You would then register this handler in your main bot setup, for example:
# application.add_handler(CommandHandler("start", start))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provides a formatted list of available commands."""
    if not await is_user_authorized(update, context):
        return
    if not update.message: return
    
    # Using MarkdownV2 for nice formatting.
    # Note that special characters like '.', '-', and '!' must be escaped with a '\'.
    help_text = (
        "Here are the available commands:\n\n"
        "`hello` \\- Show welcome message\\.\n"
        "`cancel` \\- Stop download\\.\n"
        "`plexstatus` \\- Check Plex\\.\n"
        "`plexrestart` \\- Restart Plex\\.\n"
        "`delete` \\- Delete media files\\.\n\n" # Added the new command and escaped characters
    )
    
    await update.message.reply_text(
        text=help_text,
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_authorized(update, context):
        return
        
    if not update.message: return
    chat_id = update.message.chat_id
    
    active_downloads = context.bot_data.get('active_downloads', {})
    chat_id_str = str(chat_id)
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if chat_id_str in active_downloads:
        download_data = active_downloads[chat_id_str]
        clean_name = download_data.get('source_dict', {}).get('clean_name', 'your download')
        
        print(f"[{ts}] [INFO] Received /cancel command from chat_id {chat_id} for '{clean_name}'.")
        
        if 'task' in download_data and not download_data['task'].done():
            task: asyncio.Task = download_data['task']
            task.cancel()
            
            try:
                await update.message.delete()
            except BadRequest as e:
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not delete /cancel command message: {e}")

        else:
            print(f"[{ts}] [WARN] /cancel command for chat_id {chat_id} found a record but no active task object.")
            await update.message.reply_text("⚠️ Found a record of your download, but the task is not running. It may be in a stalled state.")
    else:
        print(f"[{ts}] [INFO] Received /cancel command from chat_id {chat_id}, but no active task was found.")
        await update.message.reply_text("ℹ️ There are no active downloads for you to cancel.")

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Initiates the media deletion process by prompting the user for the title to delete.
    """
    if not await is_user_authorized(update, context):
        return
    if not update.message:
        return

    chat_id = update.message.chat_id
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Ensure user_data is initialized
    if context.user_data is None:
        context.user_data = {}

    # Disallow deletion if a download is currently active to prevent conflicts
    if str(chat_id) in context.bot_data.get('active_downloads', {}):
        await update.message.reply_text("ℹ️ You have an active download. Please /cancel it first or wait for it to complete before attempting to delete media.")
        return

    # Set a flag in user_data to indicate the bot is waiting for a delete title
    context.user_data['waiting_for_delete_input'] = True
    print(f"[{ts}] [DELETE] User {chat_id} initiated delete command. Waiting for input.")
    
    # Escape the example strings for MarkdownV2
    example_movie = escape_markdown("Movie Title (Year)")
    example_tv = escape_markdown("TV Show Name S01E01")
    explanation_tv = escape_markdown("(Season and Episode required for TV shows)")

    await update.message.reply_text(
        "🗑️ *Delete Media:*\n\n"
        "Which Movie or TV Show would you like to delete?\n\n"
        "Please provide the full title, for example:\n"
        f"`{example_movie}`\n"
        "or\n"
        f"`{example_tv}` {explanation_tv}\n\n"
        # FIX: Escaped the period at the end of the sentence
        "Send `/cancel` at any point to stop this operation\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def handle_delete_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Processes the user's input for the media title to be deleted,
    finds potential files, and asks for confirmation.
    
    This function sends messages to the user via Telegram's API (edit_text/reply_text)
    and does not return any text value in Python.
    """
    if not await is_user_authorized(update, context):
        return
    if not update.message or not update.message.text:
        return

    chat_id = update.message.chat_id
    user_input = update.message.text.strip()
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Ensure user_data is initialized (defensive, filter should ensure this is true)
    if context.user_data is None:
        context.user_data = {}

    # The filter (WaitingForDeleteInputFilter) ensures that we only enter this function
    # if context.user_data['waiting_for_delete_input'] is True.

    print(f"[{ts}] [DELETE] Processing delete input from {chat_id}: '{user_input}'")
    
    processing_message = await update.message.reply_text("🔎 Searching for media to delete...")

    parsed_info = parse_torrent_name(user_input)
    media_type = parsed_info.get('type', 'unknown')
    
    # --- Path 1: Input format for deletion is not recognized ---
    if media_type == 'unknown':
        print(f"[{ts}] [DELETE] Input '{user_input}' could not be classified as movie/TV for deletion.")
        
        # IMPORTANT: Do NOT pop 'waiting_for_delete_input' here.
        # The user is still in the 'delete input' state and needs to provide valid input.

        await processing_message.edit_text(
            f"❓ *Invalid Delete Input Format:*\n\n"
            f"I couldn't identify '{escape_markdown(user_input)}' as a movie or TV show title for deletion\\.\n"
            f"Please ensure you provide the title in the correct format:\n"
            f"`Movie Title \\(Year\\)` or `TV Show Name S01E01`\\.\n\n"
            f"Send `/cancel` to stop the deletion process\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        # Delete the user's input message for tidiness
        try:
            await update.message.delete()
            print(f"[{ts}] [DELETE] Deleted user's invalid input message for deletion.")
        except BadRequest as e:
            if "Message to delete not found" in str(e) or "not enough rights" in str(e):
                print(f"[{ts}] [WARN] Could not delete user's delete input message. Reason: {e}")
            else:
                raise # Re-raise if it's an unexpected BadRequest
        return # Exit early if input format is not recognized for deletion

    # --- Only clear the flag if the input was successfully parsed as a movie or TV show ---
    context.user_data.pop('waiting_for_delete_input', None) # Now it's safe to clear the flag

    potential_files: List[Tuple[str, str]] = [] # List of (display_name, absolute_path)
    save_paths = context.bot_data["SAVE_PATHS"]

    # --- Search for Movies ---
    if media_type == 'movie' and 'year' in parsed_info:
        movie_base_path = save_paths.get('movies', save_paths['default'])
        search_name = f"{parsed_info['title']} ({parsed_info['year']})"
        
        invalid_chars = r'<>:"/\|?*'
        safe_search_name = "".join(c for c in search_name if c not in invalid_chars)
        
        expected_filename_regex = re.compile(rf"^{re.escape(safe_search_name)}\s*\.(mkv|mp4)$", re.IGNORECASE)

        print(f"[{ts}] [DELETE] Searching for movie pattern '{safe_search_name}' in '{movie_base_path}'")
        
        if not os.path.exists(movie_base_path):
            print(f"[{ts}] [DEBUG] Movie base path '{movie_base_path}' DOES NOT EXIST or is INACCESSIBLE according to os.path.exists().")
            await processing_message.edit_text(
                f"❌ *Error*: The movie directory `{escape_markdown(movie_base_path)}` is not found or accessible from the bot's environment\\.\n\n"
                f"Please check your `config.ini` path for movies\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            try:
                await update.message.delete()
            except BadRequest: 
                pass
            return 

        print(f"[{ts}] [DEBUG] Starting os.walk on existing path: {movie_base_path}")
        found_any_files_in_walk = False 
        for root, _, files in os.walk(movie_base_path):
            found_any_files_in_walk = True
            print(f"[{ts}] [DEBUG] Currently walking in: {root}")
            print(f"[{ts}] [DEBUG] Files found in '{root}': {files}") 

            for file in files:
                print(f"[{ts}] [DEBUG] Checking file '{file}' against regex: '{expected_filename_regex.pattern}'")
                if expected_filename_regex.match(file):
                    full_path = os.path.join(root, file)
                    display_text = f"Movie: {os.path.splitext(file)[0]}" 
                    potential_files.append((display_text, full_path))
                    print(f"[{ts}] [DELETE] Found potential movie file: {full_path}")
        
        if not found_any_files_in_walk:
            print(f"[{ts}] [DEBUG] os.walk('{movie_base_path}') yielded no directories or files. Is the directory empty or inaccessible even if it exists?")


    # --- Search for TV Shows ---
    elif media_type == 'tv' and 'season' in parsed_info and 'episode' in parsed_info:
        show_title_raw = parsed_info['title']
        season_num = parsed_info['season']
        episode_num = parsed_info['episode']

        invalid_chars = r'<>:"/\|?*'
        safe_show_title = "".join(c for c in show_title_raw if c not in invalid_chars)
        
        tv_base_path = save_paths.get('tv_shows', save_paths['default'])

        if not os.path.exists(tv_base_path):
            print(f"[{ts}] [DEBUG] TV base path '{tv_base_path}' DOES NOT EXIST or is INACCESSIBLE according to os.path.exists().")
            await processing_message.edit_text(
                f"❌ *Error*: The TV show directory `{escape_markdown(tv_base_path)}` is not found or accessible from the bot's environment\\.\n\n"
                f"Please check your `config.ini` path for TV shows\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            try:
                await update.message.delete()
            except BadRequest:
                pass
            return 

        season_padded = f"{season_num:02d}"
        episode_padded = f"{episode_num:02d}"
        
        episode_file_pattern = re.compile(rf"(?i)s{season_padded}e{episode_padded}.*\.(mkv|mp4)$")

        print(f"[{ts}] [DELETE] Searching for TV show '{safe_show_title}' and episode S{season_padded}E{episode_padded} in '{tv_base_path}'")

        for root, _, files in os.walk(tv_base_path):
            path_segments = [s.lower() for s in root.replace('\\', '/').split('/')] 
            
            show_title_in_path = False
            season_dir_in_path = False

            for segment in path_segments:
                if safe_show_title.lower() in segment: 
                    show_title_in_path = True
                    break
            
            for segment in path_segments:
                season_match = re.match(r'season\s*(\d{1,2})', segment)
                if season_match and int(season_match.group(1)) == season_num:
                    season_dir_in_path = True
                    break

            if show_title_in_path and season_dir_in_path:
                for file in files:
                    if episode_file_pattern.search(file):
                        full_path = os.path.join(root, file)
                        try:
                            relative_display_name_base = os.path.splitext(os.path.relpath(full_path, tv_base_path))[0]
                            display_text = f"TV Show: {relative_display_name_base}"
                        except ValueError: 
                            display_text = f"TV Show: {os.path.splitext(os.path.basename(full_path))[0]}" 
                            
                        potential_files.append((display_text, full_path))
                        print(f"[{ts}] [DELETE] Found potential TV episode file: {full_path}")

    # --- Path 3: Confirmation Step ---
    if potential_files:
        if len(potential_files) > 1:
            reply_text = "⚠️ *Multiple potential files found\\!* Please be more specific with your title\\.\n\n"
            for i, (display_name, _) in enumerate(potential_files):
                reply_text += f"*{i+1}\\.* `{escape_markdown(display_name)}`\n"
            reply_text += "\n" + escape_markdown("Please try again with a more exact name, or consider manual deletion if unsure.")
            
            keyboard = [[
                InlineKeyboardButton("❌ Cancel Delete", callback_data="cancel_delete_operation"),
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await processing_message.edit_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            context.user_data['pending_delete_info'] = None 
        else:
            display_name, abs_path = potential_files[0] 

            reply_text = (
                f"🗑️ *Confirm Deletion:*\n\n"
                f"Are you sure you want to delete this file\\?\n\n"
                f"File: `{escape_markdown(display_name)}`\n"
                f"Path: `{escape_markdown(abs_path)}`\n\n"
                f"*This action cannot be undone\\!*"
            )
            keyboard = [[
                InlineKeyboardButton("✅ Yes, Delete", callback_data="confirm_delete"),
                InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_delete_operation"),
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            context.user_data['pending_delete_info'] = {'path': abs_path, 'display_name': display_name}
            await processing_message.edit_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

    # --- Path 4: No files found after successful parsing ---
    else: 
        print(f"[{ts}] [DELETE] No media found matching '{user_input}' (after parsing, but no files found).")
        await processing_message.edit_text(
            f"❌ No media found matching `{escape_markdown(user_input)}` in your configured media directories\\.\n\n"
            f"Please ensure the title is exact and the file is located within the bot's managed paths\\.\n"
            f"Send `/cancel` to stop this operation\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    
    # Delete the user's initial input message (if not already deleted by the 'unknown' path)
    if update.message.message_id != processing_message.message_id: 
        try:
            await update.message.delete()
            print(f"[{ts}] [DELETE] Deleted user's input message for deletion (final step).")
        except BadRequest as e:
            if "Message to delete not found" in str(e) or "not enough rights" in str(e):
                print(f"[{ts}] [WARN] Could not delete user's delete input message. Reason: {e}")
            else:
                raise 

async def plex_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks the connection to the Plex Media Server."""
    if not await is_user_authorized(update, context):
        return
    if not update.message: return

    status_message = await update.message.reply_text("Plex Status: 🟡 Checking connection...")
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    plex_config = context.bot_data.get("PLEX_CONFIG", {})
    if not plex_config:
        await status_message.edit_text("Plex Status: ⚪️ Not configured. Please add your Plex details to the `config.ini` file.")
        return

    try:
        print(f"[{ts}] [PLEX STATUS] Attempting to connect to Plex server...")
        
        # Run blocking Plex calls in a separate thread
        plex = await asyncio.to_thread(PlexServer, plex_config['url'], plex_config['token'])
        server_version = plex.version
        server_platform = plex.platform
        
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX STATUS] Success! Connected to Plex Media Server v{server_version} on {server_platform}.")
        
        success_text = (
            f"Plex Status: ✅ *Connected*\n\n"
            f"*Server Version:* `{escape_markdown(server_version)}`\n"
            f"*Platform:* `{escape_markdown(server_platform)}`"
        )
        await status_message.edit_text(success_text, parse_mode=ParseMode.MARKDOWN_V2)

    except Unauthorized:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX STATUS] ERROR: Connection failed. The Plex token is invalid.")
        error_text = (
            "Plex Status: ❌ *Authentication Failed*\n\n"
            "The Plex API token is incorrect\\. Please check your `config\\.ini` file\\."
        )
        await status_message.edit_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)

    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX STATUS] ERROR: An unexpected error occurred: {e}")
        error_text = (
            f"Plex Status: ❌ *Connection Failed*\n\n"
            f"Could not connect to the Plex server at `{escape_markdown(plex_config['url'])}`\\. "
            f"Please ensure the server is running and accessible\\."
        )
        await status_message.edit_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)

async def plex_restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(NEW - Linux Simplified) Restarts the Plex server via a direct subprocess call."""
    if not await is_user_authorized(update, context):
        return
    if not update.message: return

    # Check if the command is being run on Linux
    if platform.system() != "Linux":
        await update.message.reply_text("This command is configured to run on Linux only.")
        return

    status_message = await update.message.reply_text("Plex Restart: 🟡 Sending restart command to the server...")
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    script_path = os.path.abspath("restart_plex.sh")

    if not os.path.exists(script_path):
        print(f"[{ts}] [PLEX RESTART] ERROR: Wrapper script not found at {script_path}")
        await status_message.edit_text("❌ *Error:* The `restart_plex.sh` script was not found in the bot's directory.")
        return
    
    # The command to run, assuming sudoers is pre-configured
    command = ["/usr/bin/sudo", script_path]

    try:
        # Get the absolute path to the script, assuming it's in the same directory as the bot.
        script_path = os.path.abspath("restart_plex.sh")
        
        if not os.path.exists(script_path):
            await status_message.edit_text("❌ *Error:* `restart_plex.sh` not found in the bot's directory.")
            return

        command = ["/usr/bin/sudo", script_path]

        print(f"[{ts}] [PLEX RESTART] Executing wrapper script: {' '.join(command)}")
        
        result = await asyncio.to_thread(
            subprocess.run, command, check=True, capture_output=True, text=True
        )

        success_message = "✅ *Plex Restart Successful*"
        await status_message.edit_text(success_message, parse_mode=ParseMode.MARKDOWN_V2)
        print(f"[{ts}] [PLEX RESTART] Success!")

    except subprocess.CalledProcessError as e:
        error_output = e.stderr or e.stdout
        error_text = f"❌ *Script Failed*\n\nThis almost always means the `sudoers` rule for `restart_plex.sh` is incorrect or missing\\.\n\n*Details:*\n`{escape_markdown(error_output)}`"
        print(f"[{ts}] [PLEX RESTART] ERROR executing script: {error_output}")
        await status_message.edit_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        error_text = f"❌ *An Unexpected Error Occurred*\n\n`{escape_markdown(str(e))}`"
        print(f"[{ts}] [PLEX RESTART] ERROR: {str(e)}")
        await status_message.edit_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)

async def find_magnet_link_on_page(url: str) -> List[str]:
    """
    Fetches a web page and attempts to find all unique magnet links (href starting with 'magnet:').
    Returns a list of unique found magnet links, or an empty list if none are found.
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # --- MODIFIED: Use a set to store unique magnet links ---
    unique_magnet_links: Set[str] = set() 

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            print(f"[{ts}] [WEBSCRAPE] Fetching URL: {url}")
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status() # Raise an exception for HTTP errors (4xx or 5xx)

        soup = BeautifulSoup(response.text, 'lxml')

        # Look for all <a> tags with an href starting with 'magnet:'
        magnet_link_tags = soup.find_all('a', href=re.compile(r'^magnet:'))

        if magnet_link_tags:
            for tag in magnet_link_tags:
                if isinstance(tag, Tag):
                    magnet_link = tag.get('href')
                    if isinstance(magnet_link, str):
                        # --- MODIFIED: Add to set instead of list ---
                        unique_magnet_links.add(magnet_link) 
            
            if unique_magnet_links:
                print(f"[{ts}] [WEBSCRAPE] Found {len(unique_magnet_links)} unique magnet link(s) on page: {url}")
                # Log the first one found (arbitrary order from set) for brevity
                first_link = next(iter(unique_magnet_links)) # Get first element from set
                print(f"[{ts}] [WEBSCRAPE] First unique magnet link: {first_link[:100]}...")
            else:
                print(f"[{ts}] [WEBSCRAPE] No valid magnet links found after parsing tags on page: {url}")
        else:
            print(f"[{ts}] [WEBSCRITICAL] No <a> tags with magnet links found on page: {url}")

    except httpx.RequestError as e:
        print(f"[{ts}] [WEBSCRAPE ERROR] HTTP Request failed for {url}: {e}")
    except Exception as e:
        print(f"[{ts}] [WEBSCRAPE ERROR] An unexpected error occurred during scraping {url}: {e}")
    
    # --- MODIFIED: Convert set back to a list before returning ---
    return list(unique_magnet_links)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles incoming messages, processing magnet links, .torrent files,
    or web pages containing magnet links.
    """
    if not await is_user_authorized(update, context):
        return
        
    if not update.message or not update.message.text: return
    chat_id = update.message.chat_id
    text = update.message.text.strip()
    user_message_to_delete = update.message # Reference to the original message for deletion

    # --- FIX: Ensure context.user_data is a dictionary ---
    if context.user_data is None:
        context.user_data = {} # Initialize if it's None to prevent "Object of type 'None' is not subscriptable"
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] context.user_data was None and has been initialized.")

    # Check if user already has an active download
    if str(chat_id) in context.bot_data.get('active_downloads', {}):
        await update.message.reply_text("ℹ️ You already have a download in progress. Please /cancel it before starting a new one.")
        return

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Received link from chat_id {chat_id}. Starting analysis.")

    # Send initial "Analyzing..." message
    progress_message = await update.message.reply_text("✅ Input received. Analyzing...")

    source_value: Optional[str] = None
    source_type: Optional[str] = None
    ti: Optional[lt.torrent_info] = None # type: ignore

    # --- 1. Handle Direct Magnet Link ---
    if text.startswith('magnet:?xt=urn:btih:'):
        source_type = 'magnet'
        source_value = text
        
        ti = await fetch_metadata_from_magnet(text, progress_message, context)
        
        if not ti: # If metadata fetching failed or timed out, an error message is already sent
            return

    # --- 2. Handle Direct .torrent URL ---
    elif text.startswith(('http://', 'https://')) and text.endswith('.torrent'):
        source_type = 'file'
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(text, follow_redirects=True, timeout=30)
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            torrent_content = response.content
        except httpx.RequestError as e:
            error_msg = f"Failed to download .torrent file from URL: {e}"
            await progress_message.edit_text(f"❌ *Error:* {escape_markdown(error_msg)}", parse_mode=ParseMode.MARKDOWN_V2)
            return

        try:
            ti = lt.torrent_info(torrent_content) # type: ignore
            info_hash = str(ti.info_hashes().v1)  # type: ignore
            torrents_dir = ".torrents"
            os.makedirs(torrents_dir, exist_ok=True)
            
            # Persist .torrent file for restart recovery
            source_value = os.path.join(torrents_dir, f"{info_hash}.torrent")
            with open(source_value, "wb") as f:
                f.write(torrent_content)
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Persistently saved .torrent file to '{source_value}'")

        except RuntimeError:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Failed to parse .torrent file for chat_id {chat_id}.")
            await progress_message.edit_text(r"❌ *Error:* The provided file is not a valid torrent\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
            
    # --- 3. Handle Generic HTTP/HTTPS URL (Web Scraping for Magnet Links) ---
    elif text.startswith(('http://', 'https://')):
        ts_scrape = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts_scrape}] [INFO] Attempting to scrape magnet link from provided URL: {text}")
        
        # Escape the static part of the message containing parentheses
        safe_message_part = escape_markdown("Attempting to find magnet link(s) on:")
        await progress_message.edit_text(
            f"🌐 *Web Page Detected:*\n{safe_message_part}\n`{escape_markdown(text)}`", 
            parse_mode=ParseMode.MARKDOWN_V2
        )

        extracted_magnet_links = await find_magnet_link_on_page(text)

        if extracted_magnet_links:
            # If multiple magnet links are found, present choices to the user
            if len(extracted_magnet_links) > 1:
                print(f"[{ts_scrape}] [INFO] Multiple magnet links found. Presenting choices to user.")
                
                # Store the full list of magnet links in user_data for later retrieval by button_handler
                context.user_data['temp_magnet_choices'] = extracted_magnet_links
                
                choices_text = f"Found {len(extracted_magnet_links)} magnet links\\. Please select one to download:\n\n"
                keyboard = []
                for i, link in enumerate(extracted_magnet_links):
                    # Try to extract the display name from the magnet link (dn= parameter)
                    name_match = re.search(r'dn=([^&]+)', link)
                    link_display_name = name_match.group(1) if name_match else f"Link {i+1}"
                    
                    # Decode URL-encoded characters for display (e.g., %20 to space)
                    try:
                        link_display_name = urllib.parse.unquote_plus(link_display_name)
                    except Exception:
                        pass # Ignore if decoding fails, use as is

                    # Shorten display name if too long and escape it
                    display_escaped_name = escape_markdown(link_display_name)
                    if len(display_escaped_name) > 60: # Limit length for cleaner display
                        display_escaped_name = display_escaped_name[:57] + "..."

                    choices_text += f"*{i+1}\\.* `{display_escaped_name}`\n" # FIX: Escaped the dot
                    # Create a button for each link with a unique callback_data
                    keyboard.append([InlineKeyboardButton(f"Select {i+1}", callback_data=f"select_magnet_{i}")])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await progress_message.edit_text(
                    choices_text, 
                    reply_markup=reply_markup, 
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                
                # IMPORTANT: Exit here. The rest of the download process will be triggered
                # by the `button_handler` once the user makes a selection.
                return
            
            # If only one magnet link is found, proceed directly with it
            source_type = 'magnet'
            source_value = extracted_magnet_links[0] 
            print(f"[{ts_scrape}] [INFO] Successfully extracted a single magnet link. Proceeding with download via magnet.")

            ti = await fetch_metadata_from_magnet(source_value, progress_message, context)
            if not ti:
                return # fetch_metadata_from_magnet already handles error message
        else:
            # No magnet links found on the page
            print(f"[{ts_scrape}] [ERROR] No magnet links found on the provided page or page not accessible.")
            error_message_text = "The provided URL does not contain any magnet links, or the page could not be accessed."
            await progress_message.edit_text(f"❌ *Error:* {escape_markdown(error_message_text)}", parse_mode=ParseMode.MARKDOWN_V2)
            return
    # --- End of Generic URL Handling ---

    # --- Fallback for invalid input ---
    else:
        error_message_text = "This does not look like a valid .torrent URL, magnet link, or a web page containing a magnet link."
        await progress_message.edit_text(f"❌ *Error:* {escape_markdown(error_message_text)}", parse_mode=ParseMode.MARKDOWN_V2)
        return

    # At this point, `ti` should be populated for direct magnet/torrent inputs,
    # or after a single magnet link was successfully scraped and processed.
    # If control reached here via multi-magnet selection, this path would have exited.
    if not ti: # This check handles cases where metadata fetch failed for direct inputs
        await progress_message.edit_text("❌ *Error:* Could not analyze the torrent content.", parse_mode=ParseMode.MARKDOWN_V2)
        # Clean up the .torrent file if it was downloaded but failed to parse/analyze
        if source_type == 'file' and source_value and os.path.exists(source_value):
            os.remove(source_value)
        return

    # --- 4. Validate Torrent Size ---
    if ti.total_size() > MAX_TORRENT_SIZE_BYTES:
        error_msg = f"This torrent is *{format_bytes(ti.total_size())}*, which is larger than the *{MAX_TORRENT_SIZE_GB} GB* limit."
        await progress_message.edit_text(f"❌ *Size Limit Exceeded*\n\n{error_msg}", parse_mode=ParseMode.MARKDOWN_V2)
        if source_type == 'file' and source_value and os.path.exists(source_value): os.remove(source_value)
        return

    # --- 5. Validate Torrent File Types ---
    validation_error = validate_torrent_files(ti)
    if validation_error:
        error_msg = f"This torrent {validation_error}"
        await progress_message.edit_text(f"❌ *Unsupported File Type*\n\n{error_msg}", parse_mode=ParseMode.MARKDOWN_V2)
        if source_type == 'file' and source_value and os.path.exists(source_value): os.remove(source_value)
        return
    
    # --- 6. Parse Torrent Name and Fetch Metadata for TV Shows ---
    parsed_info = parse_torrent_name(ti.name())
    display_name = ""

    if parsed_info['type'] == 'movie':
        display_name = f"{parsed_info['title']} ({parsed_info['year']})"
    
    elif parsed_info['type'] == 'tv':
        # Update user message during Wikipedia lookup
        wiki_search_msg = escape_markdown("TV show detected. Searching Wikipedia for episode title...")
        await progress_message.edit_text(f"📺 {wiki_search_msg}", parse_mode=ParseMode.MARKDOWN_V2)
        
        episode_title, corrected_show_title = await fetch_episode_title_from_wikipedia(
            show_title=parsed_info['title'],
            season=parsed_info['season'],
            episode=parsed_info['episode']
        )
        parsed_info['episode_title'] = episode_title

        # If Wikipedia returned a corrected show title (e.g., from a redirect)
        if corrected_show_title:
            ts_wiki = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts_wiki}] [INFO] Updating show title in 'parsed_info' to reflect Wikipedia match: '{corrected_show_title}'")
            parsed_info['title'] = corrected_show_title
        
        base_name = f"{parsed_info['title']} - S{parsed_info['season']:02d}E{parsed_info['episode']:02d}"
        display_name = f"{base_name} - {episode_title}" if episode_title else base_name
    else: # 'unknown' type
        display_name = parsed_info['title']

    # --- 7. Prepare Confirmation Message and Buttons ---
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
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Sent confirmation prompt to chat_id {chat_id} for torrent '{display_name}'.")

    # --- 8. Delete Original User Message (for clean UI) ---
    try:
        await user_message_to_delete.delete()
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Deleted original link message from user {chat_id}.")
    except BadRequest as e:
        if "Message to delete not found" in str(e) or "not enough rights" in str(e):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not delete user's message. Reason: {e}")
        else:
            raise # Re-raise if it's an unexpected BadRequest

    # --- 9. Store Pending Torrent Information for Button Handler ---
    # This context.user_data is now guaranteed to be a dict by the initial fix.
    context.user_data['pending_torrent'] = {
        'type': source_type, 
        'value': source_value, # This is the actual magnet link or path to .torrent file
        'clean_name': display_name,
        'parsed_info': parsed_info,
        'original_message_id': progress_message.message_id # Store the ID of the message to be updated
    }

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_authorized(update, context):
        return

    query = update.callback_query
    if not query: return
    await query.answer()

    message = query.message
    if not isinstance(message, Message): return
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    print(f"[{ts}] [INFO] Received button press from user {query.from_user.id}: '{query.data}'")

    if context.user_data is None:
        context.user_data = {}
        print(f"[{ts}] [WARN] context.user_data was None in button_handler and has been initialized.")

    # --- 1. Handle magnet selection first ---
    if query.data and query.data.startswith("select_magnet_"):
        if 'temp_magnet_choices' not in context.user_data:
            print(f"[{ts}] [WARN] Magnet selection from user {query.from_user.id} ignored: No pending magnet choices (session expired).")
            try:
                await query.edit_message_text("This selection has expired. Please send the link again.")
            except BadRequest as e:
                if "Message is not modified" not in str(e): pass
            return

        selected_index = int(query.data.split('_')[2])
        magnet_links = context.user_data.pop('temp_magnet_choices') 

        if 0 <= selected_index < len(magnet_links):
            # FIX: Define source_type and source_value here
            source_value = magnet_links[selected_index]
            source_type = 'magnet' # For magnet selections, the type is always 'magnet'

            print(f"[{ts}] [SUCCESS] User {query.from_user.id} selected magnet link {selected_index + 1}.")
            
            await query.edit_message_text(f"✅ Selected magnet link {selected_index + 1}. Analyzing...")

            ti = await fetch_metadata_from_magnet(source_value, message, context) # Use source_value here
            
            if not ti:
                return 
            
            if ti.total_size() > MAX_TORRENT_SIZE_BYTES:
                error_msg = f"This torrent is *{format_bytes(ti.total_size())}*, which is larger than the *{MAX_TORRENT_SIZE_GB} GB* limit."
                await message.edit_text(f"❌ *Size Limit Exceeded*\n\n{escape_markdown(error_msg)}", parse_mode=ParseMode.MARKDOWN_V2) # Escaped error_msg
                return

            validation_error = validate_torrent_files(ti)
            if validation_error:
                error_msg = f"This torrent {validation_error}"
                await message.edit_text(f"❌ *Unsupported File Type*\n\n{escape_markdown(error_msg)}", parse_mode=ParseMode.MARKDOWN_V2) # Escaped error_msg
                return
            
            parsed_info = parse_torrent_name(ti.name())
            display_name = ""

            if parsed_info['type'] == 'movie':
                display_name = f"{parsed_info['title']} ({parsed_info['year']})"
            
            elif parsed_info['type'] == 'tv':
                await message.edit_text(f"📺 {escape_markdown('TV show detected. Searching Wikipedia for episode title...')}", parse_mode=ParseMode.MARKDOWN_V2)
                
                episode_title, corrected_show_title = await fetch_episode_title_from_wikipedia(
                    show_title=parsed_info['title'],
                    season=parsed_info['season'],
                    episode=parsed_info['episode']
                )
                parsed_info['episode_title'] = episode_title

                if corrected_show_title:
                    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    print(f"[{ts}] [INFO] Updating show title in 'parsed_info' to reflect Wikipedia match: '{corrected_show_title}'")
                    parsed_info['title'] = corrected_show_title
                
                base_name = f"{parsed_info['title']} - S{parsed_info['season']:02d}E{parsed_info['episode']:02d}"
                display_name = f"{base_name} - {episode_title}" if episode_title else base_name
            else:
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
            
            await message.edit_text(confirmation_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Sent confirmation prompt to chat_id {message.chat_id} for torrent '{display_name}'.")
            
            # Now source_type and source_value are correctly defined here
            context.user_data['pending_torrent'] = {
                'type': source_type, 
                'value': source_value, 
                'clean_name': display_name,
                'parsed_info': parsed_info,
                'original_message_id': message.message_id 
            }
        else:
            print(f"[{ts}] [ERROR] Invalid magnet selection index: {selected_index}")
            await query.edit_message_text("❌ Invalid selection. Please try again or send a new link.")
        return 

    # --- 2. Handle Delete Confirmation / Cancellation ---
    elif query.data == "confirm_delete" or query.data == "cancel_delete_operation":
        if 'pending_delete_info' not in context.user_data or not context.user_data['pending_delete_info']:
            print(f"[{ts}] [DELETE] Confirmation ignored: No pending delete info (session expired or invalid).")
            try:
                await query.edit_message_text("This deletion request has expired. Please start over with `/delete`.")
            except BadRequest as e:
                if "Message is not modified" not in str(e): pass
            return

        delete_info = context.user_data.pop('pending_delete_info') 
        
        if query.data == "confirm_delete":
            file_path_to_delete = delete_info['path']
            display_name = delete_info['display_name']

            print(f"[{ts}] [DELETE] User {query.from_user.id} confirmed deletion of: '{file_path_to_delete}'")
            
            try:
                safe_to_delete = False
                plex_save_paths = context.bot_data["SAVE_PATHS"]
                allowed_base_paths = [
                    plex_save_paths.get('movies', ''),
                    plex_save_paths.get('tv_shows', ''),
                    plex_save_paths.get('default', '')
                ]
                
                abs_file_path_to_delete = os.path.abspath(file_path_to_delete)

                for base_path in allowed_base_paths:
                    if base_path: 
                        abs_base_path = os.path.abspath(base_path)
                        if abs_file_path_to_delete.startswith(abs_base_path):
                            safe_to_delete = True
                            break
                
                if not safe_to_delete:
                    print(f"[{ts}] [DELETE ERROR] Attempted to delete a file outside managed paths: {file_path_to_delete}")
                    await query.edit_message_text(f"❌ *Deletion Failed:* Attempted to delete a file outside managed paths\\. This is a security precaution\\.", parse_mode=ParseMode.MARKDOWN_V2)
                    return

                if os.path.exists(file_path_to_delete):
                    if os.path.isfile(file_path_to_delete):
                        os.remove(file_path_to_delete)
                        print(f"[{ts}] [DELETE] Successfully deleted file: {file_path_to_delete}")
                        parent_dir = os.path.dirname(file_path_to_delete)
                        if not os.listdir(parent_dir) and os.path.commonpath([abs_file_path_to_delete, parent_dir]) == parent_dir:
                            os.rmdir(parent_dir)
                            print(f"[{ts}] [DELETE] Deleted empty directory: {parent_dir}")
                    elif os.path.isdir(file_path_to_delete): 
                        shutil.rmtree(file_path_to_delete)
                        print(f"[{ts}] [DELETE] Successfully deleted directory: {file_path_to_delete}")
                    
                    plex_config = context.bot_data.get("PLEX_CONFIG", {})
                    scan_status_message = ""
                    if plex_config:
                        try:
                            plex = await asyncio.to_thread(PlexServer, plex_config['url'], plex_config['token'])
                            library_name = None
                            if "Movie:" in display_name:
                                library_name = 'Movies'
                            elif "TV Show:" in display_name:
                                library_name = 'TV Shows'
                            
                            if library_name:
                                target_library = await asyncio.to_thread(plex.library.section, library_name)
                                await asyncio.to_thread(target_library.update)
                                scan_status_message = f"\n\nPlex scan for the `{escape_markdown(library_name)}` library has been initiated\\."
                                print(f"[{ts}] [PLEX] Successfully triggered scan for '{library_name}' library after deletion.")
                            else:
                                print(f"[{ts}] [PLEX] Could not infer library type for Plex scan after deletion of '{display_name}'.")

                        except Unauthorized:
                            print(f"[{ts}] [PLEX ERROR] Plex token is invalid during post-delete scan.")
                            scan_status_message = "\n\n*Plex Error:* Could not trigger scan due to an invalid token\\."
                        except NotFound:
                            print(f"[{ts}] [PLEX ERROR] Plex library not found during post-delete scan.")
                            scan_status_message = "\n\n*Plex Error:* Library not found for scan\\."
                        except Exception as e:
                            print(f"[{ts}] [PLEX ERROR] An unexpected error occurred while connecting to Plex for post-delete scan: {e}")
                            scan_status_message = "\n\n*Plex Error:* Could not connect to server to trigger scan\\."

                    await query.edit_message_text(
                        f"✅ *Deletion Successful:*\n\n"
                        f"`{escape_markdown(display_name)}` has been deleted\\."
                        f"{scan_status_message}",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                else:
                    print(f"[{ts}] [DELETE ERROR] File not found during actual deletion step: {file_path_to_delete}")
                    await query.edit_message_text(f"❌ *Deletion Failed:* File not found or already deleted: `{escape_markdown(file_path_to_delete)}`", parse_mode=ParseMode.MARKDOWN_V2)

            except Exception as e:
                print(f"[{ts}] [DELETE ERROR] Error during file deletion: {e}")
                await query.edit_message_text(f"❌ *Deletion Failed:* An error occurred during deletion: `{escape_markdown(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
        
        elif query.data == "cancel_delete_operation":
            print(f"[{ts}] [DELETE] User {query.from_user.id} cancelled deletion operation.")
            try:
                await query.edit_message_text("❌ Deletion cancelled.")
            except BadRequest as e:
                if "Message is not modified" not in str(e): pass
        return 

    # --- 3. Handle Download Confirmation / Cancellation ---
    # This is the existing logic for "confirm_download" and "cancel_operation" buttons.
    # It will only be reached if the query.data was NOT a magnet selection or a delete operation.
    
    if 'pending_torrent' not in context.user_data: 
        print(f"[{ts}] [WARN] Button press from user {query.from_user.id} ignored: No pending torrent found (session likely expired).")
        try:
            await query.edit_message_text("This action has expired. Please send the link again.")
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
        return

    pending_torrent = context.user_data.pop('pending_torrent') 
    
    if query.data == "confirm_download":
        print(f"[{ts}] [SUCCESS] Download confirmed by user {query.from_user.id}. Queuing download task.")
        try:
            await query.edit_message_text("✅ Confirmation received. Your download has been queued.")
        except BadRequest as e:
            if "Message is not modified" not in str(e): pass

        save_paths = context.bot_data["SAVE_PATHS"]
        parsed_info = pending_torrent.get('parsed_info', {})
        torrent_type = parsed_info.get('type')

        final_save_path = save_paths['default']
        if torrent_type == 'movie':
            final_save_path = save_paths['movies']
            print(f"[{ts}] [INFO] Torrent identified as a movie. Saving to: {final_save_path}")
        elif torrent_type == 'tv':
            final_save_path = save_paths['tv_shows']
            print(f"[{ts}] [INFO] Torrent identified as a TV show. Saving to: {final_save_path}")
        else:
            print(f"[{ts}] [INFO] Torrent type is unknown. Saving to default path: {final_save_path}")

        active_downloads = context.bot_data.get('active_downloads', {})
        
        download_data = {
            'source_dict': pending_torrent,
            'chat_id': message.chat_id, 
            'message_id': pending_torrent['original_message_id'], 
            'save_path': final_save_path
        }
        
        task = asyncio.create_task(download_task_wrapper(download_data, context.application))
        download_data['task'] = task
        active_downloads[str(message.chat_id)] = download_data
        
        save_active_downloads(context.bot_data['persistence_file'], active_downloads)

    elif query.data == "cancel_operation":
        print(f"[{ts}] [CANCEL] Operation cancelled by user {query.from_user.id} via button.")
        try:
            await query.edit_message_text("❌ Operation cancelled by user.")
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise

async def download_task_wrapper(download_data: Dict, application: Application):
    source_dict = download_data['source_dict']
    chat_id = download_data['chat_id']
    message_id = download_data['message_id']
    base_save_path = download_data['save_path']
    
    source_value = source_dict['value']
    source_type = source_dict['type']
    clean_name = source_dict.get('clean_name', "Download")
    parsed_info = source_dict.get('parsed_info', {})
    
    ts_start = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts_start}] [INFO] Starting/Resuming download task for '{clean_name}' for chat_id {chat_id}.")
    
    last_update_time = 0
    async def report_progress(status: lt.torrent_status): #type: ignore
        nonlocal last_update_time
        ts_progress = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_name = status.name if status.name else clean_name
        progress_percent = status.progress * 100
        speed_mbps = status.download_rate / 1024 / 1024
        print(f"[{ts_progress}] [LOG] {log_name}: {progress_percent:.2f}% | Peers: {status.num_peers} | Speed: {speed_mbps:.2f} MB/s")

        current_time = time.monotonic()
        if current_time - last_update_time > 5:
            last_update_time = current_time
            name_str = ""
            if parsed_info.get('type') == 'tv':
                show_title = parsed_info.get('title', 'Unknown Show')
                season_num = parsed_info.get('season', 0)
                episode_num = parsed_info.get('episode', 0)
                episode_title = parsed_info.get('episode_title', 'Unknown Episode')
                safe_show_title = escape_markdown(show_title)
                safe_episode_details = escape_markdown(f"S{season_num:02d}E{episode_num:02d} - {episode_title}")
                name_str = f"`{safe_show_title}`\n`{safe_episode_details}`"
            else:
                safe_clean_name = escape_markdown(clean_name)
                name_str = f"`{safe_clean_name}`"

            progress_str = escape_markdown(f"{progress_percent:.2f}")
            speed_str = escape_markdown(f"{speed_mbps:.2f}")
            state_str = escape_markdown(status.state.name)
            
            telegram_message = (
                f"⬇️ *Downloading:*\n{name_str}\n"
                f"*Progress:* {progress_str}%\n"
                f"*State:* {state_str}\n"
                f"*Peers:* {status.num_peers}\n"
                f"*Speed:* {speed_str} MB/s"
            )
            try:
                await application.bot.edit_message_text(text=telegram_message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest as e:
                if "Message is not modified" not in str(e): print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")

    try:
        success, ti = await download_with_progress(
            source=source_value, 
            save_path=base_save_path,
            status_callback=report_progress,
            bot_data=application.bot_data,
            allowed_extensions=ALLOWED_EXTENSIONS
        )
        if success and ti:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SUCCESS] Download task for '{clean_name}' completed. Starting post-processing.")
            scan_status_message = ""
            
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
                    
                    # --- NEW: Intelligent Directory Finding Logic for TV Shows ---
                    if parsed_info.get('type') == 'tv':
                        show_title = parsed_info.get('title', 'Unknown Show')
                        season_num = parsed_info.get('season', 0)
                        
                        invalid_chars = r'<>:"/\|?*'
                        safe_show_title = "".join(c for c in show_title if c not in invalid_chars)

                        show_directory = os.path.join(base_save_path, safe_show_title)
                        os.makedirs(show_directory, exist_ok=True)

                        season_prefix = f"Season {season_num:02d}"
                        found_season_dir = None
                        try:
                            for item in os.listdir(show_directory):
                                if os.path.isdir(os.path.join(show_directory, item)) and item.startswith(season_prefix):
                                    found_season_dir = os.path.join(show_directory, item)
                                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Found existing season directory: '{found_season_dir}'")
                                    break
                        except OSError as e:
                             print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Could not scan for season directory: {e}")
                        
                        if found_season_dir:
                            destination_directory = found_season_dir
                        else:
                            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] No existing directory found for '{season_prefix}'. Creating new one.")
                            destination_directory = os.path.join(show_directory, season_prefix)
                    # --- END of new logic ---

                    os.makedirs(destination_directory, exist_ok=True)
                    
                    current_path = os.path.join(base_save_path, target_file_path_in_torrent)
                    new_path = os.path.join(destination_directory, final_filename)
                    
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [MOVE] From: {current_path}\n[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [MOVE] To:   {new_path}")
                    shutil.move(current_path, new_path)
                    
                    plex_config = application.bot_data.get("PLEX_CONFIG", {})
                    if plex_config:
                        media_type = parsed_info.get('type')
                        library_name = None
                        if media_type == 'movie':
                            library_name = 'Movies'
                        elif media_type == 'tv':
                            library_name = 'TV Shows'
                        
                        if library_name:
                            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX] Attempting to scan '{library_name}' library...")
                            try:
                                plex = await asyncio.to_thread(PlexServer, plex_config['url'], plex_config['token'])
                                target_library = await asyncio.to_thread(plex.library.section, library_name)
                                await asyncio.to_thread(target_library.update)
                                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX] Successfully triggered scan for '{library_name}' library.")
                                scan_status_message = f"\n\nPlex scan for the `{escape_markdown(library_name)}` library has been initiated\\."
                            except Unauthorized:
                                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX ERROR] Plex token is invalid.")
                                scan_status_message = "\n\n*Plex Error:* Could not trigger scan due to an invalid token\\."
                            except NotFound:
                                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX ERROR] Plex library '{library_name}' not found.")
                                scan_status_message = f"\n\n*Plex Error:* Library `{escape_markdown(library_name)}` not found\\."
                            except Exception as e:
                                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX ERROR] An unexpected error occurred while connecting to Plex: {e}")
                                scan_status_message = "\n\n*Plex Error:* Could not connect to server to trigger scan\\."
                    
                    original_top_level_dir = os.path.join(base_save_path, target_file_path_in_torrent.split(os.path.sep)[0])
                    if os.path.isdir(original_top_level_dir) and not os.listdir(original_top_level_dir):
                         print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CLEANUP] Deleting empty original directory: {original_top_level_dir}")
                         shutil.rmtree(original_top_level_dir)
                    elif os.path.isfile(original_top_level_dir):
                        pass

            except Exception as e:
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Post-processing failed: {e}")

            final_message = (
                f"✅ *Success\\!*\n"
                f"Renamed and moved to Plex Server:\n"
                f"`{escape_markdown(clean_name)}`"
                f"{scan_status_message}"
            )
            await application.bot.edit_message_text(text=final_message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN_V2)

    except asyncio.CancelledError:
        ts_cancel = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if application.bot_data.get('is_shutting_down', False):
            print(f"[{ts_cancel}] [INFO] Task for '{clean_name}' paused due to bot shutdown.")
            raise
        
        print(f"[{ts_cancel}] [CANCEL] Download task for '{clean_name}' was cancelled by user {chat_id}.")
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
        ts_except = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts_except}] [ERROR] An unexpected exception occurred in download task for '{clean_name}': {e}")
        safe_error = escape_markdown(str(e))
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
        ts_final = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if not application.bot_data.get('is_shutting_down', False):
            print(f"[{ts_final}] [INFO] Cleaning up resources for task '{clean_name}' for chat_id {chat_id}.")
            active_downloads = application.bot_data.get('active_downloads', {})
            if str(chat_id) in active_downloads:
                del active_downloads[str(chat_id)]
                save_active_downloads(application.bot_data['persistence_file'], active_downloads)

            if source_type == 'file' and source_value and os.path.exists(source_value):
                os.remove(source_value)

            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CLEANUP] Scanning '{base_save_path}' for leftover .parts files...")
            try:
                for filename in os.listdir(base_save_path):
                    if filename.endswith(".parts"):
                        parts_file_path = os.path.join(base_save_path, filename)
                        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CLEANUP] Found and deleting leftover parts file: {parts_file_path}")
                        os.remove(parts_file_path)
            except Exception as e:
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Could not perform .parts file cleanup: {e}")

# --- MAIN SCRIPT EXECUTION ---
# --- MAIN SCRIPT EXECUTION ---
if __name__ == '__main__':
    PERSISTENCE_FILE = 'persistence.json'

    try:
        BOT_TOKEN, SAVE_PATHS, ALLOWED_USER_IDS, PLEX_CONFIG = get_configuration()
    except (FileNotFoundError, ValueError) as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CRITICAL ERROR: {e}")
        sys.exit(1)

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting bot...")
    
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    
    application.bot_data["SAVE_PATHS"] = SAVE_PATHS
    application.bot_data["PLEX_CONFIG"] = PLEX_CONFIG
    application.bot_data["persistence_file"] = PERSISTENCE_FILE
    application.bot_data["ALLOWED_USER_IDS"] = ALLOWED_USER_IDS
    application.bot_data.setdefault('active_downloads', {})

    # --- NEW: Create and store a single, long-lived libtorrent session ---
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Creating global libtorrent session for the application.")
    application.bot_data["TORRENT_SESSION"] = lt.session({ #type: ignore
        'listen_interfaces': '0.0.0.0:6881', 
        'dht_bootstrap_nodes': 'router.utorrent.com:6881,router.bittorrent.com:6881,dht.transmissionbt.com:6881'
    })
    
    # --- MODIFIED: Replaced CommandHandlers with MessageHandlers for flexibility ---
    # This allows users to type commands with or without the leading '/'.
    # The regex '^(?i)/?command$' matches 'command', '/command', 'Command', etc.
    # The order is crucial: these specific handlers are added BEFORE the generic one.
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?hello$', re.IGNORECASE)), start_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?start$', re.IGNORECASE)), start_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?help$', re.IGNORECASE)), help_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?cancel$', re.IGNORECASE)), cancel_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?delete$', re.IGNORECASE)), delete_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?plexstatus$', re.IGNORECASE)), plex_status_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?plexrestart$', re.IGNORECASE)), plex_restart_command))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & waiting_for_delete_input_filter_instance, # FIX: Use the instantiated filter object
            handle_delete_input
        )
    )
        
    # This generic handler for links/magnets now correctly comes after the specific command
    # handlers and will not be triggered by command words.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # This handler for button presses is unaffected.
    application.add_handler(CallbackQueryHandler(button_handler))
    
    application.run_polling()

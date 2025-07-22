# file: telegram_bot.py

import datetime
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

from plexapi.server import PlexServer
from plexapi.exceptions import NotFound, Unauthorized

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
    (Primary Strategy - REVISED & ROBUST)
    Parses a dedicated 'List of...' page by using the predictable table structure.
    It directly targets the correct table for the season and the correct column
    for the episode number, as per the user's analysis.
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [WIKI] Applying structured parsing for S{season:02d}E{episode:02d}")

    tables = soup.find_all('table', class_='wikitable')
    
    # Per analysis: Table at index 0 is season summary. Table at index 1 is Season 1, etc.
    # Therefore, the target table's index is the season number.
    # For Season 4, we want the table at index 4 (the 5th 'wikitable' on the page).
    target_table_index = season
    
    print(f"[{ts}] [WIKI] Found {len(tables)} 'wikitables'. Targeting table at index {target_table_index} for Season {season}.")

    # --- Boundary check to ensure the target table exists ---
    if target_table_index >= len(tables):
        print(f"[{ts}] [WIKI ERROR] Target table index {target_table_index} is out of bounds. The page may have fewer season tables than expected.")
        return None

    target_table = tables[target_table_index]
    if not isinstance(target_table, Tag):
        print(f"[{ts}] [WIKI ERROR] Target at index {target_table_index} is not a valid table tag.")
        return None

    rows = target_table.find_all('tr')
    print(f"[{ts}] [WIKI] Processing {len(rows)} rows in the target table...")

    # --- Iterate through rows of the correct season's table ---
    for row in rows[1:]: # Skip header row
        if not isinstance(row, Tag): continue
        cells = row.find_all(['td', 'th'])

        # A valid row needs at least 3 cells: #overall, #in_season, "Title"
        if len(cells) < 3:
            continue

        try:
            # Column 1 (index 1) reliably contains the episode number for the season.
            episode_in_season_text = cells[1].get_text(strip=True)
            episode_num_from_cell = _extract_first_int(episode_in_season_text)

            # --- Direct comparison for a precise match ---
            if episode_num_from_cell == episode:
                print(f"[{ts}] [WIKI SUCCESS] Matched episode number {episode} in the correct column.")
                
                # Column 2 (index 2) reliably contains the title.
                title_cell = cells[2]
                if not isinstance(title_cell, Tag): continue

                # The title is usually contained within quotes.
                found_text_element = title_cell.find(string=re.compile(r'"([^"]+)"'))
                if found_text_element:
                    title_str = str(found_text_element)
                    cleaned_title = title_str.strip().strip('"')
                    print(f"[{ts}] [WIKI SUCCESS] Extracted title: '{cleaned_title}'")
                    return cleaned_title
                else:
                    # Fallback for edge cases where the title isn't in quotes
                    cleaned_title = title_cell.get_text(strip=True)
                    print(f"[{ts}] [WIKI WARN] Could not find title in quotes, using full cell text: '{cleaned_title}'")
                    return cleaned_title

        except (ValueError, IndexError) as e:
            print(f"[{ts}] [WIKI WARN] Skipping a row due to a parsing error: {e}")
            continue
            
    print(f"[{ts}] [WIKI] All rows in the target table were checked, but no match was found for S{season:02d}E{episode:02d}.")
    return None

async def _parse_embedded_episode_page(soup: BeautifulSoup, season: int, episode: int) -> Optional[str]:
    """
    (Fallback Strategy - CORRECTED)
    Parses a page using proven logic for embedded episode lists, which
    includes heuristics for both multi-season and single-season shows.
    """
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Trying Fallback Strategy: Flexible Row Search")
    
    tables = soup.find_all('table', class_='wikitable')
    for table in tables:
        if not isinstance(table, Tag): continue
        rows = table.find_all('tr')
        for row in rows[1:]: # Skip header row
            if not isinstance(row, Tag): continue
            cells = row.find_all(['td', 'th'])
            if len(cells) < 2: continue

            try:
                cell_texts = [c.get_text(strip=True) for c in cells]
                
                match_found = False
                # Heuristic 1: Strict match (for multi-season shows)
                row_text_for_match = ' '.join(cell_texts[:2])
                if re.search(fr'\b{season}\b.*\b{episode}\b', row_text_for_match):
                    match_found = True
                
                # Heuristic 2: Lenient match (for single-season/limited series)
                elif season == 1 and re.fullmatch(str(episode), cell_texts[0]):
                    match_found = True

                if match_found:
                    # For this layout, the title is reliably in the second column (index 1)
                    title_cell = cells[1] 
                    if not isinstance(title_cell, Tag): continue
                    
                    found_text_element = title_cell.find(string=re.compile(r'"([^"]+)"'))
                    if found_text_element:
                        title_str = str(found_text_element)
                        cleaned_title = title_str.strip().strip('"')
                        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SUCCESS] Found title via Fallback Strategy: '{cleaned_title}'")
                        return cleaned_title
            except (ValueError, IndexError):
                # This can happen on malformed rows, just skip to the next.
                continue
                
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Fallback Strategy failed.")
    return None

async def fetch_episode_title_from_wikipedia(show_title: str, season: int, episode: int) -> Optional[str]:
    """
    (Coordinator)
    Fetches an episode title from Wikipedia by trying a primary strategy for
    dedicated pages, followed by a fallback strategy for embedded lists.
    """
    html_to_scrape = None
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # --- Step 1: Find the correct Wikipedia page (using proven async logic) ---
    try:
        direct_search_query = f"List of {show_title} episodes"
        print(f"[{ts}] [INFO] Attempting to find dedicated episode page: '{direct_search_query}'")
        page = await asyncio.to_thread(
            wikipedia.page, direct_search_query, auto_suggest=False, redirect=True
        )
        html_to_scrape = await asyncio.to_thread(page.html)
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Successfully found dedicated episode page.")
    
    except wikipedia.exceptions.PageError:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] No dedicated page found. Falling back to main show page search.")
        try:
            main_page = await asyncio.to_thread(
                wikipedia.page, show_title, auto_suggest=True, redirect=True
            )
            html_to_scrape = await asyncio.to_thread(main_page.html)
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Successfully found main show page.")
        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] An unexpected error occurred during fallback page search: {e}")
            return None
            
    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] An unexpected error occurred during direct Wikipedia search: {e}")
        return None

    if not html_to_scrape:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] All page search attempts failed.")
        return None

    # --- Step 2: Orchestrate the parsing strategies ---
    soup = BeautifulSoup(html_to_scrape, 'lxml')
    
    # Attempt the primary strategy first.
    title = await _parse_dedicated_episode_page(soup, season, episode)
    
    # If the primary strategy fails, attempt the fallback.
    if not title:
        title = await _parse_embedded_episode_page(soup, season, episode)

    if not title:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Both parsing strategies failed to find S{season:02d}E{episode:02d}.")

    return title

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

async def fetch_metadata_from_magnet(magnet_link: str, progress_message: Message, context: ContextTypes.DEFAULT_TYPE) -> Optional[lt.torrent_info]: #type: ignore
    """
    (Coordinator) Fetches metadata by running the blocking libtorrent code in a
    separate thread, while running a responsive UI timer in the main thread.
    """
    cancel_timer = asyncio.Event()
    timer_task = asyncio.create_task(
        _update_fetch_timer(progress_message, 120, cancel_timer)
    )

    ses = context.bot_data["TORRENT_SESSION"]
    
    # --- ADDED: Log that the thread is being spawned ---
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Spawning worker thread for metadata fetch.")
    bencoded_metadata = await asyncio.to_thread(_blocking_fetch_metadata, ses, magnet_link)
    
    cancel_timer.set()
    await timer_task

    if bencoded_metadata:
        # --- ADDED: Log that the thread returned successfully ---
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Worker thread returned successfully. Reconstructing torrent_info object.")
        ti = lt.torrent_info(bencoded_metadata) #type: ignore
        return ti
    else:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Metadata fetch failed or timed out.")
        await progress_message.edit_text("❌ *Error:* Timed out fetching metadata from the magnet link. It might be inactive or poorly seeded.", parse_mode=ParseMode.MARKDOWN_V2)
        return None
    
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
        timeout_seconds = 120

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
        print(f"[{ts}] [ERROR] An exception occurred in the metadata worker thread: {e}")

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
        await progress_message.edit_text("❌ *Error:* Timed out fetching metadata from the magnet link. It might be inactive or poorly seeded.", parse_mode=ParseMode.MARKDOWN_V2)
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_authorized(update, context):
        return
        
    if not update.message or not update.message.text: return
    chat_id = update.message.chat_id
    text = update.message.text.strip()
    user_message_to_delete = update.message

    if str(chat_id) in context.bot_data.get('active_downloads', {}):
        await update.message.reply_text("ℹ️ You already have a download in progress. Please /cancel it before starting a new one.")
        return

    # --- ADDED: Log that a link has been received and is being processed ---
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Received link from chat_id {chat_id}. Starting analysis.")

    progress_message = await update.message.reply_text("✅ Input received. Analyzing...")

    source_value: Optional[str] = None
    source_type: Optional[str] = None
    ti: Optional[lt.torrent_info] = None #type: ignore

    if text.startswith('magnet:?xt=urn:btih:'):
        source_type = 'magnet'
        source_value = text
        
        ti = await fetch_metadata_from_magnet(text, progress_message, context)
        
        if not ti:
            return # The fetch function already sent the error message

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
            ti = lt.torrent_info(torrent_content) #type: ignore
            info_hash = str(ti.info_hashes().v1) #type: ignore
            torrents_dir = ".torrents"
            os.makedirs(torrents_dir, exist_ok=True)
            
            source_value = os.path.join(torrents_dir, f"{info_hash}.torrent")
            with open(source_value, "wb") as f:
                f.write(torrent_content)
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Persistently saved .torrent file to '{source_value}'")

        except RuntimeError:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Failed to parse .torrent file for chat_id {chat_id}.")
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
    
    await progress_message.edit_text(confirmation_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Sent confirmation prompt to chat_id {chat_id} for torrent '{display_name}'.")

    try:
        await user_message_to_delete.delete()
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Deleted original link message from user {chat_id}.")
    except BadRequest as e:
        if "Message to delete not found" in str(e) or "not enough rights" in str(e):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not delete user's message. Reason: {e}")
        else:
            raise

    if context.user_data is None:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] context.user_data was None for chat_id {chat_id}. Aborting operation.")
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
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    print(f"[{ts}] [INFO] Received button press from user {query.from_user.id}: '{query.data}'")

    if not context.user_data or 'pending_torrent' not in context.user_data:
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
            if "Message is not modified" not in str(e): raise

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
            'message_id': message.message_id,
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
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("plexstatus", plex_status_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    application.run_polling()
# file: download_torrent.py
import libtorrent as lt
import asyncio
from typing import Callable, Awaitable

# Define a type hint for our callback function for clarity
# It's a function that takes a libtorrent status object and returns an awaitable (nothing)
StatusCallback = Callable[[lt.torrent_status], Awaitable[None]] # type:ignore

async def download_with_progress(
    torrent_file_path: str, 
    save_path: str, 
    status_callback: StatusCallback
) -> bool:
    """
    Downloads a torrent from a file and calls a callback function with status updates.

    Args:
        torrent_file_path: The full path to the .torrent file.
        save_path: The directory where the content will be saved.
        status_callback: An async function to call with status updates.

    Returns:
        True if successful, False on error.
    """
    ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})  # type: ignore
    try:
        ti = lt.torrent_info(torrent_file_path)  # type: ignore
    except RuntimeError:
        return False

    handle = ses.add_torrent({'ti': ti, 'save_path': save_path})
    
    # --- MAIN DOWNLOAD PHASE ---
    while not handle.status().is_seeding:
        s = handle.status()
        
        # --- THIS IS THE KEY ---
        # Call the provided callback function with the current status
        await status_callback(s)
        
        # Wait before the next update
        await asyncio.sleep(5) # Update status every 5 seconds

    # Final "100%" update to ensure the user sees completion
    await status_callback(handle.status())
    return True
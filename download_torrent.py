# file: download_torrent.py

import libtorrent as lt
import asyncio
from typing import Callable, Awaitable

StatusCallback = Callable[[lt.torrent_status], Awaitable[None]] #type:ignore

async def download_with_progress(
    source: str, # Can be a magnet link or a .torrent file path
    save_path: str, 
    status_callback: StatusCallback
) -> bool:
    ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})  # type: ignore
    
    # Correctly handle magnet links vs. file paths
    if source.startswith('magnet:'):
        params = lt.parse_magnet_uri(source) # type: ignore
        params.save_path = save_path
        handle = ses.add_torrent(params)
    else:
        try:
            ti = lt.torrent_info(source)  # type: ignore
            handle = ses.add_torrent({'ti': ti, 'save_path': save_path})
        except RuntimeError:
            print(f"[ERROR] Invalid .torrent file provided to download_with_progress: {source}")
            return False

    # Wait for metadata if we have a magnet link
    if not handle.status().has_metadata:
        print("[INFO] Waiting for magnet metadata before starting download...")
        while not handle.status().has_metadata:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                print(f"[INFO] Download task was cancelled while waiting for metadata.")
                # --- FILE DELETION ADDED ---
                # Also remove any potential files on early cancellation
                ses.remove_torrent(handle, lt.session.delete_files) # type: ignore
                raise
    
    while not handle.status().is_seeding:
        try:
            await asyncio.sleep(5) 
            
            s = handle.status()
            await status_callback(s)
        except asyncio.CancelledError:
            print(f"[INFO] Download task for '{handle.status().name}' was cancelled. Deleting partial files.")
            # --- FILE DELETION ADDED ---
            # The delete_files flag tells libtorrent to remove the torrent and its files.
            ses.remove_torrent(handle, lt.session.delete_files) # type: ignore
            raise 
            
    await status_callback(handle.status())
    return True
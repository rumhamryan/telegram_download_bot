# file: download_torrent.py

import libtorrent as lt
import asyncio
from typing import Callable, Awaitable

StatusCallback = Callable[[lt.torrent_status], Awaitable[None]] #type:ignore

async def download_with_progress(
    source: str, 
    save_path: str, 
    status_callback: StatusCallback,
    bot_data: dict  # <-- NEW PARAMETER TYPE
) -> bool:
    ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})  # type: ignore
    
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

    if not handle.status().has_metadata:
        print("[INFO] Waiting for magnet metadata before starting download...")
        while not handle.status().has_metadata:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                # --- THE FIX --- Check the live state from bot_data
                if not bot_data.get('is_shutting_down', False):
                    print(f"[INFO] Download cancelled while waiting for metadata. Deleting partial files.")
                    ses.remove_torrent(handle, lt.session.delete_files) # type: ignore
                else:
                    print(f"[INFO] Download paused while waiting for metadata. Preserving files.")
                    ses.remove_torrent(handle)
                raise
    
    while not handle.status().is_seeding:
        try:
            await asyncio.sleep(5) 
            s = handle.status()
            await status_callback(s)
        except asyncio.CancelledError:
            # --- THE FIX --- Check the live state from bot_data
            if not bot_data.get('is_shutting_down', False):
                print(f"[INFO] Download task for '{handle.status().name}' was cancelled. Deleting partial files.")
                ses.remove_torrent(handle, lt.session.delete_files) # type: ignore
            else:
                print(f"[INFO] Download task for '{handle.status().name}' was paused. Preserving files.")
                ses.remove_torrent(handle)
            raise 
            
    await status_callback(handle.status())
    return True
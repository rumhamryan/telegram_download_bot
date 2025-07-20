# file: download_torrent.py

import libtorrent as lt
import asyncio
from typing import Callable, Awaitable

StatusCallback = Callable[[lt.torrent_status], Awaitable[None]] #type:ignore

async def download_with_progress(
    torrent_file_path: str, 
    save_path: str, 
    status_callback: StatusCallback
) -> bool:
    ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})  # type: ignore
    try:
        ti = lt.torrent_info(torrent_file_path)  # type: ignore
    except RuntimeError:
        return False

    handle = ses.add_torrent({'ti': ti, 'save_path': save_path})
    
    while not handle.status().is_seeding:
        try:
            # THE CHANGE IS HERE: We check for cancellation on each loop.
            # When a task is cancelled, asyncio.sleep() immediately raises this error.
            await asyncio.sleep(5) 
            
            s = handle.status()
            await status_callback(s)
        except asyncio.CancelledError:
            # If cancellation is requested, log it and re-raise the error.
            print(f"[INFO] Download task for '{handle.status().name}' was cancelled.")
            # Important: we must remove the torrent from the session to stop it.
            ses.remove_torrent(handle)
            raise # Propagate the cancellation
            
    await status_callback(handle.status())
    return True
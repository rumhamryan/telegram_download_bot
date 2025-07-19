import libtorrent as lt
import time
import asyncio
import sys
from typing import Optional

# --- CORE LIBRARY FUNCTION ---

async def download_from_file(torrent_file_path: str, save_path: str) -> bool:
    """
    Downloads a torrent from a .torrent file.

    This is the primary library function, designed to be imported into other scripts.

    Args:
        torrent_file_path: The full path to the .torrent file.
        save_path: The directory where the downloaded content should be saved.

    Returns:
        True if the download completed successfully, False otherwise.
    """
    # Set up the libtorrent session
    ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})  # type: ignore
    
    # --- LOAD THE .TORRENT FILE ---
    print(f"Loading torrent file: {torrent_file_path}")
    try:
        ti = lt.torrent_info(torrent_file_path)  # type: ignore
    except RuntimeError as e:
        print(f"\nError loading torrent file: {e}")
        return False

    # Add the torrent to the session
    params = {'ti': ti, 'save_path': save_path}
    handle = ses.add_torrent(params)
    
    print(f"Successfully added torrent. Starting download for: {handle.status().name}")
    
    # --- MAIN DOWNLOAD PHASE ---
    while not handle.status().is_seeding:
        s = handle.status()
        state_str = s.state.name

        # Format the progress bar and the full status line
        progress_bar = f"[{'#' * int(s.progress * 20)}{' ' * (20 - int(s.progress * 20))}]"
        status_line = (
            f"{s.progress * 100:.2f}% {progress_bar} "
            f"| Peers: {s.num_peers} "
            f"| Speed: {s.download_rate / 1024 / 1024:.2f} MB/s "
            f"| State: {state_str}"
        )

        print(f"\r{status_line.ljust(90)}", end="")
        sys.stdout.flush()
        await asyncio.sleep(1)

    print(f"\n\nDownload complete! File saved in '{save_path}' directory.")
    return True

# --- STANDALONE SCRIPT EXECUTION ---

async def main_for_testing():
    """
    A simple "main" function to test the download_from_file function
    when this script is run directly.
    """
    print("--- Running in Standalone Test Mode ---")

    # --- Configuration for testing ---
    # You can change these values to test with different files.
    test_torrent_file = 'ubuntu-24.04.2-live-server-amd64.iso.torrent'
    test_save_path = "."

    try:
        success = await download_from_file(test_torrent_file, test_save_path)
        if success:
            print("\nTest download completed successfully.")
        else:
            print("\nTest download failed.")
    except KeyboardInterrupt:
        print("\n\nDownload interrupted by user.")
    finally:
        print("Exiting test mode.")

# This block runs ONLY when you execute `python download_torrent.py`
if __name__ == "__main__":
    asyncio.run(main_for_testing())
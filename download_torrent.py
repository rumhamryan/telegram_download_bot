import libtorrent as lt
import time
import asyncio
import sys

# --- CONFIGURATION ---
TORRENT_FILE_PATH = 'D70C3B9880EBCF05969462CD180F83F8B5350310.torrent'

# The directory where you want to save the downloaded content.
DOWNLOAD_PATH = "."

async def main():
    """
    Initializes a libtorrent session and downloads a torrent from a .torrent file,
    printing real-time status updates to the console.
    """
    # Set up the libtorrent session. The # type: ignore comments suppress
    # incorrect errors from some IDEs due to incomplete library type hints.
    ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})  # type: ignore
    
    # --- LOAD THE .TORRENT FILE ---
    print(f"Loading torrent file: {TORRENT_FILE_PATH}")
    try:
        # The 'ti' (torrent_info) object holds all metadata from the file.
        # This is the modern way to load a .torrent file.
        ti = lt.torrent_info(TORRENT_FILE_PATH)  # type: ignore
    except RuntimeError as e:
        print(f"\nError loading torrent file: {e}")
        print("Please make sure the file exists and is a valid torrent file.")
        return

    # Add the torrent to the session using the torrent_info object
    params = {'ti': ti, 'save_path': DOWNLOAD_PATH}
    handle = ses.add_torrent(params)
    
    print(f"Successfully added torrent. Starting download for: {handle.status().name}")
    
    # --- MAIN DOWNLOAD PHASE ---
    # Loop until the download is 100% complete and the state is "seeding".
    while not handle.status().is_seeding:
        s = handle.status()
        
        # Get the state's name directly from the state object (e.g., "downloading").
        state_str = s.state.name

        # Format the progress bar and the full status line
        progress_bar = f"[{'#' * int(s.progress * 20)}{' ' * (20 - int(s.progress * 20))}]"
        status_line = (
            f"{s.progress * 100:.2f}% {progress_bar} "
            f"| Peers: {s.num_peers} "
            f"| Speed: {s.download_rate / 1024 / 1024:.2f} MB/s "
            f"| State: {state_str}"
        )

        # Print the status line, overwriting the previous one to keep it on a single line.
        print(f"\r{status_line.ljust(90)}", end="")
        sys.stdout.flush()
        
        # Wait for 1 second before refreshing the status. This is a non-blocking
        # sleep that allows the asyncio event loop to run.
        await asyncio.sleep(1)

    print(f"\n\nDownload complete! File saved in '{DOWNLOAD_PATH}' directory.")
    await asyncio.sleep(2) # Pause briefly so the user can see the final message.


if __name__ == "__main__":
    try:
        # Run the main asynchronous function
        asyncio.run(main())
    except KeyboardInterrupt:
        # Handle the user pressing Ctrl+C cleanly.
        print("\n\nDownload interrupted by user.")
    finally:
        print("Exiting.")
import asyncio
import httpx
import os
import tempfile
from download_torrent import download_from_file

# --- CONFIGURATION ---
# The URL pointing directly to the .torrent file.
TORRENT_URL = 'http://itorrents.org/torrent/D70C3B9880EBCF05969462CD180F83F8B5350310.torrent'

# The final location for the downloaded content.
SAVE_LOCATION = "."

async def main():
    """
    Downloads a .torrent file from a URL, saves it to a temporary
    location, and then passes it to our download_torrent library.
    """
    print(f"--- Starting Download From URL: {TORRENT_URL} ---")

    # Use an async HTTP client to download the .torrent file
    async with httpx.AsyncClient() as client:
        try:
            print("Downloading .torrent file...")
            response = await client.get(TORRENT_URL, follow_redirects=True, timeout=30.0)
            
            # Raise an error if the download failed (e.g., 404 Not Found)
            response.raise_for_status()
            print(".torrent file downloaded successfully.")
            
        except httpx.RequestError as e:
            print(f"\nError: Failed to download the .torrent file from the URL: {e}")
            return

    # A TemporaryDirectory is automatically created and then deleted when the block is exited.
    # This is the perfect place to store the .torrent file temporarily.
    with tempfile.TemporaryDirectory() as temp_dir:
        
        # Get the filename from the end of the URL
        filename = TORRENT_URL.split('/')[-1]
        temp_torrent_path = os.path.join(temp_dir, filename)
        
        print(f"Saving temporary .torrent file to: {temp_torrent_path}")
        
        # Write the downloaded content (in binary mode) to the temporary file
        with open(temp_torrent_path, 'wb') as f:
            f.write(response.content)
            
        print("\n--- Handing off to torrent download library ---")
        
        # Now, call our library function with the path to the temporary .torrent file
        await download_from_file(temp_torrent_path, SAVE_LOCATION)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nOperation interrupted by user.")
    finally:
        print("Exiting.")
import requests
import json
import uuid

# Note: 'getpass' is no longer imported as it is not used.

def get_plex_token():
    """
    An interactive script to retrieve a Plex access token for a user,
    handling 2FA if necessary.
    """
    client_identifier = str(uuid.uuid4())
    PLEX_HEADERS = {
        "X-Plex-Product": "Plex Telegram Bot Setup",
        "X-Plex-Version": "1.0",
        "X-Plex-Platform": "Python",
        "accept": "application/json",
        "X-Plex-Client-Identifier": client_identifier,
    }
    SIGN_IN_URL = "https://plex.tv/users/sign_in.json"


    print("--- Plex Token Retrieval Script ---")
    print(f"This script will guide you through retrieving your Plex access token.")
    print(f"A unique Client Identifier has been generated for this session: {client_identifier}")
    print("---------------------------------------\n")

    # 1. Get user credentials (password is now visible on input)
    username = input("Please enter your Plex username or email: ")
    password = input("Please enter your Plex password: ")
    print("\nCredentials received. Preparing first authentication attempt...")
    input("Press Enter to continue...")
    print("-" * 50)

    # 2. First authentication attempt
    payload = {
        "user": {
            "login": username,
            "password": password
        }
    }

    print("STEP 1: Authenticating with username and password...")
    print(f"POST Request URL: {SIGN_IN_URL}")
    print("Request Headers:")
    print(json.dumps(PLEX_HEADERS, indent=2))

    # Mask password for security in the log output
    masked_payload = payload.copy()
    masked_payload['user']['password'] = '********'
    print("Request Payload:")
    print(json.dumps(masked_payload, indent=2))
    print("\n>>> Sending request...")

    try:
        response = requests.post(SIGN_IN_URL, headers=PLEX_HEADERS, json=payload)
    except requests.exceptions.RequestException as e:
        print(f"\n--- ERROR ---")
        print(f"An error occurred during the request: {e}")
        print("Please check your network connection and try again.")
        return

    print(">>> Response received.")
    input("Press Enter to view the server response...")

    print(f"\nResponse Status Code: {response.status_code}")
    print("Response Body:")
    try:
        response_json = response.json()
        print(json.dumps(response_json, indent=2))
    except json.JSONDecodeError:
        print("Could not decode JSON response. Raw response text:")
        print(response.text)

    print("-" * 50)

    # 3. Handle response - check for success or 2FA requirement
    final_token = None
    if response.status_code == 201: # Success
        print("Authentication successful!")
        final_token = response.json().get("user", {}).get("authentication_token")
    elif response.status_code == 401 and "two-factor" in response.text: # 2FA required
        print("STEP 2: Two-Factor Authentication (2FA) is required.")
        mfa_code = input("Please enter your 6-digit 2FA code: ")

        # Add the 2FA code to the headers for the next request
        mfa_headers = PLEX_HEADERS.copy()
        mfa_headers["X-Plex-Token"] = mfa_code.strip()

        print("\nPreparing second authentication attempt with 2FA code...")
        input("Press Enter to continue...")
        print("-" * 50)

        print(f"POST Request URL: {SIGN_IN_URL}")
        print("Request Headers (with 2FA token):")
        print(json.dumps(mfa_headers, indent=2))
        print("Request Payload:")
        print(json.dumps(masked_payload, indent=2))
        print("\n>>> Sending request...")

        try:
            mfa_response = requests.post(SIGN_IN_URL, headers=mfa_headers, json=payload)
        except requests.exceptions.RequestException as e:
            print(f"\n--- ERROR ---")
            print(f"An error occurred during the request: {e}")
            return

        print(">>> Response received.")
        input("Press Enter to view the server response...")

        print(f"\nResponse Status Code: {mfa_response.status_code}")
        print("Response Body:")
        try:
            print(json.dumps(mfa_response.json(), indent=2))
        except json.JSONDecodeError:
            print("Could not decode JSON response. Raw response text:")
            print(mfa_response.text)

        print("-" * 50)

        if mfa_response.status_code == 201:
            print("2FA Authentication successful!")
            final_token = mfa_response.json().get("user", {}).get("authentication_token")
        else:
            print("--- AUTHENTICATION FAILED ---")
            print("Could not authenticate, even with the 2FA code.")
            print("Please run the script again.")
            final_token = None
    else:
        print("--- AUTHENTICATION FAILED ---")
        print("Received an unexpected server response. Please check your credentials and run the script again.")
        final_token = None

    # 4. Final output
    if final_token:
        print("\n\n" + "="*60)
        print("=== SUCCESS! Your Plex Access Token has been retrieved. ===")
        print("="*60)
        print("\nYour token is:")
        print(f"\n{final_token}\n")
        print("Copy this token and paste it into your bot_config.ini file for the 'plex_token' value.")
        print("="*60)
    else:
        print("\n\nCould not retrieve the Plex token. Please try again.")
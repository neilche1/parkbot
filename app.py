from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
import openai
from datetime import datetime, timedelta
import os
import requests
import re
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

app = Flask(__name__)

# Twilio Credentials (loaded from environment variables)
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN")
MESSAGING_SID = os.getenv("MESSAGING_SID", "MGfeeb018ce3174b051057f0c0176d395d")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "+19853799364")

# Owner's Phone Number for Notifications
OWNER_PHONE = os.getenv("OWNER_PHONE", "+15049090355")

# OpenAI Credentials
OPENAI_KEY = os.getenv("OPENAI_KEY")
openai.api_key = OPENAI_KEY

# Rent Manager API Credentials
RENT_MANAGER_USERNAME = os.getenv("RENT_MANAGER_USERNAME")
RENT_MANAGER_PASSWORD = os.getenv("RENT_MANAGER_PASSWORD")
RENT_MANAGER_LOCATION_ID = os.getenv("RENT_MANAGER_LOCATION_ID", "1")
RENT_MANAGER_AUTH_URL = "https://shadynook.api.rentmanager.com/Authentication/AuthorizeUser"
RENT_MANAGER_BASE_URL = "https://shadynook.api.rentmanager.com/Tenants?embeds=Balance,Addresses&filters=Status,eq,Current"

# Testing Mode (set to True to disable actual SMS sends)
TESTING_MODE = os.getenv("TESTING_MODE", "False").lower() == "true"

# Global variable to store the API token
RENT_MANAGER_API_TOKEN = None

# Authenticate with Rent Manager API to obtain a token
def authenticate_with_rent_manager():
    global RENT_MANAGER_API_TOKEN
    if not RENT_MANAGER_USERNAME or not RENT_MANAGER_PASSWORD:
        print("Rent Manager credentials not found in environment variables.")
        return None

    payload = {
        "Username": RENT_MANAGER_USERNAME,
        "Password": RENT_MANAGER_PASSWORD
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    try:
        print(f"Attempting to authenticate with Rent Manager at {RENT_MANAGER_AUTH_URL}")
        response = requests.post(RENT_MANAGER_AUTH_URL, json=payload, headers=headers)
        print(f"Authentication Response Status: {response.status_code}")
        print(f"Authentication Response Text: {response.text}")
        response.raise_for_status()

        # The API returns the token as a raw string, not JSON
        token = response.text.strip().strip('"')  # Remove any surrounding quotes
        if not token:
            print("Authentication failed: No token received from Rent Manager API.")
            return None

        RENT_MANAGER_API_TOKEN = token
        print(f"Successfully authenticated with Rent Manager. Token: {RENT_MANAGER_API_TOKEN}")
        return RENT_MANAGER_API_TOKEN
    except requests.exceptions.RequestException as e:
        print(f"Error authenticating with Rent Manager: {str(e)}")
        return None

# Parse the Link header to extract the next page URL
def parse_link_header(link_header):
    if not link_header:
        return None
    links = link_header.split(",")
    for link in links:
        if 'rel="next"' in link:
            match = re.search(r'<(.+?)>', link)
            if match:
                return match.group(1)
    return None

# Fetch tenant data from Rent Manager API with pagination, only fetching active tenants (Status="Current")
def fetch_tenants_from_rent_manager():
    global RENT_MANAGER_API_TOKEN
    # Ensure we have a valid token
    if not RENT_MANAGER_API_TOKEN:
        authenticate_with_rent_manager()
    
    if not RENT_MANAGER_API_TOKEN:
        print("Failed to authenticate with Rent Manager. Cannot fetch tenants.")
        return {}

    # Headers for the API request
    headers = {
        "X-RM12Api-ApiToken": RENT_MANAGER_API_TOKEN,
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    # Parameters for the initial request
    params = {
        "LocationID": RENT_MANAGER_LOCATION_ID,
        "PageSize": 1000  # Match the API's default page size
    }

    all_tenants = []
    url = RENT_MANAGER_BASE_URL

    while url:
        try:
            print(f"Fetching tenants from {url} with X-RM12Api-ApiToken header: {headers}, params: {params}")
            response = requests.get(url, headers=headers, params=params)
            print(f"Tenant Fetch Response Status (X-RM12Api-ApiToken, {url}): {response.status_code}")
            print(f"Tenant Fetch Response Text (X-RM12Api-ApiToken, {url}): {response.text[:500]}...")  # Truncate for brevity
            print(f"Tenant Fetch Response Headers (X-RM12Api-ApiToken, {url}): {response.headers}")
            response.raise_for_status()
            tenants_data = response.json()
            all_tenants.extend(tenants_data)

            # Check for the next page
            link_header = response.headers.get("Link")
            url = parse_link_header(link_header)
            params = None  # Clear params for subsequent requests, as the URL already includes them
        except requests.exceptions.RequestException as e:
            print(f"Error fetching tenants from {url}: {str(e)}")
            return {}

    # Process the tenants into the required format (all tenants are current due to API filter)
    tenants = {}
    processed_tenant_ids = set()  # Track processed TenantIDs to avoid duplicates

    for tenant in all_tenants:
        tenant_id = tenant.get("TenantID", "Unknown")
        # Skip if we've already processed this TenantID
        if tenant_id in processed_tenant_ids:
            print(f"Skipping duplicate tenant: TenantID={tenant_id}, Name='{tenant.get('Name', 'Unknown')}'")
            continue

        processed_tenant_ids.add(tenant_id)  # Mark this TenantID as processed
        name = tenant.get("Name", "Unknown")
        # Split name into first and last name
        try:
            if " " in name:
                first_name, last_name = name.split(" ", 1)
            else:
                first_name = name
                last_name = ""
        except Exception as e:
            print(f"Error splitting name '{name}' for TenantID {tenant_id}: {str(e)}")
            first_name = name
            last_name = ""

        # Extract unit number from Addresses field
        addresses = tenant.get("Addresses", [])
        lot = "Unknown"
        if addresses and isinstance(addresses, list) and len(addresses) > 0:
            lot = addresses[0].get("Unit", "Unknown")
        if not lot or lot.strip() == "":
            lot = "Unknown"
            print(f"TenantID {tenant_id} has missing or empty Unit field in Addresses, using 'Unknown'")

        balance = f"${float(tenant.get('Balance', 0.00)):.2f}"
        due_date = str(tenant.get("RentDueDay", "1st"))

        # Use TenantID as part of the key to avoid duplicates
        tenant_key = (tenant_id, first_name, last_name, lot)
        tenants[tenant_key] = {
            "balance": balance,
            "due_date": due_date
        }
        print(f"Stored tenant: TenantID={tenant_id}, Name='{name}', Lot='{lot}', Status=Current")

    print(f"Successfully fetched {len(tenants)} current tenants from Rent Manager (total tenants fetched: {len(all_tenants)})")
    return tenants

# Initialize tenant data at startup
TENANTS = fetch_tenants_from_rent_manager()

# Rent Rule
RENT_DUE_DAY = 1  # Due on the 1st of each month
LATE_FEE_PER_DAY = 5  # $5 per day after the 5th
LATE_FEE_START_DAY = 5  # Late fees start after the 5th

MAINTENANCE_REQUESTS = []
CALL_LOGS = []
PENDING_IDENTIFICATION = {}
CURRENT_CONVERSATIONS = {}  # Maps phone_number to {"tenant_key": (tenant_id, first_name, last_name, unit), "last_message_time": datetime, "pending_end": bool}

def identify_tenant(input_text):
    input_text = input_text.lower().strip()
    possible_matches = []
    
    print(f"Attempting to identify tenant with input: '{input_text}'")
    for tenant_key in TENANTS:
        tenant_id, first_name, last_name, unit = tenant_key
        full_name = f"{first_name} {last_name}".lower()
        first_name_lower = first_name.lower()
        last_name_lower = last_name.lower()
        unit_lower = unit.lower()

        print(f"Checking tenant: TenantID={tenant_id}, FullName='{full_name}', FirstName='{first_name_lower}', LastName='{last_name_lower}', Unit='{unit_lower}'")

        # Check for matches
        # Match by unit number
        if unit_lower == input_text:
            print(f"Match found by unit: {tenant_key}")
            possible_matches.append(tenant_key)
        # Match by full name
        elif full_name == input_text:
            print(f"Match found by full name: {tenant_key}")
            possible_matches.append(tenant_key)
        # Match by first name or last name
        elif first_name_lower == input_text or last_name_lower == input_text:
            print(f"Match found by first or last name: {tenant_key}")
            possible_matches.append(tenant_key)
        # Check for partial matches in name or unit
        elif input_text in full_name or input_text in unit_lower:
            print(f"Match found by partial name or unit: {tenant_key}")
            possible_matches.append(tenant_key)
        else:
            # Check for combined input (e.g., "Clara Lopez 02" or "02 Clara Lopez")
            input_words = input_text.split()
            input_has_unit = any(word == unit_lower for word in input_words)
            input_has_first_name = any(word == first_name_lower for word in input_words)
            input_has_last_name = any(word == last_name_lower for word in input_words)
            if input_has_unit and (input_has_first_name or input_has_last_name):
                print(f"Match found by combined input: {tenant_key}")
                possible_matches.append(tenant_key)
            # Check for partial name match (e.g., "Clara Ines" for "Clara Ines Wood Lopez")
            # Split the input into words and check if all words are present in the full name
            all_words_present = all(word in full_name for word in input_words)
            if all_words_present:
                print(f"Match found by all input words in full name: {tenant_key}")
                possible_matches.append(tenant_key)
            # New check: Match first name and any part of the last name (e.g., "Clara Ines Lopez" matches "Clara Ines Wood Lopez")
            last_name_words = last_name_lower.split()
            input_has_first_name = first_name_lower in input_words
            input_has_any_last_name_part = any(word in last_name_words for word in input_words if word != first_name_lower)
            if input_has_first_name and input_has_any_last_name_part:
                print(f"Match found by first name and part of last name: {tenant_key}")
                possible_matches.append(tenant_key)

    # If there's exactly one match, return it
    if len(possible_matches) == 1:
        print(f"Exactly one match found: {possible_matches[0]}")
        return possible_matches[0], None
    # If there are multiple matches, return the list of matches to prompt for more details
    elif len(possible_matches) > 1:
        print(f"Multiple matches found: {possible_matches}")
        return None, possible_matches  # Return None for tenant_key, and the list of matches
    else:
        print("No matches found")
        return None, None  # No match

def get_ai_response(user_input, tenant_data, is_maintenance_request=False):
    prompt = f"Tenant data: {tenant_data}. Query: {user_input}"
    
    @retry(
        stop=stop_after_attempt(3),  # Retry up to 3 times
        wait=wait_exponential(multiplier=1, min=1, max=10),  # Exponential backoff: 1s, 2s, 4s
        retry=retry_if_exception_type(Exception)  # Retry on any exception
    )
    def call_openai():
        return openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a professional mobile home park manager assisting tenants. Respond in a natural, conversational tone as a human would, without explicitly stating your role (e.g., avoid phrases like 'As a professional mobile home park manager'). Provide concise, actionable responses. For maintenance requests, confirm the issue has been logged, the owner has been notified, and provide a clear next step (e.g., scheduling a repair). For other queries, respond helpfully and professionally."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=500,
            temperature=0.5
        )

    try:
        response = call_openai()
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error in get_ai_response after retries: {str(e)}")
        if is_maintenance_request:
            return "I’m sorry to hear about your issue. I’ve logged your request and notified the owner. The maintenance team will contact you soon to schedule a repair."
        return "I’m sorry, I couldn’t process your request at this time. Please try again later or contact the park manager directly."

def send_sms(to_number, message):
    # No redirection; send directly to the specified number
    recipient = to_number

    print(f"Preparing to send SMS to {recipient}: {message}")
    print(f"TWILIO_SID: {TWILIO_SID}")
    print(f"TWILIO_TOKEN: {TWILIO_TOKEN}")
    
    if TESTING_MODE:
        print(f"TESTING_MODE enabled: SMS not sent. Would have sent to {recipient}: {message}")
        return
    
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        print("Twilio client initialized")
        if MESSAGING_SID:
            response = client.messages.create(
                messaging_service_sid=MESSAGING_SID,
                body=message,
                to=recipient
            )
        else:
            response = client.messages.create(
                from_=TWILIO_NUMBER,
                body=message,
                to=recipient
            )
        print(f"SMS sent successfully: {response.sid}")
    except Exception as e:
        print(f"Error sending SMS: {str(e)}")
        raise

# Homepage route to avoid 404 error
@app.route("/", methods=["GET"])
def home():
    return "ParkBot is running! Use Twilio to interact via SMS or voice."

# Keep-alive endpoint (optional, since you're on a paid plan)
@app.route("/keep_alive", methods=["GET"])
def keep_alive():
    return "App is awake!"

# Endpoint to manually refresh tenant data
@app.route("/refresh_tenants", methods=["GET"])
def refresh_tenants():
    global TENANTS
    TENANTS = fetch_tenants_from_rent_manager()
    return "Tenants refreshed successfully!"

@app.route("/sms", methods=["POST"])
def sms_reply():
    print("Received SMS request")
    from_number = request.values.get("From")
    message = request.values.get("Body").strip()
    print(f"From: {from_number}, Message: {message}")

    # Check for expired conversations or pending end prompts
    current_time = datetime.now()
    if from_number in CURRENT_CONVERSATIONS:
        last_message_time = CURRENT_CONVERSATIONS[from_number]["last_message_time"]
        time_delta = (current_time - last_message_time).total_seconds() / 60.0  # Time in minutes

        # Check for 5-minute inactivity timeout
        if time_delta >= 5 and not CURRENT_CONVERSATIONS[from_number].get("pending_end", False):
            CURRENT_CONVERSATIONS[from_number]["pending_end"] = True
            CURRENT_CONVERSATIONS[from_number]["pending_end_time"] = current_time
            send_sms(from_number, "It’s been a while since your last message. Is there anything else I can assist you with? If not, I’ll close this conversation.")
            return "OK"

        # Check for 3-minute timeout after end prompt
        if CURRENT_CONVERSATIONS[from_number].get("pending_end", False):
            pending_end_time = CURRENT_CONVERSATIONS[from_number]["pending_end_time"]
            end_delta = (current_time - pending_end_time).total_seconds() / 60.0
            if end_delta >= 3:
                del CURRENT_CONVERSATIONS[from_number]
                send_sms(from_number, "No response received. I’ve closed this conversation. Feel free to reach out if you need further assistance.")
                return "OK"

    # Update last message time for active conversations
    if from_number in CURRENT_CONVERSATIONS:
        CURRENT_CONVERSATIONS[from_number]["last_message_time"] = current_time
        # Reset pending end if tenant responds
        if CURRENT_CONVERSATIONS[from_number].get("pending_end", False):
            CURRENT_CONVERSATIONS[from_number]["pending_end"] = False
            del CURRENT_CONVERSATIONS[from_number]["pending_end_time"]

    # Always prompt for identification if not in an active conversation
    if from_number not in CURRENT_CONVERSATIONS:
        if from_number in PENDING_IDENTIFICATION:
            # Try to identify the tenant based on the input
            tenant_key, possible_matches = identify_tenant(message)
            if tenant_key:
                # Successfully identified
                # Retrieve the pending message before deleting the entry
                pending_message = PENDING_IDENTIFICATION[from_number].get("pending_message")
                # Now safe to delete the entry
                del PENDING_IDENTIFICATION[from_number]
                CURRENT_CONVERSATIONS[from_number] = {
                    "tenant_key": tenant_key,
                    "last_message_time": current_time,
                    "pending_end": False
                }
                # Send the personalized greeting first
                send_sms(from_number, f"Hello {tenant_key[1]}! I’ve identified you. How can I assist you today?")
                # Process the pending message only if it's substantive
                if pending_message:
                    message_lower = pending_message.lower()
                    # Skip generic greetings that don't require a response
                    generic_greetings = ["hello", "hi", "hey", "greetings", "good morning", "good afternoon", "good evening"]
                    if message_lower in generic_greetings:
                        print(f"Skipping generic pending message: '{pending_message}'")
                    else:
                        tenant_data = TENANTS[tenant_key]
                        if "maintenance" in message_lower or "fix" in message_lower or "broken" in message_lower or "leak" in message_lower or "leaking" in message_lower or "flood" in message_lower or "damage" in message_lower or "repair" in message_lower or "clog" in message_lower or "power" in message_lower:
                            # Log the maintenance request
                            tenant_name = f"{tenant_key[1]} {tenant_key[2]}"
                            tenant_lot = tenant_key[3]
                            MAINTENANCE_REQUESTS.append({
                                "tenant_phone": from_number,
                                "tenant_name": tenant_name,
                                "tenant_lot": tenant_lot,
                                "issue": pending_message
                            })
                            # Notify the owner (will be sent to OWNER_PHONE)
                            owner_message = f"Maintenance request from {tenant_name}, Unit {tenant_lot}: {pending_message}"
                            send_sms(OWNER_PHONE, owner_message)
                            # Generate an AI response for the tenant
                            reply = get_ai_response(pending_message, tenant_data, is_maintenance_request=True)
                        else:
                            reply = get_ai_response(pending_message, tenant_data)
                        # Do not append "Is there anything else I can assist you with?" here
                        send_sms(from_number, reply)
                return "OK"
            else:
                # Handle ambiguous or no matches
                if possible_matches:
                    # Multiple matches found; prompt for more specific information
                    tenant_names = [f"{match[1]} {match[2]}" for match in possible_matches]
                    tenant_list = ", ".join(tenant_names)
                    send_sms(from_number, f"I found multiple tenants matching '{message}': {tenant_list}. Please provide more details, such as your full name or unit number, to identify yourself.")
                else:
                    # No matches found
                    send_sms(from_number, "I couldn’t identify you with the information provided. Please try again with your first name, last name, or unit number (e.g., John Doe, Unit 5).")
                return "OK"

        PENDING_IDENTIFICATION[from_number] = {"state": "awaiting_identification", "pending_message": message}
        send_sms(from_number, "Please identify yourself with your first name, last name, or unit number (e.g., John Doe, Unit 5).")
        return "OK"

    # Tenant is in an active conversation
    message_lower = message.lower()
    tenant_key = CURRENT_CONVERSATIONS[from_number]["tenant_key"]
    tenant_data = TENANTS[tenant_key]

    # Check if the tenant wants to end the conversation
    end_conversation_phrases = ["no", "nope", "that's all", "that's it", "goodbye", "bye", "done"]
    if message_lower in end_conversation_phrases:
        del CURRENT_CONVERSATIONS[from_number]
        send_sms(from_number, "Goodbye! Feel free to reach out if you need further assistance.")
        return "OK"

    # Handle the tenant's query
    if "maintenance" in message_lower or "fix" in message_lower or "broken" in message_lower or "leak" in message_lower or "leaking" in message_lower or "flood" in message_lower or "damage" in message_lower or "repair" in message_lower or "clog" in message_lower or "power" in message_lower:
        # Log the maintenance request
        tenant_name = f"{tenant_key[1]} {tenant_key[2]}"
        tenant_lot = tenant_key[3]
        MAINTENANCE_REQUESTS.append({
            "tenant_phone": from_number,
            "tenant_name": tenant_name,
            "tenant_lot": tenant_lot,
            "issue": message
        })
        # Notify the owner (will be sent to OWNER_PHONE)
        owner_message = f"Maintenance request from {tenant_name}, Unit {tenant_lot}: {message}"
        send_sms(OWNER_PHONE, owner_message)
        # Generate an AI response for the tenant
        reply = get_ai_response(message, tenant_data, is_maintenance_request=True)
    else:
        reply = get_ai_response(message, tenant_data)

    # Now that the conversation has started, append the follow-up question
    send_sms(from_number, reply + " Is there anything else I can assist you with?")
    return "OK"

@app.route("/voice", methods=["POST"])
def voice_reply():
    print("Received voice request")
    from_number = request.values.get("From")
    CALL_LOGS.append({
        "phone_number": from_number,
        "call_type": "incoming",
        "timestamp": datetime.now().isoformat(),
        "notes": "Incoming call handled by ParkBot"
    })
    resp = VoiceResponse()
    resp.say("Hello, this is ParkBot. Please text me your first name, last name, or unit number to identify yourself.")
    return str(resp)

def send_rent_reminders():
    print("Starting send_rent_reminders")
    print("Rent reminders are disabled because tenant phone numbers are not available.")
    print("Finished send_rent_reminders")
    # Since we don't have tenant phone numbers, we can't send reminders
    # This will be updated manually later
    return

@app.route("/send_rent_reminders", methods=["GET"])
def trigger_rent_reminders():
    print("Triggering rent reminders")
    try:
        send_rent_reminders()
        print("Rent reminders processed (no messages sent due to missing phone numbers)")
        return "Rent reminders processed (no messages sent due to missing phone numbers)!"
    except Exception as e:
        print(f"Error in send_rent_reminders: {str(e)}")
        return f"Error: {str(e)}", 500

if __name__ == "__main__":
    app.run(debug=True)
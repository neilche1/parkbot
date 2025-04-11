from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
import requests
import datetime
import os
import json
import re
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from fuzzywuzzy import fuzz

app = Flask(__name__)

# Twilio Credentials (loaded from environment variables)
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN")
MESSAGING_SID = os.getenv("MESSAGING_SID", "MGfeeb018ce3174b051057f0c0176d395d")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "+19853799364")

# Owner's Phone Number for Notifications
OWNER_PHONE = os.getenv("OWNER_PHONE", "+15049090355")

# xAI API Credentials
XAI_API_KEY = os.getenv("XAI_API_KEY", "xai-MRHpt2WdHOo1S1DSpLsdzXEDBoOpzBagOAAh4BB14NnEcVoGkzsasVgAUfC3RN1LLgkj7CpVBda4v0oS")

# Rent Manager API Credentials
RENT_MANAGER_USERNAME = os.getenv("RENT_MANAGER_USERNAME")
RENT_MANAGER_PASSWORD = os.getenv("RENT_MANAGER_PASSWORD")
RENT_MANAGER_LOCATION_ID = os.getenv("RENT_MANAGER_LOCATION_ID", "1")
RENT_MANAGER_AUTH_URL = "https://shadynook.api.rentmanager.com/Authentication/AuthorizeUser"
RENT_MANAGER_BASE_URL = "https://shadynook.api.rentmanager.com/Tenants?embeds=Property,Property.Addresses,Addresses,Leases.Unit.UnitType,Balance&filters=Status,eq,Current"

# Testing Mode (set to True to disable actual SMS sends)
TESTING_MODE = os.getenv("TESTING_MODE", "False").lower() == "true"

# Park Office Contact Information
PARK_OFFICE_PHONE = "(504) 313-0024"
PARK_OFFICE_HOURS = "Monday to Friday, 9 AM to 5 PM"

# Global variable to store the API token
RENT_MANAGER_API_TOKEN = None

# Global dictionaries for conversation state
MAINTENANCE_REQUESTS = []
CALL_LOGS = []
PENDING_IDENTIFICATION = {}
CURRENT_CONVERSATIONS = {}  # Maps phone_number to {"tenant_key": (tenant_id, first_name, last_name, unit), "last_message_time": datetime, "pending_end": bool, "pending_identification": bool}

# File path for storing CURRENT_CONVERSATIONS
CONVERSATIONS_FILE = "current_conversations.json"

# Load CURRENT_CONVERSATIONS from file at startup
def load_conversations():
    global CURRENT_CONVERSATIONS
    try:
        if os.path.exists(CONVERSATIONS_FILE):
            with open(CONVERSATIONS_FILE, "r") as f:
                data = json.load(f)
                # Convert last_message_time and pending_end_time back to datetime objects
                for phone_number, conversation in data.items():
                    conversation["last_message_time"] = datetime.datetime.fromisoformat(conversation["last_message_time"])
                    if "pending_end_time" in conversation and conversation["pending_end_time"]:
                        conversation["pending_end_time"] = datetime.datetime.fromisoformat(conversation["pending_end_time"])
                    # Convert tenant_key from list to tuple if necessary
                    if "tenant_key" in conversation and conversation["tenant_key"] is not None:
                        if isinstance(conversation["tenant_key"], list):
                            conversation["tenant_key"] = tuple(conversation["tenant_key"])
                CURRENT_CONVERSATIONS = data
                print("Loaded CURRENT_CONVERSATIONS from file")
        else:
            CURRENT_CONVERSATIONS = {}
            print("No existing CURRENT_CONVERSATIONS file found. Starting with an empty dictionary.")
    except Exception as e:
        print(f"Error loading CURRENT_CONVERSATIONS from file: {str(e)}")
        CURRENT_CONVERSATIONS = {}

# Save CURRENT_CONVERSATIONS to file
def save_conversations():
    try:
        # Convert datetime objects to strings for JSON serialization
        data = {}
        for phone_number, conversation in CURRENT_CONVERSATIONS.items():
            data[phone_number] = {
                "tenant_key": conversation["tenant_key"],
                "last_message_time": conversation["last_message_time"].isoformat(),
                "pending_end": conversation["pending_end"],
                "pending_identification": conversation.get("pending_identification", False)
            }
            if "pending_end_time" in conversation and conversation["pending_end_time"]:
                data[phone_number]["pending_end_time"] = conversation["pending_end_time"].isoformat()
        with open(CONVERSATIONS_FILE, "w") as f:
            json.dump(data, f)
        print("Saved CURRENT_CONVERSATIONS to file")
    except Exception as e:
        print(f"Error saving CURRENT_CONVERSATIONS to file: {str(e)}")

# Load conversations at startup
load_conversations()

# Reset CURRENT_CONVERSATIONS on startup
def reset_conversations_on_startup():
    global CURRENT_CONVERSATIONS
    CURRENT_CONVERSATIONS = {}
    save_conversations()
    print("Reset CURRENT_CONVERSATIONS on startup")

# Call the reset function when the app starts
reset_conversations_on_startup()

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

    # Parameters for the initial request without Transactions embed
    params = {
        "LocationID": RENT_MANAGER_LOCATION_ID,
        "PageSize": 1000  # Match the API's default page size
    }

    all_tenants = []
    url = RENT_MANAGER_BASE_URL

    while url:
        try:
            print(f"Fetching tenants from {url}")
            response = requests.get(url, headers=headers, params=params)
            print(f"Tenant Fetch Response Status: {response.status_code}")
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
        try:
            tenant_id = tenant.get("TenantID", "Unknown")
            # Skip if we've already processed this TenantID
            if tenant_id in processed_tenant_ids:
                continue

            processed_tenant_ids.add(tenant_id)  # Mark this TenantID as processed
            name = tenant.get("Name", "Unknown")
            # Split name into first and last name
            if " " in name:
                first_name, last_name = name.split(" ", 1)
            else:
                first_name = name
                last_name = ""

            # Extract unit/lot from Leases.Unit.Name
            leases = tenant.get("Leases", [])
            lot = "Unknown"
            if leases and isinstance(leases, list) and len(leases) > 0:
                lot = leases[0].get("Unit", {}).get("Name", "Unknown")

            balance = f"${float(tenant.get('Balance', 0.00)):.2f}"
            due_date = str(tenant.get("RentDueDay", "1st"))
            move_in_date = tenant.get("PostingStartDate", "Unknown")
            # Extract tenant's address details
            addresses = tenant.get("Addresses", [])
            address_details = {
                "street": addresses[0].get("Street", "Unknown") if addresses else "Unknown",
                "city": addresses[0].get("City", "Unknown") if addresses else "Unknown",
                "state": addresses[0].get("State", "Unknown") if addresses else "Unknown",
                "postal_code": addresses[0].get("PostalCode", "Unknown") if addresses else "Unknown"
            }

            # Extract park information from the Property object
            property_info = tenant.get("Property", {})
            park_addresses = property_info.get("Addresses", [])
            primary_address = next((addr for addr in park_addresses if addr.get("IsPrimary", False)), {
                "street": "Unknown",
                "city": "Unknown",
                "state": "Unknown",
                "postal_code": "Unknown"
            })
            park = {
                "name": property_info.get("Name", "Unknown Park"),
                "address": {
                    "street": primary_address.get("Street", "Unknown"),
                    "city": primary_address.get("City", "Unknown"),
                    "state": primary_address.get("State", "Unknown"),
                    "postal_code": primary_address.get("PostalCode", "Unknown")
                },
                "payment_methods": "Checks and money orders only (no cash)",  # Default until API provides this
                "payment_procedure": "Drop off at the park’s dropbox",  # Default until API provides this
                "payee": property_info.get("BillingName1", "Unknown Park")
            }

            # Use TenantID as part of the key to avoid duplicates
            tenant_key = (tenant_id, first_name, last_name, lot)
            tenants[tenant_key] = {
                "balance": balance,
                "due_date": due_date,
                "move_in_date": move_in_date,
                "address": address_details,
                "park": park,
                "transactions": None,  # Transactions will be fetched on-demand
                "last_payment_date": None  # Will be set when transactions are fetched
            }
        except Exception as e:
            print(f"Error processing tenant TenantID={tenant_id}: {str(e)}")
            continue

    print(f"Successfully fetched {len(tenants)} current tenants from Rent Manager (total tenants fetched: {len(all_tenants)})")
    return tenants

# Fetch transaction data for a specific tenant on-demand
def fetch_tenant_transactions(tenant_id):
    global RENT_MANAGER_API_TOKEN
    if not RENT_MANAGER_API_TOKEN:
        authenticate_with_rent_manager()
    
    if not RENT_MANAGER_API_TOKEN:
        print("Failed to authenticate with Rent Manager. Cannot fetch transactions.")
        return None, None

    # Headers for the API request
    headers = {
        "X-RM12Api-ApiToken": RENT_MANAGER_API_TOKEN,
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    # Parameters for the API request
    params = {
        "LocationID": RENT_MANAGER_LOCATION_ID
    }

    # Construct the URL for the specific tenant with Transactions embed
    url = f"https://shadynook.api.rentmanager.com/Tenants/{tenant_id}?embeds=Transactions"

    for attempt in range(2):  # Try twice: once with the current token, and once after re-authenticating if needed
        try:
            print(f"Fetching transactions for TenantID={tenant_id} from {url}")
            response = requests.get(url, headers=headers, params=params)
            print(f"Transaction Fetch Response Status (TenantID={tenant_id}): {response.status_code}")
            print(f"Transaction Fetch Response Text (TenantID={tenant_id}): {response.text[:500]}...")  # Truncate for brevity
            response.raise_for_status()
            tenant_data = response.json()

            # Extract all transactions (no limit)
            transactions = tenant_data.get("Transactions", [])
            # Sort transactions by TransactionDate in ascending order for statement generation
            transactions.sort(key=lambda x: x.get("TransactionDate", ""))
            # Find the most recent payment
            payment_transactions = [t for t in transactions if t.get("TransactionType") == "Payment"]
            last_payment_date = "Unknown"
            if payment_transactions:
                # Sort in descending order to get the most recent payment
                payment_transactions.sort(key=lambda x: x.get("TransactionDate", ""), reverse=True)
                last_payment_date = payment_transactions[0].get("TransactionDate", "Unknown")

            print(f"Fetched {len(transactions)} transactions for TenantID={tenant_id}")
            return transactions, last_payment_date

        except requests.exceptions.HTTPError as e:
            if response.status_code == 401:  # Unauthorized, token might be invalid
                print(f"Received 401 Unauthorized error: {str(e)}. Attempting to re-authenticate...")
                # Clear the current token and re-authenticate
                RENT_MANAGER_API_TOKEN = None
                authenticate_with_rent_manager()
                if not RENT_MANAGER_API_TOKEN:
                    print("Failed to re-authenticate with Rent Manager after 401 error.")
                    return None, None
                # Update headers with the new token
                headers["X-RM12Api-ApiToken"] = RENT_MANAGER_API_TOKEN
                continue  # Retry the request with the new token
            else:
                print(f"Error fetching transactions for TenantID={tenant_id}: {str(e)}")
                return None, None
        except requests.exceptions.RequestException as e:
            print(f"Error fetching transactions for TenantID={tenant_id}: {str(e)}")
            return None, None

    print("Failed to fetch transactions after re-authentication attempt.")
    return None, None

# Initialize tenant data synchronously at startup
print("Fetching tenant data at startup...")
TENANTS = fetch_tenants_from_rent_manager()
print("Tenant data fetch completed.")

# Rent Rule
RENT_DUE_DAY = 1  # Due on the 1st of each month
LATE_FEE_PER_DAY = 5  # $5 per day after the 5th
LATE_FEE_START_DAY = 5  # Late fees start after the 5th

def identify_tenant(input_text):
    # Normalize the input by converting to lowercase, removing extra spaces, and replacing multiple spaces with a single space
    input_text = " ".join(input_text.split()).lower().strip()
    input_words = input_text.split()
    # Remove spaces for unit comparison
    input_text_normalized = input_text.replace(" ", "")
    
    possible_matches = []
    park_filtered_matches = []
    
    print(f"Attempting to identify tenant with input: '{input_text}'")
    
    # First, try to identify a park name or city in the input
    possible_park_name = " ".join(input_words)
    park_name_in_input = None
    city_in_input = None
    for tenant_key in TENANTS:
        park_name = TENANTS[tenant_key]["park"]["name"].lower()
        tenant_city = TENANTS[tenant_key]["address"]["city"].lower()
        if park_name in possible_park_name:
            park_name_in_input = park_name
            break
        if tenant_city and tenant_city in possible_park_name:
            city_in_input = tenant_city
    
    # Check for name matches first (prioritize name over unit)
    for tenant_key in TENANTS:
        tenant_id, first_name, last_name, unit = tenant_key
        # Normalize the tenant's full name: lowercase, remove extra spaces
        full_name = " ".join(f"{first_name} {last_name}".split()).lower().strip()
        first_name_lower = " ".join(first_name.split()).lower().strip()
        last_name_lower = " ".join(last_name.split()).lower().strip()
        unit_lower = unit.lower()
        unit_normalized = unit_lower.replace(" ", "")
        tenant_park_name = TENANTS[tenant_key]["park"]["name"].lower()
        tenant_city = TENANTS[tenant_key]["address"]["city"].lower()

        print(f"Checking tenant: TenantID={tenant_id}, FullName='{full_name}', FirstName='{first_name_lower}', LastName='{last_name_lower}', Unit='{unit_lower}', Park='{tenant_park_name}', City='{tenant_city}'")

        # Exact full name match
        if full_name == input_text and tenant_key not in possible_matches:
            print(f"Match found by full name: {tenant_key}")
            possible_matches.append(tenant_key)
            continue  # Found an exact match, no need to check other conditions for this tenant

        # Exact first or last name match
        if (first_name_lower == input_text or last_name_lower == input_text) and tenant_key not in possible_matches:
            print(f"Match found by first or last name: {tenant_key}")
            possible_matches.append(tenant_key)
            continue

        # Input is a substring of the full name
        if input_text in full_name and tenant_key not in possible_matches:
            print(f"Match found by partial name: {tenant_key}")
            possible_matches.append(tenant_key)
            continue

        # Check for all input words in the full name
        all_words_present = all(word in full_name for word in input_words)
        if all_words_present and tenant_key not in possible_matches:
            print(f"Match found by all input words in full name: {tenant_key}")
            possible_matches.append(tenant_key)
            continue

        # Match first name and any part of the last name
        last_name_words = last_name_lower.split()
        input_has_first_name = first_name_lower in input_words
        input_has_any_last_name_part = any(word in last_name_words for word in input_words if word != first_name_lower)
        if input_has_first_name and input_has_any_last_name_part and tenant_key not in possible_matches:
            print(f"Match found by first name and part of last name: {tenant_key}")
            possible_matches.append(tenant_key)
            continue

    # If no name matches, check for unit matches
    if not possible_matches:
        for tenant_key in TENANTS:
            tenant_id, first_name, last_name, unit = tenant_key
            unit_lower = unit.lower()
            unit_normalized = unit_lower.replace(" ", "")
            tenant_park_name = TENANTS[tenant_key]["park"]["name"].lower()
            tenant_city = TENANTS[tenant_key]["address"]["city"].lower()

            # Exact unit match
            if unit_normalized == input_text_normalized and tenant_key not in possible_matches:
                print(f"Match found by unit (exact normalized match): {tenant_key}")
                possible_matches.append(tenant_key)
                continue

            # Partial unit match (input starts with unit)
            if (input_text_normalized.startswith(unit_normalized) and 
                  (len(input_text_normalized) == len(unit_normalized) or 
                   input_text_normalized[len(unit_normalized)] in [' ', '']) and 
                  tenant_key not in possible_matches):
                print(f"Match found by unit (partial normalized match - input starts with unit): {tenant_key}")
                possible_matches.append(tenant_key)
                continue

            # Fuzzy unit match
            unit_similarity = fuzz.ratio(unit_normalized, input_text_normalized)
            if unit_similarity >= 90 and tenant_key not in possible_matches:
                print(f"Match found by unit (fuzzy match, similarity {unit_similarity}%): {tenant_key}")
                possible_matches.append(tenant_key)
                continue

            # Check for combined input (e.g., "Clara Lopez 02" or "02 Clara Lopez")
            input_has_unit = any(unit_normalized == word.replace(" ", "") for word in input_words)
            full_name = " ".join(f"{first_name} {last_name}".split()).lower().strip()
            first_name_lower = " ".join(first_name.split()).lower().strip()
            last_name_lower = " ".join(last_name.split()).lower().strip()
            input_has_first_name = any(word == first_name_lower for word in input_words)
            input_has_last_name = any(word == last_name_lower for word in input_words)
            if input_has_unit and (input_has_first_name or input_has_last_name) and tenant_key not in possible_matches:
                print(f"Match found by combined input: {tenant_key}")
                possible_matches.append(tenant_key)
                continue

    # If a park name or city was identified in the input, filter matches
    if park_name_in_input:
        print(f"Filtering matches by park name: '{park_name_in_input}'")
        for match in possible_matches:
            tenant_park_name = TENANTS[match]["park"]["name"].lower()
            if park_name_in_input == tenant_park_name:
                park_filtered_matches.append(match)
        possible_matches = park_filtered_matches
        print(f"After park filter, possible matches: {possible_matches}")
    elif city_in_input:
        print(f"Filtering matches by city: '{city_in_input}'")
        for match in possible_matches:
            tenant_city = TENANTS[match]["address"]["city"].lower()
            if city_in_input == tenant_city:
                park_filtered_matches.append(match)
        possible_matches = park_filtered_matches
        print(f"After city filter, possible matches: {possible_matches}")

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
    # Include the tenant's park details and transaction data in the prompt
    park_details = tenant_data.get("park", {
        "name": "Unknown Park",
        "address": {"street": "Unknown", "city": "Unknown", "state": "Unknown", "postal_code": "Unknown"},
        "payment_methods": "Checks and money orders only (no cash)",
        "payment_procedure": "Drop off at the park’s dropbox",
        "payee": "Unknown Park"
    })
    # Include all transactions, sorted by date
    transactions = tenant_data.get("transactions", [])
    if transactions:
        transactions.sort(key=lambda x: x.get("TransactionDate", ""), reverse=True)
    # Calculate the monthly rent charge based on transaction history
    monthly_rent_charge = None
    for transaction in transactions:
        if "rent" in transaction.get("Comment", "").lower() and transaction.get("TransactionType") != "Payment":
            monthly_rent_charge = float(transaction.get("Amount", 0.00))
            break
    prompt = (
        f"Tenant data: {tenant_data}. "
        f"Tenant is from park: {park_details}. "
        f"All transactions: {transactions}. "
        f"Monthly rent charge (if available): ${monthly_rent_charge:.2f} if known, otherwise unknown. "
        f"Query: {user_input}"
    )
    
    @retry(
        stop=stop_after_attempt(3),  # Retry up to 3 times
        wait=wait_exponential(multiplier=1, min=1, max=10),  # Exponential backoff: 1s, 2s, 4s
        retry=retry_if_exception_type(Exception)  # Retry on any exception
    )
    def call_xai():
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {XAI_API_KEY}"
        }
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a professional mobile home park manager assisting tenants across multiple mobile home parks. "
                        "Respond in a natural, conversational tone as a human would, without explicitly stating your role. "
                        "Provide concise, actionable responses tailored to the tenant’s specific park, which will be provided in the prompt as 'Tenant is from park: {park_details}'. "
                        "Use the tenant's full transaction history, provided as 'All transactions: {transactions}', to inform your responses when relevant, such as for payment-related queries. "
                        "For financial queries (e.g., balance, rent charge, payment history), use the tenant's balance, due date, monthly rent charge, and transaction history to provide accurate and context-aware answers. "
                        "For example, if asked 'What did I pay last month?', calculate the total payments made in the previous month from the transaction history and respond accordingly. "
                        "If asked about the rent charge, use the 'Monthly rent charge' provided in the prompt if available, or infer it from the transaction history if possible. "
                        "For maintenance requests, confirm the issue has been logged, the owner has been notified, and provide a clear next step (e.g., scheduling a repair). "
                        "For other queries, respond helpfully and professionally using the park-specific details provided (e.g., payment_methods, payment_procedure, payee). "
                        "Do not make up information (e.g., addresses, procedures, or any details not explicitly provided). "
                        "If you lack specific details to answer a query, respond with: 'I’m sorry, I don’t have that information. Please contact the park office at (504) 313-0024, available Monday to Friday, 9 AM to 5 PM, for more details.'"
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "model": "grok-3-beta",  # Updated to the smartest and most powerful model
            "stream": False,
            "temperature": 0.5,
            "max_tokens": 500
        }
        response = requests.post("https://api.x.ai/v1/chat/completion", headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    try:
        response = call_xai()
        # Extract the response content from the xAI API response
        return response["choices"][0]["message"]["content"].strip()
    except requests.exceptions.HTTPError as e:
        # Log the HTTP error details
        print(f"HTTPError in get_ai_response: Status Code: {e.response.status_code}, Response Text: {e.response.text}")
        raise  # Re-raise the exception to trigger the retry logic
    except Exception as e:
        print(f"Error in get_ai_response after retries: {str(e)}")
        # If the query is financial, provide a basic fallback response using available data
        if any(keyword in user_input.lower() for keyword in ["balance", "pay", "due", "payment history", "last payment", "recent transactions", "last month", "rent charge"]):
            return f"I couldn’t process your request fully, but I can tell you that your current balance is {tenant_data['balance']}, due on the {tenant_data['due_date']} of each month. For more details, please try again later or contact the park office at {PARK_OFFICE_PHONE}, available {PARK_OFFICE_HOURS}."
        elif is_maintenance_request:
            return "I’m sorry to hear about your issue. I’ve logged your request and notified the owner. The maintenance team will contact you soon to schedule a repair."
        else:
            return f"I’m sorry, I couldn’t process your request at this time. Please try again later or contact the park office at {PARK_OFFICE_PHONE}, available {PARK_OFFICE_HOURS}."

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

# Endpoint to check for inactive conversations (to be called by a cron job)
@app.route("/check_inactive_conversations", methods=["GET"])
def check_inactive_conversations():
    # Load the latest conversations from file
    load_conversations()

    current_time = datetime.datetime.now()
    conversations_to_close = []
    
    print(f"Checking for inactive conversations at {current_time}")
    for phone_number, conversation in list(CURRENT_CONVERSATIONS.items()):
        # Skip if the conversation is not active (e.g., no last_message_time)
        if "last_message_time" not in conversation:
            print(f"Skipping conversation for {phone_number}: No last_message_time")
            continue
        last_message_time = conversation["last_message_time"]
        time_delta = (current_time - last_message_time).total_seconds() / 60.0  # Time in minutes
        print(f"Conversation for {phone_number}: Last message at {last_message_time}, Time delta: {time_delta:.2f} minutes")

        # Check for 3-minute inactivity timeout
        if time_delta >= 3 and not conversation.get("pending_end", False):
            conversation["pending_end"] = True
            conversation["pending_end_time"] = current_time
            send_sms(phone_number, "It’s been a while since your last message. Is there anything else I can assist you with? If not, I’ll close this conversation.")
            print(f"Inactivity timeout triggered for {phone_number} by cron job")
            save_conversations()  # Save after modification

        # Check for 1-minute timeout after end prompt
        if conversation.get("pending_end", False):
            pending_end_time = conversation["pending_end_time"]
            end_delta = (current_time - pending_end_time).total_seconds() / 60.0
            print(f"Conversation for {phone_number}: Pending end at {pending_end_time}, End delta: {end_delta:.2f} minutes")
            if end_delta >= 1:
                conversations_to_close.append(phone_number)

    # Close conversations that have timed out
    for phone_number in conversations_to_close:
        if phone_number in CURRENT_CONVERSATIONS:
            del CURRENT_CONVERSATIONS[phone_number]
            if phone_number in PENDING_IDENTIFICATION:
                del PENDING_IDENTIFICATION[phone_number]
            send_sms(phone_number, "No response received. I’ve closed this conversation. Feel free to reach out if you need further assistance.")
            print(f"Conversation closed for {phone_number} by cron job due to no response after end prompt")
            save_conversations()  # Save after modification

    return "Checked for inactive conversations"

@app.route("/sms", methods=["POST"])
def sms_reply():
    print("Received SMS request")
    from_number = request.values.get("From")
    message = request.values.get("Body").strip()
    print(f"From: {from_number}, Message: {message}")

    # Update last message time for active conversations, with a safeguard for stale conversations
    current_time = datetime.datetime.now()
    if from_number in CURRENT_CONVERSATIONS:
        last_message_time = CURRENT_CONVERSATIONS[from_number]["last_message_time"]
        time_delta = (current_time - last_message_time).total_seconds() / 3600.0  # Time in hours
        # If the conversation is older than 1 hour, treat it as stale and reset it
        if time_delta > 1:
            print(f"Conversation for {from_number} is stale (inactive for {time_delta:.2f} hours). Resetting conversation.")
            del CURRENT_CONVERSATIONS[from_number]
            if from_number in PENDING_IDENTIFICATION:
                del PENDING_IDENTIFICATION[from_number]
            save_conversations()  # Save after modification
        else:
            CURRENT_CONVERSATIONS[from_number]["last_message_time"] = current_time
            # Reset pending end if tenant responds
            if CURRENT_CONVERSATIONS[from_number].get("pending_end", False):
                CURRENT_CONVERSATIONS[from_number]["pending_end"] = False
                if "pending_end_time" in CURRENT_CONVERSATIONS[from_number]:
                    del CURRENT_CONVERSATIONS[from_number]["pending_end_time"]
            print(f"Updated last message time for {from_number}")
            save_conversations()  # Save after modification

    # Always prompt for identification if not in an active conversation
    if from_number not in CURRENT_CONVERSATIONS:
        # Start a new conversation, add to both PENDING_IDENTIFICATION and CURRENT_CONVERSATIONS
        PENDING_IDENTIFICATION[from_number] = {"state": "awaiting_identification", "pending_message": message}
        CURRENT_CONVERSATIONS[from_number] = {
            "tenant_key": None,
            "last_message_time": current_time,
            "pending_end": False,
            "pending_identification": True  # Indicate this conversation is in identification phase
        }
        save_conversations()
        send_sms(from_number, "Please identify yourself with your first name, last name, or unit number (e.g., John Doe, Unit 5).")
        return "OK"

    # Check if the conversation is still in the identification phase
    if CURRENT_CONVERSATIONS[from_number].get("pending_identification", False):
        # Try to identify the tenant based on the input
        tenant_key, possible_matches = identify_tenant(message)
        if tenant_key:
            # Successfully identified
            # Retrieve the pending message before deleting the entry
            if from_number in PENDING_IDENTIFICATION:
                pending_message = PENDING_IDENTIFICATION[from_number].get("pending_message")
                del PENDING_IDENTIFICATION[from_number]
            else:
                pending_message = None
            # Update the existing conversation entry
            CURRENT_CONVERSATIONS[from_number] = {
                "tenant_key": tenant_key,
                "last_message_time": current_time,
                "pending_end": False,
                "pending_identification": False  # Identification complete
            }
            print(f"Tenant identified for {from_number}: {tenant_key}")
            save_conversations()  # Save after adding new conversation
            # Send the personalized greeting first (no follow-up question here)
            park_name = TENANTS[tenant_key]["park"]["name"]
            send_sms(from_number, f"Hello {tenant_key[1]}! I’ve identified you. How can I assist you today regarding {park_name}?")
            # Process the pending message only if it's substantive
            if pending_message:
                message_lower = pending_message.lower()
                generic_greetings = ["hello", "hi", "hey", "greetings", "good morning", "good afternoon", "good evening"]
                if message_lower in generic_greetings:
                    print(f"Skipping generic pending message: '{pending_message}'")
                else:
                    tenant_data = TENANTS[tenant_key]
                    # Fetch transactions if needed for the pending message
                    if ("balance" in message_lower or "pay" in message_lower or "due" in message_lower or
                        "payment history" in message_lower or "last payment" in message_lower or
                        "recent transactions" in message_lower or "last month" in message_lower or
                        "why is my balance" in message_lower or "statement" in message_lower or
                        "rent charge" in message_lower):
                        tenant_id = tenant_key[0]
                        transactions, last_payment_date = fetch_tenant_transactions(tenant_id)
                        if transactions is not None:
                            TENANTS[tenant_key]["transactions"] = transactions
                            TENANTS[tenant_key]["last_payment_date"] = last_payment_date
                            tenant_data = TENANTS[tenant_key]
                        else:
                            send_sms(from_number, f"I’m sorry, I couldn’t retrieve your transaction data at this time. Please contact the park office at {PARK_OFFICE_PHONE}, available {PARK_OFFICE_HOURS}, for assistance.")
                            return "OK"
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
                        # Add follow-up question for maintenance requests
                        reply += " Is there anything else I can assist you with?"
                        send_sms(from_number, reply)
                    else:
                        reply = get_ai_response(pending_message, tenant_data)
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
            # Update the last message time even if identification fails
            CURRENT_CONVERSATIONS[from_number]["last_message_time"] = current_time
            save_conversations()
            return "OK"

    # Tenant is in an active conversation (identification is complete)
    message_lower = message.lower()
    tenant_key = CURRENT_CONVERSATIONS[from_number]["tenant_key"]
    print(f"Accessing tenant_key for {from_number}: {tenant_key} (type: {type(tenant_key)})")
    tenant_data = TENANTS[tenant_key]

    # Check if the tenant wants to end the conversation (prioritize this check)
    end_conversation_phrases = ["no", "nope", "that's all", "that's it", "goodbye", "bye", "done", "i'm done", "nevermind"]
    if message_lower in end_conversation_phrases:
        del CURRENT_CONVERSATIONS[from_number]
        if from_number in PENDING_IDENTIFICATION:
            del PENDING_IDENTIFICATION[from_number]
        save_conversations()  # Save after removing conversation
        send_sms(from_number, "Goodbye! Feel free to reach out if you need further assistance.")
        print(f"Conversation ended for {from_number}")
        return "OK"

    # Fetch transaction data if needed for financial queries
    needs_transactions = (
        "balance" in message_lower or
        "pay" in message_lower or
        "due" in message_lower or
        "payment history" in message_lower or
        "last payment" in message_lower or
        "recent transactions" in message_lower or
        "last month" in message_lower or
        "why is my balance" in message_lower or
        "statement" in message_lower or
        "rent charge" in message_lower
    )
    if needs_transactions and tenant_data["transactions"] is None:
        tenant_id = tenant_key[0]  # Extract TenantID from tenant_key
        transactions, last_payment_date = fetch_tenant_transactions(tenant_id)
        if transactions is not None:
            # Update the TENANTS dictionary with the fetched transactions
            TENANTS[tenant_key]["transactions"] = transactions
            TENANTS[tenant_key]["last_payment_date"] = last_payment_date
            tenant_data = TENANTS[tenant_key]  # Refresh tenant_data with updated info
        else:
            # Handle the case where fetching transactions fails
            send_sms(from_number, f"I’m sorry, I couldn’t retrieve your transaction data at this time. Please contact the park office at {PARK_OFFICE_PHONE}, available {PARK_OFFICE_HOURS}, for assistance.")
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
        # Add follow-up question for maintenance requests
        reply += " Is there anything else I can assist you with?"
        send_sms(from_number, reply)
    elif "address" in message_lower:
        address = tenant_data['address']
        full_address = f"{address['street']}, {address['city']}, {address['state']} {address['postal_code']}"
        reply = f"Your primary address on file is {full_address}. Is there anything else I can assist you with?"
        send_sms(from_number, reply)
    elif "unit" in message_lower or "lot" in message_lower:
        reply = f"Your unit number is {tenant_key[3]}. Is there anything else I can assist you with?"
        send_sms(from_number, reply)
    elif "move in" in message_lower or "move-in" in message_lower:
        move_in_date = tenant_data['move_in_date']
        if move_in_date != "Unknown":
            # Parse the date and format it nicely
            try:
                date_obj = datetime.datetime.strptime(move_in_date, "%Y-%m-%dT%H:%M:%S")
                formatted_date = date_obj.strftime("%B %d, %Y")
                reply = f"Your move-in date was {formatted_date}. If you need any more information, feel free to ask! Is there anything else I can assist you with?"
            except ValueError:
                reply = f"I’m sorry, I couldn’t determine your move-in date. Please contact the park office at {PARK_OFFICE_PHONE}, available {PARK_OFFICE_HOURS}, for more details. Is there anything else I can assist you with?"
        else:
            reply = f"I’m sorry, I couldn’t determine your move-in date. Please contact the park office at {PARK_OFFICE_PHONE}, available {PARK_OFFICE_HOURS}, for more details. Is there anything else I can assist you with?"
        send_sms(from_number, reply)
    elif "phone number" in message_lower:
        reply = f"I'm here to help with any questions you have. For your phone number, you can find it on your lease agreement or by checking any recent communication from the park office at {PARK_OFFICE_PHONE}, available {PARK_OFFICE_HOURS}. If you need further assistance, feel free to ask! Is there anything else I can assist you with?"
        send_sms(from_number, reply)
    else:
        # Let the AI handle all other queries, including financial ones
        reply = get_ai_response(message, tenant_data)
        send_sms(from_number, reply)
    return "OK"

@app.route("/voice", methods=["POST"])
def voice_reply():
    print("Received voice request")
    from_number = request.values.get("From")
    CALL_LOGS.append({
        "phone_number": from_number,
        "call_type": "incoming",
        "timestamp": datetime.datetime.now().isoformat(),
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
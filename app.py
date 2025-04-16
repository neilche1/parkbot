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
import logging
import langdetect
from collections import deque
from dateutil.relativedelta import relativedelta

app = Flask(__name__)

# Set up logging to DEBUG level
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

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
CURRENT_CONVERSATIONS = {}  # Maps phone_number to {"tenant_key": (tenant_id, first_name, last_name, unit), "last_message_time": datetime, "pending_end": bool, "pending_identification": bool, "language": str, "initial_language": str, "message_history": deque}

# File path for storing CURRENT_CONVERSATIONS
CONVERSATIONS_FILE = "current_conversations.json"

# Load CURRENT_CONVERSATIONS from file at startup
def load_conversations():
    global CURRENT_CONVERSATIONS
    try:
        if os.path.exists(CONVERSATIONS_FILE):
            with open(CONVERSATIONS_FILE, "r") as f:
                data = json.load(f)
                # Convert last_message_time back to datetime objects and tenant_key back to tuple
                for phone_number, conversation in data.items():
                    conversation["last_message_time"] = datetime.datetime.fromisoformat(conversation["last_message_time"])
                    if "pending_end_time" in conversation and conversation["pending_end_time"]:
                        conversation["pending_end_time"] = datetime.datetime.fromisoformat(conversation["pending_end_time"])
                    # Convert tenant_key list back to tuple if it exists
                    if "tenant_key" in conversation and conversation["tenant_key"] is not None:
                        if isinstance(conversation["tenant_key"], list):
                            conversation["tenant_key"] = tuple(conversation["tenant_key"])
                            logger.debug(f"Converted tenant_key to tuple for {phone_number}: {conversation['tenant_key']}")
                        elif not isinstance(conversation["tenant_key"], tuple):
                            logger.error(f"Invalid tenant_key type for {phone_number}: {type(conversation['tenant_key'])}. Expected tuple or list, got {conversation['tenant_key']}")
                            conversation["tenant_key"] = None  # Reset to None to force re-identification
                    conversation["message_history"] = deque(conversation.get("message_history", []), maxlen=5)
                CURRENT_CONVERSATIONS = data
                logger.info("Loaded CURRENT_CONVERSATIONS from file")
        else:
            CURRENT_CONVERSATIONS = {}
            logger.info("No existing CURRENT_CONVERSATIONS file found. Starting with an empty dictionary.")
    except Exception as e:
        logger.error(f"Error loading CURRENT_CONVERSATIONS from file: {str(e)}")
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
                "pending_identification": conversation.get("pending_identification", False),
                "language": conversation.get("language", "en"),
                "initial_language": conversation.get("initial_language", "en"),
                "message_history": list(conversation["message_history"])
            }
            if "pending_end_time" in conversation and conversation["pending_end_time"]:
                data[phone_number]["pending_end_time"] = conversation["pending_end_time"].isoformat()
        with open(CONVERSATIONS_FILE, "w") as f:
            json.dump(data, f)
        logger.info("Saved CURRENT_CONVERSATIONS to file")
    except Exception as e:
        logger.error(f"Error saving CURRENT_CONVERSATIONS to file: {str(e)}")

# Load conversations at startup
load_conversations()

# Reset CURRENT_CONVERSATIONS on startup
def reset_conversations_on_startup():
    global CURRENT_CONVERSATIONS
    CURRENT_CONVERSATIONS = {}
    save_conversations()
    logger.info("Reset CURRENT_CONVERSATIONS on startup")

# Call the reset function when the app starts
reset_conversations_on_startup()

# Authenticate with Rent Manager API to obtain a token
def authenticate_with_rent_manager():
    global RENT_MANAGER_API_TOKEN
    if not RENT_MANAGER_USERNAME or not RENT_MANAGER_PASSWORD:
        logger.error("Rent Manager credentials not found in environment variables.")
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
        logger.info(f"Attempting to authenticate with Rent Manager at {RENT_MANAGER_AUTH_URL}")
        response = requests.post(RENT_MANAGER_AUTH_URL, json=payload, headers=headers)
        logger.info(f"Authentication Response Status: {response.status_code}")
        logger.info(f"Authentication Response Text: {response.text}")
        response.raise_for_status()

        # The API returns the token as a raw string, not JSON
        token = response.text.strip().strip('"')  # Remove any surrounding quotes
        if not token:
            logger.error("Authentication failed: No token received from Rent Manager API.")
            return None

        RENT_MANAGER_API_TOKEN = token
        logger.info(f"Successfully authenticated with Rent Manager. Token: {RENT_MANAGER_API_TOKEN}")
        return RENT_MANAGER_API_TOKEN
    except requests.exceptions.RequestException as e:
        logger.error(f"Error authenticating with Rent Manager: {str(e)}")
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
        logger.error("Failed to authenticate with Rent Manager. Cannot fetch tenants.")
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
            logger.info(f"Fetching tenants from {url}")
            response = requests.get(url, headers=headers, params=params)
            logger.info(f"Tenant Fetch Response Status: {response.status_code}")
            response.raise_for_status()
            tenants_data = response.json()
            all_tenants.extend(tenants_data)

            # Check for the next page
            link_header = response.headers.get("Link")
            url = parse_link_header(link_header)
            params = None  # Clear params for subsequent requests, as the URL already includes them
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching tenants from {url}: {str(e)}")
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

            # Use TenantID as part of the key to avoid duplicates, normalize unit name
            tenant_key = (tenant_id, first_name, last_name, lot.lower().replace(" ", ""))
            tenants[tenant_key] = {
                "tenant_id": tenant_id,  # Add tenant_id for logging purposes
                "balance": balance,
                "due_date": due_date,
                "move_in_date": move_in_date,
                "address": address_details,
                "park": park,
                "transactions": None,  # Transactions will be fetched on-demand
                "last_payment_date": None  # Will be set when transactions are fetched
            }
        except Exception as e:
            logger.error(f"Error processing tenant TenantID={tenant_id}: {str(e)}")
            continue

    logger.info(f"Successfully fetched {len(tenants)} current tenants from Rent Manager (total tenants fetched: {len(all_tenants)})")
    return tenants

# Fetch transaction data for a specific tenant on-demand
def fetch_tenant_transactions(tenant_id):
    global RENT_MANAGER_API_TOKEN
    if not RENT_MANAGER_API_TOKEN:
        authenticate_with_rent_manager()
    
    if not RENT_MANAGER_API_TOKEN:
        logger.error("Failed to authenticate with Rent Manager. Cannot fetch transactions.")
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
            logger.info(f"Fetching transactions for TenantID={tenant_id} from {url}")
            response = requests.get(url, headers=headers, params=params)
            logger.info(f"Transaction Fetch Response Status (TenantID={tenant_id}): {response.status_code}")
            logger.info(f"Transaction Fetch Response Text (TenantID={tenant_id}): {response.text[:500]}...")  # Truncate for brevity
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

            logger.info(f"Fetched {len(transactions)} transactions for TenantID={tenant_id}")
            return transactions, last_payment_date

        except requests.exceptions.HTTPError as e:
            if response.status_code == 401:  # Unauthorized, token might be invalid
                logger.warning(f"Received 401 Unauthorized error: {str(e)}. Attempting to re-authenticate...")
                # Clear the current token and re-authenticate
                RENT_MANAGER_API_TOKEN = None
                authenticate_with_rent_manager()
                if not RENT_MANAGER_API_TOKEN:
                    logger.error("Failed to re-authenticate with Rent Manager after 401 error.")
                    return None, None
                # Update headers with the new token
                headers["X-RM12Api-ApiToken"] = RENT_MANAGER_API_TOKEN
                continue  # Retry the request with the new token
            else:
                logger.error(f"Error fetching transactions for TenantID={tenant_id}: {str(e)}")
                return None, None
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching transactions for TenantID={tenant_id}: {str(e)}")
            return None, None

    logger.error("Failed to fetch transactions after re-authentication attempt.")
    return None, None

# Initialize tenant data synchronously at startup
logger.info("Fetching tenant data at startup...")
TENANTS = fetch_tenants_from_rent_manager()
logger.info("Tenant data fetch completed.")

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
    
    logger.info(f"Attempting to identify tenant with input: '{input_text}' (normalized: '{input_text_normalized}')")
    
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
    
    # Check for unit matches first if the input contains digits (likely a unit number)
    contains_digits = any(char.isdigit() for char in input_text)
    if contains_digits:
        logger.debug("Input contains digits, prioritizing unit match")
        for tenant_key in TENANTS:
            tenant_id, first_name, last_name, unit = tenant_key
            unit_lower = unit.lower()
            unit_normalized = unit_lower.replace(" ", "")
            tenant_park_name = TENANTS[tenant_key]["park"]["name"].lower()
            tenant_city = TENANTS[tenant_key]["address"]["city"].lower()

            logger.debug(f"Checking unit for TenantID={tenant_id}, Unit='{unit_lower}', UnitNormalized='{unit_normalized}'")

            # Exact unit match
            if unit_normalized == input_text_normalized and tenant_key not in possible_matches:
                logger.info(f"Match found by unit (exact normalized match): {tenant_key}")
                possible_matches.append(tenant_key)
                continue

            # Partial unit match (input starts with unit)
            if (input_text_normalized.startswith(unit_normalized) and 
                  (len(input_text_normalized) == len(unit_normalized) or 
                   input_text_normalized[len(unit_normalized)] in [' ', '']) and 
                  tenant_key not in possible_matches):
                logger.info(f"Match found by unit (partial normalized match - input starts with unit): {tenant_key}")
                possible_matches.append(tenant_key)
                continue

            # Fuzzy unit match
            unit_similarity = fuzz.ratio(unit_normalized, input_text_normalized)
            if unit_similarity >= 90 and tenant_key not in possible_matches:
                logger.info(f"Match found by unit (fuzzy match, similarity {unit_similarity}%): {tenant_key}")
                possible_matches.append(tenant_key)
                continue

    # If no unit matches (or input doesn't contain digits), check for name matches
    if not possible_matches:
        logger.debug("No unit matches found, checking for name matches")
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

            logger.debug(f"Checking tenant: TenantID={tenant_id}, FullName='{full_name}', FirstName='{first_name_lower}', LastName='{last_name_lower}', Unit='{unit_lower}', UnitNormalized='{unit_normalized}', Park='{tenant_park_name}', City='{tenant_city}'")

            # Exact full name match
            if full_name == input_text and tenant_key not in possible_matches:
                logger.info(f"Match found by full name: {tenant_key}")
                possible_matches.append(tenant_key)
                continue

            # Exact first or last name match
            if (first_name_lower == input_text or last_name_lower == input_text) and tenant_key not in possible_matches:
                logger.info(f"Match found by first or last name: {tenant_key}")
                possible_matches.append(tenant_key)
                continue

            # Input is a substring of the full name
            if input_text in full_name and tenant_key not in possible_matches:
                logger.info(f"Match found by partial name: {tenant_key}")
                possible_matches.append(tenant_key)
                continue

            # Check for all input words in the full name
            all_words_present = all(word in full_name for word in input_words)
            if all_words_present and tenant_key not in possible_matches:
                logger.info(f"Match found by all input words in full name: {tenant_key}")
                possible_matches.append(tenant_key)
                continue

            # Match first name and any part of the last name
            last_name_words = last_name_lower.split()
            input_has_first_name = first_name_lower in input_words
            input_has_any_last_name_part = any(word in last_name_words for word in input_words if word != first_name_lower)
            if input_has_first_name and input_has_any_last_name_part and tenant_key not in possible_matches:
                logger.info(f"Match found by first name and part of last name: {tenant_key}")
                possible_matches.append(tenant_key)
                continue

    # If still no matches, check for combined input (e.g., "Clara Lopez 02")
    if not possible_matches:
        logger.debug("No name matches found, checking for combined input")
        for tenant_key in TENANTS:
            tenant_id, first_name, last_name, unit = tenant_key
            unit_lower = unit.lower()
            unit_normalized = unit_lower.replace(" ", "")
            tenant_park_name = TENANTS[tenant_key]["park"]["name"].lower()
            tenant_city = TENANTS[tenant_key]["address"]["city"].lower()

            input_has_unit = any(unit_normalized == word.replace(" ", "") for word in input_words)
            full_name = " ".join(f"{first_name} {last_name}".split()).lower().strip()
            first_name_lower = " ".join(first_name.split()).lower().strip()
            last_name_lower = " ".join(last_name.split()).lower().strip()
            input_has_first_name = any(word == first_name_lower for word in input_words)
            input_has_last_name = any(word == last_name_lower for word in input_words)
            if input_has_unit and (input_has_first_name or input_has_last_name) and tenant_key not in possible_matches:
                logger.info(f"Match found by combined input: {tenant_key}")
                possible_matches.append(tenant_key)
                continue

    # If a park name or city was identified in the input, filter matches
    if park_name_in_input:
        logger.info(f"Filtering matches by park name: '{park_name_in_input}'")
        for match in possible_matches:
            tenant_park_name = TENANTS[match]["park"]["name"].lower()
            if park_name_in_input == tenant_park_name:
                park_filtered_matches.append(match)
        possible_matches = park_filtered_matches
        logger.info(f"After park filter, possible matches: {possible_matches}")
    elif city_in_input:
        logger.info(f"Filtering matches by city: '{city_in_input}'")
        for match in possible_matches:
            tenant_city = TENANTS[match]["address"]["city"].lower()
            if city_in_input == tenant_city:
                park_filtered_matches.append(match)
        possible_matches = park_filtered_matches
        logger.info(f"After city filter, possible matches: {possible_matches}")

    # If there's exactly one match, return it
    if len(possible_matches) == 1:
        logger.info(f"Exactly one match found: {possible_matches[0]}")
        return possible_matches[0], None
    # If there are multiple matches, return the list of matches to prompt for more details
    elif len(possible_matches) > 1:
        logger.info(f"Multiple matches found: {possible_matches}")
        return None, possible_matches  # Return None for tenant_key, and the list of matches
    else:
        logger.info("No matches found")
        return None, None  # No match

def get_ai_response(user_input, tenant_data, conversation_language, message_history=None, is_maintenance_request=False, include_transactions=True, check_for_end=False):
    start_time = datetime.datetime.now()
    
    # Include the full tenant_data and park_details in the prompt (no exclusions)
    park_details = tenant_data.get("park", {
        "name": "Unknown Park",
        "address": {"street": "Unknown", "city": "Unknown", "state": "Unknown", "postal_code": "Unknown"},
        "payment_methods": "Checks and money orders only (no cash)",
        "payment_procedure": "Drop off at the park’s dropbox",
        "payee": "Unknown Park"
    })
    # Ensure transactions is always a list, even if None
    transactions = tenant_data.get("transactions", []) if include_transactions else []
    if transactions is None:
        transactions = []  # Default to empty list if transactions is None

    # Handle statement requests with default time period if not specified
    statement_period = None
    filtered_transactions = transactions
    if "statement" in user_input.lower() and include_transactions:
        # Check if a specific time period is mentioned (e.g., "March", "last month")
        current_date = datetime.datetime.now()
        # Default to the last fully completed month if no period is specified
        last_month = current_date - relativedelta(months=1)
        start_date = last_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_date = (start_date + relativedelta(months=1)) - relativedelta(seconds=1)
        statement_period = f"{start_date.strftime('%B %Y')}"
        
        # Filter transactions for the specified period
        filtered_transactions = [
            t for t in transactions
            if t.get("TransactionDate") and start_date <= datetime.datetime.strptime(t["TransactionDate"], "%Y-%m-%dT%H:%M:%S") <= end_date
        ]
        logger.info(f"Filtered {len(filtered_transactions)} transactions for period {statement_period}")

    # If the query is about rent, try to infer the monthly rent charge from transactions
    monthly_rent_charge = None
    if "rent" in user_input.lower() and include_transactions:
        for transaction in transactions:
            if "rent" in transaction.get("Comment", "").lower() and transaction.get("TransactionType") != "Payment":
                monthly_rent_charge = float(transaction.get("Amount", 0.00))
                break
    else:
        monthly_rent_charge = None

    if filtered_transactions and include_transactions:
        filtered_transactions.sort(key=lambda x: x.get("TransactionDate", ""), reverse=True)
    
    # Create a copy of tenant_data without transactions to avoid double-counting
    tenant_data_copy = tenant_data.copy()
    if "transactions" in tenant_data_copy:
        del tenant_data_copy["transactions"]
    
    # Estimate input token count more accurately
    transaction_str = json.dumps(filtered_transactions)
    tenant_data_str = json.dumps(tenant_data_copy)
    park_details_str = json.dumps(park_details)
    user_input_str = user_input
    message_history_str = json.dumps(list(message_history)) if message_history else "[]"
    transaction_tokens = len(transaction_str) * 0.25
    tenant_data_tokens = len(tenant_data_str) * 0.25
    park_details_tokens = len(park_details_str) * 0.25
    user_input_tokens = len(user_input_str) * 0.25
    message_history_tokens = len(message_history_str) * 0.25
    total_input_tokens = transaction_tokens + tenant_data_tokens + park_details_tokens + user_input_tokens + message_history_tokens
    logger.info(f"Sending {len(filtered_transactions)} transactions to xAI API for tenant {tenant_data.get('tenant_id', 'Unknown')}. Estimated input tokens: {total_input_tokens:.0f}")
    logger.debug(f"Transaction string length: {len(transaction_str)}, Tenant data string length: {len(tenant_data_str)}, Park details string length: {len(park_details_str)}, User input length: {len(user_input_str)}, Message history length: {len(message_history_str)}")
    
    # Use the inferred monthly rent charge if available
    rent_charge_str = f"${monthly_rent_charge:.2f}" if monthly_rent_charge is not None else "unknown"
    
    # Construct the prompt
    prompt = (
        f"Tenant data: {tenant_data_copy}. "
        f"Tenant is from park: {park_details}. "
        f"All transactions: {filtered_transactions}. "
        f"Monthly rent charge (if available): {rent_charge_str}. "
        f"Conversation language: {conversation_language}. "
        f"Conversation history (last 5 messages, format [{{'role': 'user' or 'bot', 'content': message}}]): {message_history_str}. "
        f"Query: {user_input}"
    )
    if statement_period:
        prompt += f"\nStatement period (if applicable): {statement_period}."
    prompt_length = len(prompt)
    logger.debug(f"Prompt length: {prompt_length} characters")
    
    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=3, max=6),
        retry=retry_if_exception_type(Exception)
    )
    def call_xai():
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {XAI_API_KEY}"
        }
        logger.info(f"Generating AI response for conversation_language: {conversation_language}")
        system_prompt = (
            "You are a professional mobile home park manager assisting tenants across multiple mobile home parks. "
            "Respond in a natural, conversational tone as a human would, without explicitly stating your role. "
            "If 'Conversation language' is 'es', respond in Spanish. Otherwise, respond in English. "
            "Ensure responses flow seamlessly as part of an ongoing conversation, avoiding repetitive greetings like 'Hey there' after the initial message. "
            "Provide concise, actionable responses tailored to the tenant’s specific park, provided as 'Tenant is from park: {park_details}'. "
            "Use the tenant's full transaction history ('All transactions: {transactions}') for payment-related queries. "
            "For financial queries (e.g., balance, rent charge, payment history), use the tenant's balance, due date, monthly rent charge, and transaction history. "
            "If the query is about the tenant's rent (e.g., 'What is my rent?'), use the 'Monthly rent charge' provided in the prompt if available; otherwise, infer it from the transaction history by identifying recurring charges labeled as 'rent'. "
            "For example, if asked 'What did I pay last month?', calculate the total payments made last month from the transaction history. "
            "If asked for a statement (e.g., 'Give me my statement'), generate a detailed statement for the 'Statement period' (if provided), including all charges and payments within that period, and calculate the resulting balance. Format the statement clearly, e.g., 'Here’s your statement for [period]: Charges: [list charges with dates and amounts], Payments: [list payments with dates and amounts], Total Balance: [amount].' "
            "If asked about the rent charge, use the 'Monthly rent charge' if available, or infer from transaction history. "
            "For payment policies, state that tenants can be evicted for not paying utilities or other fees, as non-payment of any charges can lead to eviction. "
            "Do not suggest payment plans; encourage immediate payment or direct to the park office. "
            "For maintenance requests, confirm the issue is logged, the owner is notified, and provide a next step (e.g., scheduling a repair). "
            "For other queries, respond using park-specific details (e.g., payment_methods, payment_procedure, payee). "
            "If lacking details, respond with: 'I’m sorry, I don’t have that information. Please contact the park office at (504) 313-0024, available Monday to Friday, 9 AM to 5 PM, for more details.' (in English) or 'Lo siento, no tengo esa información. Por favor, contacta a la oficina del parque al (504) 313-0024, disponible de lunes a viernes, de 9 AM a 5 PM, para más detalles.' (in Spanish). "
            "Do not make up information. "
        )
        if check_for_end:
            system_prompt += (
                "Based on the conversation history and the current query, determine if the tenant intends to end the conversation. "
                "Look for phrases like 'He terminado', 'Eso es todo', 'Gracias', 'Adiós', 'I'm done', 'That's all', 'Thank you', 'Goodbye', etc. "
                "If the tenant wants to end, respond with 'END_CONVERSATION'. Otherwise, respond with 'CONTINUE'."
            )
        else:
            system_prompt += (
                "Provide a helpful response to the tenant’s query, considering the conversation history for context."
            )
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "model": "grok-3-fast-beta",
            "stream": False,
            "temperature": 0.5,
            "max_tokens": 500
        }
        try:
            xai_start_time = datetime.datetime.now()
            response = requests.post("https://api.x.ai/v1/chat/completions", headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            xai_end_time = datetime.datetime.now()
            logger.info(f"xAI API call completed in {(xai_end_time - xai_start_time).total_seconds() * 1000:.2f} ms")
            return response.json()
        except requests.exceptions.HTTPError as e:
            error_message = f"HTTPError in call_xai: Status Code: {e.response.status_code}, Response Text: {e.response.text}"
            logger.error(error_message)
            raise Exception(error_message)
        except requests.exceptions.RequestException as e:
            error_message = f"RequestException in call_xai: {str(e)}"
            logger.error(error_message)
            raise Exception(error_message)

    try:
        if include_transactions:
            logger.info("Using standard API call for financial query (consider enabling DeepSearch mode via UI for faster responses)")
        else:
            logger.info("Using standard API call (DeepSearch mode requires user activation via UI)")
        response = call_xai()
        end_time = datetime.datetime.now()
        logger.info(f"get_ai_response completed in {(end_time - start_time).total_seconds() * 1000:.2f} ms")
        if check_for_end:
            intent_response = response["choices"][0]["message"]["content"].strip()
            logger.info(f"Intent detection response: {intent_response}")
            return intent_response
        reply = response["choices"][0]["message"]["content"].strip()
        logger.info(f"AI response: {reply}")
        return reply
    except Exception as e:
        logger.error(f"Error in get_ai_response after retries: {str(e)}")
        if check_for_end:
            return "CONTINUE"  # Default to continuing if intent check fails
        if any(keyword in user_input.lower() for keyword in ["balance", "pay", "due", "payment history", "last payment", "recent transactions", "last month", "rent charge", "statement"]):
            if conversation_language == "es":
                return f"No pude procesar tu solicitud por completo, pero puedo decirte que tu saldo actual es {tenant_data['balance']}, con vencimiento el {tenant_data['due_date']} de cada mes. Para más detalles, intenta de nuevo más tarde o contacta a la oficina del parque al {PARK_OFFICE_PHONE}, disponible {PARK_OFFICE_HOURS}."
            else:
                return f"I couldn’t process your request fully, but I can tell you that your current balance is {tenant_data['balance']}, due on the {tenant_data['due_date']} of each month. For more details, please try again later or contact the park office at {PARK_OFFICE_PHONE}, available {PARK_OFFICE_HOURS}."
        elif is_maintenance_request:
            if conversation_language == "es":
                return "Lamento escuchar sobre tu problema. He registrado tu solicitud y he notificado al propietario. El equipo de mantenimiento te contactará pronto para programar una reparación."
            else:
                return "I’m sorry to hear about your issue. I’ve logged your request and notified the owner. The maintenance team will contact you soon to schedule a repair."
        else:
            if conversation_language == "es":
                return f"Lo siento, no pude procesar tu solicitud en este momento. Por favor, intenta de nuevo más tarde o contacta a la oficina del parque al {PARK_OFFICE_PHONE}, disponible {PARK_OFFICE_HOURS}."
            else:
                return f"I’m sorry, I couldn’t process your request at this time. Please try again later or contact the park office at {PARK_OFFICE_PHONE}, available {PARK_OFFICE_HOURS}."

def send_sms(to_number, message):
    recipient = to_number
    logger.info(f"Preparing to send SMS to {recipient}: {message}")
    logger.debug(f"TWILIO_SID: {TWILIO_SID}")
    logger.debug(f"TWILIO_TOKEN: {TWILIO_TOKEN}")
    
    if TESTING_MODE:
        logger.info(f"TESTING_MODE enabled: SMS not sent. Would have sent to {recipient}: {message}")
        return
    
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        logger.info("Twilio client initialized")
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
        logger.info(f"SMS sent successfully: {response.sid}")
    except Exception as e:
        logger.error(f"Error sending SMS to {recipient}: {str(e)}")
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
    load_conversations()
    current_time = datetime.datetime.now()
    conversations_to_close = []
    
    logger.info(f"Checking for inactive conversations at {current_time}")
    for phone_number, conversation in list(CURRENT_CONVERSATIONS.items()):
        if "last_message_time" not in conversation:
            logger.info(f"Skipping conversation for {phone_number}: No last_message_time")
            continue
        last_message_time = conversation["last_message_time"]
        time_delta = (current_time - last_message_time).total_seconds() / 60.0
        logger.info(f"Conversation for {phone_number}: Last message at {last_message_time}, Time delta: {time_delta:.2f} minutes")

        language = conversation.get("initial_language", "en")  # Use initial_language
        if language == "es":
            inactivity_message = "Ha pasado un tiempo desde tu último mensaje. ¿Hay algo más en lo que pueda ayudarte? Si no, cerraré esta conversación."
            closure_message = "No he recibido respuesta. He cerrado esta conversación. Si necesitas más ayuda, no dudes en contactarme."
        else:
            inactivity_message = "It’s been a while since your last message. Is there anything else I can assist you with? If not, I’ll close this conversation."
            closure_message = "No response received. I’ve closed this conversation. Feel free to reach out if you need further assistance."

        if time_delta >= 2 and not conversation.get("pending_end", False):  # Reduced to 2 minutes
            conversation["pending_end"] = True
            conversation["pending_end_time"] = current_time
            send_sms(phone_number, inactivity_message)
            logger.info(f"Inactivity timeout triggered for {phone_number} by cron job")
            save_conversations()

        if conversation.get("pending_end", False):
            pending_end_time = conversation["pending_end_time"]
            end_delta = (current_time - pending_end_time).total_seconds() / 60.0
            logger.info(f"Conversation for {phone_number}: Pending end at {pending_end_time}, End delta: {end_delta:.2f} minutes")
            if end_delta >= 1:
                conversations_to_close.append(phone_number)

    for phone_number in conversations_to_close:
        if phone_number in CURRENT_CONVERSATIONS:
            language = CURRENT_CONVERSATIONS[phone_number].get("initial_language", "en")
            if language == "es":
                closure_message = "No he recibido respuesta. He cerrado esta conversación. Si necesitas más ayuda, no dudes en contactarme."
            else:
                closure_message = "No response received. I’ve closed this conversation. Feel free to reach out if you need further assistance."
            del CURRENT_CONVERSATIONS[phone_number]
            if phone_number in PENDING_IDENTIFICATION:
                del PENDING_IDENTIFICATION[phone_number]
            send_sms(phone_number, closure_message)
            logger.info(f"Conversation closed for {phone_number} by cron job due to no response after end prompt")
            save_conversations()

    return "Checked for inactive conversations"

@app.route("/sms", methods=["POST"])
def sms_reply():
    logger.info("Received SMS request")
    from_number = request.values.get("From")
    message = request.values.get("Body").strip()
    logger.info(f"From: {from_number}, Message: {message}")

    current_time = datetime.datetime.now()

    if from_number not in CURRENT_CONVERSATIONS:
        try:
            language = langdetect.detect(message)
            if language != 'es':
                language = 'en'
            logger.info(f"Detected language: {language} for message: '{message}'")
        except Exception as e:
            logger.warning(f"Language detection failed for message '{message}': {str(e)}. Defaulting to English.")
            language = "en"
        PENDING_IDENTIFICATION[from_number] = {"state": "awaiting_identification", "pending_message": message}
        CURRENT_CONVERSATIONS[from_number] = {
            "tenant_key": None,
            "last_message_time": current_time,
            "pending_end": False,
            "pending_identification": True,
            "language": language,
            "initial_language": language,
            "message_history": deque(maxlen=5)
        }
        CURRENT_CONVERSATIONS[from_number]["message_history"].append({"role": "user", "content": message})
        save_conversations()
        if language == "es":
            identification_prompt = "Por favor, identifícate con tu nombre, apellido o número de unidad (por ejemplo, Juan Pérez, Unidad 5)."
        else:
            identification_prompt = "Please identify yourself with your first name, last name, or unit number (e.g., John Doe, Unit 5)."
        send_sms(from_number, identification_prompt)
        CURRENT_CONVERSATIONS[from_number]["message_history"].append({"role": "bot", "content": identification_prompt})
        save_conversations()
        return "OK"

    # Update last message time for active conversations
    if from_number in CURRENT_CONVERSATIONS:
        CURRENT_CONVERSATIONS[from_number]["last_message_time"] = current_time
        CURRENT_CONVERSATIONS[from_number]["message_history"].append({"role": "user", "content": message})
        save_conversations()

    conversation_language = CURRENT_CONVERSATIONS[from_number].get("initial_language", "en")
    message_history = CURRENT_CONVERSATIONS[from_number]["message_history"]

    if CURRENT_CONVERSATIONS[from_number].get("pending_identification", False):
        tenant_key, possible_matches = identify_tenant(message)
        if tenant_key:
            if from_number in PENDING_IDENTIFICATION:
                del PENDING_IDENTIFICATION[from_number]
            CURRENT_CONVERSATIONS[from_number]["tenant_key"] = tenant_key
            CURRENT_CONVERSATIONS[from_number]["pending_identification"] = False
            logger.info(f"Tenant identified for {from_number}: {tenant_key}")
            save_conversations()
            park_name = TENANTS[tenant_key]["park"]["name"]
            if conversation_language == "es":
                greeting = f"¡Hola {tenant_key[1]}! Te he identificado. ¿Cómo puedo ayudarte hoy con respecto a {park_name}?"
            else:
                greeting = f"Hello {tenant_key[1]}! I’ve identified you. How can I assist you today regarding {park_name}?"
            send_sms(from_number, greeting)
            CURRENT_CONVERSATIONS[from_number]["message_history"].append({"role": "bot", "content": greeting})
            save_conversations()
            return "OK"
        else:
            if possible_matches:
                if conversation_language == "es":
                    disambig_msg = f"Encontré varios inquilinos que coinciden con '{message}'. Por favor, proporciona más detalles, como tu nombre completo o número de unidad, para identificarte."
                else:
                    disambig_msg = f"I found multiple tenants matching '{message}'. Please provide more details, such as your full name or unit number, to identify yourself."
                send_sms(from_number, disambig_msg)
                CURRENT_CONVERSATIONS[from_number]["message_history"].append({"role": "bot", "content": disambig_msg})
            else:
                if conversation_language == "es":
                    no_match_msg = "No pude identificarte con la información proporcionada. Por favor, intenta de nuevo con tu nombre, apellido o número de unidad (por ejemplo, Juan Pérez, Unidad 5)."
                else:
                    no_match_msg = "I couldn’t identify you with the information provided. Please try again with your first name, last name, or unit number (e.g., John Doe, Unit 5)."
                send_sms(from_number, no_match_msg)
                CURRENT_CONVERSATIONS[from_number]["message_history"].append({"role": "bot", "content": no_match_msg})
            save_conversations()
            return "OK"

    # Handle tenant's query
    tenant_key = CURRENT_CONVERSATIONS[from_number]["tenant_key"]
    # Validate tenant_key type
    if not isinstance(tenant_key, tuple):
        logger.error(f"Invalid tenant_key type for {from_number}: {type(tenant_key)}. Expected tuple, got {tenant_key}")
        if conversation_language == "es":
            error_msg = "Lo siento, hubo un problema al procesar tu solicitud. Por favor, identifícate nuevamente con tu nombre, apellido o número de unidad."
        else:
            error_msg = "I’m sorry, there was an issue processing your request. Please identify yourself again with your first name, last name, or unit number."
        send_sms(from_number, error_msg)
        CURRENT_CONVERSATIONS[from_number]["message_history"].append({"role": "bot", "content": error_msg})
        CURRENT_CONVERSATIONS[from_number]["tenant_key"] = None
        CURRENT_CONVERSATIONS[from_number]["pending_identification"] = True
        save_conversations()
        return "OK"

    try:
        tenant_data = TENANTS[tenant_key]
        # Fetch transactions for financial queries (balance, statement, rent, etc.)
        if any(keyword in message.lower() for keyword in ["balance", "pay", "due", "payment history", "last payment", "recent transactions", "last month", "rent charge", "statement", "charge for", "rent"]):
            transactions, last_payment_date = fetch_tenant_transactions(tenant_key[0])
            if transactions is not None:
                tenant_data["transactions"] = transactions
                # If the query is specifically about rent, ensure we try to infer it
                if "rent" in message.lower():
                    monthly_rent_charge = None
                    for transaction in transactions:
                        if "rent" in transaction.get("Comment", "").lower() and transaction.get("TransactionType") != "Payment":
                            monthly_rent_charge = float(transaction.get("Amount", 0.00))
                            break
                    if monthly_rent_charge is not None:
                        tenant_data["monthly_rent_charge"] = monthly_rent_charge
            if last_payment_date is not None:
                tenant_data["last_payment_date"] = last_payment_date
    except Exception as e:
        logger.error(f"Error accessing tenant data for {from_number} with tenant_key {tenant_key}: {str(e)}")
        if conversation_language == "es":
            error_msg = "Lo siento, hubo un problema al procesar tu solicitud. Por favor, identifícate nuevamente con tu nombre, apellido o número de unidad."
        else:
            error_msg = "I’m sorry, there was an issue processing your request. Please identify yourself again with your first name, last name, or unit number."
        send_sms(from_number, error_msg)
        CURRENT_CONVERSATIONS[from_number]["message_history"].append({"role": "bot", "content": error_msg})
        CURRENT_CONVERSATIONS[from_number]["tenant_key"] = None
        CURRENT_CONVERSATIONS[from_number]["pending_identification"] = True
        save_conversations()
        return "OK"

    message_lower = message.lower()

    if "maintenance" in message_lower or "fix" in message_lower or "broken" in message_lower or "leak" in message_lower or "leaking" in message_lower or "flood" in message_lower or "damage" in message_lower or "repair" in message_lower or "clog" in message_lower or "power" in message_lower:
        tenant_name = f"{tenant_key[1]} {tenant_key[2]}"
        tenant_lot = tenant_key[3]
        MAINTENANCE_REQUESTS.append({
            "tenant_phone": from_number,
            "tenant_name": tenant_name,
            "tenant_lot": tenant_lot,
            "issue": message
        })
        owner_message = f"Maintenance request from {tenant_name}, Unit {tenant_lot}: {message}"
        try:
            send_sms(OWNER_PHONE, owner_message)
        except Exception as e:
            logger.error(f"Failed to notify owner for maintenance request from {tenant_name}: {str(e)}")
        reply = get_ai_response(message, tenant_data, conversation_language, message_history, is_maintenance_request=True, include_transactions=False)
        if conversation_language == "es":
            reply += f" Para asistencia inmediata, puedes contactar a la oficina del parque al {PARK_OFFICE_PHONE}, disponible {PARK_OFFICE_HOURS}. ¿Hay algo más con lo que pueda ayudarte?"
        else:
            reply += f" For immediate assistance, you can contact the park office at {PARK_OFFICE_PHONE}, available {PARK_OFFICE_HOURS}. Is there anything else I can assist you with?"
        send_sms(from_number, reply)
        CURRENT_CONVERSATIONS[from_number]["message_history"].append({"role": "bot", "content": reply})
    else:
        reply = get_ai_response(message, tenant_data, conversation_language, message_history, include_transactions=True)
        send_sms(from_number, reply)
        CURRENT_CONVERSATIONS[from_number]["message_history"].append({"role": "bot", "content": reply})

    # Check if the tenant intends to end the conversation
    intent = get_ai_response(message, tenant_data, conversation_language, message_history, check_for_end=True, include_transactions=False)
    if "END_CONVERSATION" in intent:
        if conversation_language == "es":
            goodbye_msg = "¡Adiós! Si necesitas más ayuda, no dudes en contactarme."
        else:
            goodbye_msg = "Goodbye! Feel free to reach out if you need further assistance."
        send_sms(from_number, goodbye_msg)
        CURRENT_CONVERSATIONS[from_number]["message_history"].append({"role": "bot", "content": goodbye_msg})
        del CURRENT_CONVERSATIONS[from_number]
        if from_number in PENDING_IDENTIFICATION:
            del PENDING_IDENTIFICATION[from_number]
        logger.info(f"Conversation ended for {from_number} based on AI intent detection")
        save_conversations()
    else:
        save_conversations()
    return "OK"

if __name__ == "__main__":
    app.run(debug=True)

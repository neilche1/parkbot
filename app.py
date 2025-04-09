from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
import openai
from datetime import datetime, timedelta
import os
import requests

app = Flask(__name__)

# Twilio Credentials (loaded from environment variables)
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN")
MESSAGING_SID = os.getenv("MESSAGING_SID", "MGfeeb018ce3174b051057f0c0176d395d")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "+19853799364")

# Owner's Phone Number for Notifications
OWNER_PHONE = os.getenv("OWNER_PHONE", "+15049090355")

# Test Number for Tenant Communications During Testing
TEST_TENANT_PHONE = "+14247775480"

# OpenAI Credentials
OPENAI_KEY = os.getenv("OPENAI_KEY")
openai.api_key = OPENAI_KEY

# Rent Manager API Credentials
RENT_MANAGER_USERNAME = os.getenv("RENT_MANAGER_USERNAME")
RENT_MANAGER_PASSWORD = os.getenv("RENT_MANAGER_PASSWORD")
RENT_MANAGER_LOCATION_ID = os.getenv("RENT_MANAGER_LOCATION_ID", "1")
RENT_MANAGER_AUTH_URL = "https://shadynook.api.rentmanager.com/Authentication/AuthorizeUser"
RENT_MANAGER_API_URL = "https://shadynook.api.rentmanager.com/v1/tenants"

# Testing Mode (set to True to disable actual SMS sends)
TESTING_MODE = os.getenv("TESTING_MODE", "False").lower() == "true"

# Global variable to store the API token
RENT_MANAGER_API_TOKEN = None

# Authenticate with Rent Manager API to obtain a token
def authenticate_with_rent_manager():
    global RENT_MANAGER_API_TOKEN
    auth_data = {
        "Username": RENT_MANAGER_USERNAME,
        "Password": RENT_MANAGER_PASSWORD,
        "LocationID": int(RENT_MANAGER_LOCATION_ID)
    }
    try:
        response = requests.post(RENT_MANAGER_AUTH_URL, json=auth_data)
        response.raise_for_status()
        token_data = response.json()
        RENT_MANAGER_API_TOKEN = token_data.get("apiToken")
        print(f"Successfully authenticated with Rent Manager. Token: {RENT_MANAGER_API_TOKEN}")
        return RENT_MANAGER_API_TOKEN
    except Exception as e:
        print(f"Error authenticating with Rent Manager: {str(e)}")
        return None

# Fetch tenant data from Rent Manager API
def fetch_tenants_from_rent_manager():
    global RENT_MANAGER_API_TOKEN
    # Ensure we have a valid token
    if not RENT_MANAGER_API_TOKEN:
        authenticate_with_rent_manager()
    
    if not RENT_MANAGER_API_TOKEN:
        print("Failed to authenticate with Rent Manager. Cannot fetch tenants.")
        return {}, {}

    headers = {
        "X-RM12-Api-Token": RENT_MANAGER_API_TOKEN,
        "Content-Type": "application/json"
    }
    try:
        response = requests.get(RENT_MANAGER_API_URL, headers=headers)
        # Handle token expiration (401 Unauthorized)
        if response.status_code == 401:
            print("API token expired or invalid. Re-authenticating...")
            authenticate_with_rent_manager()
            headers["X-RM12-Api-Token"] = RENT_MANAGER_API_TOKEN
            response = requests.get(RENT_MANAGER_API_URL, headers=headers)
        
        response.raise_for_status()
        tenants_data = response.json()
        
        tenants = {}
        phone_to_tenant = {}  # Will be populated after identification
        for tenant in tenants_data:
            name = tenant.get("name", "Unknown")
            # Split name into first and last name (assuming format "First Last")
            try:
                first_name, last_name = name.split(" ", 1)
            except ValueError:
                first_name = name
                last_name = ""
            lot = tenant.get("unit", "Unknown")
            balance = f"${tenant.get('balance', 0.00):.2f}"
            due_date = tenant.get("due_date", "1st")
            phone_numbers = []  # Ignore phone numbers from Rent Manager since they're outdated
            
            tenant_key = (first_name, last_name, lot)
            tenants[tenant_key] = {
                "balance": balance,
                "due_date": due_date,
                "phones": phone_numbers
            }
        
        print(f"Successfully fetched {len(tenants)} tenants from Rent Manager")
        return tenants, phone_to_tenant
    except Exception as e:
        print(f"Error fetching tenants from Rent Manager: {str(e)}")
        return {}, {}

# Initialize tenant data at startup
TENANTS, PHONE_TO_TENANT = fetch_tenants_from_rent_manager()

# Rent Rule
RENT_DUE_DAY = 1  # Due on the 1st of each month
LATE_FEE_PER_DAY = 5  # $5 per day after the 5th
LATE_FEE_START_DAY = 5  # Late fees start after the 5th

MAINTENANCE_REQUESTS = []
CALL_LOGS = []
PENDING_IDENTIFICATION = {}

def get_tenant_data(phone_number):
    tenant_key = PHONE_TO_TENANT.get(phone_number)
    if tenant_key:
        return TENANTS.get(tenant_key, {"name": "Unknown", "balance": "N/A", "due_date": "N/A"})
    return None

def identify_tenant(first_name, last_name, unit):
    tenant_key = (first_name.strip(), last_name.strip(), unit.strip())
    if tenant_key in TENANTS:
        return tenant_key
    return None

def get_ai_response(user_input, tenant_data, is_maintenance_request=False):
    prompt = f"Act as a mobile home park manager. Tenant data: {tenant_data}. Query: {user_input}"
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a professional mobile home park manager assisting tenants. Provide concise, actionable responses. For maintenance requests, confirm the issue has been logged, the owner has been notified, and provide a clear next step (e.g., scheduling a repair). For other queries, respond helpfully and professionally."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=60,
            temperature=0.5
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error in get_ai_response: {str(e)}")
        if is_maintenance_request:
            return "I’m sorry to hear about your issue. I’ve logged your request and notified the owner. The maintenance team will contact you soon to schedule a repair."
        return "I’m sorry, I couldn’t process your request at this time. Please try again later or contact the park manager directly."

def send_sms(to_number, message):
    # Determine the recipient number: use TEST_TENANT_PHONE for tenants, OWNER_PHONE for owner
    recipient = to_number
    if to_number != OWNER_PHONE:
        recipient = TEST_TENANT_PHONE
        print(f"Redirecting tenant SMS to test number: {recipient}")

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

@app.route("/sms", methods=["POST"])
def sms_reply():
    print("Received SMS request")
    from_number = request.values.get("From")
    message = request.values.get("Body").strip()
    print(f"From: {from_number}, Message: {message}")

    tenant_data = get_tenant_data(from_number)
    if tenant_data:
        message_lower = message.lower()
        if "maintenance" in message_lower or "fix" in message_lower or "broken" in message_lower or "leak" in message_lower or "leaking" in message_lower or "flood" in message_lower or "damage" in message_lower or "repair" in message_lower or "clog" in message_lower or "power" in message_lower:
            # Log the maintenance request
            tenant_key = PHONE_TO_TENANT[from_number]
            tenant_name = f"{tenant_key[0]} {tenant_key[1]}"
            tenant_lot = tenant_key[2]
            MAINTENANCE_REQUESTS.append({
                "tenant_phone": from_number,
                "tenant_name": tenant_name,
                "tenant_lot": tenant_lot,
                "issue": message
            })
            # Notify the owner (will be sent to OWNER_PHONE)
            owner_message = f"Maintenance request from {tenant_name}, Unit {tenant_lot}: {message}"
            send_sms(OWNER_PHONE, owner_message)
            # Generate an AI response for the tenant (will be sent to TEST_TENANT_PHONE)
            reply = get_ai_response(message, tenant_data, is_maintenance_request=True)
        else:
            reply = get_ai_response(message, tenant_data)
        send_sms(from_number, reply)
        return "OK"

    if from_number in PENDING_IDENTIFICATION:
        try:
            # Expecting format: "FirstName LastName, Unit"
            parts = message.split(",", 1)
            if len(parts) != 2:
                raise ValueError("Invalid format")
            name_part = parts[0].strip()
            unit = parts[1].strip()
            # Split name into first and last name
            name_parts = name_part.split(" ", 1)
            if len(name_parts) != 2:
                raise ValueError("Invalid name format")
            first_name = name_parts[0]
            last_name = name_parts[1]
            
            tenant_key = identify_tenant(first_name, last_name, unit)
            if tenant_key:
                PHONE_TO_TENANT[from_number] = tenant_key
                TENANTS[tenant_key]["phones"].append(from_number)
                del PENDING_IDENTIFICATION[from_number]
                reply = "Thank you! I’ve identified you. How can I assist you today?"
            else:
                reply = "Sorry, I couldn’t find a tenant with that name and unit number. Please try again (e.g., John Doe, Unit 5)."
        except ValueError:
            reply = "Please provide your first name, last name, and unit number in the format: FirstName LastName, Unit (e.g., John Doe, Unit 5)."
        send_sms(from_number, reply)
        return "OK"

    PENDING_IDENTIFICATION[from_number] = "awaiting_name_unit"
    send_sms(from_number, "I don’t recognize this number. Please provide your first name, last name, and unit number (e.g., John Doe, Unit 5).")
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
    resp.say("Hello, this is ParkBot. I don’t recognize this number. Please text me your first name, last name, and unit number.")
    return str(resp)

def send_rent_reminders():
    print("Starting send_rent_reminders")
    today = datetime.now()
    day = today.day
    last_day_of_prev_month = (today.replace(day=1) - timedelta(days=1)).day

    for tenant_key, tenant in TENANTS.items():
        print(f"Processing tenant: {tenant_key}")
        try:
            due_day_str = tenant["due_date"].split()[0]
            due_day = int(''.join(filter(str.isdigit, due_day_str)))
            tenant_phones = tenant["phones"]

            if day == last_day_of_prev_month:
                for phone in tenant_phones:
                    print(f"Sending reminder to {phone}")
                    send_sms(phone, f"Reminder: Your rent of {tenant['balance']} is due tomorrow on the {tenant['due_date']}.")
            elif day > LATE_FEE_START_DAY:
                days_late = day - LATE_FEE_START_DAY
                late_fee = days_late * LATE_FEE_PER_DAY
                for phone in tenant_phones:
                    print(f"Sending late fee notice to {phone}")
                    send_sms(phone, f"Your rent of {tenant['balance']} is past due. A late fee of ${late_fee} has been applied.")
        except Exception as e:
            print(f"Error processing tenant {tenant_key}: {str(e)}")
    print("Finished send_rent_reminders")

@app.route("/send_rent_reminders", methods=["GET"])
def trigger_rent_reminders():
    print("Triggering rent reminders")
    try:
        send_rent_reminders()
        print("Rent reminders sent successfully")
        return "Rent reminders sent!"
    except Exception as e:
        print(f"Error in send_rent_reminders: {str(e)}")
        return f"Error: {str(e)}", 500

if __name__ == "__main__":
    app.run(debug=True)
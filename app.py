# app.py
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
from config import Anthropic_key, BUSINESS_NAME, GOOGLE_CREDENTIALS_FILE, BUSINESS_EMAIL
from anthropic import Anthropic
import asyncio
import concurrent.futures
from datetime import datetime, timedelta
import json
import re
import os

# Google API imports
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

app = FastAPI()

# Initialize Claude client
client = Anthropic(api_key=Anthropic_key)

# Google API scopes
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.send'
]

# Track conversation per call
conversations = {}
booking_data = {}  # Store booking info per call

# Thread pool for async calls
executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

def get_google_service(service_name):
    """Authenticate and return Google service"""
    creds = None
    
    # Load existing credentials
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    # If no valid credentials, let user authorize
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Save credentials for next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    
    return build(service_name, 'v3', credentials=creds)

def find_available_slots(start_date=None, days_ahead=7):
    """Find available appointment slots"""
    try:
        service = get_google_service('calendar')
        
        # Default to tomorrow if no start date
        if not start_date:
            start_date = datetime.now() + timedelta(days=1)
        
        # Search for next 7 days
        end_date = start_date + timedelta(days=days_ahead)
        
        # Get busy times from calendar
        body = {
            "timeMin": start_date.isoformat() + 'Z',
            "timeMax": end_date.isoformat() + 'Z',
            "items": [{"id": "primary"}]
        }
        
        events_result = service.freebusy().query(body=body).execute()
        busy_times = events_result['calendars']['primary']['busy']
        
        # Generate available slots (9 AM - 5 PM, weekdays)
        available_slots = []
        current_date = start_date.date()
        
        for day in range(days_ahead):
            check_date = current_date + timedelta(days=day)
            
            # Skip weekends
            if check_date.weekday() >= 5:
                continue
                
            # Check each hour from 9 AM to 5 PM
            for hour in range(9, 17):
                slot_start = datetime.combine(check_date, datetime.min.time().replace(hour=hour))
                slot_end = slot_start + timedelta(hours=1)
                
                # Check if slot conflicts with busy times
                is_available = True
                for busy in busy_times:
                    busy_start = datetime.fromisoformat(busy['start'].replace('Z', '+00:00'))
                    busy_end = datetime.fromisoformat(busy['end'].replace('Z', '+00:00'))
                    
                    if (slot_start < busy_end) and (slot_end > busy_start):
                        is_available = False
                        break
                
                if is_available:
                    available_slots.append(slot_start)
        
        return available_slots[:10]  # Return first 10 available slots
        
    except Exception as e:
        print(f"Calendar error: {e}")
        return []

def book_appointment(start_time, duration_minutes, customer_name, customer_email, description="Phone consultation"):
    """Book an appointment in Google Calendar"""
    try:
        service = get_google_service('calendar')
        
        end_time = start_time + timedelta(minutes=duration_minutes)
        
        event = {
            'summary': f'Appointment with {customer_name}',
            'description': description,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': 'America/New_York',  # Adjust to your timezone
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'America/New_York',
            },
            'attendees': [
                {'email': customer_email},
                {'email': BUSINESS_EMAIL}
            ],
            'reminders': {
                'useDefault': True
            }
        }
        
        created_event = service.events().insert(calendarId='primary', body=event).execute()
        return created_event
        
    except Exception as e:
        print(f"Booking error: {e}")
        return None

def send_confirmation_email(customer_email, customer_name, appointment_time, event_id):
    """Send confirmation email via Gmail"""
    try:
        # Gmail uses v1, not v3
        creds = None
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(GoogleRequest())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
        
        service = build('gmail', 'v1', credentials=creds)
        
        # Format appointment time
        formatted_time = appointment_time.strftime("%A, %B %d, %Y at %I:%M %p")
        
        # Email content
        subject = f"Appointment Confirmation - {BUSINESS_NAME}"
        body = f"""
        Hi {customer_name},

        Thank you for scheduling an appointment with {BUSINESS_NAME}!

        Appointment Details:
        Date & Time: {formatted_time}
        Duration: 1 hour

        If you need to reschedule or cancel, please call us back.

        We look forward to speaking with you!

        Best regards,
        {BUSINESS_NAME} Team
        """
        
        # Create email message
        message = {
            'raw': create_message(BUSINESS_EMAIL, customer_email, subject, body)
        }
        
        sent_message = service.users().messages().send(userId='me', body=message).execute()
        return sent_message
        
    except Exception as e:
        print(f"Email error: {e}")
        return None

def create_message(sender, to, subject, body):
    """Create email message in base64 format"""
    import base64
    from email.mime.text import MIMEText
    
    message = MIMEText(body)
    message['to'] = to
    message['from'] = sender
    message['subject'] = subject
    
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return raw_message

def ask_claude_sync(call_sid: str, user_input: str) -> str:
    """Enhanced Claude call with calendar context"""
    try:
        if call_sid not in conversations:
            conversations[call_sid] = []

        conversations[call_sid].append({"role": "user", "content": user_input})
        
        # Check if user is asking about appointments
        appointment_keywords = ["appointment", "booking", "schedule", "available", "meet", "time", "calendar"]
        is_appointment_request = any(keyword in user_input.lower() for keyword in appointment_keywords)
        
        system_prompt = f"""You are a friendly receptionist for {BUSINESS_NAME}. 

        If the caller wants to:
        1. Check availability - ask what day they prefer
        2. Book appointment - get their name and email
        3. General inquiry - answer normally

        Keep responses SHORT (10-15 words max), natural and helpful.
        
        If they mention booking/scheduling, say "I can help with that! What day works for you?"
        If they give a day, say "Let me check our availability for [day]"
        If they want to book, ask "Great! Can I get your name and email address?"
        """
        
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            system=system_prompt,
            messages=conversations[call_sid],
            max_tokens=75,
            temperature=0.1
        )

        text = "".join(block.text for block in response.content).strip()
        conversations[call_sid].append({"role": "assistant", "content": text})
        return text

    except Exception as e:
        print("Claude API error:", e)
        return "Sorry, I couldn't get a response."

async def ask_claude(call_sid: str, user_input: str) -> str:
    """Async wrapper for Claude API call"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, ask_claude_sync, call_sid, user_input)

def extract_booking_info(text):
    """Extract name and email from user input"""
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    emails = re.findall(email_pattern, text)
    
    # Simple name extraction (everything before email or common patterns)
    name = ""
    if "my name is" in text.lower():
        name = text.lower().split("my name is")[-1].split()[0].title()
    elif "i'm" in text.lower():
        name = text.lower().split("i'm")[-1].split()[0].title()
    
    return {
        "name": name,
        "email": emails[0] if emails else ""
    }

# Initial greeting endpoint (same as before)
@app.post("/voice", response_class=PlainTextResponse)
async def voice_entry(request: Request):
    resp = VoiceResponse()
    gather = Gather(
        input="speech",
        action="/handle_speech",
        method="POST",
        speech_timeout="auto",
        speech_model="phone_call",
        language="en-US",
        enhanced=True,
        timeout=8
    )
    
    greeting_text = f"<speak><prosody rate='medium' pitch='medium'>Hi there! <break time='0.3s'/> Thanks for calling {BUSINESS_NAME}. <break time='0.5s'/> How can I help you today?</prosody></speak>"

    gather.say(greeting_text, voice="Polly.Joanna", language="en-US")
    resp.append(gather)
    resp.say("<speak><prosody rate='medium'>Sorry, I didn't catch that. <break time='0.3s'/> Could you tell me how I can help you?</prosody></speak>", voice="Polly.Joanna")
    resp.redirect("/voice")
    return PlainTextResponse(str(resp), media_type="application/xml")

@app.post("/handle_speech", response_class=PlainTextResponse)
async def handle_speech(request: Request):
    form = await request.form()
    call_sid = form["CallSid"]
    speech_text = form.get("SpeechResult", "").strip()
    print("Received speech:", speech_text)

    resp = VoiceResponse()

    if not speech_text:
        gather = Gather(
            input="speech",
            action="/handle_speech",
            method="POST",
            speech_timeout="auto",
            speech_model="phone_call",
            language="en-US",
            enhanced=True,
            timeout=8
        )
        gather.say("<speak><prosody rate='medium'>Sorry, I didn't hear anything. <break time='0.3s'/> Could you repeat that?</prosody></speak>", voice="Polly.Joanna")
        resp.append(gather)
        return PlainTextResponse(str(resp), media_type="application/xml")

    # Handle appointment booking flow
    if call_sid not in booking_data:
        booking_data[call_sid] = {"step": "initial"}
    
    booking_state = booking_data[call_sid]
    
    # Check for availability request
    if any(word in speech_text.lower() for word in ["available", "appointment", "book", "schedule"]):
        if booking_state["step"] == "initial":
            # Get available slots
            slots = find_available_slots()
            if slots:
                slot_text = ", ".join([slot.strftime("%A %B %d at %I %p") for slot in slots[:3]])
                response_text = f"I have openings: {slot_text}. Which time works for you?"
                booking_state["step"] = "time_selection"
                booking_state["available_slots"] = slots
            else:
                response_text = "I don't see any immediate openings. Let me transfer you to check other options."
        else:
            response_text = await ask_claude(call_sid, speech_text)
    
    # Handle time selection
    elif booking_state.get("step") == "time_selection":
        # Simple time matching (you can make this more sophisticated)
        selected_slot = None
        for slot in booking_state.get("available_slots", []):
            if slot.strftime("%A").lower() in speech_text.lower() or slot.strftime("%d") in speech_text:
                selected_slot = slot
                break
        
        if selected_slot:
            booking_state["selected_time"] = selected_slot
            booking_state["step"] = "get_details"
            response_text = "Perfect! Can I get your name and email address for the appointment?"
        else:
            response_text = "I didn't catch which time you prefer. Could you repeat that?"
    
    # Handle getting customer details
    elif booking_state.get("step") == "get_details":
        booking_info = extract_booking_info(speech_text)
        
        if booking_info["email"] and booking_info["name"]:
            # Book the appointment
            appointment_time = booking_state["selected_time"]
            event = book_appointment(appointment_time, 60, booking_info["name"], booking_info["email"])
            
            if event:
                # Send confirmation email
                send_confirmation_email(
                    booking_info["email"], 
                    booking_info["name"], 
                    appointment_time,
                    event.get('id')
                )
                
                formatted_time = appointment_time.strftime("%A, %B %d at %I:%M %p")
                response_text = f"Great! I've booked your appointment for {formatted_time}. You'll receive a confirmation email shortly."
                booking_state["step"] = "completed"
            else:
                response_text = "Sorry, there was an issue booking your appointment. Let me transfer you to someone who can help."
        else:
            response_text = "I need both your name and email. Could you provide both please?"
    
    else:
        # Regular conversation flow
        end_phrases = ["cool", "sounds good", "thank you", "thanks", "that's all", "bye", "goodbye"]
        if any(phrase in speech_text.lower() for phrase in end_phrases):
            resp.say("Great, glad we're all set! Goodbye!", voice="Polly.Joanna")
            resp.hangup()
            return PlainTextResponse(str(resp), media_type="application/xml")
        
        response_text = await ask_claude(call_sid, speech_text)

    # Speak the response
    resp.say(response_text, voice="Polly.Joanna")

    # Continue conversation
    gather = Gather(
        input="speech",
        action="/handle_speech",
        method="POST",
        speech_timeout="auto",
        speech_model="phone_call",
        language="en-US",
        enhanced=True,
        timeout=8
    )
    gather.say("", voice="Polly.Joanna")
    resp.append(gather)

    return PlainTextResponse(str(resp), media_type="application/xml")

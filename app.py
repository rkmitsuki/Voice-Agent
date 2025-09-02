# app.py
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
from config import Anthropic_key, BUSINESS_NAME
from anthropic import Anthropic
import time
import asyncio
import concurrent.futures

app = FastAPI()

# Initialize Claude client
client = Anthropic(api_key=Anthropic_key)

# Track conversation per call
conversations = {}

# Thread pool for async Claude calls
executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

def ask_claude_sync(call_sid: str, user_input: str) -> str:
    """
    Synchronous Claude call for threading
    """
    try:
        if call_sid not in conversations:
            conversations[call_sid] = []

        conversations[call_sid].append({"role": "user", "content": user_input})

        # Optimized Claude call with reduced max_tokens for faster response
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            system="You are a friendly receptionist on the phone. Respond as if speaking to a real person. Keep responses SHORT (10-15 words max for first response), but use natural spoken language. Be conversational and helpful.",
            messages=conversations[call_sid],
            max_tokens=75,  # Even smaller for first response speed
            temperature=0.1  # Slight randomness for more natural feel
        )

        text = "".join(block.text for block in response.content).strip()
        conversations[call_sid].append({"role": "assistant", "content": text})
        return text

    except Exception as e:
        print("Claude API error:", e)
        return "Sorry, I couldn't get a response."

async def ask_claude(call_sid: str, user_input: str) -> str:
    """
    Async wrapper for Claude API call
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, ask_claude_sync, call_sid, user_input)

# Initial greeting endpoint
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
        timeout=8  # Give caller time to start speaking
    )
    
    # More natural greeting with pauses and prosody
    greeting_text = f"<speak><prosody rate='medium' pitch='medium'>Hi there! <break time='0.3s'/> Thanks for calling {BUSINESS_NAME}. <break time='0.5s'/> How can I help you today?</prosody></speak>"

    gather.say(
        greeting_text,
        voice="Polly.Joanna",
        language="en-US"
    )

    resp.append(gather)
    # More natural fallback
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

    # Check for end phrases
    end_phrases = ["cool", "sounds good", "thank you", "thanks", "that's all", "bye", "goodbye"]
    if any(phrase in speech_text.lower() for phrase in end_phrases):
        resp.say("Great, glad we're all set! Goodbye!", voice="Polly.Joanna")
        resp.hangup()
        return PlainTextResponse(str(resp), media_type="application/xml")

    # Get Claude's response asynchronously for faster processing
    answer = await ask_claude(call_sid, speech_text)

    # Speak the answer with better voice
    resp.say(answer, voice="Polly.Joanna")

    # Continue conversation
    gather = Gather(
        input="speech",
        action="/handle_speech",
        method="POST",
        speech_timeout="auto",
        speech_model="phone_call",
        language="en-US",
        enhanced=True
    )
    gather.say("", voice="Polly.Joanna")  # Silent prompt to keep listening
    resp.append(gather)

    return PlainTextResponse(str(resp), media_type="application/xml")
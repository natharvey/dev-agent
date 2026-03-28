import os

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import Response
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse

import agent
from fastapi import FastAPI

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")
ALLOWED_NUMBER = os.getenv("ALLOWED_WHATSAPP_NUMBER", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
REPOS_DIR = os.path.expanduser(os.getenv("REPOS_DIR", "./repos"))

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)

app = FastAPI()

WHATSAPP_MAX_LEN = 1500

HELP_TEXT = """Dev Agent — available commands:
/reset — clear conversation history
/help — show this message

Otherwise, just talk to me. I can:
• Run shell commands on your Mac
• Read, write, and search files
• Clone GitHub repos
• Commit, push, and create PRs
• Run tests, installs, builds
• Answer questions about your code

Example: "clone https://github.com/you/myapp and run the tests"
"""


def send_whatsapp(to: str, body: str) -> None:
    """Send a WhatsApp message, splitting if over the character limit."""
    chunks = [body[i : i + WHATSAPP_MAX_LEN] for i in range(0, len(body), WHATSAPP_MAX_LEN)]
    for chunk in chunks:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to,
            body=chunk,
        )


def validate_twilio_signature(request_url: str, params: dict, signature: str) -> bool:
    return twilio_validator.validate(request_url, params, signature)


async def handle_message(from_number: str, body: str) -> None:
    """Background task: run the agent and send reply via Twilio outbound API."""
    try:
        if agent.is_processing(from_number):
            send_whatsapp(from_number, "Still working on your previous request...")
            return

        reply = await agent.process_message(from_number, body)
        send_whatsapp(from_number, reply)
    except Exception as e:
        send_whatsapp(from_number, f"Internal error: {type(e).__name__}: {e}")


@app.on_event("startup")
async def startup():
    os.makedirs(REPOS_DIR, exist_ok=True)


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(...),
):
    # Validate Twilio signature
    if WEBHOOK_URL:
        params = dict(await request.form())
        signature = request.headers.get("X-Twilio-Signature", "")
        if not validate_twilio_signature(WEBHOOK_URL, params, signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    # Whitelist check — silent reject for unknown numbers
    if From != ALLOWED_NUMBER:
        return Response(content="<Response/>", media_type="application/xml")

    message = Body.strip()

    # Handle special commands immediately (no Claude call)
    if message.startswith("/"):
        cmd = message.lower().split()[0]
        if cmd == "/reset":
            agent.reset_session(From)
            background_tasks.add_task(send_whatsapp, From, "Conversation cleared.")
        elif cmd == "/help":
            background_tasks.add_task(send_whatsapp, From, HELP_TEXT)
        else:
            background_tasks.add_task(send_whatsapp, From, f"Unknown command: {cmd}. Try /help.")
        return Response(content="<Response/>", media_type="application/xml")

    # Schedule agent processing as background task — respond to Twilio immediately
    background_tasks.add_task(handle_message, From, message)

    return Response(content="<Response/>", media_type="application/xml")


@app.get("/health")
async def health():
    return {"status": "ok"}

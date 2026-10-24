
import json
import os
import re
import shutil
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from pathlib import Path

from flask import Flask, jsonify, request, Response
from functools import wraps
import secrets
import base64
import threading
import time
from openai import OpenAI

app = Flask(__name__)
QR_ACCESS_USER = os.getenv("QR_ACCESS_USER", "")
QR_ACCESS_PASSWORD = os.getenv("QR_ACCESS_PASSWORD", "")


def require_qr_auth(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        auth = request.authorization

        if not QR_ACCESS_USER or not QR_ACCESS_PASSWORD:
            return "QR access is not configured.", 503

        if (
            not auth
            or not secrets.compare_digest(auth.username or "", QR_ACCESS_USER)
            or not secrets.compare_digest(auth.password or "", QR_ACCESS_PASSWORD)
        ):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="WhatsApp QR"'},
            )

        return func(*args, **kwargs)
    return wrapper


@app.get("/qr")
@require_qr_auth
def qr_page():
    return """
    <!doctype html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>WhatsApp QR Setup</title>
    </head>
    <body style="font-family:Arial;max-width:600px;margin:40px auto;padding:16px">
      <h2>WhatsApp QR Setup</h2>
      <p>This page is protected, but the WhatsApp browser session is not connected yet.</p>
      <p>The next step is to connect Selenium and display its live QR code here.</p>
    </body>
    </html>
    """
def create_chrome_driver():
    options = Options()
    options.binary_location = os.getenv(
        "CHROME_BIN", "/usr/bin/chromium"
    )

    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")

    driver_path = os.getenv(
        "CHROMEDRIVER_PATH"
    ) or shutil.which("chromedriver")

    service = Service(executable_path=driver_path) if driver_path else Service()
        return webdriver.Chrome(service=service, options=options)


# WhatsApp browser session manager
whatsapp_driver = None
whatsapp_lock = threading.Lock()
whatsapp_status = "Not started"
whatsapp_qr_data = None


def start_whatsapp_browser():
    global whatsapp_driver, whatsapp_status, whatsapp_qr_data

    with whatsapp_lock:
        if whatsapp_driver is not None:
            return

        whatsapp_status = "Starting Chromium..."
        try:
            driver = create_chrome_driver()
            driver.get("https://web.whatsapp.com/")
            whatsapp_driver = driver
            whatsapp_status = "Waiting for WhatsApp Web login"
        except Exception:
            app.logger.exception("WhatsApp browser startup failed")
            whatsapp_status = "Browser startup failed"
# OpenAI configuration
api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=api_key) if api_key else None

# Authorized WhatsApp administrators
ADMIN_NUMBERS = {
    "+918004789374",
    "+917039630284",
    "+919063420648",
}

# Persistent storage. Set STATE_FILE to a persistent disk path
# when deploying to a server with persistent storage.
STATE_FILE = Path(os.getenv("STATE_FILE", "bot_state.json"))

DEFAULT_STATE = {
    "global_enabled": False,
    "enabled_chats": [],
    "disabled_chats": [],
}


def load_state():
    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            saved = json.load(file)
        return {**DEFAULT_STATE, **saved}
    except (OSError, json.JSONDecodeError):
        return DEFAULT_STATE.copy()


state = load_state()


def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = STATE_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(STATE_FILE)


def normalize_number(value):
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return None
    return "+" + digits


def is_enabled(number):
    if not state["global_enabled"]:
        return False
    if number in state["disabled_chats"]:
        return False
    if number in state["enabled_chats"]:
        return True
    return False


def handle_admin_command(sender, command):
    """Handle commands only from authorized admins."""
    sender = normalize_number(sender)
    if sender not in ADMIN_NUMBERS:
        return "Unauthorized: admin commands are restricted."

    parts = command.strip().split(maxsplit=1)
    action = parts[0].lower()
    target = parts[1].strip() if len(parts) > 1 else None

    if action == "/status":
        if target:
            number = normalize_number(target)
            if not number:
                return "Usage: /status +countrycode_number"
            status = "ON" if is_enabled(number) else "OFF"
            return f"Auto-replies for {number}: {status}"

        enabled = sorted(
            number for number in state["enabled_chats"]
            if is_enabled(number)
        )
        if not state["global_enabled"]:
            return "Global auto-reply: OFF\nNo replies are currently active."
        if not enabled:
            return "Global auto-reply: ON\nNo chats are enabled."
        return (
            "WhatsApp AI Auto-Reply Status\n"
            f"Global mode: ON\nEnabled chats: {len(enabled)}\n\n"
            + "\n".join(enabled)
        )

    if action in ("/on", "/off"):
        if not target:
            return f"Usage: {action} +countrycode_number (or all)"

        enable = action == "/on"

        if target.lower() == "all":
            state["global_enabled"] = enable
            if enable:
                return (
                    "Global mode ON. Only individually enabled chats "
                    "will receive AI replies."
                )
            return "Global mode OFF. All AI auto-replies are paused."

        number = normalize_number(target)
        if not number:
            return "Please provide a valid phone number."

        if enable:
            if number not in state["enabled_chats"]:
                state["enabled_chats"].append(number)
            state["disabled_chats"] = [
                n for n in state["disabled_chats"] if n != number
            ]
            # Enabling a chat also enables global mode.
            state["global_enabled"] = True
            result = f"Auto-replies enabled for {number}."
        else:
            state["disabled_chats"] = list(
                set(state["disabled_chats"] + [number])
            )
            result = f"Auto-replies disabled for {number}."

        save_state()
        return result

    return "Unknown command. Use /on, /off, or /status."


def generate_reply(message):
    if client is None:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    response = client.responses.create(
        model="gpt-4.1-mini",
        instructions=(
            "You are a friendly AI assistant replying to incoming "
            "WhatsApp messages. Be concise and natural. Use the same "
            "language as the sender. Do not claim to be the account owner."
        ),
        input=message,
    )
    return response.output_text.strip()


@app.get("/")
def home():
    return "WhatsApp AI service is running. WhatsApp integration is not connected."


@app.get("/health")
def health():
    return jsonify({"ok": True, "whatsapp_connected": False})


# Temporary testing endpoint.
# Do not expose admin commands to an unauthenticated public endpoint.
@app.post("/admin-command")
def admin_command():
    data = request.get_json(silent=True) or {}
    sender = data.get("sender", "")
    command = data.get("command", "")

    if not isinstance(command, str):
        return jsonify({"error": "Invalid command"}), 400

    return jsonify({"result": handle_admin_command(sender, command)})


@app.post("/reply")
def reply():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    number = normalize_number(data.get("sender", ""))

    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "Please provide a message."}), 400

    if not number or not is_enabled(number):
        return jsonify({"reply": None, "reason": "Auto-replies are OFF"})

    try:
        return jsonify({"reply": generate_reply(message)})
    except Exception:
        app.logger.exception("AI reply generation failed")
        return jsonify({"error": "Could not generate a reply"}), 500
@app.get("/browser-test")
def browser_test():
    driver = None
    try:
        driver = create_chrome_driver()
        driver.get("https://example.com")
        return jsonify({
            "ok": True,
            "title": driver.title,
            "browser": "Chromium started successfully"
        })
    except Exception:
        app.logger.exception("Chromium test failed")
        return jsonify({
            "ok": False,
            "error": "Chromium could not start; check Render logs."
        }), 500
    finally:
        if driver:
            driver.quit()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
    

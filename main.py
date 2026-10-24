"""WhatsApp Web AI auto-reply service (unofficial Selenium automation).

Required Render environment variables:
  OPENAI_API_KEY       OpenAI API key
  QR_ACCESS_USER       Username for protected web/admin endpoints
  QR_ACCESS_PASSWORD   Strong password for protected web/admin endpoints

Optional:
  OPENAI_MODEL          Defaults to gpt-4.1-mini
  STATE_FILE            Defaults to /tmp/whatsapp_bot_state.json
  CHROME_BIN            Defaults to /usr/bin/chromium
  CHROMEDRIVER_PATH     Defaults to /usr/bin/chromedriver

Important:
- This uses unofficial WhatsApp Web browser automation, which may break and may
  expose the account to WhatsApp restrictions. Use only for personal, low-volume
  conversations; do not bulk-message people.
- Render Free services sleep and have ephemeral storage. Login/session and state
  may be lost after sleep/redeploy/restart. This is not an always-on guarantee.
- WhatsApp Web's DOM changes frequently. Incoming-chat detection below is best
  effort and must be tested with a non-sensitive test contact before relying on it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
from openai import OpenAI
from selenium import webdriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
)
log = logging.getLogger("whatsapp-auto-ai")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

ADMIN_NUMBERS = {
    "+918004789374",
    "+917039630284",
    "+919063420648",
}

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
STATE_FILE = Path(os.getenv("STATE_FILE", "/tmp/whatsapp_bot_state.json"))
QR_USER = os.getenv("QR_ACCESS_USER", "")
QR_PASSWORD = os.getenv("QR_ACCESS_PASSWORD", "")

api_key = os.getenv("OPENAI_API_KEY", "")
openai_client = OpenAI(api_key=api_key) if api_key else None

state_lock = threading.RLock()
whatsapp_lock = threading.RLock()

# Persistent only for the lifetime of this container unless a persistent disk is
# attached. Render Free's filesystem is ephemeral.
state: dict[str, Any] = {
    "enabled_contacts": [],
    "reply_all": False,
    "last_updated": None,
}

whatsapp_driver = None
whatsapp_status = "Not started"
whatsapp_qr_data: str | None = None
whatsapp_starting = False
whatsapp_logged_in = False
whatsapp_worker_thread: threading.Thread | None = None
processed_message_ids: set[str] = set()
processed_ids_lock = threading.Lock()


def normalize_phone(value: Any) -> str:
    """Normalize a phone number to digits with a leading + when possible."""
    if value is None:
        return ""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return f"+{digits}" if digits else ""


def load_state() -> None:
    global state
    try:
        if STATE_FILE.exists():
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            enabled = loaded.get("enabled_contacts", [])
            with state_lock:
                state = {
                    "enabled_contacts": sorted(
                        {normalize_phone(number) for number in enabled if normalize_phone(number)}
                    ),
                    "reply_all": bool(loaded.get("reply_all", False)),
                    "last_updated": loaded.get("last_updated"),
                }
    except Exception:
        log.exception("Could not load state; starting with empty settings")


def save_state() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with state_lock:
            snapshot = dict(state)
        temp_file = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        temp_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        temp_file.replace(STATE_FILE)
    except Exception:
        log.exception("Could not save state")


def update_state(mutator) -> None:
    with state_lock:
        mutator()
        state["enabled_contacts"] = sorted(
            {normalize_phone(n) for n in state.get("enabled_contacts", []) if normalize_phone(n)}
        )
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_state()


def protected_endpoint(fn):
    """Require HTTP Basic Auth for admin and QR routes."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not QR_USER or not QR_PASSWORD:
            return jsonify({
                "error": "Protected endpoints are disabled. Set QR_ACCESS_USER and QR_ACCESS_PASSWORD in Render environment variables."
            }), 503
        auth = request.authorization
        if (
            not auth
            or not secrets_compare(auth.username or "", QR_USER)
            or not secrets_compare(auth.password or "", QR_PASSWORD)
        ):
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="WhatsApp Auto AI"'},
            )
        return fn(*args, **kwargs)
    return wrapper


def secrets_compare(a: str, b: str) -> bool:
    # compare_digest avoids leaking password comparisons by timing.
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def create_chrome_driver():
    options = Options()
    options.binary_location = os.getenv("CHROME_BIN", "/usr/bin/chromium")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,1100")
    options.add_argument("--lang=en-US")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--user-data-dir=/tmp/whatsapp-chrome-profile")

    driver_path = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")
    service = Service(executable_path=driver_path)
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    return driver


def set_whatsapp_status(status: str) -> None:
    global whatsapp_status
    with whatsapp_lock:
        whatsapp_status = status
    log.info("WhatsApp status: %s", status)


def is_logged_in(driver) -> bool:
    # These selectors are not an official API and may change.
    selectors = [
        '[aria-label="Chat list"]',
        '[data-testid="chat-list"]',
        'div[role="grid"]',
    ]
    for selector in selectors:
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
        except WebDriverException:
            pass
    return False


def find_qr_image_data(driver) -> str | None:
    """Try to capture a visible QR canvas as PNG data URL payload."""
    try:
        canvases = driver.find_elements(By.CSS_SELECTOR, "canvas")
        candidates = []
        for canvas in canvases:
            try:
                if not canvas.is_displayed():
                    continue
                size = canvas.size or {}
                width = int(size.get("width", 0))
                height = int(size.get("height", 0))
                if width >= 120 and height >= 120:
                    candidates.append((width * height, canvas))
            except (StaleElementReferenceException, WebDriverException):
                continue
        if not candidates:
            return None
        # The QR is normally one of the largest visible square canvases.
        candidates.sort(key=lambda item: item[0], reverse=True)
        canvas = candidates[0][1]
        png = canvas.screenshot_as_png
        return base64.b64encode(png).decode("ascii")
    except WebDriverException:
        log.debug("Unable to capture QR canvas", exc_info=True)
        return None


def start_whatsapp_browser() -> dict[str, Any]:
    """Start exactly one browser worker; safe to call repeatedly from routes."""
    global whatsapp_starting, whatsapp_worker_thread
    with whatsapp_lock:
        if whatsapp_driver is not None and whatsapp_logged_in:
            return {"status": whatsapp_status, "started": False}
        if whatsapp_starting and whatsapp_worker_thread and whatsapp_worker_thread.is_alive():
            return {"status": whatsapp_status, "started": False}
        whatsapp_starting = True
        whatsapp_worker_thread = threading.Thread(
            target=whatsapp_browser_worker,
            name="whatsapp-browser-worker",
            daemon=True,
        )
        whatsapp_worker_thread.start()
        return {"status": "Starting WhatsApp Web", "started": True}


def whatsapp_browser_worker() -> None:
    global whatsapp_driver, whatsapp_starting, whatsapp_qr_data, whatsapp_logged_in
    driver = None
    try:
        set_whatsapp_status("Starting Chromium")
        driver = create_chrome_driver()
        with whatsapp_lock:
            whatsapp_driver = driver
            whatsapp_qr_data = None
            whatsapp_logged_in = False
        set_whatsapp_status("Opening WhatsApp Web")
        driver.get("https://web.whatsapp.com/")
        set_whatsapp_status("Waiting for QR scan or existing login")

        deadline = time.time() + 180
        last_qr_capture = 0.0
        while time.time() < deadline:
            if is_logged_in(driver):
                with whatsapp_lock:
                    whatsapp_logged_in = True
                    whatsapp_qr_data = None
                set_whatsapp_status("Logged in; monitoring chats")
                whatsapp_chat_monitor_loop(driver)
                # Monitor loop exits only if the browser becomes unavailable.
                break

            if time.time() - last_qr_capture >= 3:
                qr_data = find_qr_image_data(driver)
                if qr_data:
                    with whatsapp_lock:
                        whatsapp_qr_data = qr_data
                    set_whatsapp_status("QR code available; scan it with WhatsApp on your phone")
                    last_qr_capture = time.time()
            time.sleep(2)

        if not whatsapp_logged_in:
            set_whatsapp_status("QR/login not detected within 3 minutes; refresh the QR page to retry")
    except Exception as exc:
        log.exception("WhatsApp browser worker failed")
        set_whatsapp_status(f"Browser error: {type(exc).__name__}: {str(exc)[:180]}")
    finally:
        with whatsapp_lock:
            if whatsapp_driver is driver:
                whatsapp_driver = None
            whatsapp_starting = False
            whatsapp_logged_in = False
            whatsapp_qr_data = None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        log.info("WhatsApp browser worker stopped")


def get_chat_rows(driver):
    """Return likely chat-list rows; selectors are best-effort."""
    selectors = [
        '[data-testid="cell-frame-container"]',
        'div[role="listitem"]',
    ]
    for selector in selectors:
        try:
            rows = driver.find_elements(By.CSS_SELECTOR, selector)
            if rows:
                return rows
        except WebDriverException:
            continue
    return []


def row_has_unread_badge(row) -> bool:
    selectors = [
        '[data-testid="icon-unread-count"]',
        'span[aria-label*="unread"]',
        'span[aria-label*="unread message"]',
    ]
    for selector in selectors:
        try:
            if row.find_elements(By.CSS_SELECTOR, selector):
                return True
        except (StaleElementReferenceException, WebDriverException):
            continue
    # Some WhatsApp versions expose unread counts as aria-label text.
    try:
        label = (row.get_attribute("aria-label") or "").lower()
        if "unread" in label:
            return True
    except WebDriverException:
        pass
    return False


def chat_title_from_row(row) -> str:
    for selector in ['[data-testid="cell-frame-title"]', 'span[title]']:
        try:
            elements = row.find_elements(By.CSS_SELECTOR, selector)
            for element in elements:
                value = (element.get_attribute("title") or element.text or "").strip()
                if value:
                    return value
        except (StaleElementReferenceException, WebDriverException):
            pass
    try:
        return (row.text or "").splitlines()[0].strip()
    except WebDriverException:
        return ""


def chat_phone_from_header(driver) -> str:
    """Best-effort extract phone from the open chat's header/title."""
    candidates = []
    selectors = [
        'header span[title]',
        'header [data-testid="conversation-info-header-chat-title"]',
        'header [title]',
    ]
    for selector in selectors:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                for value in [element.get_attribute("title"), element.text]:
                    if value and value.strip():
                        candidates.append(value.strip())
        except WebDriverException:
            pass
    for candidate in candidates:
        phone = normalize_phone(candidate)
        # Avoid interpreting ordinary names as numbers; require 8+ digits.
        digits = "".join(ch for ch in candidate if ch.isdigit())
        if len(digits) >= 8:
            return phone
    return ""


def get_message_bubbles(driver):
    try:
        bubbles = driver.find_elements(
            By.CSS_SELECTOR,
            "div.message-in, div.message-out",
        )
        if bubbles:
            return bubbles
    except WebDriverException:
        pass
    return []


def extract_message_text(bubble) -> str:
    selectors = [
        '[data-testid="selectable-text"]',
        'span.selectable-text',
    ]
    for selector in selectors:
        try:
            elements = bubble.find_elements(By.CSS_SELECTOR, selector)
            text_parts = [e.text.strip() for e in elements if e.text and e.text.strip()]
            if text_parts:
                return "\n".join(text_parts).strip()
        except (StaleElementReferenceException, WebDriverException):
            pass
    return ""


def message_fingerprint(bubble, chat_title: str, message_text: str) -> str:
    metadata = ""
    for selector in ["[data-pre-plain-text]", "[data-testid='msg-meta']"]:
        try:
            elements = bubble.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                metadata = elements[0].get_attribute("data-pre-plain-text") or elements[0].text or ""
                break
        except (StaleElementReferenceException, WebDriverException):
            pass
    raw = f"{chat_title}|{metadata}|{message_text}"
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def click_chat_row(row) -> bool:
    try:
        row.click()
        return True
    except (StaleElementReferenceException, WebDriverException):
        try:
            row.find_element(By.CSS_SELECTOR, "[role='gridcell']").click()
            return True
        except Exception:
            return False


def send_whatsapp_message(driver, message: str) -> bool:
    """Send a reply into the currently open chat."""
    selectors = [
        'footer [contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"][data-tab]',
        'footer div[contenteditable="true"]',
    ]
    composer = None
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            visible = [e for e in elements if e.is_displayed()]
            if visible:
                composer = visible[-1]
                break
        except WebDriverException:
            continue
    if composer is None:
        log.warning("Message composer not found; reply not sent")
        return False
    try:
        composer.click()
        composer.send_keys(message)
        composer.send_keys("\n")
        time.sleep(1)
        return True
    except WebDriverException:
        log.exception("Could not send WhatsApp message")
        return False


def handle_admin_command(sender: str, command: str) -> str:
    """Handle commands only when sender is one of the configured admins."""
    sender_number = normalize_phone(sender)
    if sender_number not in ADMIN_NUMBERS:
        return ""

    parts = (command or "").strip().split(maxsplit=1)
    if not parts:
        return "Commands: /on +number, /off +number, /on all, /off all, /status"
    action = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if action == "/on" and argument.lower() == "all":
        update_state(lambda: state.update({"reply_all": True}))
        return "Auto-reply enabled for all chats. Use /off all to disable."

    if action == "/off" and argument.lower() == "all":
        update_state(lambda: state.update({"reply_all": False}))
        return "Auto-reply disabled for all chats."

    if action == "/on":
        number = normalize_phone(argument)
        if len("".join(ch for ch in number if ch.isdigit())) < 8:
            return "Usage: /on +countrycode-number (example: /on +919876543210)"
        def enable_contact():
            if number not in state["enabled_contacts"]:
                state["enabled_contacts"].append(number)
        update_state(enable_contact)
        return f"Auto-reply enabled for {number}."

    if action == "/off":
        number = normalize_phone(argument)
        if len("".join(ch for ch in number if ch.isdigit())) < 8:
            return "Usage: /off +countrycode-number (example: /off +919876543210)"
        update_state(lambda: state.update({
            "enabled_contacts": [n for n in state["enabled_contacts"] if n != number]
        }))
        return f"Auto-reply disabled for {number}."

    if action == "/status":
        with state_lock:
            enabled = list(state["enabled_contacts"])
            reply_all = state["reply_all"]
        if reply_all:
            return "Auto-reply: ON for all chats."
        if enabled:
            return "Auto-reply enabled for:\n" + "\n".join(enabled)
        return "Auto-reply is OFF for all contacts. Use /on +number to enable a contact."

    return "Unknown command. Use /on +number, /off +number, /on all, /off all, or /status."


def should_reply_to(sender_number: str) -> bool:
    if not sender_number:
        return False
    if sender_number in ADMIN_NUMBERS:
        return False
    with state_lock:
        return bool(state.get("reply_all")) or sender_number in state.get("enabled_contacts", [])



def generate_ai_reply(message: str) -> str:
    if not openai_client:
        return "Sorry, my AI reply service is not configured right now."

    try:
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            instructions=(
                "You are a friendly personal WhatsApp assistant. "
                "Reply naturally and briefly, usually in 1-3 sentences. "
                "Match the language and tone of the incoming message. "
                "Do not claim to be the account owner. "
                "Do not send unsolicited follow-up messages. "
                "If a request involves urgent danger, advise contacting "
                "local emergency services."
            ),
            input=message[:6000],
        )

        result = (getattr(response, "output_text", "") or "").strip()

        if result:
            return result[:3500]

        return "Sorry, I couldn't prepare a reply just now."

    except Exception:
        log.exception("OpenAI reply generation failed")
        return (
            "Sorry, I'm unable to reply right now. "
            "I'll get back to you later."
        )
          

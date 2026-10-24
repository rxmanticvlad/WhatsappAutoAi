from __future__ import annotations

import base64
import hashlib
import hmac
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
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
)
log = logging.getLogger("whatsapp-auto-ai")

app = Flask(__name__)

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
processed_ids_lock = threading.Lock()

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


def normalize_phone(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return f"+{digits}" if digits else ""


def load_state() -> None:
    global state

    try:
        if STATE_FILE.exists():
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            enabled = loaded.get("enabled_contacts", [])

            with state_lock:
                state = {
                    "enabled_contacts": sorted({
                        normalize_phone(number)
                        for number in enabled
                        if normalize_phone(number)
                    }),
                    "reply_all": bool(loaded.get("reply_all", False)),
                    "last_updated": loaded.get("last_updated"),
                }
    except Exception:
        log.exception("Could not load state")


def save_state() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        with state_lock:
            snapshot = dict(state)

        temp_file = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        temp_file.write_text(
            json.dumps(snapshot, indent=2),
            encoding="utf-8",
        )
        temp_file.replace(STATE_FILE)

    except Exception:
        log.exception("Could not save state")


def update_state(mutator) -> None:
    with state_lock:
        mutator()
        state["enabled_contacts"] = sorted({
            normalize_phone(number)
            for number in state.get("enabled_contacts", [])
            if normalize_phone(number)
        })
        state["last_updated"] = datetime.now(timezone.utc).isoformat()

    save_state()


def secrets_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(
        a.encode("utf-8"),
        b.encode("utf-8"),
    )


def protected_endpoint(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not QR_USER or not QR_PASSWORD:
            return jsonify({
                "error": "Set QR_ACCESS_USER and QR_ACCESS_PASSWORD in Render."
            }), 503

        auth = request.authorization

        if (
            not auth
            or not secrets_compare(auth.username or "", QR_USER)
            or not secrets_compare(auth.password or "", QR_PASSWORD)
        ):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="WhatsApp Auto AI"'},
            )

        return fn(*args, **kwargs)

    return wrapper


def create_chrome_driver():
    options = Options()
    options.binary_location = os.getenv(
        "CHROME_BIN",
        "/usr/bin/chromium",
    )

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

    driver_path = os.getenv(
        "CHROMEDRIVER_PATH",
        "/usr/bin/chromedriver",
    )

    driver = webdriver.Chrome(
        service=Service(executable_path=driver_path),
        options=options,
    )
    driver.set_page_load_timeout(60)

    return driver


def set_whatsapp_status(status: str) -> None:
    global whatsapp_status

    with whatsapp_lock:
        whatsapp_status = status

    log.info("WhatsApp status: %s", status)


def is_logged_in(driver) -> bool:
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

        candidates.sort(key=lambda item: item[0], reverse=True)
        png = candidates[0][1].screenshot_as_png

        return base64.b64encode(png).decode("ascii")

    except WebDriverException:
        log.debug("Could not capture QR code", exc_info=True)
        return None


def start_whatsapp_browser() -> dict[str, Any]:
    global whatsapp_starting, whatsapp_worker_thread

    with whatsapp_lock:
        if whatsapp_driver is not None and whatsapp_logged_in:
            return {"status": whatsapp_status, "started": False}

        if (
            whatsapp_starting
            and whatsapp_worker_thread
            and whatsapp_worker_thread.is_alive()
        ):
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
    global whatsapp_driver
    global whatsapp_starting
    global whatsapp_qr_data
    global whatsapp_logged_in

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
                break

            if time.time() - last_qr_capture >= 3:
                qr_data = find_qr_image_data(driver)

                if qr_data:
                    with whatsapp_lock:
                        whatsapp_qr_data = qr_data

                    set_whatsapp_status(
                        "QR code available; scan it with WhatsApp"
                    )
                    last_qr_capture = time.time()

            time.sleep(2)

        if not whatsapp_logged_in:
            set_whatsapp_status(
                "Login not detected within 3 minutes; refresh the QR page"
            )

    except Exception as exc:
        log.exception("WhatsApp browser worker failed")
        set_whatsapp_status(
            f"Browser error: {type(exc).__name__}: {str(exc)[:180]}"
        )

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
    for selector in [
        '[data-testid="cell-frame-container"]',
        'div[role="listitem"]',
    ]:
        try:
            rows = driver.find_elements(By.CSS_SELECTOR, selector)
            if rows:
                return rows
        except WebDriverException:
            pass

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
            pass

    try:
        label = (row.get_attribute("aria-label") or "").lower()
        return "unread" in label
    except WebDriverException:
        return False


def chat_title_from_row(row) -> str:
    for selector in [
        '[data-testid="cell-frame-title"]',
        "span[title]",
    ]:
        try:
            for element in row.find_elements(By.CSS_SELECTOR, selector):
                value = (
                    element.get_attribute("title") or element.text or ""
                ).strip()

                if value:
                    return value
        except (StaleElementReferenceException, WebDriverException):
            pass

    try:
        return (row.text or "").splitlines()[0].strip()
    except WebDriverException:
        return ""


def chat_phone_from_header(driver) -> str:
    candidates = []

    selectors = [
        "header span[title]",
        'header [data-testid="conversation-info-header-chat-title"]',
        "header [title]",
    ]

    for selector in selectors:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                for value in [
                    element.get_attribute("title"),
                    element.text,
                ]:
                    if value and value.strip():
                        candidates.append(value.strip())
        except WebDriverException:
            pass

    for candidate in candidates:
        digits = "".join(ch for ch in candidate if ch.isdigit())

        if len(digits) >= 8:
            return normalize_phone(candidate)

    return ""


def get_message_bubbles(driver):
    try:
        return driver.find_elements(
            By.CSS_SELECTOR,
            "div.message-in, div.message-out",
        )
    except WebDriverException:
        return []


def extract_message_text(bubble) -> str:
    for selector in [
        '[data-testid="selectable-text"]',
        "span.selectable-text",
    ]:
        try:
            elements = bubble.find_elements(By.CSS_SELECTOR, selector)
            parts = [
                element.text.strip()
                for element in elements
                if element.text and element.text.strip()
            ]

            if parts:
                return "\n".join(parts).strip()

        except (StaleElementReferenceException, WebDriverException):
            pass

    return ""


def message_fingerprint(bubble, chat_title: str, message_text: str) -> str:
    metadata = ""

    for selector in [
        "[data-pre-plain-text]",
        "[data-testid='msg-meta']",
    ]:
        try:
            elements = bubble.find_elements(By.CSS_SELECTOR, selector)

            if elements:
                metadata = (
                    elements[0].get_attribute("data-pre-plain-text")
                    or elements[0].text
                    or ""
                )
                break

        except (StaleElementReferenceException, WebDriverException):
            pass

    raw = f"{chat_title}|{metadata}|{message_text}"

    return hashlib.sha256(
        raw.encode("utf-8", errors="ignore")
    ).hexdigest()


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
    selectors = [
        'footer [contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"][data-tab]',
        'footer div[contenteditable="true"]',
    ]

    composer = None

    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            visible = [element for element in elements if element.is_displayed()]

            if visible:
                composer = visible[-1]
                break

        except WebDriverException:
            pass

    if composer is None:
        log.warning("Message composer not found")
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

        if len(number.lstrip("+")) < 8:
            return "Usage: /on +countrycode-number"

        def enable_contact():
            if number not in state["enabled_contacts"]:
                state["enabled_contacts"].append(number)

        update_state(enable_contact)
        return f"Auto-reply enabled for {number}."

    if action == "/off":
        number = normalize_phone(argument)

        if len(number.lstrip("+")) < 8:
            return "Usage: /off +countrycode-number"

        update_state(lambda: state.update({
            "enabled_contacts": [
                n for n in state["enabled_contacts"] if n != number
            ]
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

        return "Auto-reply is OFF for all contacts. Use /on +number."

    return "Unknown command. Use /on +number, /off +number, /on all, /off all, or /status."


def should_reply_to(sender_number: str) -> bool:
    if not sender_number or sender_number in ADMIN_NUMBERS:
        return False

    with state_lock:
        return (
            bool(state.get("reply_all"))
            or sender_number in state.get("enabled_contacts", [])
        )


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
                "For urgent danger, advise contacting local emergency services."
            ),
            input=message[:6000],
        )

        result = (getattr(response, "output_text", "") or "").strip()

        if result:
            return result[:3500]

        return "Sorry, I couldn't prepare a reply just now."

    except Exception:
        log.exception("OpenAI reply generation failed")
        return "Sorry, I'm unable to reply right now. I'll get back to you later."


def handle_incoming_message(
    driver,
    chat_title: str,
    sender_number: str,
    message_text: str,
    fingerprint: str,
) -> None:
    if not message_text.strip():
        return

    with processed_ids_lock:
        if fingerprint in processed_message_ids:
            return

        processed_message_ids.add(fingerprint)

        if len(processed_message_ids) > 2000:
            processed_message_ids.clear()
            processed_message_ids.add(fingerprint)

    if (
        sender_number in ADMIN_NUMBERS
        and message_text.strip().startswith("/")
    ):
        response_text = handle_admin_command(sender_number, message_text)

        if response_text:
            send_whatsapp_message(driver, response_text)

        return

    if not should_reply_to(sender_number):
        log.info("Skipping chat %r; contact is not enabled", chat_title)
        return

    if message_text.strip().startswith("/"):
        return

    reply = generate_ai_reply(message_text)
    send_whatsapp_message(driver, reply)


def whatsapp_chat_monitor_loop(driver) -> None:
    while True:
        try:
            _ = driver.current_url
            rows = get_chat_rows(driver)
            unread_rows = []

            for row in rows:
                try:
                    if row_has_unread_badge(row):
                        unread_rows.append((row, chat_title_from_row(row)))
                except StaleElementReferenceException:
                    continue

            for row, title in unread_rows[:10]:
                if not click_chat_row(row):
                    continue

                time.sleep(1.5)
                sender_number = chat_phone_from_header(driver)
                bubbles = get_message_bubbles(driver)
                incoming = []

             for bubble in bubbles[-15:]:
                    try:
                        classes = (
                            bubble.get_attribute("class") or ""
                        ).split()

                        if "message-in" not in classes:
                            continue

                        text_value = extract_message_text(bubble)

                        if not text_value:
                            continue

                        fingerprint = message_fingerprint(
                            bubble,
                            title,
                            text_value,
                        )
                        incoming.append((text_value, fingerprint))

                    except (
                        StaleElementReferenceException,
                        WebDriverException,
                    ):
                        continue

                if incoming:
                    message_text, fingerprint = incoming[-1]

                    handle_incoming_message(
                        driver,
                        title,
                        sender_number,
                        message_text,
                        fingerprint,
                    )

                time.sleep(0.5)

            time.sleep(2)

        except (InvalidSessionIdException, WebDriverException) as exc:
            log.warning("WhatsApp Web session ended: %s", exc)
            set_whatsapp_status(
                "Browser session ended; refresh the QR page to restart"
            )
            return

        except Exception:
            log.exception("Unexpected monitoring error")
            time.sleep(3)


@app.get("/")
def home():
    return jsonify({
        "service": "WhatsApp Auto AI",
        "status": whatsapp_status,
        "whatsapp_logged_in": whatsapp_logged_in,
        "endpoints": [
            "/health",
            "/qr",
            "/qr-status",
            "/qr-image",
            "/admin-command",
            "/reply",
            "/browser-test",
        ],
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "WhatsApp Auto AI",
        "whatsapp_status": whatsapp_status,
        "whatsapp_logged_in": whatsapp_logged_in,
        "openai_configured": bool(openai_client),
        "time_utc": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/qr")
@protected_endpoint
def qr_page():
    html = r"""<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhatsApp QR Login</title>
<style>
body{font-family:Arial,sans-serif;max-width:700px;margin:24px auto;padding:0 16px;color:#222}
img{max-width:100%;width:320px;height:auto;border:1px solid #ddd;padding:8px}
#status{padding:12px;background:#f3f3f3;border-radius:8px}
small{color:#555}
</style>
</head>
<body>
<h2>WhatsApp Web Login</h2>
<p>On your phone, open WhatsApp → Linked devices → Link a device, then scan this QR code.</p>
<div id="status">Checking browser status...</div>
<p><img id="qr" alt="WhatsApp QR code" style="display:none"></p>
<p><button onclick="refreshNow()">Refresh status</button></p>
<small>Do not share this QR code or this page with anyone.</small>
<script>
async function refreshNow(){
  try{
    const r=await fetch('/qr-status',{cache:'no-store'});
    const d=await r.json();

   document.getElementById('status').textContent=d.status||d.error||'No status';
    const img=document.getElementById('qr');
    if(d.qr_available){
      img.src='/qr-image?t='+Date.now();
      img.style.display='block';
    }else{
      img.style.display='none';
    }
  }catch(e){
    document.getElementById('status').textContent='Unable to reach service. Refresh the page.';
  }
}
refreshNow();
setInterval(refreshNow,5000);
</script>
</body>
</html>"""

    return Response(html, mimetype="text/html")


@app.get("/qr-status")
@protected_endpoint
def qr_status():
    start_whatsapp_browser()

    with whatsapp_lock:
        return jsonify({
            "status": whatsapp_status,
            "qr_available": bool(whatsapp_qr_data),
            "whatsapp_logged_in": whatsapp_logged_in,
        })


@app.get("/qr-image")
@protected_endpoint
def qr_image():
    with whatsapp_lock:
        data = whatsapp_qr_data

    if not data:
        return jsonify({
            "error": "QR code is not available yet. Wait and refresh."
        }), 404

    try:
        image_bytes = base64.b64decode(data)
    except Exception:
        return jsonify({"error": "Invalid QR image data."}), 500

    return Response(
        image_bytes,
        mimetype="image/png",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.post("/admin-command")
@protected_endpoint
def admin_command_route():
    body = request.get_json(silent=True) or {}
    sender = normalize_phone(body.get("sender", ""))
    command = str(body.get("command", ""))

    if sender not in ADMIN_NUMBERS:
        return jsonify({
            "ok": False,
            "error": "Sender is not an authorized admin number.",
        }), 403

    result = handle_admin_command(sender, command)

    return jsonify({"ok": True, "result": result})


@app.post("/reply")
@protected_endpoint
def reply_route():
    body = request.get_json(silent=True) or {}
    message = str(body.get("message", "")).strip()

    if not message:
        return jsonify({
            "ok": False,
            "error": "Provide a non-empty message.",
        }), 400

    if len(message) > 6000:
        return jsonify({
            "ok": False,
            "error": "Message is too long; maximum is 6000 characters.",
        }), 400

  return jsonify({
        "ok": True,
        "reply": generate_ai_reply(message),
    })


@app.post("/browser-test")
@protected_endpoint
def browser_test_route():
    start_whatsapp_browser()

    with whatsapp_lock:
        return jsonify({
            "ok": True,
            "status": whatsapp_status,
            "browser_running": whatsapp_driver is not None,
            "whatsapp_logged_in": whatsapp_logged_in,
        })


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(_error):
    log.exception("Unhandled server error")
    return jsonify({"error": "Internal server error"}), 500


load_state()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True,
    )

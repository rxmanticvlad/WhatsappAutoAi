
import os
import json
import re
import time
import base64
import shutil
import secrets
import threading
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, request, Response
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from openai import OpenAI

app = Flask(__name__)

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

QR_ACCESS_USER = os.getenv("QR_ACCESS_USER", "")
QR_ACCESS_PASSWORD = os.getenv("QR_ACCESS_PASSWORD", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

STATE_FILE = Path(os.getenv("STATE_FILE", "bot_state.json"))

DEFAULT_STATE = {
    "global_enabled": False,
    "enabled_chats": [],
    "disabled_chats": [],
}

# --------------------------------------------------
# SHARED WHATSAPP BROWSER STATE
# --------------------------------------------------

whatsapp_driver = None
whatsapp_lock = threading.Lock()
whatsapp_status = "Not started"
whatsapp_qr_data = None
whatsapp_starting = False


# --------------------------------------------------
# QR PAGE AUTHENTICATION
# --------------------------------------------------

def require_qr_auth(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        auth = request.authorization

        if not QR_ACCESS_USER or not QR_ACCESS_PASSWORD:
            return "QR access is not configured.", 503

        if (
            not auth
            or not secrets.compare_digest(
                auth.username or "", QR_ACCESS_USER
            )
            or not secrets.compare_digest(
                auth.password or "", QR_ACCESS_PASSWORD
            )
        ):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="WhatsApp QR"'},
            )

        return func(*args, **kwargs)

    return wrapper


# --------------------------------------------------
# CHROMIUM DRIVER
# --------------------------------------------------


def create_chrome_driver():
    options = Options()

    options.binary_location = os.getenv(
        "CHROME_BIN", "/usr/bin/chromium"
    )

    
options.add_argument("--headless")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-gpu")
options.add_argument("--window-size=800,600")
options.add_argument("--disable-extensions")
options.add_argument("--disable-background-networking")
options.add_argument("--disable-sync")
options.add_argument("--disable-translate")
options.add_argument("--no-first-run")

    options.add_argument(
        "--disable-blink-features=AutomationControlled"
    )
    options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    driver_path = (
        os.getenv("CHROMEDRIVER_PATH")
        or shutil.which("chromedriver")
    )

    if driver_path:
        service = Service(executable_path=driver_path)
    else:
        service = Service()

    return webdriver.Chrome(
        service=service,
        options=options,
    )


# --------------------------------------------------
# WHATSAPP WEB WORKER
# --------------------------------------------------

def start_whatsapp_browser():
    global whatsapp_starting, whatsapp_status, whatsapp_qr_data

    with whatsapp_lock:
        if whatsapp_starting or whatsapp_driver is not None:
            return

        whatsapp_starting = True
        whatsapp_status = "Starting Chromium..."
        whatsapp_qr_data = None

    thread = threading.Thread(
        target=_whatsapp_browser_worker,
        daemon=True,
        name="whatsapp-browser-worker",
    )
    thread.start()


def _whatsapp_browser_worker():
    global whatsapp_driver
    global whatsapp_status
    global whatsapp_qr_data
    global whatsapp_starting

    driver = None

    try:
        app.logger.info("Starting Chromium")
        driver = create_chrome_driver()

        with whatsapp_lock:
            whatsapp_driver = driver
            whatsapp_status = "Opening WhatsApp Web..."
            whatsapp_qr_data = None

        driver.set_page_load_timeout(60)
        
        driver.get("https://web.whatsapp.com/")

        app.logger.info("WhatsApp Web navigation completed")

        time.sleep(10)

        app.logger.info("WhatsApp page title: %s", driver.title)
        app.logger.info("WhatsApp page URL: %s", driver.current_url)

        try:
            body_text = driver.find_element(
                By.TAG_NAME, "body"
            ).text

            app.logger.info(
                "WhatsApp page text: %s",
                body_text[:1000]
            )
        except Exception:
            app.logger.exception("Could not read WhatsApp page text")

        app.logger.info(
            "WhatsApp canvas count: %s",
            len(driver.find_elements(By.TAG_NAME, "canvas"))
        )

        app.logger.info(
            "WhatsApp image count: %s",
            len(driver.find_elements(By.TAG_NAME, "img"))
        )


        deadline = time.time() + 240

        while time.time() < deadline:
            try:
                # Check whether WhatsApp is already logged in.
                logged_in = driver.find_elements(
                    By.CSS_SELECTOR,
                    '[aria-label="Chat list"], '
                    '[data-testid="chat-list"]',
                )

                if logged_in:
                    with whatsapp_lock:
                        whatsapp_qr_data = None
                        whatsapp_status = (
                            "WhatsApp Web appears logged in. "
                            "Automatic replies are not connected yet."
                        )

                    app.logger.info("WhatsApp login detected")
                    return

                # Look for visible, square QR canvases.
                qr_png = None

                canvases = driver.find_elements(
                    By.CSS_SELECTOR, "canvas"
                )

                for canvas in canvases:
                    try:
                        if not canvas.is_displayed():
                            continue

                        width = canvas.size.get("width", 0)
                        height = canvas.size.get("height", 0)

                        if width < 100 or height < 100:
                            continue

                        if abs(width - height) > 30:
                            continue

                        image = canvas.screenshot_as_png

                        if image:
                            qr_png = image
                            break

                    except Exception:
                        continue

                if qr_png:
                    encoded = base64.b64encode(
                        qr_png
                    ).decode("ascii")

                    with whatsapp_lock:
                        whatsapp_qr_data = encoded
                        whatsapp_status = (
                            "QR image captured. Scan it only if "
                            "the displayed image is a valid QR code."
                        )

                else:
                    with whatsapp_lock:
                        if whatsapp_qr_data is None:
                            whatsapp_status = (
                                "Waiting for WhatsApp QR code..."
                            )

                time.sleep(3)

            except Exception:
                app.logger.exception(
                    "Error checking WhatsApp Web"
                )

                with whatsapp_lock:
                    whatsapp_status = (
                        "WhatsApp page check failed. "
                        "Check the Render logs."
                    )

                time.sleep(3)

        with whatsapp_lock:
            if whatsapp_qr_data is None:
                whatsapp_status = (
                    "QR code was not detected within four minutes. "
                    "Check the Render logs."
                )

    except Exception:
        app.logger.exception("WhatsApp browser startup failed")

        with whatsapp_lock:
            whatsapp_status = (
                "Browser startup failed. Check the Render logs."
            )
            whatsapp_qr_data = None

    finally:
        with whatsapp_lock:
            whatsapp_starting = False

            # Keep the driver reference so a second browser
            # is not accidentally started over the first.
            if driver is not None:
                whatsapp_driver = driver


# --------------------------------------------------
# QR SETUP PAGE
# --------------------------------------------------

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
    <body style="font-family:Arial;max-width:600px;margin:30px auto;padding:16px">
      <h2>WhatsApp QR Setup</h2>
      <p id="status">Checking browser status...</p>
      <div id="qr-container"></div>
      <button onclick="refreshStatus()">Refresh</button>

      <script>
        async function refreshStatus() {
          try {
            const response = await fetch('/qr-status', {
              cache: 'no-store'
            });

            if (!response.ok) {
              throw new Error('Status unavailable');
            }

            const data = await response.json();

            document.getElementById('status').textContent =
              data.status;

            const container = document.getElementById('qr-container');
let img = document.getElementById('whatsapp-qr');

if (data.qr_available) {
  if (!img) {
    img = document.createElement('img');
    img.id = 'whatsapp-qr';
    img.alt = 'WhatsApp login QR code';
    img.style.maxWidth = '100%';
    container.appendChild(img);
  }

  img.src = '/qr-image?t=' + Date.now();
} else if (img) {
  img.remove();
}
          } catch (error) {
            document.getElementById('status').textContent =
              'Could not get status. Refresh and try again.';
          }
        }

        refreshStatus();
        setInterval(refreshStatus, 5000);
      </script>
    </body>
    </html>
    """


@app.get("/qr-status")
@require_qr_auth
def qr_status():
    start_whatsapp_browser()

    with whatsapp_lock:
        return jsonify({
            "status": whatsapp_status,
            "qr_available": whatsapp_qr_data is not None,
        })


@app.get("/qr-image")
@require_qr_auth
def qr_image():
    with whatsapp_lock:
        image_data = whatsapp_qr_data

    if not image_data:
        return jsonify({
            "error": "QR code is not available yet"
        }), 404

    try:
        image_bytes = base64.b64decode(
            image_data, validate=True
        )
    except Exception:
        app.logger.exception("QR image decoding failed")
        return jsonify({
            "error": "QR image is temporarily unavailable"
        }), 500

    return Response(
        image_bytes,
        mimetype="image/png",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.get("/")
def home():
    return (
        "WhatsApp AI service is running. "
        "WhatsApp message integration is not connected yet."
    )


@app.get("/health")
def health():
    with whatsapp_lock:
        connected = (
            whatsapp_driver is not None
            and whatsapp_status.startswith(
                "WhatsApp Web appears logged in"
            )
        )

        status = whatsapp_status

    return jsonify({
        "ok": True,
        "whatsapp_connected": connected,
        "whatsapp_status": status,
    })


# --------------------------------------------------
# BASIC AI REPLY TEST
# This endpoint does not send WhatsApp messages.
# --------------------------------------------------

def generate_reply(message):
    if client is None:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    response = client.responses.create(
        model="gpt-4.1-mini",
        instructions=(
            "You are a friendly AI assistant. "
            "Reply naturally and concisely, using the same "
            "language as the incoming message. "
            "Do not claim to be the account owner."
        ),
        input=message,
    )

    return response.output_text.strip()


@app.post("/reply")
def reply():
    # Deliberately disabled until real WhatsApp sender
    # authentication and message integration are implemented.
    return jsonify({
        "error": (
            "WhatsApp auto-replies are not enabled yet. "
            "Real message integration must be configured first."
        )
    }), 501


# --------------------------------------------------
# BROWSER TEST
# --------------------------------------------------

@app.get("/browser-test")
def browser_test():
    driver = None

    try:
        driver = create_chrome_driver()
        driver.get("https://example.com")

        return jsonify({
            "ok": True,
            "title": driver.title,
            "browser": "Chromium started successfully",
        })

    except Exception:
        app.logger.exception("Chromium test failed")

        return jsonify({
            "ok": False,
            "error": "Chromium could not start. Check Render logs.",
        }), 500

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                app.logger.exception(
                    "Could not close browser test session"
                )


# --------------------------------------------------
# START FLASK
# --------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        )
    

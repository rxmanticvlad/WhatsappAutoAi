
import express from 'express';
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState
} from '@whiskeysockets/baileys';
import QRCode from 'qrcode';
import fs from 'node:fs/promises';

const app = express();
app.use(express.json());

const PORT = Number(process.env.PORT || 10000);
const AI_URL = process.env.AI_URL || 'http://127.0.0.1:5001/reply';
const AUTH_DIR = process.env.AUTH_DIR || './auth_info_baileys';

let sock;
let qrDataUrl = null;
let connectionStatus = 'Starting';
let connecting = false;

function authorized(req, res, next) {
  const username = process.env.QR_ACCESS_USER;
  const password = process.env.QR_ACCESS_PASSWORD;

  if (!username || !password) {
    return res.status(503).send('QR access credentials are not configured.');
  }

  const header = req.headers.authorization || '';
  const [scheme, token] = header.split(' ');

  if (scheme !== 'Basic' || !token) {
    res.set('WWW-Authenticate', 'Basic realm="WhatsApp QR"');
    return res.status(401).send('Authentication required.');
  }

  let decoded;
  try {
    decoded = Buffer.from(token, 'base64').toString('utf8');
  } catch {
    return res.status(401).send('Invalid credentials.');
  }

  const separator = decoded.indexOf(':');
  const user = decoded.slice(0, separator);
  const pass = decoded.slice(separator + 1);

  if (separator < 0 || user !== username || pass !== password) {
    res.set('WWW-Authenticate', 'Basic realm="WhatsApp QR"');
    return res.status(401).send('Invalid credentials.');
  }

  next();
}

app.get('/health', (_req, res) => {
  res.json({ ok: true, whatsapp_status: connectionStatus });
});

app.get('/qr', authorized, (_req, res) => {
  res.type('html').send(`<!doctype html>
<html>
<head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhatsApp pairing</title></head>
<body style="font-family:sans-serif;text-align:center;padding:24px">
<h2>WhatsApp pairing</h2>
<p id="status">Checking connection...</p>
<img id="qr" style="width:280px;max-width:90%;display:none" />
<script>
async function refresh() {
  try {
    const r = await fetch('/qr-status');
    const s = await r.json();
    document.getElementById('status').textContent = s.status;
    const img = document.getElementById('qr');
    if (s.qr) {
      img.src = s.qr;
      img.style.display = 'inline-block';
    } else {
      img.style.display = 'none';
    }
  } catch {
    document.getElementById('status').textContent = 'Unable to get status';
  }
}
refresh();
setInterval(refresh, 3000);
</script>
</body></html>`);
});

app.get('/qr-status', authorized, (_req, res) => {
  res.json({ status: connectionStatus, qr: qrDataUrl });
});

async function askAI(text) {
  const response = await fetch(AI_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text })
  });

  if (!response.ok) {
    throw new Error(`AI service returned HTTP ${response.status}`);
  }

  const result = await response.json();

  if (!result.reply || typeof result.reply !== 'string') {
    throw new Error('AI service returned no reply text');
  }

  return result.reply;
}

async function connectWhatsApp() {
  if (connecting) return;
  connecting = true;

  try {
    await fs.mkdir(AUTH_DIR, { recursive: true });

    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    sock = makeWASocket({ auth: state });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', async (update) => {
      if (update.qr) {
        qrDataUrl = await QRCode.toDataURL(update.qr);
        connectionStatus = 'Scan the QR code with WhatsApp';
        console.log('A new WhatsApp QR code is available.');
      }

      if (update.connection === 'open') {
        qrDataUrl = null;
        connectionStatus = 'Connected';
        console.log('WhatsApp connected.');
      }

      if (update.connection === 'close') {
        qrDataUrl = null;

        const code = update.lastDisconnect?.error?.output?.statusCode;
        const loggedOut = code === DisconnectReason.loggedOut;

        connectionStatus = loggedOut
          ? 'Logged out; remove the old session before pairing again'
          : 'Disconnected; reconnecting';

        if (!loggedOut) {
          setTimeout(() => {
            connecting = false;
            connectWhatsApp().catch(console.error);
          }, 3000);
        }
      }
    });

    sock.ev.on('messages.upsert', async ({ messages, type }) => {
      if (type !== 'notify') return;

      for (const message of messages) {
        if (!message.message || message.key.fromMe) continue;

        const chatId = message.key.remoteJid;
        if (!chatId || chatId === 'status@broadcast') continue;

        // Initial version handles ordinary text messages only.
        const text =
          message.message.conversation ||
          message.message.extendedTextMessage?.text;

        if (!text?.trim()) continue;

        try {
          const reply = await askAI(text);
          await sock.sendMessage(chatId, { text: reply });
          console.log(`Replied to message in ${chatId}`);
        } catch (error) {
          console.error('Reply failed:', error.message);
        }
      }
    });
  } catch (error) {
    connecting = false;
    connectionStatus = 'Failed to start';
    throw error;
  }
}

app.listen(PORT, '0.0.0.0', () => {
  console.log(`WhatsApp service listening on port ${PORT}`);
  connectWhatsApp().catch(console.error);
});

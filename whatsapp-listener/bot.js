require("dotenv").config();
const { default: makeWASocket, useMultiFileAuthState, downloadMediaMessage, DisconnectReason, fetchLatestBaileysVersion } = require("@whiskeysockets/baileys");
const qrcode = require("qrcode-terminal");
const FormData = require("form-data");
const fetch = require("node-fetch");

// Was hardcoded in source ("my_super_secret_dev_key_123") - a real secret
// baked into a file that's one `git add -A` away from being pushed to a
// public repo. Now read from .env (gitignored) instead, matching how the
// rest of this project keeps every other secret out of source.
const BACKEND_URL = process.env.BACKEND_URL || "http://127.0.0.1:8000";
const SETU_API_KEY = process.env.SETU_API_KEY;
if (!SETU_API_KEY) {
  console.error("SETU_API_KEY is not set - copy .env.example to .env and fill it in.");
  process.exit(1);
}

async function startBot() {
  const { state, saveCreds } = await useMultiFileAuthState("auth_info");
  
  // Fetch the latest compatible WhatsApp web version dynamically
  const { version, isLatest } = await fetchLatestBaileysVersion();
  console.log(`Using WA v${version.join('.')}, isLatest: ${isLatest}`);

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: true
  });
  
  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect } = update;
    
    if (connection === "open") {
      console.log("\n>>> WhatsApp listener is active and connected successfully! <<<\n");
    }
    
    if (connection === "close") {
      const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
      if (shouldReconnect) {
        startBot();
      }
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    const msg = messages[0];
    
    if (!msg.message || msg.key.fromMe || !msg.message.audioMessage) return;

    const sender = msg.key.remoteJid;
    // A group JID (@g.us) isn't any individual's phone number - reporting it
    // as reported_by would fabricate a fake identity for a real feature
    // (driver-history/false-alarm tracking) that depends on this being a
    // real person. An SOS should come from an identifiable individual.
    if (sender.endsWith("@g.us")) {
      console.log("Ignoring group voice message from:", sender);
      return;
    }
    // WhatsApp JIDs are bare digits ("919876543210@s.whatsapp.net") - Twilio's
    // channel reports phones as E.164 with a "+" ("whatsapp:+919876543210").
    // Without normalizing here, the same person messaging through both
    // channels would show up as two different "contacts", splitting their
    // report history and false-alarm count across two identities.
    const phone = `+${sender.split("@")[0]}`;
    console.log("Downloading audio from:", phone);

    const audioBuffer = await downloadMediaMessage(msg, "buffer", {}, { logger: console });
    
    const form = new FormData();
    form.append("file", audioBuffer, { filename: "sos.ogg", contentType: "audio/ogg" });
    // Real sender identity, actually used now - the old /api/voice-sos/upload
    // dev endpoint this used to call ignores this field entirely and
    // hardcodes "TEST-UPLOAD" for every request, which would've made every
    // real SOS through this channel show the same fake identity (breaking
    // driver-history tracking and the false-alarm flag). The dedicated
    // /api/whatsapp-listener/voice-sos endpoint below actually uses it.
    form.append("phone", phone);

    try {
      const response = await fetch(`${BACKEND_URL}/api/whatsapp-listener/voice-sos`, {
        method: "POST",
        body: form,
        headers: {
          ...form.getHeaders(),
          "X-API-Key": SETU_API_KEY
        },
      });
      
      const result = await response.json();
      
      if (result.rejected) {
        await sock.sendMessage(sender, { text: `System: ${result.reason}` });
      } else {
        await sock.sendMessage(sender, { text: `Emergency verified. Dispatching logistics for: ${result.cargo}` });
      }
    } catch (err) {
      console.error("Failed to send to backend:", err);
    }
  });
}

startBot();
import { makeWASocket, useMultiFileAuthState, DisconnectReason, downloadMediaMessage } from '@whiskeysockets/baileys';
import qrcode from 'qrcode';
import pino from 'pino';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const LOG_FILE = path.join(__dirname, 'messages.log');
const QR_FILE = path.join(__dirname, 'qr.png');
const AUTH_DIR = path.join(__dirname, 'auth');
const OUTBOX_DIR = path.join(__dirname, 'outbox');
const OUTBOX_SENT_DIR = path.join(__dirname, 'outbox_sent');
const LAST_ACK_FILE = path.join(__dirname, 'last_ack.json');
const SENDERS_FILE = path.join(__dirname, 'senders.json');
const ATTACHMENTS_DIR = path.join(__dirname, 'attachments');
const STATUS_FILE = path.join(__dirname, 'status.json');
const HEARTBEAT_INTERVAL_MS = 7000; // tunable

fs.mkdirSync(OUTBOX_DIR, { recursive: true });
fs.mkdirSync(OUTBOX_SENT_DIR, { recursive: true });
fs.mkdirSync(ATTACHMENTS_DIR, { recursive: true });

const EXT_BY_MIME = {
  'application/pdf': 'pdf',
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/webp': 'webp',
};

async function saveAttachmentIfAny(sock, msg) {
  const imageMsg = msg.message.imageMessage;
  const docMsg = msg.message.documentMessage;
  const media = imageMsg || docMsg;
  if (!media) return null;

  const mimetype = media.mimetype || '';
  const isPdf = mimetype === 'application/pdf';
  const isImage = mimetype.startsWith('image/');
  if (!isPdf && !isImage) {
    return { skipped: true, mimetype };
  }

  try {
    const buffer = await downloadMediaMessage(msg, 'buffer', {});
    const ext = EXT_BY_MIME[mimetype] || 'bin';
    const safeName = (docMsg?.fileName || `attachment_${msg.key.id}`).replace(/[^a-zA-Z0-9._-]/g, '_');
    const filename = `${Date.now()}_${safeName}${safeName.endsWith('.' + ext) ? '' : '.' + ext}`;
    const filePath = path.join(ATTACHMENTS_DIR, filename);
    fs.writeFileSync(filePath, buffer);
    return {
      path: filePath,
      mimetype,
      caption: media.caption || null,
      original_filename: docMsg?.fileName || null,
    };
  } catch (err) {
    console.error('ATTACHMENT_DOWNLOAD_ERROR', err.message);
    return { error: err.message, mimetype };
  }
}

// Same self-echo dedup problem as the personal bridge: WhatsApp syncs our own
// sent replies back through messages.upsert on every linked device, including
// this one -- without this, the bot would log its own reply as new inbound
// input and react to itself forever.
const sentMessageIds = new Set();

// Single mutable reference to whichever socket is currently live. Every
// reconnect (start() called again on connection close) used to register a
// brand new fs.watch() listener on the outbox dir while the old one, still
// bound to the dead socket via closure, kept running too -- two watchers
// racing on the same files, and whichever one won fs.renameSync's race could
// be the stale one, silently swallowing the send. Now there's exactly one
// watcher for the process's lifetime, and it always sends via whatever socket
// is currently live.
let currentSock = null;
let outboxWatcherStarted = false;

function normalizeJid(jid) {
  if (!jid) return jid;
  return jid.replace(/:\d+(?=@)/, '');
}

function loadSenders() {
  try {
    const raw = JSON.parse(fs.readFileSync(SENDERS_FILE, 'utf8'));
    return raw.senders || {};
  } catch (err) {
    console.error('SENDERS_LOAD_ERROR', err.message);
    return {};
  }
}

// Node's fs.watch can fire more than one event for a single file write, so
// relying on fs.renameSync alone as the dedup lock (the original design) is
// unsafe once rename happens *after* the send instead of before: two
// concurrent calls for the same file would both pass the read step and both
// call sendMessage, double-sending before either rename runs. inFlight is an
// explicit lock so at most one send is ever in progress per filename,
// independent of when the rename happens.
const inFlight = new Set();

async function sendOutboxFile(filename) {
  const filePath = path.join(OUTBOX_DIR, filename);
  if (inFlight.has(filename)) return; // a concurrent duplicate watch event is already handling this file
  if (!fs.existsSync(filePath)) return; // already sent and moved
  inFlight.add(filename);
  try {
    let payload;
    try {
      payload = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    } catch (err) {
      console.error('OUTBOX_READ_ERROR', filename, err.message);
      return;
    }

    if (!currentSock) {
      console.error('OUTBOX_SEND_ERROR', filename, 'no live socket yet -- left in outbox, will retry on next watch event');
      return; // do NOT move the file -- retry once a socket comes back
    }

    const sentPath = path.join(OUTBOX_SENT_DIR, filename);
    try {
      const content = payload.editKey
        ? { text: payload.text, edit: payload.editKey }
        : { text: payload.text };
      const sent = await currentSock.sendMessage(payload.to, content);
      if (sent?.key?.id) {
        sentMessageIds.add(sent.key.id);
        fs.writeFileSync(LAST_ACK_FILE, JSON.stringify({ key: sent.key }));
      }
      // Only move to outbox_sent AFTER a confirmed successful send -- moving
      // first (the original behaviour) meant a failed send still looked
      // "sent" and was never retried.
      fs.renameSync(filePath, sentPath);
      if (payload.final) writeStatus({ active: false });
      console.log('OUTBOX_SENT', filename, 'to=' + payload.to);
    } catch (err) {
      console.error('OUTBOX_SEND_ERROR', filename, err.message, '-- left in outbox, will retry on next watch event');
    }
  } finally {
    inFlight.delete(filename);
  }
}

// Heartbeat: decouples "what to say" from "when to say it". Claude writes its
// current phase to status.json as work progresses (active:true, text, plus
// which message this status is for); this timer polls on a fixed cadence and
// edits the ack bubble only if the text actually changed since the last tick
// -- so it's a real cadence, not a flood, and never repeats the same update
// twice. Deactivated by the bridge itself (not a race-prone second writer)
// the moment a final outbox reply for that key actually sends.
let lastHeartbeatText = null;

function readStatus() {
  try {
    return JSON.parse(fs.readFileSync(STATUS_FILE, 'utf8'));
  } catch {
    return null;
  }
}

function writeStatus(obj) {
  fs.writeFileSync(STATUS_FILE, JSON.stringify(obj));
}

async function heartbeatTick() {
  if (!currentSock) return;
  const status = readStatus();
  if (!status || !status.active) return;
  if (status.text === lastHeartbeatText) return; // nothing new to say
  try {
    const sent = await currentSock.sendMessage(status.to, { text: status.text, edit: status.editKey });
    // Same dedup requirement as the ack and outbox sends: WhatsApp syncs this
    // edit back through messages.upsert on every linked device, including
    // this one. Without registering it here, the echo wasn't recognized as
    // our own message and got processed as new inbound input -- triggering a
    // whole new ack+heartbeat cycle, which is what showed up as a spurious
    // "message" containing the heartbeat's own text.
    if (sent?.key?.id) sentMessageIds.add(sent.key.id);
    lastHeartbeatText = status.text;
    console.log('HEARTBEAT_SENT', status.text);
  } catch (err) {
    console.error('HEARTBEAT_SEND_ERROR', err.message);
  }
}

setInterval(heartbeatTick, HEARTBEAT_INTERVAL_MS);

function watchOutbox() {
  if (outboxWatcherStarted) return;
  outboxWatcherStarted = true;
  for (const filename of fs.readdirSync(OUTBOX_DIR)) {
    sendOutboxFile(filename);
  }
  fs.watch(OUTBOX_DIR, (eventType, filename) => {
    if (!filename) return;
    if (!fs.existsSync(path.join(OUTBOX_DIR, filename))) return;
    sendOutboxFile(filename);
  });
  console.log('OUTBOX_WATCHING', OUTBOX_DIR);
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  const sock = makeWASocket({
    auth: state,
    logger: pino({ level: 'silent' }),
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      await qrcode.toFile(QR_FILE, qr);
      console.log('QR_READY', QR_FILE);
    }
    if (connection === 'close') {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      console.log('CONNECTION_CLOSED', 'statusCode=' + statusCode, 'loggedOut=' + loggedOut);
      if (currentSock === sock) currentSock = null; // don't send via a dead socket
      if (!loggedOut) {
        start();
      }
    } else if (connection === 'open') {
      console.log('CONNECTION_OPEN', 'own_id=' + sock.user?.id, 'own_lid=' + sock.user?.lid);
      currentSock = sock;
      watchOutbox();
    }
  });

  sock.ev.on('messages.upsert', async (m) => {
    const senders = loadSenders();
    const bootstrapMode = Object.keys(senders).length === 0;

    for (const msg of m.messages) {
      if (!msg.message) continue;
      if (msg.key.id && sentMessageIds.has(msg.key.id)) {
        sentMessageIds.delete(msg.key.id);
        continue; // echo of our own outbox reply
      }

      const sender = normalizeJid(msg.key.remoteJid);
      const identity = senders[sender];

      if (!bootstrapMode && !identity) {
        // Real RBAC gate: unknown number, silently dropped, never even logged.
        continue;
      }

      if (bootstrapMode) {
        console.log('BOOTSTRAP_UNKNOWN_SENDER', sender, '-- add to senders.json to lock down');
      }

      try {
        const ack = await sock.sendMessage(msg.key.remoteJid, { text: '🔄 Thinking...' });
        if (ack?.key?.id) sentMessageIds.add(ack.key.id);
        fs.writeFileSync(LAST_ACK_FILE, JSON.stringify({ key: ack?.key }));
        lastHeartbeatText = '🔄 Thinking...'; // matches what was just sent, so heartbeat doesn't immediately re-send the same text
        writeStatus({ active: true, text: '🔄 Thinking...', to: msg.key.remoteJid, editKey: ack?.key });
        console.log('ACK_SENT');
      } catch (err) {
        console.error('ACK_SEND_ERROR', err.message);
      }

      const attachment = await saveAttachmentIfAny(sock, msg);

      const text =
        msg.message.conversation ||
        msg.message.extendedTextMessage?.text ||
        attachment?.caption ||
        (attachment ? '' : '[non-text message]');
      const entry = {
        ts: new Date().toISOString(),
        sender,
        identity: identity || null,
        fromMe: msg.key.fromMe,
        text,
        attachment: attachment || null,
      };
      fs.appendFileSync(LOG_FILE, JSON.stringify(entry) + '\n');
      console.log('MESSAGE_LOGGED', JSON.stringify(entry));
    }
  });
}

start().catch((err) => {
  console.error('FATAL', err);
  process.exit(1);
});

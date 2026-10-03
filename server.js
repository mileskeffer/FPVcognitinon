import express from "express";
import http from "http";
import https from "https";
import fs from "fs";
import path from "path";

const PORT = process.env.PORT || 3000;

// ---------------------------------------------------------------------------
// Express app: static files + a small REST API
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json());                                           // parse JSON bodies
app.use(express.static(path.join(import.meta.dirname, "public"))); // serve /public

app.get("/", (req, res) => res.redirect("/viewer.html"));

app.get("/api/health", (req, res) => res.json({ ok: true }));

// Browsers fetch their ICE (STUN/TURN) config from here, so nothing is hardcoded in the frontend.
// Set TURN_URL / TURN_USER / TURN_PASS in the environment if you run a TURN server.
app.get("/api/config", (req, res) => {
  const iceServers = [{ urls: process.env.STUN_URL || "stun:stun.l.google.com:19302" }];
  if (process.env.TURN_URL) {
    iceServers.push({
      urls: process.env.TURN_URL,
      username: process.env.TURN_USER,
      credential: process.env.TURN_PASS,
    });
  }
  res.json({ iceServers });
});

// WebRTC signaling uses ordinary HTTP polling. The video never passes through
// these endpoints; they only carry offer, answer, and ICE messages.
const signalingRooms = new Map();

function getSignalingRoom(roomId) {
  let room = signalingRooms.get(roomId);
  if (!room) {
    room = {
      drone: { queue: [] },
      viewer: { queue: [] },
    };
    signalingRooms.set(roomId, room);
  }
  return room;
}

function validRole(role) {
  return role === "drone" || role === "viewer";
}

function otherRole(role) {
  return role === "drone" ? "viewer" : "drone";
}

app.post("/api/signaling/join", (req, res) => {
  const { room: roomId = "default", role } = req.body ?? {};
  if (!validRole(role)) return res.status(400).json({ error: "invalid role" });

  const room = getSignalingRoom(roomId);
  if (room[role].joined) return res.json({ type: "full" });

  room[role].joined = true;
  if (room.drone.joined && room.viewer.joined) room.drone.queue.push({ type: "ready" });
  res.json({ type: "joined", role });
});

app.get("/api/signaling/messages", (req, res) => {
  const { room: roomId = "default", role } = req.query;
  if (!validRole(role)) return res.status(400).json({ error: "invalid role" });

  const room = signalingRooms.get(roomId);
  const messages = room?.[role].queue.splice(0) ?? [];
  res.json(messages);
});

app.post("/api/signaling/messages", (req, res) => {
  const { room: roomId = "default", role, message } = req.body ?? {};
  if (!validRole(role) || !message?.type) {
    return res.status(400).json({ error: "role and message.type are required" });
  }

  const room = signalingRooms.get(roomId);
  const recipient = room?.[otherRole(role)];
  if (recipient?.joined) recipient.queue.push(message);
  res.status(202).end();
});

app.delete("/api/signaling/join", (req, res) => {
  const { room: roomId = "default", role } = req.body ?? {};
  if (!validRole(role)) return res.status(400).json({ error: "invalid role" });

  const room = signalingRooms.get(roomId);
  if (room?.[role].joined) {
    room[role] = { queue: [] };
    const peer = room[otherRole(role)];
    if (peer.joined) peer.queue.push({ type: "peer-left" });
  }
  if (room && !room.drone.joined && !room.viewer.joined) signalingRooms.delete(roomId);
  res.status(204).end();
});

// In-memory log of recognized faces. Whatever runs recognition POSTs here; the UI can GET it.
const sightings = [];

app.post("/api/sightings", (req, res) => {
  const { name, confidence } = req.body ?? {};
  if (typeof name !== "string" || !name) {
    return res.status(400).json({ error: "name (string) is required" });
  }
  const entry = { name, confidence: confidence ?? null, ts: Date.now() };
  sightings.push(entry);
  if (sightings.length > 500) sightings.shift(); // cap memory use
  res.status(201).json(entry);
});

app.get("/api/sightings", (req, res) => {
  const limit = Math.min(Number(req.query.limit) || 50, 500);
  res.json(sightings.slice(-limit).reverse()); // newest first
});

// ---------------------------------------------------------------------------
// HTTP or HTTPS server (browsers only allow camera access on HTTPS or localhost)
// Set HTTPS_KEY and HTTPS_CERT to file paths to enable HTTPS.
// ---------------------------------------------------------------------------
const server =
  process.env.HTTPS_KEY && process.env.HTTPS_CERT
    ? https.createServer(
        { key: fs.readFileSync(process.env.HTTPS_KEY), cert: fs.readFileSync(process.env.HTTPS_CERT) },
        app
      )
    : http.createServer(app);

server.listen(PORT, "0.0.0.0", () => {
  console.log(`Server running on ${process.env.HTTPS_KEY ? "https" : "http"}://localhost:${PORT}`);
});
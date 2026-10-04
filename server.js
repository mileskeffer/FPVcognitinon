import express from "express";
import path from "path";

const PORT = process.env.PORT || 3000;

const app = express();
app.use(express.json());
app.use(express.static(path.join(import.meta.dirname, "public"))); // serve /public

// [ { "name": "my laptop", "time": 0.05 } ]
let avg_times = [];

app.get("/", (req, res) => {
  res.sendFile(path.join(import.meta.dirname, "index.html"));
});

app.get("/api/stats", (req, res) => {
    res.json(avg_times);
});

app.post("/api/stats", (req, res) => {  
  // [ { "name": "my laptop", "time": 0.05 } ]
  const { ping_times } = req.body;
  if (ping_times === null || ping_times === undefined) {
    console.error("[-] Invalid ping_times JSON payload!s")
    return res.status(400).send("FAIL");
  }

  avg_times = ping_times;
  console.log("[*] AVG-TIMES:", avg_times);
  return res.send("OK");
});

app.get("/api/ping", (req, res) => {
  res.set("Cache-Control", "no-store").json({ ok: true, time: Date.now() });
});

app.post("/api/control", (req, res) => {
  const validCommands = ["forward", "back", "left", "right", "rotate-left", "rotate-right"];
  const { command } = req.body ?? {};
  if (!validCommands.includes(command)) {
    return res.status(400).json({ ok: false, error: "Invalid movement command" });
  }

  console.log(`[control] ${command}`);
  res.json({ ok: true, command });
});

// Which stream the page embeds. Point it at the face-recognition feed with
// VIDEO_URL=http://<host>:8889/faces (see face_id/SETUP.md).
const VIDEO_URL = process.env.VIDEO_URL || "http://192.168.0.196:8889/cam";

app.get("/api/config", (req, res) => {
  res.json({ video_url: VIDEO_URL });
});

// Face recognition: face_id/stream.py on the laptop posts who is in view.
// { present: [{ track, name, score }], events: [{ event, name, track, score, duration_s, time }], signal }
// signal is false while the face service is running but getting no video from the drone.
let sightings = { present: [], log: [], signal: false, updated: 0 };

app.get("/api/sightings", (req, res) => {
  const age_ms = sightings.updated ? Date.now() - sightings.updated : null;
  res.set("Cache-Control", "no-store").json({ ...sightings, age_ms });
});

app.post("/api/sightings", (req, res) => {
  const { present, events = [], signal = true } = req.body ?? {};
  if (!Array.isArray(present) || !Array.isArray(events)) {
    return res.status(400).json({ ok: false, error: "Expected { present: [], events: [] }" });
  }

  sightings.present = present;
  sightings.signal = Boolean(signal);
  for (const e of events) {
    console.log(`[faces] ${e.event} ${e.name} #${e.track}`);
    sightings.log.unshift(e);
  }
  sightings.log = sightings.log.slice(0, 50);
  sightings.updated = Date.now();
  res.json({ ok: true });
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Server running on http://0.0.0.0:${PORT}`);
});
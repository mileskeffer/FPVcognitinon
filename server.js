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

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Server running on http://0.0.0.0:${PORT}`);
});
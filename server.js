import express from "express";
import path from "path";

const PORT = process.env.PORT || 3000;

const app = express();
app.use(express.json());
app.use(express.static(path.join(import.meta.dirname, "public"))); // serve /public

app.get("/", (req, res) => {
  res.sendFile(path.join(import.meta.dirname, "index.html"));
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
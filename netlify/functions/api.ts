import serverless from "serverless-http";
import express from "express";
import cors from "cors";

const app = express();
app.use(express.json());

// --- CORS ---
const allowed = new Set<string>([
  "https://cosmic-frangipane-bf2f87.netlify.app",
]);

app.use((req, res, next) => {
  const origin = req.headers.origin as string | undefined;
  if (origin && allowed.has(origin)) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Vary", "Origin");
    res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");
  }
  if (req.method === "OPTIONS") return res.status(204).end();
  next();
});

// --- Routes ---
const router = express.Router();

router.get("/health", (_req, res) => {
  res.json({ ok: true, service: "medikah-chat-api" });
});

router.post("/chat", async (req, res) => {
  const { message } = req.body ?? {};
  if (!message) return res.status(400).json({ ok: false, error: "message required" });

  const reply = `Medikah says: I received "${message}" and I'm alive!`;
  return res.json({ ok: true, reply });
});

app.use("/.netlify/functions/api", router);

export const handler = serverless(app);

import serverless from "serverless-http";
import express from "express";
import cors from "cors";
import OpenAI from "openai";

const app = express();
app.use(express.json());

// --- CORS ---
const allowed = new Set<string>([
  "https://cosmic-frangipane-bf2f87.netlify.app", // your frontend
  // add "https://medikah.app" when ready
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
  try {
    const { message } = req.body ?? {};
    if (!message) return res.status(400).json({ ok: false, error: "message required" });

    const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });
    const completion = await client.chat.completions.create({
      model: "gpt-4o-mini",
      temperature: 0.5,
      messages: [
        { role: "system", content: "You are Medikah, an empathetic, concise healthcare assistant. You do not diagnose; you suggest next steps, symptom triage, and advise seeing licensed professionals when appropriate. Respond in the user's language (English or Spanish)." },
        { role: "user", content: String(message) },
      ],
    });

    const reply = completion.choices?.[0]?.message?.content ?? "";
    return res.json({ ok: true, reply });
  } catch (err: any) {
    console.error("chat error:", err?.message || err);
    return res.status(500).json({ ok: false, error: "chat failed" });
  }
});

app.use("/.netlify/functions/api", router);

export const handler = serverless(app);

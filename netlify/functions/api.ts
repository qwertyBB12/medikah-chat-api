import serverless from "serverless-http";
import express from "express";

const app = express();
app.use(express.json());

// Health check
app.get("/health", (_req, res) => {
  res.json({ ok: true, service: "medikah-chat-api" });
});

// TODO: add your real endpoints here (e.g., /chat)
app.post("/chat", async (req, res) => {
  res.status(501).json({ ok: false, message: "chat endpoint not implemented yet" });
});

export const handler = serverless(app);

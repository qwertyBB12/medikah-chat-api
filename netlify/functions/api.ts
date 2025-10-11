import serverless from "serverless-http";
import express from "express";

const app = express();
app.use(express.json());

app.get("/health", (_req, res) => {
  res.json({ ok: true, service: "medikah-chat-api" });
});

app.post("/sanity/webhook", (req, res) => {
  // TODO: verify secret before accepting
  res.status(202).json({ received: true });
});

export const handler = serverless(app);

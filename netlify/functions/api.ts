import serverless from "serverless-http";
import express from "express";

const app = express();
app.use(express.json());

const router = express.Router();

router.get("/health", (_req, res) => {
  res.json({ ok: true, service: "medikah-chat-api" });
});

router.post("/chat", async (req, res) => {
  res.status(501).json({ ok: false, message: "chat endpoint not implemented yet" });
});

// IMPORTANT: mount under Netlify functions base
app.use("/.netlify/functions/api", router);

export const handler = serverless(app);

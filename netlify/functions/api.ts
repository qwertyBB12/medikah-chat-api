import serverless from "serverless-http";
import express from "express";

const app = express();
app.use(express.json());

// Create a router for your API endpoints
const router = express.Router();

// Health route
router.get("/health", (_req, res) => {
  res.json({ ok: true, service: "medikah-chat-api" });
});

// Stub chat route (replace with your real logic later)
router.post("/chat", async (req, res) => {
  res.status(501).json({ ok: false, message: "chat endpoint not implemented yet" });
});

// IMPORTANT: mount the router at the Netlify base path
app.use("/.netlify/functions/api", router);

export const handler = serverless(app);

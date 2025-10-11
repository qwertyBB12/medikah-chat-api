import serverless from "serverless-http";
import express from "express";
import cors from "cors";

const app = express();
app.use(express.json());

// Allow your frontend sites (add staging later if you have one)
const allowed = [
  "https://<your-frontend-site>.netlify.app",
  // "https://staging-<your-frontend>.netlify.app",
  // "https://medikah.app"  // when you attach a custom domain
];
app.use(cors({ origin: allowed, methods: ["GET","POST","OPTIONS"], allowedHeaders: ["Content-Type","Authorization"] }));

const router = express.Router();

router.get("/health", (_req, res) => {
  res.json({ ok: true, service: "medikah-chat-api" });
});

// TODO: replace with your real chat logic
router.post("/chat", async (req, res) => {
  res.status(501).json({ ok: false, message: "chat endpoint not implemented yet" });
});

// Mount under Netlify base path
app.use("/.netlify/functions/api", router);

export const handler = serverless(app);

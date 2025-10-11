import serverless from "serverless-http";
import express from "express";
import cors from "cors";

const app = express();
app.use(express.json());

// Allow your frontend origin(s) exactly (must match scheme + host)
const allowed = new Set<string>([
  "https://cosmic-frangipane-bf2f87.netlify.app",
  // add more later, e.g. "https://medikah.app"
]);

// Dynamic CORS origin check
const corsOrigin: cors.CorsOptions['origin'] = (origin, cb) => {
  // allow same-origin / serverless internal calls (no Origin header)
  if (!origin) return cb(null, true);
  if (allowed.has(origin)) return cb(null, true);
  return cb(new Error("CORS: origin not allowed"));
};

app.use(
  cors({
    origin: corsOrigin,
    methods: ["GET", "POST", "OPTIONS"],
    allowedHeaders: ["Content-Type", "Authorization"],
    credentials: false,
  })
);

// Optional: ensure preflight gets 200
app.options("*", cors({ origin: corsOrigin }));

const router = express.Router();

// Health
router.get("/health", (_req, res) => {
  res.setHeader("Vary", "Origin"); // good practice for caches
  res.json({ ok: true, service: "medikah-chat-api" });
});

// Stub chat
router.post("/chat", async (_req, res) => {
  res.setHeader("Vary", "Origin");
  res.status(501).json({ ok: false, message: "chat endpoint not implemented yet" });
});

// Mount under Netlify functions base path
app.use("/.netlify/functions/api", router);

export const handler = serverless(app);

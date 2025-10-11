import serverless from "serverless-http";
import express from "express";
import cors from "cors";

const app = express();
app.use(express.json());

// --- CORS config ---
const allowed = new Set<string>([
  "https://cosmic-frangipane-bf2f87.netlify.app", // your frontend
  // add more later, e.g. "https://medikah.app"
]);

// 1) Explicit header setter (works reliably with serverless/http)
app.use((req, res, next) => {
  const origin = req.headers.origin as string | undefined;
  if (origin && allowed.has(origin)) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Vary", "Origin");
    res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");
  }
  // handle preflight quickly
  if (req.method === "OPTIONS") {
    return res.status(204).end();
  }
  next();
});

// 2) Also keep cors() as a belt-and-suspenders
app.use(cors({
  origin: (origin, cb) => {
    if (!origin) return cb(null, true);
    return allowed.has(origin) ? cb(null, true) : cb(new Error("CORS: origin not allowed"));
  },
  methods: ["GET", "POST", "OPTIONS"],
  allowedHeaders: ["Content-Type", "Authorization"],
  credentials: false,
}));

// --- Routes ---
const router = express.Router();

router.get("/health", (_req, res) => {
  res.json({ ok: true, service: "medikah-chat-api" });
});

router.post("/chat", async (_req, res) => {
  res.status(501).json({ ok: false, message: "chat endpoint not implemented yet" });
});

// Mount under Netlify functions base
app.use("/.netlify/functions/api", router);

export const handler = serverless(app);

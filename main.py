import os
import traceback
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from pydantic import BaseModel

app = FastAPI(title="Medikah Chat API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

load_dotenv()

try:
    openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    print("OpenAI client initialised successfully.")
except KeyError:
    openai_client = None
    print("OPENAI_API_KEY not set; OpenAI client disabled.")


class ChatRequest(BaseModel):
    """Schema for a chat request."""
    message: str


class ChatResponse(BaseModel):
    """Schema for a chat response."""
    response: str


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest) -> ChatResponse:
    """
    Accepts a user message, forwards it to the configured LLM provider, and returns
    the response. This implementation calls OpenAI's GPT-4o model.
    """
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    answer = "OpenAI call failed"

    if openai_client is not None:
        try:
            print("Dispatching request to OpenAI GPT-4o:", message)
            completion = await openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": "You are Medikah's assistant. Provide concise, helpful medical guidance without offering diagnoses.",
                    },
                    {"role": "user", "content": message},
                ],
            )
            print("OpenAI raw completion received:", completion)
            choice = completion.choices[0] if completion.choices else None
            answer_text = ""
            if choice and choice.message and choice.message.content:
                answer_text = choice.message.content.strip()
            if answer_text:
                answer = answer_text
            else:
                print("OpenAI response missing content; returning fallback.")
        except Exception as exc:
            print("OpenAI request failed with exception:", exc)
            traceback.print_exc()
            answer = "OpenAI call failed"

    return ChatResponse(response=answer)


@app.get("/")
async def read_root():
    """Root endpoint providing basic info."""
    return {"message": "Medikah Chat API is running"}


@app.get("/ping")
def ping() -> dict[str, str]:
    """Lightweight health check endpoint."""
    return {"message": "pong"}

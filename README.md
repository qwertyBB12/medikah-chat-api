# Medikah Chat API

This repository contains the FastAPI backend for the Medikah MVP Chat. It exposes a
simple `/chat` endpoint that accepts user messages and returns a response. For
production use you should integrate this endpoint with an LLM provider such as
DeepSeek or OpenAI.

## Setup

1. Create and activate a Python virtual environment (optional but recommended):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Run the development server:

   ```bash
   uvicorn main:app --reload --port 8000
   ```

4. The API will be available at `http://localhost:8000`. You can test the `/chat`
   endpoint using `curl` or any HTTP client:

   ```bash
   curl -X POST http://localhost:8000/chat \
     -H "Content-Type: application/json" \
     -d '{"message": "Hello"}'
   ```

## Environment Variables

To integrate with an LLM provider, define the appropriate environment variables in
your deployment environment, for example:

- `DEEPSEEK_API_URL` – The endpoint for the DeepSeek API
- `DEEPSEEK_API_KEY` – Your DeepSeek API key

Update the `chat_endpoint` function in `main.py` to call the provider and parse
its response.
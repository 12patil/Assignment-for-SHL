"""
app/main.py
────────────────────────────────────────────────────────────────────────────
FastAPI application exposing:
  GET  /         → serves the chat UI (index.html)
  GET  /health   → {"status": "ok"}
  POST /chat     → ChatResponse (stateless, full history per call)
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import ChatRequest, ChatResponse
from . import retriever
from . import agent as agent_module

STATIC_DIR = Path(__file__).parent.parent / "static"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan: load FAISS index once at startup ────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Loading FAISS index and embedding model …")
    retriever.load_index()
    logger.info("Service ready.")
    yield


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="SHL Assessment Recommender",
    description="Conversational agent for SHL Individual Test Solutions catalog.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Mount static files ────────────────────────────────────────────────────────
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_ui():
    """Serve the chat UI at the root URL."""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Stateless conversational endpoint.
    Caller must send the full conversation history on every request.
    """
    if not request.messages:
        raise HTTPException(status_code=422, detail="messages list is empty")

    # Basic role validation
    for msg in request.messages:
        if msg.role not in ("user", "assistant"):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid role '{msg.role}'. Must be 'user' or 'assistant'.",
            )

    # Last message must come from user
    if request.messages[-1].role != "user":
        raise HTTPException(
            status_code=422,
            detail="Last message must have role 'user'.",
        )

    try:
        response = await agent_module.chat(request.messages)
        return response
    except Exception as exc:
        logger.exception("Agent error: %s", exc)
        raise HTTPException(status_code=500, detail="Internal agent error") from exc

import os
from datetime import datetime
import json

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import get_db
from database.models import Conversation, Message

load_dotenv()

RODIUMAI_URL = "https://api.rodiumai.io/v1/chat/completions"
RODIUMAI_API_KEY = os.environ["RODIUMAI_API_KEY"]
DEFAULT_MODEL = os.getenv("RODIUMAI_MODEL", "anthropic/claude-sonnet-4-5-20250929")

# Allowed models - can be overridden via env (comma-separated)
ALLOWED_MODELS_STR = os.getenv("ALLOWED_MODELS", f"{DEFAULT_MODEL},google/gemini-2.5-pro,openai/gpt-4o-mini")
_raw = [m.strip() for m in ALLOWED_MODELS_STR.split(",") if m.strip()]
ALLOWED_MODELS = []
_seen = set()
for m in _raw:
    if m not in _seen:
        _seen.add(m)
        ALLOWED_MODELS.append(m)
if not ALLOWED_MODELS:
    ALLOWED_MODELS = [DEFAULT_MODEL]

PREVIEW_LENGTH = 60

# Roles the LLM understands. Anything else stored in the DB (e.g. notifications, notes) stays out of its prompt.
LLM_ROLES = {"user", "assistant"}
NOTIFICATION_ROLE = "system-notification"
CUSTOM_ROLE = "note"  # custom role: personal note visible in chat, never sent to LLM (filtered)
NOTIFICATION_EVERY = 10  # a notification each time the dialogue reaches a multiple of this many messages
NOTIFICATION_TEXT = "Notification système : Une dizaine de messages écrits."

# Load system prompt from file
def load_system_prompt() -> str:
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "system.md")
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return (
            "Tu es Python Mentor, un tuteur bienveillant pour l'apprentissage de Python."
            "Guide par des questions plutôt que de donner directement la solution."
        )

SYSTEM_PROMPT = load_system_prompt()

app = FastAPI(title="Study Buddy Chatbot")

from schema import ConversationResponse, ConversationSummary, ChatRequest, ChatResponse, MessageResponse, ModelsResponse, NoteCreate, NoteResponse

def load_messages(db: Session, conversation_id: int) -> list[Message]:
    # A conversation's messages in order; 404 if the conversation doesn't exist.
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    ).all()


def build_llm_history(rows: list[Message]) -> list[dict]:
    # The DB history is not necessarily what the LLM sees: filter it here, just before building the request.
    # We only send roles the LLM understands (user, assistant). Custom roles like 'note' and
    # 'system-notification' are filtered out - they remain in DB for display but are never sent to the LLM.
    return [{"role": m.role, "content": m.content} for m in rows if m.role in LLM_ROLES]


@app.get("/models")
def get_models() -> ModelsResponse:
    return ModelsResponse(models=ALLOWED_MODELS)


@app.post("/conversations", status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationResponse:
    conversation = Conversation()
    db.add(conversation)
    db.commit()
    return ConversationResponse(conversation_id=conversation.id)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    # Newest first, each joined to its first message (seq 1, always the user's) for the preview.
    rows = db.execute(
        select(Conversation, Message.content)
        .outerjoin(Message, and_(Message.conversation_id == Conversation.id, Message.seq == 1))
        .order_by(Conversation.id.desc())
    ).all()
    return [
        ConversationSummary(
            id=conversation.id,
            created_at=conversation.created_at,
            preview=content[:PREVIEW_LENGTH] if content else None,
        )
        for conversation, content in rows
    ]


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[MessageResponse]:
    return [
        MessageResponse(seq=m.seq, role=m.role, content=m.content, created_at=m.created_at)
        for m in load_messages(db, conversation_id)
    ]


@app.post("/conversations/{conversation_id}/notes", status_code=201)
def create_note(conversation_id: int, req: NoteCreate, db: Session = Depends(get_db)) -> NoteResponse:
    # Ensure conversation exists
    rows = load_messages(db, conversation_id)
    next_seq = rows[-1].seq + 1 if rows else 1
    note_msg = Message(conversation_id=conversation_id, seq=next_seq, role=CUSTOM_ROLE, content=req.content)
    db.add(note_msg)
    try:
        db.commit()
        db.refresh(note_msg)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="The conversation was updated concurrently, please retry.")
    return NoteResponse(seq=note_msg.seq, role=note_msg.role, content=note_msg.content, created_at=note_msg.created_at)


@app.post("/notes", status_code=201)
def create_note_alt(req: NoteCreate, db: Session = Depends(get_db)) -> NoteResponse:
    return create_note(req.conversation_id, req, db)


@app.post("/chat")
async def chat(req: ChatRequest, db: Session = Depends(get_db)):
    # Validate model
    model_to_use = req.model or DEFAULT_MODEL
    if model_to_use not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail=f"Model not allowed. Allowed models: {ALLOWED_MODELS}")

    # Load this conversation from the database, in message order.
    rows = load_messages(db, req.conversation_id)
    history = build_llm_history(rows)
    # seq follows every stored row (notifications included) so it stays unique.
    next_seq = rows[-1].seq + 1 if rows else 1

    # The LLM is stateless: resend the system prompt + the whole conversation each turn.
    user_message = {"role": "user", "content": req.message}
    messages_llm = [{"role": "system", "content": SYSTEM_PROMPT}, *history, user_message]

    # First, persist user message
    user_msg = Message(conversation_id=req.conversation_id, seq=next_seq, role="user", content=req.message)
    db.add(user_msg)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="The conversation was updated concurrently, please retry."
        )

    assistant_seq = next_seq + 1

    async def event_generator():
        full_chunks = []
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    RODIUMAI_URL,
                    headers={"Authorization": f"Bearer {RODIUMAI_API_KEY}"},
                    json={
                        "model": model_to_use,
                        "messages": messages_llm,
                        "max_tokens": 512,
                        "stream": True,
                    },
                    timeout=60,
                ) as response:
                    if response.status_code != 200:
                        raise HTTPException(status_code=502, detail="The LLM API call failed.")
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                break
                            try:
                                parsed = json.loads(data)
                                delta = parsed.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content")
                                if content:
                                    full_chunks.append(content)
                                    yield f"data: {json.dumps({'content': content})}\n\n"
                            except Exception:
                                continue
                    yield f"data: {json.dumps({'done': True})}\n\n"
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail="The LLM API call failed.") from exc

        # After streaming to client completes, save assistant + notification to DB
        reply_text = "".join(full_chunks)
        try:
            db.add(Message(conversation_id=req.conversation_id, seq=assistant_seq, role="assistant", content=reply_text))
            notification = None
            if (len(history) + 2) % NOTIFICATION_EVERY == 0:
                notification = NOTIFICATION_TEXT
                db.add(Message(conversation_id=req.conversation_id, seq=assistant_seq + 1, role=NOTIFICATION_ROLE, content=notification))
            db.commit()
        except IntegrityError:
            db.rollback()
        except Exception:
            db.rollback()

    return StreamingResponse(event_generator(), media_type="text/event-stream")
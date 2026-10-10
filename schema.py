from pydantic import BaseModel
from datetime import datetime

class ConversationResponse(BaseModel):
    conversation_id: int


class ConversationSummary(BaseModel):
    id: int
    created_at: datetime
    preview: str | None  # first user message, truncated; None while the conversation is empty


class ChatRequest(BaseModel):
    conversation_id: int
    message: str
    model: str | None = None


class ChatResponse(BaseModel):
    reply: str
    notification: str | None = None  # set when this turn also stored a system-notification


class MessageResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime


class ModelsResponse(BaseModel):
    models: list[str]


class NoteCreate(BaseModel):
    conversation_id: int | None = None
    content: str


class NoteResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime
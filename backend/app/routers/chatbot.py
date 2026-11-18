from fastapi import APIRouter
from app import schemas
from app.services.chatbot_service import detect_intent

router = APIRouter(prefix="/chatbot", tags=["chatbot"])


@router.post("/message", response_model=schemas.ChatMessageOut)
def send_message(payload: schemas.ChatMessageIn):
    result = detect_intent(payload.message)
    return schemas.ChatMessageOut(
        intent=result.intent,
        response=result.response,
        needs_human_handoff=result.needs_human_handoff,
    )

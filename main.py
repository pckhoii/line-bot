import base64
import asyncio
import hashlib
import hmac
import json
import logging
import os
from functools import lru_cache
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from google import genai

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("line-bot")

app = FastAPI(title="LINE AI mention bot")

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
SYSTEM_PROMPT = (
    "Bạn là trợ lý của Bot mua chia trong nhóm LINE. "
    "Trả lời bằng tiếng Việt, lịch sự, ngắn gọn và hữu ích. "
    "Nếu không chắc, hãy nói rõ rằng bạn chưa có đủ thông tin; đừng bịa. "
    "Không dùng bảng Markdown."
)
TEXT_TRIGGER = os.getenv("BOT_TEXT_TRIGGER", "@bot").strip().casefold()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "line-ai-mention-bot"}


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature: str | None = Header(default=None),
) -> JSONResponse:
    raw_body = await request.body()
    verify_line_signature(raw_body, x_line_signature)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="Invalid JSON") from error

    # Reply to LINE immediately; process valid events in a background task.
    background_tasks.add_task(process_events, payload.get("events", []))
    return JSONResponse({"ok": True})


def verify_line_signature(raw_body: bytes, signature: str | None) -> None:
    channel_secret = required_env("LINE_CHANNEL_SECRET")
    if not signature:
        raise HTTPException(status_code=401, detail="Missing LINE signature")

    expected = base64.b64encode(
        hmac.new(channel_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    ).decode("utf-8")
    if not hmac.compare_digest(expected, signature):
        logger.warning("Rejected webhook with an invalid LINE signature")
        raise HTTPException(status_code=401, detail="Invalid LINE signature")


async def process_events(events: list[dict[str, Any]]) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        for event in events:
            await process_event(client, event)


async def process_event(client: httpx.AsyncClient, event: dict[str, Any]) -> None:
    message = event.get("message", {})
    if event.get("type") != "message" or message.get("type") != "text":
        return

    if not should_reply(event, message):
        logger.info("Ignored a message without a bot trigger")
        return

    reply_token = event.get("replyToken")
    if not reply_token:
        logger.warning("Ignored a bot mention without a reply token")
        return

    user_text = remove_text_trigger(message.get("text", "").strip())
    if not user_text:
        return

    try:
        answer = await generate_answer(user_text)
        await reply_to_line(client, reply_token, answer)
    except Exception:
        logger.exception("Could not answer the LINE mention")
        await reply_to_line(
            client,
            reply_token,
            "Mình đang gặp lỗi khi xử lý câu hỏi. Bạn thử lại sau ít phút nhé.",
        )


def mentions_this_bot(message: dict[str, Any]) -> bool:
    mentionees = message.get("mention", {}).get("mentionees", [])
    return any(mention.get("isSelf") is True for mention in mentionees)


def should_reply(event: dict[str, Any], message: dict[str, Any]) -> bool:
    # Keep the bot silent in 1:1 chats. It responds in groups only when it is
    # a real LINE mention or the member starts the text with the fallback trigger.
    if event.get("source", {}).get("type") not in {"group", "room"}:
        return False
    return mentions_this_bot(message) or message.get("text", "").strip().casefold().startswith(TEXT_TRIGGER)


def remove_text_trigger(text: str) -> str:
    if text.casefold().startswith(TEXT_TRIGGER):
        return text[len(TEXT_TRIGGER):].strip()
    return text


async def generate_answer(user_text: str) -> str:
    interaction = await asyncio.to_thread(
        gemini_client().interactions.create,
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        input=f"{SYSTEM_PROMPT}\n\nCâu hỏi của người dùng: {user_text}",
    )
    answer = getattr(interaction, "output_text", "")
    if not answer:
        raise RuntimeError("Gemini returned no text output")
    return answer[:5000]


@lru_cache(maxsize=1)
def gemini_client() -> genai.Client:
    return genai.Client(api_key=required_env("GEMINI_API_KEY"))


async def reply_to_line(client: httpx.AsyncClient, reply_token: str, text: str) -> None:
    response = await client.post(
        LINE_REPLY_URL,
        headers={
            "Authorization": f"Bearer {required_env('LINE_CHANNEL_ACCESS_TOKEN')}",
            "Content-Type": "application/json",
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        },
    )
    response.raise_for_status()


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

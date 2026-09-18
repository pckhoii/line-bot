import base64
import hashlib
import hmac
import json
import logging
import os
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("line-bot")

app = FastAPI(title="LINE AI mention bot")

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
SYSTEM_PROMPT = (
    "Bạn là trợ lý của Bot mua chia trong nhóm LINE. "
    "Trả lời bằng tiếng Việt, lịch sự, ngắn gọn và hữu ích. "
    "Nếu không chắc, hãy nói rõ rằng bạn chưa có đủ thông tin; đừng bịa. "
    "Không dùng bảng Markdown."
)


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

    if not mentions_this_bot(message):
        logger.info("Ignored a text message without a bot mention")
        return

    reply_token = event.get("replyToken")
    if not reply_token:
        logger.warning("Ignored a bot mention without a reply token")
        return

    user_text = message.get("text", "").strip()
    if not user_text:
        return

    try:
        answer = await generate_answer(client, user_text)
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


async def generate_answer(client: httpx.AsyncClient, user_text: str) -> str:
    response = await client.post(
        OPENAI_RESPONSES_URL,
        headers={
            "Authorization": f"Bearer {required_env('OPENAI_API_KEY')}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            "instructions": SYSTEM_PROMPT,
            "input": user_text,
            "max_output_tokens": 350,
            "store": False,
        },
    )
    response.raise_for_status()
    answer = response.json().get("output_text") or extract_output_text(response.json())
    if not answer:
        raise RuntimeError("OpenAI returned no text output")
    return answer[:5000]


def extract_output_text(response: dict[str, Any]) -> str:
    text_parts: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                text_parts.append(content["text"])
    return "\n".join(text_parts)


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

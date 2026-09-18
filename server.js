import crypto from "node:crypto";
import http from "node:http";
import fs from "node:fs";

loadEnvFile(".env");

const port = Number(process.env.PORT || 3000);
const channelSecret = process.env.LINE_CHANNEL_SECRET;
const channelAccessToken = process.env.LINE_CHANNEL_ACCESS_TOKEN;

if (!channelSecret || !channelAccessToken) {
  console.error("Missing LINE credentials. Copy .env.example to .env and add the two values.");
  process.exit(1);
}

const botReplies = {
  "xin chào": "Chào bạn! Mình là bot LINE đang chạy qua webhook. Bạn muốn hỏi gì?",
  "hello": "Hello! Kết nối LINE Messaging API đã hoạt động.",
  "giờ làm việc": "Giờ hỗ trợ: Thứ 2–Thứ 6, 08:30–17:30.",
  "địa chỉ": "Bạn cho mình biết khu vực, mình sẽ gửi địa chỉ gần nhất nhé.",
};

const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    return sendJson(res, 200, { ok: true, service: "line-webhook" });
  }

  if (req.method !== "POST" || req.url !== "/webhook") {
    return sendJson(res, 404, { error: "Not found" });
  }

  const rawBody = await readBody(req);
  const signature = req.headers["x-line-signature"];

  if (!isValidSignature(rawBody, signature)) {
    console.warn("Rejected webhook with invalid LINE signature.");
    return sendJson(res, 401, { error: "Invalid signature" });
  }

  // LINE expects a fast 200 response, including the Verify request (events: []).
  sendJson(res, 200, { ok: true });

  try {
    const payload = JSON.parse(rawBody);
    await Promise.all((payload.events || []).map(handleEvent));
  } catch (error) {
    console.error("Could not handle LINE event:", error);
  }
});

server.listen(port, () => {
  console.log(`LINE webhook is listening at http://localhost:${port}/webhook`);
  console.log(`Health check: http://localhost:${port}/health`);
});

async function handleEvent(event) {
  if (event.type !== "message" || event.message?.type !== "text") return;

  const userText = event.message.text.trim();
  const normalized = userText.toLocaleLowerCase("vi-VN");
  const answer = botReplies[normalized]
    || `Bạn vừa nhắn: “${userText}”\n\nGõ “giờ làm việc” hoặc “địa chỉ” để thử kịch bản.`;

  console.log(`Message received: ${userText}`);
  await reply(event.replyToken, answer);
}

async function reply(replyToken, text) {
  const response = await fetch("https://api.line.me/v2/bot/message/reply", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${channelAccessToken}`,
    },
    body: JSON.stringify({
      replyToken,
      messages: [{ type: "text", text }],
    }),
  });

  if (!response.ok) {
    throw new Error(`LINE reply failed (${response.status}): ${await response.text()}`);
  }
}

function isValidSignature(rawBody, signature) {
  if (typeof signature !== "string") return false;
  const expected = crypto
    .createHmac("sha256", channelSecret)
    .update(rawBody)
    .digest("base64");
  const received = Buffer.from(signature);
  const calculated = Buffer.from(expected);
  return received.length === calculated.length && crypto.timingSafeEqual(received, calculated);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", chunk => { body += chunk; });
    req.on("end", () => resolve(body));
    req.on("error", reject);
  });
}

function sendJson(res, status, data) {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(data));
}

function loadEnvFile(path) {
  if (!fs.existsSync(path)) return;
  for (const line of fs.readFileSync(path, "utf8").split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (match && !process.env[match[1]]) process.env[match[1]] = match[2];
  }
}

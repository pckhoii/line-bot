# LINE team mua chia assistant

Bot Groq cho nhóm LINE của team mua chia. Bot chỉ trả lời khi được nhắc bằng `@bot` ở đầu tin nhắn (hoặc LINE mention thực), lưu ngữ cảnh gần đây riêng theo từng nhóm và tra web qua Groq khi cần.

## Biến Railway

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `GROQ_API_KEY`
- `GROQ_MODEL` (tuỳ chọn, mặc định `openai/gpt-oss-20b`)
- `GROQ_WEB_MODEL` (tuỳ chọn, mặc định `groq/compound`)
- `BOT_TEXT_TRIGGER` (tuỳ chọn, mặc định `@bot`)
- `BOT_HISTORY_DB_PATH` (tuỳ chọn, mặc định `/tmp/line_bot_history.db`)
- `HISTORY_MESSAGE_LIMIT` (tuỳ chọn, mặc định 12; tối đa 30)
- `HISTORY_STORAGE_LIMIT` (tuỳ chọn, mặc định 200; tối đa 1000)
- `MAX_MODEL_HISTORY_CHARS` (tuỳ chọn, mặc định 6000; tối đa 12000)

## Lưu lịch sử sau khi deploy

Để lịch sử không mất khi Railway redeploy, trong service Railway tạo **Volume** và mount tại `/data`. Sau đó thêm biến `BOT_HISTORY_DB_PATH` với giá trị `/data/line_bot_history.db`.

Không cần gắn Volume thì bot vẫn chạy, nhưng lịch sử sẽ mất nếu instance được tạo lại.

Bot lưu tối đa 200 tin nhắn gần nhất của mỗi nhóm và gửi 12 tin mới nhất, tối đa 6.000 ký tự, cho Groq để lấy ngữ cảnh. Bot vẫn chỉ trả lời khi có `@bot`.

## Webhook và sử dụng

Webhook URL trong LINE:

```text
https://YOUR-RAILWAY-DOMAIN/webhook
```

Health check:

```text
https://YOUR-RAILWAY-DOMAIN/health
```

Trong nhóm, ví dụ:

```text
@bot tổng hợp giúp các việc cần chốt hôm nay
@bot tra giá thị trường sản phẩm X mới nhất
@bot /web tìm thông tin nhà cung cấp X
@bot dựa trên trao đổi phía trên, soạn tin nhắn hỏi nhà cung cấp
```

Bot tự tra web khi có các từ như `tra`, `tìm`, `giá`, `mới nhất`, `hôm nay`; dùng `/web` sau `@bot` để ép tra cứu. Quota phụ thuộc Groq project. Không gửi khóa, mật khẩu, dữ liệu khách hàng hoặc dữ liệu nội bộ nhạy cảm qua bot.

# LINE AI mention bot (Python)

This FastAPI bot receives LINE webhooks at `POST /webhook`, validates the `x-line-signature` HMAC, and uses OpenAI to reply only when a LINE group member mentions the bot.

## AI configuration

Add these variables in Railway (and in a local `.env` only if running locally):

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `OPENAI_API_KEY`
- Optional: `OPENAI_MODEL` (defaults to `gpt-5-mini`)

The bot only responds in group chats. It checks LINE's structured `mention.mentionees[].isSelf` flag and also supports a text fallback: start a group message with `@bot`, for example `@bot kiểm tra giúp tôi`. Set `BOT_TEXT_TRIGGER` in Railway to use another trigger.

## 1. Rotate the secret shown in the screenshot

The Channel secret in the screenshot should be treated as exposed. Reissue it in the LINE Developers Console before placing a new value in `.env`.

## 2. Get the two credentials

In **LINE Developers Console → your Messaging API channel → Messaging API**:

1. Copy the newly reissued **Channel secret**.
2. In **Channel access token**, issue a long-lived token and copy it.

Do not paste either value into chat or commit it to Git.

## 3. Deploy permanently to Railway (recommended)

1. Create an empty GitHub repository and push this project. The `.gitignore` prevents `.env` from being uploaded.
2. In Railway, create a project and select **Deploy from GitHub Repo**.
3. In the service's **Variables** tab, add `LINE_CHANNEL_SECRET` and `LINE_CHANNEL_ACCESS_TOKEN`. Do not set `PORT`; Railway supplies it.
4. In **Settings → Networking**, choose **Generate Domain**.
5. Wait for the deploy to become active. Open `https://YOUR-APP.up.railway.app/health`; it should return an `ok: true` response.

The included `railway.json` sets the start command, `/health` deployment check, and restart behavior. The service keeps running when your computer is off.

## 4. Connect it in LINE

In the Webhook URL box, enter:

```
https://YOUR-APP.up.railway.app/webhook
```

Click **Save**, then **Verify**. A successful verify request contains no events, which this server handles. Turn on **Use webhook** in the Messaging API settings.

In LINE Official Account Manager, turn off **Greeting message** and **Auto-reply messages** while testing, otherwise those built-in messages can be confused with the bot's reply.

## Optional: run only on your computer for development

In PowerShell:

```powershell
Copy-Item .env.example .env
notepad .env
node server.js
```

`http://localhost:3000/health` should return `{ "ok": true, "service": "line-webhook" }`.

### Make localhost public with HTTPS

In a second PowerShell window:

```powershell
npx.cmd localtunnel --port 3000
```

Copy the HTTPS URL it prints, for example `https://purple-bot.loca.lt`.

### Connect it in LINE

In the Webhook URL box shown in the screenshot, enter:

```
https://YOUR-TUNNEL-URL.loca.lt/webhook
```

Click **Save**, then **Verify**. A successful verify request contains no events, which this server handles. Turn on **Use webhook** in the Messaging API settings.

In LINE Official Account Manager, turn off **Greeting message** and **Auto-reply messages** while testing, otherwise those built-in messages can be confused with the bot's reply.

## 5. Test in LINE

Scan the QR code in the Messaging API tab to add the Official Account as a friend. Send `xin chào`, `giờ làm việc`, `địa chỉ`, or any other text. The terminal prints received messages and the bot replies in the LINE chat.

## Keep it running

Localtunnel is only for development. Its address changes when stopped, so update the Webhook URL after restarting it. For production, deploy this server to a host with persistent HTTPS and configure the same two environment variables there.

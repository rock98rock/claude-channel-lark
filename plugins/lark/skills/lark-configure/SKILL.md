---
name: lark-configure
description: Set up the Lark channel — save app credentials and review access policy. Use when the user pastes Lark app credentials, asks to configure Lark, asks "how do I set this up" or wants to check channel status.
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Bash(ls *)
  - Bash(mkdir *)
  - Bash(chmod *)
---

# /lark:configure — Lark Channel Setup

Writes the Lark app credentials to `~/.claude/channels/lark/.env` and orients the
user on access policy. The server reads the .env file at boot.

Arguments passed: `$ARGUMENTS`

---

## Dispatch on arguments

### No args — status and guidance

Read both state files and give the user a complete picture:

1. **Credentials** — check `~/.claude/channels/lark/.env` for
   `LARK_APP_ID` and `LARK_APP_SECRET`. Show set/not-set; if set, show first 10
   chars masked (`cli_a94...`).

2. **Access** — check `~/.claude/channels/lark/access.json`. Show:
   - Number of allowlisted open_ids
   - Current ack reaction emoji
   - If empty allowlist: note that all users are allowed (convenience mode)

3. **Next steps** — guide based on what's missing:
   - No credentials → show how to create a Lark Custom Bot
   - No access.json → it will be created automatically on first message
   - Everything set → show how to launch: `claude --channels plugin:lark`

### With arguments — save credentials

If the user provides one or two arguments, interpret as:
- `$ARG1` = `LARK_APP_ID`
- `$ARG2` = `LARK_APP_SECRET`

Create the directory and write credentials:
```bash
mkdir -p ~/.claude/channels/lark
```

Write to `~/.claude/channels/lark/.env`:
```
LARK_APP_ID=<value>
LARK_APP_SECRET=<value>
```

Set permissions:
```bash
chmod 600 ~/.claude/channels/lark/.env
```

Confirm what was saved and suggest next steps.

---

## Lark App Creation Guide

If the user needs to create a new Lark app, provide these instructions:

1. Go to **Lark Open Platform** → **Developer Console**
   - International: `open.larksuite.com`
   - China: `open.feishu.cn`
2. Click **Create Custom App**
3. Under **Capabilities** → enable **Bot**
4. Under **Permissions & Scopes** → add:
   - `im:message` — read and send direct messages and group chat messages
   - `im:message:send_as_bot` — send messages as an app
   - `im:message.group_msg:readonly` — obtain all messages in associated group chats
   - `im:message.p2p_msg:readonly` — get direct messages sent to bot
   - `im:resource` — read and upload images or other files
5. Under **Event Subscriptions** → enable **Receive messages** (`im.message.receive_v1`)
6. **Publish** the app (or request admin approval)
7. Copy the **App ID** and **App Secret** from the Credentials page
8. Run: `/lark:configure <app_id> <app_secret>`

### For China Feishu users

Set the domain in `~/.claude/channels/lark/.env`:
```
LARK_DOMAIN=feishu
```

---

## Message flow (explain to the user)

When the channel is running:
1. User sends a message in Lark → bot reacts with OK and replies "Working on it..." in thread
2. Claude Code processes the message and replies → the "Working on it..." card is updated with the answer
3. Bot adds DONE reaction to the original message

---

## Important

- Never display the full App Secret. Always mask it.
- The `.env` file should be owner-readable only (chmod 600).
- If the user asks about switching from Telegram: this is a separate channel plugin
  that can run alongside or instead of the Telegram channel.
- The channel uses WebSocket mode — no public IP or webhook URL is needed.

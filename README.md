# claude-channel-lark

Lark/Feishu channel plugin for Claude Code. Bridges Lark IM messages to your Claude Code session via MCP.

**No public IP required** — uses Lark's WebSocket long-connection mode.

## Features

- Receive and reply to Lark messages from within Claude Code
- Interactive cards with full markdown rendering
- "Working on it..." running card — immediate feedback, updated in-place with the final answer
- Emoji reactions (OK on receive, DONE on reply)
- File and image attachments (send and receive)
- Allowlist access control
- Permission relay (Allow/Deny buttons for tool approvals)
- Retry with exponential backoff on API failures
- Supports both international Lark and China Feishu

## Quick Start

### 1. Create a Lark App

1. Go to [Lark Open Platform](https://open.larksuite.com) (or [Feishu](https://open.feishu.cn) for China)
2. Click **Create Custom App**
3. Under **Capabilities** → enable **Bot**
4. Under **Permissions & Scopes** → add:
   - `im:message` — read and send direct messages and group chat messages
   - `im:message:send_as_bot` — send messages as an app
   - `im:message.group_msg:readonly` — obtain all messages in associated group chats
   - `im:message.p2p_msg:readonly` — get direct messages sent to bot
   - `im:resource` — read and upload images or other files
5. Under **Event Subscriptions** → enable **Receive messages (im.message.receive_v1)**
6. **Publish** the app (or request approval from your admin)
7. Copy the **App ID** and **App Secret** from the Credentials page

### 2. Install the Plugin

Inside a Claude Code session, run:

```
/plugin marketplace add snsoft-my/lark-mcp-claude
/plugin install lark@snsoft-my-lark-mcp-claude
```

### 3. Configure Credentials

Run inside Claude Code:

```
/lark:configure <app_id> <app_secret>
```

Or manually create `~/.claude/channels/lark/.env`:

```
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=xxx
```

### 4. Set Up Access (Optional)

By default, all users can message the bot. To restrict:

```
/lark:access allow ou_xxxxxxxxxxxxx
```

### 5. Launch

```bash
claude --channels plugin:lark
```

Send a message to your bot in Lark — it should appear in your Claude Code session.

## Message Flow

```
User sends message in Lark
  → Bot adds OK reaction
  → Bot creates "Working on it..." card in thread
  → Message forwarded to Claude Code session
  → Claude processes and calls reply tool
  → Running card updated with the answer
  → Bot adds DONE reaction to original message
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LARK_APP_ID` | Yes | — | Lark app ID |
| `LARK_APP_SECRET` | Yes | — | Lark app secret |
| `LARK_DOMAIN` | No | `lark` | `lark` for international, `feishu` for China |
| `LARK_STATE_DIR` | No | `~/.claude/channels/lark` | State directory |

## Skills

| Skill | Command | Description |
|-------|---------|-------------|
| configure | `/lark:configure` | Save credentials and check status |
| access | `/lark:access` | Manage allowlist and ack settings |

## Architecture

- **Transport:** MCP over stdio
- **Lark connection:** WebSocket (lark-oapi SDK) in a background thread
- **Notification bridge:** `queue.Queue` drains from Lark thread → MCP notifications on stdout
- **Reply format:** Interactive cards with markdown (patchable in-place)

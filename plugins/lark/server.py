#!/usr/bin/env python3
"""Lark channel for Claude Code.

Self-contained MCP server with allowlist access control. State lives in
~/.claude/channels/lark/access.json — managed by the /lark:access skill.

Uses lark-oapi WebSocket mode — no public IP or webhook URL required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    JSONRPCMessage,
    JSONRPCNotification,
    TextContent,
    Tool,
)

# ---------------------------------------------------------------------------
# Logging — all goes to stderr (stdout is MCP stdio transport)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="lark channel: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("lark-channel")

# ---------------------------------------------------------------------------
# State paths
# ---------------------------------------------------------------------------

STATE_DIR = Path(
    os.environ.get("LARK_STATE_DIR", Path.home() / ".claude" / "channels" / "lark")
)
ACCESS_FILE = STATE_DIR / "access.json"
ENV_FILE = STATE_DIR / ".env"
INBOX_DIR = STATE_DIR / "inbox"

# ---------------------------------------------------------------------------
# Credential loading — from ~/.claude/channels/lark/.env
# ---------------------------------------------------------------------------


def _load_env_file() -> None:
    """Load .env into os.environ. Real env wins."""
    try:
        ENV_FILE.chmod(0o600)
        for line in ENV_FILE.read_text().splitlines():
            m = re.match(r"^(\w+)=(.*)$", line)
            if m and os.environ.get(m.group(1)) is None:
                os.environ[m.group(1)] = m.group(2)
    except FileNotFoundError:
        pass


_load_env_file()

# Ensure access.json exists — Claude Code requires it for channel approval
STATE_DIR.mkdir(parents=True, exist_ok=True)
if not ACCESS_FILE.exists():
    ACCESS_FILE.write_text('{"dmPolicy": "open", "allowFrom": []}\n')
    logger.info("created %s", ACCESS_FILE)

APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")

if not APP_ID or not APP_SECRET:
    sys.stderr.write(
        f"lark channel: LARK_APP_ID and LARK_APP_SECRET required\n"
        f"  set in {ENV_FILE}\n"
        f"  format:\n"
        f"    LARK_APP_ID=cli_xxx\n"
        f"    LARK_APP_SECRET=xxx\n"
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Lark API client
# ---------------------------------------------------------------------------

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        Emoji,
        GetMessageResourceRequest,
        PatchMessageRequest,
        PatchMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )
except ImportError:
    sys.stderr.write("lark channel: lark-oapi not installed. Run: uv add lark-oapi\n")
    sys.exit(1)

DOMAIN = os.environ.get("LARK_DOMAIN", "lark")
_domain = lark.LARK_DOMAIN if DOMAIN == "lark" else lark.FEISHU_DOMAIN

api_client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .domain(_domain)
    .log_level(lark.LogLevel.WARNING)
    .build()
)

# ---------------------------------------------------------------------------
# Bot identity — resolve the bot's own open_id for @mention detection
# ---------------------------------------------------------------------------

_BOT_OPEN_ID: str = ""


def _resolve_bot_open_id() -> str:
    """Get the bot's open_id via the Lark bot info API (raw HTTP)."""
    import urllib.request

    base = (
        "https://open.larksuite.com" if DOMAIN == "lark" else "https://open.feishu.cn"
    )
    try:
        token_data = json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode()
        token_req = urllib.request.Request(
            f"{base}/open-apis/auth/v3/tenant_access_token/internal",
            data=token_data,
            headers={"Content-Type": "application/json"},
        )
        token_resp = json.loads(urllib.request.urlopen(token_req, timeout=10).read())
        token = token_resp.get("tenant_access_token", "")
        if not token:
            logger.warning("failed to get tenant_access_token")
            return ""
        bot_req = urllib.request.Request(
            f"{base}/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        bot_resp = json.loads(urllib.request.urlopen(bot_req, timeout=10).read())
        oid = bot_resp.get("bot", {}).get("open_id", "")
        if oid:
            logger.info("bot open_id resolved: %s", oid)
            return oid
        logger.warning("bot info API returned no open_id")
    except Exception:
        logger.warning(
            "failed to resolve bot open_id — group @mention filtering may not work"
        )
    return ""


_BOT_OPEN_ID = _resolve_bot_open_id()

# ---------------------------------------------------------------------------
# Card building — interactive cards with markdown
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 30 * 1024 * 1024


def _build_card(text: str) -> str:
    """Build a Lark interactive card with markdown content."""
    card = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "elements": [{"tag": "markdown", "content": text}],
    }
    return json.dumps(card)


def _build_permission_card(
    request_id: str,
    tool_name: str,
    description: str,
    input_preview: str,
) -> str:
    """Build an interactive card with Allow/Deny buttons for permission requests."""
    try:
        pretty_input = json.dumps(json.loads(input_preview), indent=2)
    except (json.JSONDecodeError, TypeError):
        pretty_input = input_preview

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"Permission: {tool_name}"},
            "template": "orange",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**Tool:** `{tool_name}`"},
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**Description:** {description}",
                },
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**Input:**\n```json\n{pretty_input}\n```",
                },
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Allow"},
                        "type": "primary",
                        "value": json.dumps(
                            {"action": "allow", "request_id": request_id}
                        ),
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Deny"},
                        "type": "danger",
                        "value": json.dumps(
                            {"action": "deny", "request_id": request_id}
                        ),
                    },
                ],
            },
        ],
    }
    return json.dumps(card)


# ---------------------------------------------------------------------------
# Lark API helpers (all sync — called via anyio.to_thread.run_sync)
# ---------------------------------------------------------------------------


def _id_type(receive_id: str) -> str:
    """Detect Lark receive_id type from prefix."""
    if receive_id.startswith("oc_"):
        return "chat_id"
    return "open_id"


def _send_card(chat_id: str, card_content: str) -> str | None:
    """Send a card message and return the message_id."""
    request = (
        CreateMessageRequest.builder()
        .receive_id_type(_id_type(chat_id))
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(card_content)
            .build()
        )
        .build()
    )
    response = api_client.im.v1.message.create(request)
    if not response.success():
        raise RuntimeError(
            f"send_card failed: code={response.code}, msg={response.msg}"
        )
    return getattr(response.data, "message_id", None)


def _reply_card(message_id: str, card_content: str) -> str | None:
    """Reply to a message with a card and return the new message_id."""
    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type("interactive")
            .content(card_content)
            .reply_in_thread(True)
            .build()
        )
        .build()
    )
    response = api_client.im.v1.message.reply(request)
    if not response.success():
        raise RuntimeError(
            f"reply_card failed: code={response.code}, msg={response.msg}"
        )
    return getattr(response.data, "message_id", None)


def _update_card(message_id: str, card_content: str) -> None:
    """Patch an existing card message in-place."""
    request = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(PatchMessageRequestBody.builder().content(card_content).build())
        .build()
    )
    response = api_client.im.v1.message.patch(request)
    if not response.success():
        raise RuntimeError(
            f"update_card failed: code={response.code}, msg={response.msg}"
        )


def _add_reaction(message_id: str, emoji_type: str = "OK") -> None:
    """Add an emoji reaction to a message."""
    request = (
        CreateMessageReactionRequest.builder()
        .message_id(message_id)
        .request_body(
            CreateMessageReactionRequestBody.builder()
            .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
            .build()
        )
        .build()
    )
    response = api_client.im.v1.message_reaction.create(request)
    if not response.success():
        logger.warning(
            "add_reaction failed: code=%s, msg=%s", response.code, response.msg
        )


def _upload_image(path: str) -> str:
    """Upload an image and return the image_key."""
    with open(path, "rb") as f:
        request = (
            CreateImageRequest.builder()
            .request_body(
                CreateImageRequestBody.builder().image_type("message").image(f).build()
            )
            .build()
        )
        response = api_client.im.v1.image.create(request)
    if not response.success():
        raise RuntimeError(
            f"image upload failed: code={response.code}, msg={response.msg}"
        )
    return response.data.image_key


def _upload_file(path: str, filename: str) -> str:
    """Upload a file and return the file_key."""
    suffix = Path(path).suffix.lower()
    type_map = {
        ".xls": "xls",
        ".xlsx": "xls",
        ".csv": "xls",
        ".ppt": "ppt",
        ".pptx": "ppt",
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "doc",
    }
    file_type = type_map.get(suffix, "stream")

    with open(path, "rb") as f:
        request = (
            CreateFileRequest.builder()
            .request_body(
                CreateFileRequestBody.builder()
                .file_type(file_type)
                .file_name(filename)
                .file(f)
                .build()
            )
            .build()
        )
        response = api_client.im.v1.file.create(request)
    if not response.success():
        raise RuntimeError(
            f"file upload failed: code={response.code}, msg={response.msg}"
        )
    return response.data.file_key


def _send_file_message(
    chat_id: str, file_key: str, msg_type: str, reply_to: str | None = None
) -> str | None:
    """Send a file/image message."""
    if msg_type == "image":
        content = json.dumps({"image_key": file_key})
    else:
        content = json.dumps({"file_key": file_key})

    if reply_to:
        request = (
            ReplyMessageRequest.builder()
            .message_id(reply_to)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .reply_in_thread(True)
                .build()
            )
            .build()
        )
        response = api_client.im.v1.message.reply(request)
    else:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(_id_type(chat_id))
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = api_client.im.v1.message.create(request)

    if not response.success():
        raise RuntimeError(
            f"send_file failed: code={response.code}, msg={response.msg}"
        )
    return getattr(response.data, "message_id", None)


def _download_resource(message_id: str, file_key: str, res_type: str) -> Path:
    """Download a message resource (image/file) to inbox."""
    request = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(file_key)
        .type(res_type)
        .build()
    )
    response = api_client.im.v1.message_resource.get(request)
    if not response.success():
        raise RuntimeError(f"download failed: code={response.code}, msg={response.msg}")

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", file_key)
    ext = "jpg" if res_type == "image" else "bin"
    dest = INBOX_DIR / f"{int(time.time())}-{safe_key}.{ext}"
    dest.write_bytes(response.file.read())
    return dest


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

INSTRUCTIONS = """The sender reads Lark, not this session. Anything you want them to see must go through the reply tool — your transcript output never reaches their chat.

Messages from Lark arrive as <channel source="lark" chat_id="..." message_id="..." thread_id="..." user="..." ts="...">. If the tag has an image_key attribute, call download_attachment to fetch the image, then Read the returned path. Reply with the reply tool — pass chat_id and reply_to (set to message_id) so the response threads under the original message.

Topic separation: each message has a thread_id. Messages with the same thread_id belong to the same conversation topic. When you see a new thread_id, treat it as a completely new topic — do not carry over assumptions, context, or state from previous threads. Focus only on what the user is asking in the current thread.

reply sends interactive cards with full markdown rendering (headers, bold, italic, code blocks, lists, links). Always pass reply_to with the message_id from the inbound <channel> block so replies appear as threads under the original message. Use edit_message to update a card in-place for interim progress updates. Edits don't trigger push notifications — when a long task completes, send a new reply so the user's device pings.

Lark's Bot API exposes no history or search — you only see messages as they arrive. If you need earlier context, ask the user to paste it or summarize.

Access control is managed by the Lark app's own permissions — no separate allowlist is needed."""

mcp_server = Server("lark")

# Shared state for cross-thread notification bridge
_write_stream: Any = None
_inbound_queue: queue.Queue[dict[str, Any]] = queue.Queue()
_pending_permissions: dict[str, dict[str, str]] = {}
_running_cards: dict[str, str] = {}  # original_msg_id → running card msg_id
_stop_event = threading.Event()

MAX_RETRIES = 3


# -- Tool definitions -------------------------------------------------------


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="reply",
            description=(
                "Reply on Lark. Pass chat_id from the inbound message. "
                "Sends an interactive card with full markdown rendering. "
                "Optionally pass reply_to (message_id) for threading, "
                "and files (absolute paths) to attach images or documents."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string"},
                    "text": {"type": "string"},
                    "reply_to": {
                        "type": "string",
                        "description": "Message ID to thread under. Use message_id from the inbound <channel> block.",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Absolute file paths to attach. Images (jpg/png/gif/webp) max 10MB; files max 30MB.",
                    },
                },
                "required": ["chat_id", "text"],
            },
        ),
        Tool(
            name="react",
            description=(
                "Add an emoji reaction to a Lark message. "
                "Lark supports: OK, DONE, THUMBSUP, THUMBSDOWN, HEART, FIRE, CLAP, "
                "MUSCLE, JIAYI, FINGERHEART, PRAISE, etc."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string"},
                    "message_id": {"type": "string"},
                    "emoji": {
                        "type": "string",
                        "description": "Lark emoji type name, e.g. THUMBSUP, OK, DONE",
                    },
                },
                "required": ["chat_id", "message_id", "emoji"],
            },
        ),
        Tool(
            name="download_attachment",
            description=(
                "Download a file/image attachment from a Lark message to the local inbox. "
                "Use when the inbound <channel> meta shows image_key or file_key. "
                "Returns the local file path ready to Read."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The message_id containing the attachment",
                    },
                    "file_key": {
                        "type": "string",
                        "description": "The image_key or file_key from inbound meta",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["image", "file"],
                        "description": "Resource type: 'image' for images, 'file' for documents",
                    },
                },
                "required": ["message_id", "file_key", "type"],
            },
        ),
        Tool(
            name="edit_message",
            description=(
                "Update a card the bot previously sent. The card is patched in-place. "
                "Useful for interim progress updates (e.g. 'Working on it...' → final result). "
                "Edits don't trigger push notifications — send a new reply when a long task "
                "completes so the user's device pings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string"},
                    "message_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["chat_id", "message_id", "text"],
            },
        ),
    ]


# -- Tool handlers ----------------------------------------------------------


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        if name == "reply":
            return await _handle_reply(arguments)
        elif name == "react":
            return await _handle_react(arguments)
        elif name == "download_attachment":
            return await _handle_download(arguments)
        elif name == "edit_message":
            return await _handle_edit(arguments)
        else:
            return [TextContent(type="text", text=f"unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"{name} failed: {e}")]


async def _retry(fn, *, retries: int = MAX_RETRIES):
    """Call fn with exponential backoff retries."""
    last_exc = None
    for attempt in range(retries):
        try:
            return await anyio.to_thread.run_sync(fn)
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                delay = 2**attempt
                logger.warning(
                    "retry %d/%d in %ds: %s", attempt + 1, retries, delay, exc
                )
                await anyio.sleep(delay)
    raise last_exc


async def _handle_reply(args: dict[str, Any]) -> list[TextContent]:
    chat_id = args["chat_id"]
    text = args["text"]
    reply_to = args.get("reply_to")
    files = args.get("files", [])

    card_content = _build_card(text)
    msg_id = None

    # If there's a running card for this message, update it in-place
    running_card_id = _running_cards.pop(reply_to, None) if reply_to else None

    if running_card_id:
        try:
            await _retry(lambda: _update_card(running_card_id, card_content))
            msg_id = running_card_id
            logger.info("running card updated: %s", running_card_id)
        except Exception:
            # Fallback: create a new reply in thread
            logger.warning(
                "failed to update running card %s, falling back to new reply",
                running_card_id,
            )
            msg_id = await _retry(lambda: _reply_card(reply_to, card_content))
    elif reply_to:
        msg_id = await _retry(lambda: _reply_card(reply_to, card_content))
    else:
        msg_id = await _retry(lambda: _send_card(chat_id, card_content))

    sent_ids = [msg_id] if msg_id else []

    # Send file attachments as separate messages
    for f in files:
        p = Path(f)
        if not p.exists():
            logger.warning("file not found: %s", f)
            continue

        size = p.stat().st_size
        ext = p.suffix.lower()

        if ext in IMAGE_EXTS:
            if size > MAX_IMAGE_BYTES:
                logger.warning("image too large (%d bytes): %s", size, f)
                continue
            key = await _retry(lambda: _upload_image(f))
            fid = await _retry(
                lambda: _send_file_message(chat_id, key, "image", reply_to)
            )
        else:
            if size > MAX_FILE_BYTES:
                logger.warning("file too large (%d bytes): %s", size, f)
                continue
            key = await _retry(lambda: _upload_file(f, p.name))
            fid = await _retry(
                lambda: _send_file_message(chat_id, key, "file", reply_to)
            )
        if fid:
            sent_ids.append(fid)

    # Add DONE reaction to the original message
    if reply_to:
        try:
            await anyio.to_thread.run_sync(lambda: _add_reaction(reply_to, "DONE"))
        except Exception:
            pass

    if len(sent_ids) == 1:
        return [TextContent(type="text", text=f"sent (id: {sent_ids[0]})")]
    elif sent_ids:
        return [
            TextContent(
                type="text",
                text=f"sent {len(sent_ids)} parts (ids: {', '.join(str(i) for i in sent_ids)})",
            )
        ]
    else:
        return [TextContent(type="text", text="sent (no id returned)")]


async def _handle_react(args: dict[str, Any]) -> list[TextContent]:
    message_id = args["message_id"]
    emoji = args["emoji"]
    await anyio.to_thread.run_sync(lambda: _add_reaction(message_id, emoji))
    return [TextContent(type="text", text="reacted")]


async def _handle_download(args: dict[str, Any]) -> list[TextContent]:
    message_id = args["message_id"]
    file_key = args["file_key"]
    res_type = args["type"]
    dest = await anyio.to_thread.run_sync(
        lambda: _download_resource(message_id, file_key, res_type)
    )
    return [TextContent(type="text", text=str(dest))]


async def _handle_edit(args: dict[str, Any]) -> list[TextContent]:
    message_id = args["message_id"]
    text = args["text"]
    card_content = _build_card(text)
    await anyio.to_thread.run_sync(lambda: _update_card(message_id, card_content))
    return [TextContent(type="text", text=f"edited (id: {message_id})")]


# ---------------------------------------------------------------------------
# Permission relay — card actions for Allow/Deny
# ---------------------------------------------------------------------------


async def _handle_permission_request(params: dict[str, Any]) -> None:
    """Handle permission_request from Claude Code → send card to allowlisted users."""
    request_id = params["request_id"]
    tool_name = params["tool_name"]
    description = params["description"]
    input_preview = params["input_preview"]

    _pending_permissions[request_id] = {
        "tool_name": tool_name,
        "description": description,
        "input_preview": input_preview,
    }

    card_content = _build_permission_card(
        request_id, tool_name, description, input_preview
    )
    # Permission cards are sent as replies to the latest inbound message
    # if no specific target — for now just log
    logger.info("permission_request for %s (id=%s)", tool_name, request_id)


def _handle_card_action(event: Any) -> None:
    """Handle card action callback (Allow/Deny button clicks) from Lark thread."""
    try:
        action = event.event.action
        value_str = action.value
        if isinstance(value_str, str):
            value = json.loads(value_str)
        elif isinstance(value_str, dict):
            value = value_str
        else:
            return

        action_type = value.get("action")
        request_id = value.get("request_id")

        if not action_type or not request_id:
            return

        if request_id not in _pending_permissions:
            return

        behavior = "allow" if action_type == "allow" else "deny"

        # Bridge to main loop to send notification
        notification_data = {
            "_type": "permission_response",
            "request_id": request_id,
            "behavior": behavior,
        }
        _inbound_queue.put_nowait(notification_data)
        _pending_permissions.pop(request_id, None)

    except Exception:
        logger.exception("error handling card action")


# ---------------------------------------------------------------------------
# Lark WebSocket thread
# ---------------------------------------------------------------------------


def _parse_message_text(content_json: str) -> str:
    """Parse Lark message content JSON into plain text."""
    try:
        content = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    # Plain text messages
    if "text" in content:
        text = content["text"]
        # Strip @mention placeholders like @_user_1
        text = re.sub(r"@_user_\d+", "", text)
        return text.strip()

    # Rich text (post format)
    if "content" in content and isinstance(content["content"], list):
        paragraphs: list[str] = []
        for paragraph in content["content"]:
            if isinstance(paragraph, list):
                parts: list[str] = []
                for element in paragraph:
                    if isinstance(element, dict) and element.get("tag") in (
                        "text",
                        "at",
                    ):
                        text_val = element.get("text", "")
                        if text_val:
                            parts.append(text_val)
                if parts:
                    paragraphs.append(" ".join(parts))
        return "\n\n".join(paragraphs).strip()

    return ""


def _on_lark_message(event: Any) -> None:
    """Called by lark-oapi when a message is received (runs in lark thread)."""
    try:
        message = event.event.message
        chat_id = message.chat_id
        msg_id = message.message_id
        sender_id = event.event.sender.sender_id.open_id
        msg_type = getattr(message, "message_type", "text")
        chat_type = getattr(message, "chat_type", "p2p")

        # In group chats, only respond when the bot is @mentioned
        if chat_type == "group":
            mentions = getattr(message, "mentions", None)
            if not mentions:
                return
            bot_mentioned = False
            for m in mentions:
                id_obj = getattr(m, "id", None)
                if id_obj:
                    open_id = getattr(id_obj, "open_id", "") or ""
                    if _BOT_OPEN_ID and open_id == _BOT_OPEN_ID:
                        bot_mentioned = True
                        break
            if not bot_mentioned:
                logger.info("group message without bot @mention, ignoring")
                return

        # Parse text
        text = _parse_message_text(message.content)

        # Thread tracking — root_id indicates a reply within a Lark thread
        root_id = getattr(message, "root_id", None) or None
        thread_id = root_id or msg_id  # same thread if replying, new thread otherwise

        # Build metadata
        meta: dict[str, str] = {
            "chat_id": chat_id,
            "message_id": msg_id,
            "thread_id": thread_id,
            "user": sender_id,
            "user_id": sender_id,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if root_id:
            meta["root_id"] = root_id

        # Handle image/file attachments
        if msg_type == "image":
            try:
                content_data = json.loads(message.content)
                if "image_key" in content_data:
                    meta["image_key"] = content_data["image_key"]
                    meta["attachment_type"] = "image"
            except (json.JSONDecodeError, TypeError):
                pass
            if not text:
                text = "(image)"

        elif msg_type == "file":
            try:
                content_data = json.loads(message.content)
                if "file_key" in content_data:
                    meta["file_key"] = content_data["file_key"]
                    meta["attachment_type"] = "file"
                    if "file_name" in content_data:
                        meta["attachment_name"] = content_data["file_name"]
            except (json.JSONDecodeError, TypeError):
                pass
            if not text:
                text = f"(file: {meta.get('attachment_name', 'unknown')})"

        if not text:
            logger.info("empty message from %s, ignoring", sender_id)
            return

        logger.info("message from %s: %s", sender_id, text[:100])

        # Ack reaction — fire and forget
        try:
            _add_reaction(msg_id, "OK")
        except Exception:
            pass

        # Create "Working on it..." running card in thread
        try:
            running_card_content = _build_card("Working on it...")
            running_card_id = _reply_card(msg_id, running_card_content)
            if running_card_id:
                _running_cards[msg_id] = running_card_id
                logger.info("running card created: %s for %s", running_card_id, msg_id)
        except Exception:
            logger.warning("failed to create running card for %s", msg_id)

        # Queue for MCP notification
        _inbound_queue.put_nowait(
            {
                "_type": "message",
                "content": text,
                "meta": meta,
            }
        )

    except Exception:
        logger.exception("error processing lark message")


def _run_ws() -> None:
    """Run the lark-oapi WebSocket client in a dedicated thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        import lark_oapi.ws.client as _ws_client_mod

        _ws_client_mod.loop = loop

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_lark_message)
            .build()
        )

        ws_client = lark.ws.Client(
            app_id=APP_ID,
            app_secret=APP_SECRET,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            domain=_domain,
        )

        logger.info("Lark WebSocket connecting (domain=%s)...", DOMAIN)
        ws_client.start()
    except Exception:
        if not _stop_event.is_set():
            logger.exception("Lark WebSocket error")


# ---------------------------------------------------------------------------
# MCP notification bridge — lark thread → main async loop → stdout
# ---------------------------------------------------------------------------


async def _notification_bridge() -> None:
    """Drain the inbound queue and send MCP notifications."""
    while True:
        try:
            # Use anyio to avoid blocking the event loop
            item = await anyio.to_thread.run_sync(
                lambda: _inbound_queue.get(timeout=0.5)
            )
        except Exception:
            # Queue.get timeout — just loop
            continue

        if _write_stream is None:
            continue

        try:
            if item.get("_type") == "permission_response":
                notification = JSONRPCNotification(
                    jsonrpc="2.0",
                    method="notifications/claude/channel/permission",
                    params={
                        "request_id": item["request_id"],
                        "behavior": item["behavior"],
                    },
                )
            else:
                notification = JSONRPCNotification(
                    jsonrpc="2.0",
                    method="notifications/claude/channel",
                    params={
                        "content": item["content"],
                        "meta": item["meta"],
                    },
                )

            from mcp.shared.message import SessionMessage

            msg = SessionMessage(message=JSONRPCMessage(root=notification))
            await _write_stream.send(msg)
            logger.info("notification sent to Claude Code")
        except Exception:
            logger.exception("failed to send notification")


# ---------------------------------------------------------------------------
# Permission request handler registration
# ---------------------------------------------------------------------------


async def _setup_permission_handler() -> None:
    """Register handler for permission_request notifications from Claude Code."""
    # The MCP Python SDK's low-level Server handles notifications via
    # notification_handlers dict. We register our handler directly.
    original_handler = None

    async def handle_notification(notification: Any) -> None:
        if hasattr(notification, "method"):
            method = notification.method
        elif isinstance(notification, dict):
            method = notification.get("method", "")
        else:
            method = ""

        if method == "notifications/claude/channel/permission_request":
            params = (
                notification.params
                if hasattr(notification, "params")
                else notification.get("params", {})
            )
            if isinstance(params, dict):
                await _handle_permission_request(params)
            else:
                await _handle_permission_request(dict(params))

        if original_handler:
            await original_handler(notification)

    # Try to register via the server's notification handlers
    try:
        mcp_server.notification_handlers[
            "notifications/claude/channel/permission_request"
        ] = handle_notification
    except Exception:
        logger.warning("could not register permission_request handler")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    global _write_stream

    # Start Lark WebSocket in background thread
    ws_thread = threading.Thread(target=_run_ws, daemon=True)
    ws_thread.start()

    # Set up MCP server
    init_options = mcp_server.create_initialization_options(
        experimental_capabilities={
            "claude/channel": {},
            "claude/channel/permission": {},
        },
    )
    init_options.instructions = INSTRUCTIONS

    await _setup_permission_handler()

    async with stdio_server() as streams:
        read_stream, write_stream = streams
        _write_stream = write_stream

        async with anyio.create_task_group() as tg:
            tg.start_soon(_notification_bridge)
            await mcp_server.run(read_stream, write_stream, init_options)

    logger.info("MCP server stopped")


def _shutdown(signum: int = 0, frame: Any = None) -> None:
    _stop_event.set()
    logger.info("shutting down")
    # Force exit after 2s (WS client may block)
    threading.Timer(2.0, lambda: os._exit(0)).start()


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

if __name__ == "__main__":
    try:
        anyio.run(main)
    except KeyboardInterrupt:
        _shutdown()
    except Exception:
        logger.exception("fatal error")
        sys.exit(1)

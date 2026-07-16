import time
from datetime import datetime
from typing import Set, Dict, List
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request
from pydantic import BaseModel
from shared import get_db, get_user_id_from_session, logger, ADMIN_USER_IDS, broadcast_to_set

router = APIRouter(prefix="/api/chat", tags=["chat"])

chat_history: List[dict] = []
chat_connections: Set[WebSocket] = set()
user_map: Dict[int, WebSocket] = {}
RATE_LIMIT_SECONDS = 2
_last_message_time: Dict[int, float] = {}

@router.websocket("/ws")
async def chat_ws(websocket: WebSocket):
    await websocket.accept()
    token = websocket.cookies.get("session_token")
    session_data = None
    if token:
        from shared import get_session
        session_data = get_session(token)
    user_id = session_data.get("user_id") if session_data else None
    username = "Guest"
    avatar_url = None
    is_vip = False
    is_admin = False

    if user_id:
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT username, avatar_url, vip_tier FROM users WHERE user_id=$1", user_id)
            if row:
                username = row["username"] or f"User{user_id}"
                avatar_url = row.get("avatar_url")
                is_vip = row.get("vip_tier") not in (None, "none")
        is_admin = user_id in ADMIN_USER_IDS

    chat_connections.add(websocket)
    if user_id:
        user_map[user_id] = websocket

    # Send chat history
    try:
        await websocket.send_json({"type": "history", "messages": chat_history[-50:]})
    except Exception:
        pass

    # Join notification
    join_msg = {"type": "system", "text": f"{username} joined the chat", "timestamp": datetime.utcnow().isoformat()}
    dead = await broadcast_to_set(chat_connections, join_msg)
    chat_connections.difference_update(dead)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if msg_type == "message":
                text = data.get("text", "").strip()
                # Sanitize: strip HTML tags to prevent XSS
                import re as _re
                text = _re.sub(r'<[^>]*>', '', text)
                if not text or len(text) > 500:
                    continue
                if user_id:
                    now = time.time()
                    last = _last_message_time.get(user_id, 0)
                    if now - last < RATE_LIMIT_SECONDS:
                        await websocket.send_json({"type": "error", "text": "Slow down!"})
                        continue
                    _last_message_time[user_id] = now

                msg = {
                    "type": "message",
                    "user_id": user_id,
                    "username": username,
                    "avatar_url": avatar_url,
                    "text": text,
                    "is_vip": is_vip,
                    "is_admin": is_admin,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                chat_history.append(msg)
                if len(chat_history) > 200:
                    chat_history[:100] = []
                dead = await broadcast_to_set(chat_connections, msg)
                chat_connections.difference_update(dead)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"Chat WS error: {e}")
    finally:
        chat_connections.discard(websocket)
        if user_id:
            user_map.pop(user_id, None)
            leave_msg = {"type": "system", "text": f"{username} left the chat", "timestamp": datetime.utcnow().isoformat()}
            dead = await broadcast_to_set(chat_connections, leave_msg)
            chat_connections.difference_update(dead)

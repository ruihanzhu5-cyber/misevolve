"""
OpenClaw API Server
Provides REST API for interacting with OpenClaw sessions
"""

import asyncio
import glob
import json
import logging
import os
import random
import re
from datetime import datetime
from functools import wraps
from typing import Dict, List, Optional

import aiofiles
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="OpenClaw API Server")

OPENCLAW_HOME = "/root/.openclaw"
SESSIONS_DIR = f"{OPENCLAW_HOME}/agents/main/sessions"
GATEWAY_URL = "http://localhost:18789"

active_sessions: Dict[str, dict] = {}
STAGE7 = os.environ.get("MISEVOLVE_OPENCLAW_STAGE7") == "1"


def async_retry(
    max_attempts=3, base_delay=1.0, max_delay=10.0, backoff_factor=2.0, jitter=True, exceptions=(Exception,)
):
    """
    通用异步 retry 装饰器

    Args:
        max_attempts: 最大尝试次数
        base_delay: 初始等待时间（秒）
        max_delay: 最大等待时间
        backoff_factor: 指数退避系数
        jitter: 是否加随机抖动
        exceptions: 哪些异常触发重试，默认所有异常
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_attempts:
                        logger.error(f"[Retry] {func.__name__} failed after {max_attempts} attempts: {e}")
                        raise

                    delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)
                    if jitter:
                        delay *= 0.5 + random.random()

                    logger.warning(
                        "[Retry] %s attempt %s/%s failed: %s, retrying in %.2fs...",
                        func.__name__,
                        attempt,
                        max_attempts,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)

        return wrapper

    return decorator


class PromptRequest(BaseModel):
    session_id: Optional[str] = None
    prompt: str
    create_new_session: bool = False


class SessionResponse(BaseModel):
    session_id: str
    response: str
    status: str


class SessionInfo(BaseModel):
    session_id: str
    created_at: Optional[str]
    updated_at: Optional[str]
    file_path: str
    message_count: int


def generate_session_id():
    """Generate a unique session ID"""
    import uuid

    return str(uuid.uuid4())


def get_session_file_path(session_id: str) -> str:
    """Get the file path for a session"""
    return os.path.join(SESSIONS_DIR, f"{session_id}.jsonl")


def get_all_session_files() -> List[str]:
    """Get all session JSONL files"""
    pattern = os.path.join(SESSIONS_DIR, "*.jsonl")
    files = glob.glob(pattern)

    return [f for f in files if not f.endswith("sessions.json")]


def _stage7_session_key(session_id: str) -> str:
    return f"agent:main:stage7-{session_id}"


async def _gateway_history(session_id: str) -> dict:
    params = json.dumps({"key": _stage7_session_key(session_id), "limit": 100})
    process = await asyncio.create_subprocess_exec(
        "openclaw", "gateway", "call", "sessions.get", "--params", params, "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise HTTPException(status_code=504, detail="OpenClaw history request timed out") from exc
    if process.returncode != 0:
        raise HTTPException(status_code=502, detail="OpenClaw gateway did not return session history")
    try:
        messages = json.loads(stdout)["messages"]
        if not isinstance(messages, list):
            raise ValueError("invalid messages")
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="Invalid OpenClaw gateway history") from exc
    history = [m if m.get("type") == "message" and isinstance(m.get("message"), dict)
               else {"type": "message", "message": m} for m in messages if isinstance(m, dict)]
    return {"session_id": session_id, "total_records": len(history), "history": history}


@async_retry(max_attempts=3, base_delay=0.2, exceptions=(IOError, OSError))
async def read_session_metadata(session_id: str) -> Optional[Dict]:
    """Read session metadata from sessions.json"""
    sessions_json_path = os.path.join(SESSIONS_DIR, "sessions.json")

    if not os.path.exists(sessions_json_path):
        return None

    try:
        async with aiofiles.open(sessions_json_path, "r") as f:
            content = await f.read()
            sessions_data = json.loads(content)

            for key, session in sessions_data.items():
                if session.get("sessionId") == session_id or key == session_id:
                    return session
    except Exception as e:
        print(f"Error reading session metadata: {e}")

    return None


@async_retry(max_attempts=3, base_delay=0.2, exceptions=(IOError, OSError))
async def count_session_messages(session_file: str) -> int:
    """Count messages in a session JSONL file"""
    if not os.path.exists(session_file):
        return 0

    try:
        count = 0
        async with aiofiles.open(session_file, "r") as f:
            async for line in f:
                if line.strip():
                    count += 1
        return count
    except Exception:
        return 0


def _diagnostic_stream(data):
    text = data.decode("utf-8", errors="replace")
    for name, value in os.environ.items():
        if value and re.search(r"(?:^|_)(?:API_KEY|TOKEN|PASSWORD|SECRET)$", name.upper()):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r'''(?im)((?:authorization|x-api-key|api_key|password)["']?\s*[:=]\s*["']?)'''
                  r'''(?:bearer\s+)?[^\s,"'}]+''', r'\1[REDACTED]', text)
    return {"text": text[-16384:], "original_bytes": len(data), "truncated": len(text) > 16384}


@async_retry(max_attempts=1 if STAGE7 else int(os.environ.get("MISEVOLVE_OPENCLAW_PROMPT_ATTEMPTS", "5")),
             base_delay=1.0)
async def send_prompt_to_openclaw(session_id: str, prompt: str) -> Dict:
    """Send a prompt to OpenClaw via CLI"""
    try:
        selector = ["--session-key", _stage7_session_key(session_id)] if STAGE7 else ["--session-id", session_id]
        cmd = ["openclaw", "agent", "--message", prompt, *selector, "--json"]

        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            if STAGE7:
                raise HTTPException(status_code=500, detail={
                    "error": "openclaw_process_exit", "returncode": process.returncode,
                    "signal": -process.returncode if process.returncode < 0 else None,
                    "pid": process.pid, "session_id": session_id,
                    "stdout": _diagnostic_stream(stdout), "stderr": _diagnostic_stream(stderr),
                })
            error_msg = stderr.decode() if stderr else "Unknown error"
            raise Exception(f"OpenClaw command failed: {error_msg}")

        response_text = stdout.decode()
        if STAGE7:
            try:
                result = json.loads(response_text)
            except ValueError as exc:
                raise ValueError("invalid OpenClaw agent JSON") from exc
            if result.get("status") not in ("ok", "completed", "success"):
                raise RuntimeError("OpenClaw agent did not complete")
            payloads = (result.get("result") or {}).get("payloads", [])
            return {"response": "\n".join(item["text"] for item in payloads if isinstance(item.get("text"), str)),
                    "status": "completed"}

        try:
            lines = response_text.strip().split("\n")
            for line in lines:
                if line.strip().startswith("{"):
                    response_data = json.loads(line)
                    return response_data
        except json.JSONDecodeError:
            pass

        return {"response": response_text, "status": "completed"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    """Root endpoint"""
    return {"service": "OpenClaw API Server", "version": "1.0.0", "status": "running"}


@app.get("/health")
async def health_check():
    """Health check endpoint"""

    config_path = f"{OPENCLAW_HOME}/openclaw.json"
    config_exists = os.path.exists(config_path)

    sessions_dir_exists = os.path.exists(SESSIONS_DIR)

    return {"status": "healthy", "openclaw_configured": config_exists, "sessions_dir_ready": sessions_dir_exists}


@app.post("/prompt", response_model=SessionResponse)
async def send_prompt(request: PromptRequest):
    """Send a prompt to OpenClaw"""
    session_id = request.session_id

    if request.create_new_session or not session_id:
        session_id = generate_session_id()
        active_sessions[session_id] = {"created_at": datetime.utcnow().isoformat(), "message_count": 0}

    if not session_id:
        raise HTTPException(status_code=400, detail="Session ID is required")

    try:
        result = await send_prompt_to_openclaw(session_id, request.prompt)

        if session_id in active_sessions:
            active_sessions[session_id]["message_count"] += 1
            active_sessions[session_id]["updated_at"] = datetime.utcnow().isoformat()

        return SessionResponse(
            session_id=session_id,
            response=result.get("response", str(result)),
            status=result.get("status", "completed"),
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions", response_model=List[SessionInfo])
async def list_sessions():
    """List all available sessions"""
    session_files = get_all_session_files()
    sessions = []

    for file_path in session_files:
        session_id = os.path.basename(file_path).replace(".jsonl", "")

        stat = os.stat(file_path)
        created_at = datetime.fromtimestamp(stat.st_ctime).isoformat()
        updated_at = datetime.fromtimestamp(stat.st_mtime).isoformat()

        message_count = await count_session_messages(file_path)

        sessions.append(
            SessionInfo(
                session_id=session_id,
                created_at=created_at,
                updated_at=updated_at,
                file_path=file_path,
                message_count=message_count,
            )
        )

    sessions.sort(key=lambda x: x.updated_at or "", reverse=True)

    return sessions


@app.get("/sessions/{session_id}")
async def get_session_info(session_id: str):
    """Get information about a specific session"""
    session_file = get_session_file_path(session_id)

    if not os.path.exists(session_file):
        raise HTTPException(status_code=404, detail="Session not found")

    stat = os.stat(session_file)
    created_at = datetime.fromtimestamp(stat.st_ctime).isoformat()
    updated_at = datetime.fromtimestamp(stat.st_mtime).isoformat()

    message_count = await count_session_messages(session_file)

    metadata = await read_session_metadata(session_id)

    return {
        "session_id": session_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "file_path": session_file,
        "message_count": message_count,
        "metadata": metadata,
    }


@app.get("/sessions/{session_id}/export")
async def export_session(session_id: str):
    """Export a session JSONL file"""
    session_file = get_session_file_path(session_id)

    if not os.path.exists(session_file):
        raise HTTPException(status_code=404, detail="Session not found")

    return FileResponse(path=session_file, filename=f"session_{session_id}.jsonl", media_type="application/x-ndjson")


@app.get("/sessions/{session_id}/history")
async def get_session_history(session_id: str):
    """Get complete session history including all tool calls"""
    if STAGE7:
        return await _gateway_history(session_id)
    session_file = get_session_file_path(session_id)

    if not os.path.exists(session_file):
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        history = []
        async with aiofiles.open(session_file, "r") as f:
            async for line in f:
                line = line.strip()
                if line:
                    try:
                        record = json.loads(line)
                        history.append(record)
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse line in session {session_id}: {e}")
                        continue

        return {"session_id": session_id, "total_records": len(history), "history": history}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read session history: {str(e)}")


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session"""
    session_file = get_session_file_path(session_id)

    if not os.path.exists(session_file):
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        os.remove(session_file)

        if session_id in active_sessions:
            del active_sessions[session_id]

        return {"status": "deleted", "session_id": session_id}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sessions/{session_id}/clear")
async def clear_session(session_id: str):
    """Clear a session (delete and recreate)"""
    session_file = get_session_file_path(session_id)

    if os.path.exists(session_file):
        os.remove(session_file)

    if session_id in active_sessions:
        del active_sessions[session_id]

    active_sessions[session_id] = {"created_at": datetime.utcnow().isoformat(), "message_count": 0}

    return {"status": "cleared", "session_id": session_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

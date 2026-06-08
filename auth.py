"""
App-level authentication: user storage, password hashing, session tokens.
"""
import json
import hashlib
from typing import Optional

from fastapi import Cookie, Depends, HTTPException

from config import AUTH_FILE, DEFAULT_USERNAME, DEFAULT_PASSWORD, get_logger

logger = get_logger(__name__)


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed


# ── User store ────────────────────────────────────────────────────────────────

def load_users() -> dict:
    if not AUTH_FILE.exists():
        return {DEFAULT_USERNAME: hash_password(DEFAULT_PASSWORD)}
    try:
        return json.loads(AUTH_FILE.read_text())
    except Exception:
        return {DEFAULT_USERNAME: hash_password(DEFAULT_PASSWORD)}


def save_users(users: dict) -> None:
    AUTH_FILE.write_text(json.dumps(users))


def authenticate_user(username: str, password: str) -> bool:
    users = load_users()
    return username in users and verify_password(password, users[username])


def make_session_token(username: str, hashed_pw: str) -> str:
    return hashlib.sha256(f"{username}{hashed_pw}".encode()).hexdigest()


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def get_current_user(session_token: Optional[str] = Cookie(None)) -> Optional[str]:
    if session_token is None:
        return None
    users = load_users()
    for username, hashed_pw in users.items():
        if session_token == make_session_token(username, hashed_pw):
            return username
    return None


def require_auth(current_user: str = Depends(get_current_user)) -> str:
    if current_user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return current_user

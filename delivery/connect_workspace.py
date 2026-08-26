"""OAuth connect for client Google Workspace (send-as-client)."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]

# pending state -> client_email (in-memory; fine for local server)
_PENDING: dict[str, str] = {}


def _secrets_path() -> Path:
    raw = os.environ.get("GMAIL_CLIENT_SECRETS", "client_secrets.json").strip()
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _load_oauth_client() -> tuple[str, str]:
    """Return (client_id, client_secret) from env or client_secrets.json."""
    cid = os.environ.get("OAUTH_CLIENT_ID", "").strip()
    csec = os.environ.get("OAUTH_CLIENT_SECRET", "").strip()
    if cid and csec:
        return cid, csec
    path = _secrets_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    block = data.get("web") or data.get("installed") or {}
    return block["client_id"], block["client_secret"]


def _redirect_uri() -> str:
    return os.environ.get(
        "OAUTH_REDIRECT_URI", "http://localhost:8000/oauth/callback"
    ).strip()


def _state_secret() -> bytes:
    raw = os.environ.get("OAUTH_STATE_SECRET", "").strip()
    if raw:
        return raw.encode()
    # fallback derived secret
    return hashlib.sha256(
        (os.environ.get("OAUTH_CLIENT_SECRET", "") + str(ROOT)).encode()
    ).digest()


def _make_state(client_email: str) -> str:
    nonce = secrets.token_urlsafe(16)
    payload = f"{client_email}|{nonce}"
    sig = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    state = f"{payload}|{sig}"
    _PENDING[state] = client_email.strip().lower()
    return state


def _validate_state(state: str) -> str:
    """Return client_email if state is valid."""
    if state in _PENDING:
        return _PENDING.pop(state)
    try:
        email, nonce, sig = state.rsplit("|", 2)
        payload = f"{email}|{nonce}"
        expect = hmac.new(
            _state_secret(), payload.encode(), hashlib.sha256
        ).hexdigest()[:24]
        if not hmac.compare_digest(expect, sig):
            raise ValueError("bad signature")
        return email.strip().lower()
    except Exception as e:
        raise ValueError(f"invalid OAuth state: {e}") from e


def generate_oauth_url(client_email: str, client_name: str = "") -> str:
    """Build Google OAuth consent URL for the client Workspace account."""
    client_id, _ = _load_oauth_client()
    state = _make_state(client_email)
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "login_hint": client_email,
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    print(f"OAuth URL for {client_name or client_email}: {url[:80]}...")
    return url


def handle_oauth_callback(auth_code: str, state: str) -> dict:
    """Exchange code for tokens and store encrypted on disk."""
    from google_auth_oauthlib.flow import Flow

    from .token_store import save_encrypted

    try:
        client_email = _validate_state(state)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    client_id, client_secret = _load_oauth_client()
    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [_redirect_uri()],
        }
    }
    flow = Flow.from_client_config(
        client_config, scopes=SCOPES, state=state, redirect_uri=_redirect_uri()
    )
    try:
        flow.fetch_token(code=auth_code)
        creds = flow.credentials
        save_encrypted(client_email, creds.to_json())
        set_workspace_status(client_email, "connected")
        return {"ok": True, "client_email": client_email}
    except Exception as e:
        return {"ok": False, "error": str(e), "client_email": client_email}


def refresh_tokens(client_email: str):
    """Load + refresh client credentials; return google.oauth2.credentials.Credentials."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from .token_store import load_decrypted, save_encrypted

    data = json.loads(load_decrypted(client_email))
    creds = Credentials.from_authorized_user_info(data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_encrypted(client_email, creds.to_json())
    if not creds.valid:
        raise RuntimeError(f"Invalid credentials for {client_email} — re-connect OAuth")
    return creds


def send_connect_instructions(client_email: str, client_name: str = "") -> bool:
    """Email the client a secure OAuth link to connect Workspace."""
    from pipeline.email_sender import send_email
    from pipeline.to_sheets import is_email

    if not is_email(client_email):
        print(f"bad email: {client_email}")
        return False

    oauth_url = generate_oauth_url(client_email, client_name)
    first = (client_name or "").strip().split()[0] or "there"
    domain = client_email.split("@")[-1]

    subject = "connect your Google Workspace (2 minutes)"
    body = (
        f"Hi {first},\n"
        f"\n"
        f"To launch your campaign, I need to connect your Google Workspace\n"
        f"so emails go out from your domain ({domain}).\n"
        f"\n"
        f"This takes 2 minutes:\n"
        f"\n"
        f"1. Click this secure link: {oauth_url}\n"
        f"2. Sign in with your Google Workspace account ({client_email})\n"
        f"3. Click \"Allow\" when prompted\n"
        f"\n"
        f"What this gives us access to:\n"
        f"✅ Send emails from your domain\n"
        f"✅ Read replies (so we can track responses)\n"
        f"✅ Access your Calendar (for meeting tracking)\n"
        f"\n"
        f"We NEVER:\n"
        f"❌ Delete any emails\n"
        f"❌ Access files or documents\n"
        f"❌ Change your settings\n"
        f"\n"
        f"If you have questions about security, just reply here.\n"
        f"\n"
        f"— Vladislav\n"
        f"Founder, DentalMeet\n"
    )
    return send_email(client_email, subject, body)


def set_workspace_status(client_email: str, status: str) -> None:
    """Update workspace_status on Clients sheet."""
    from pipeline.to_sheets import get_all_values_safe, update_cell_safe
    from delivery.client_tracker import _clients_ws, _find_matches, ensure_tracker_columns

    email = client_email.strip().lower()
    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    if "workspace_status" not in cols:
        return
    values = get_all_values_safe(ws)
    hits = _find_matches(values, cols, email, "")
    if not hits:
        return
    row_index, _ = hits[-1]
    update_cell_safe(ws, row_index, cols["workspace_status"], status)


def get_workspace_status(client_email: str) -> str:
    """Return workspace_status from Clients (pending/connected/declined/fallback)."""
    from pipeline.to_sheets import get_all_values_safe
    from delivery.client_tracker import _clients_ws, _find_matches, ensure_tracker_columns

    email = client_email.strip().lower()
    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    values = get_all_values_safe(ws)
    hits = _find_matches(values, cols, email, "")
    if not hits:
        return "pending"
    _, row = hits[-1]
    idx = cols.get("workspace_status")
    if idx is None or idx - 1 >= len(row):
        return "pending"
    return (row[idx - 1] or "pending").strip().lower() or "pending"


def decline_workspace(client_email: str, client_name: str = "") -> bool:
    """Client refused Workspace — switch to Plan B fallback mode."""
    from pipeline.email_sender import send_email
    from pipeline.to_sheets import is_email

    email = client_email.strip().lower()
    if not is_email(email):
        return False
    set_workspace_status(email, "fallback")
    first = (client_name or "").strip().split()[0] or "there"
    subject = "got it — we'll send on your behalf"
    body = (
        f"Hi {first},\n\n"
        f"Got it! We'll send emails on your behalf from our accounts. "
        f"You'll receive all replies forwarded to this inbox.\n\n"
        f"No Google Workspace connection needed — your campaign can start "
        f"as soon as we finish building your dental practice list.\n\n"
        f"— Vladislav\n"
        f"Founder, DentalMeet\n"
    )
    ok = send_email(email, subject, body)
    print(f"workspace declined → fallback mode for {email}")
    return ok


def start_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run Flask callback server for OAuth redirects."""
    from flask import Flask, redirect, request

    app = Flask(__name__)

    @app.get("/oauth/callback")
    def oauth_callback():
        err = request.args.get("error")
        if err:
            return f"<h2>OAuth error: {err}</h2>", 400
        code = request.args.get("code", "")
        state = request.args.get("state", "")
        result = handle_oauth_callback(code, state)
        if not result.get("ok"):
            return f"<h2>Connection failed</h2><pre>{result}</pre>", 400
        return (
            "<h2>Connected!</h2>"
            f"<p>Workspace linked for <b>{result.get('client_email')}</b>.</p>"
            "<p>You can close this window.</p>"
        )

    @app.get("/")
    def home():
        return "<p>DentalMeet OAuth callback server. Waiting for /oauth/callback</p>"

    print(f"OAuth callback server on http://{host}:{port}")
    print(f"Redirect URI must be exactly: {_redirect_uri()}")
    app.run(host=host, port=port, debug=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Client Workspace OAuth")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_link = sub.add_parser("send_link", help="Email OAuth connect link to client")
    p_link.add_argument("client_email")
    p_link.add_argument("--name", default="", help="Client first/full name")

    p_decline = sub.add_parser("decline", help="Mark Workspace declined → Plan B fallback")
    p_decline.add_argument("client_email")
    p_decline.add_argument("--name", default="")

    p_url = sub.add_parser("url", help="Print OAuth URL only (no email)")
    p_url.add_argument("client_email")
    p_url.add_argument("--name", default="")

    p_srv = sub.add_parser("start_server", help="Run local OAuth callback server")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.cmd == "send_link":
        ok = send_connect_instructions(args.client_email, args.name)
        return 0 if ok else 1
    if args.cmd == "decline":
        ok = decline_workspace(args.client_email, args.name)
        return 0 if ok else 1
    if args.cmd == "url":
        print(generate_oauth_url(args.client_email, args.name))
        return 0
    if args.cmd == "start_server":
        start_server(args.host, args.port)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

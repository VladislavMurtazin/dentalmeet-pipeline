"""One-time OAuth setup for Gmail REST send (opens browser for consent).

Usage:
  1. In Google Cloud Console → APIs & Services → enable Gmail API
  2. Create OAuth client ID type "Desktop app"
  3. Download JSON → save as client_secrets.json in project root
  4. python -m pipeline.setup_gmail_oauth

This writes gmail_token.json (gitignored). Later sends refresh it automatically.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
ROOT = Path(__file__).resolve().parent.parent


def _path_from_env(key: str, default: str) -> Path:
    raw = os.environ.get(key, default).strip() or default
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = _path_from_env("GMAIL_CLIENT_SECRETS", "client_secrets.json")
    token_file = _path_from_env("GMAIL_TOKEN_FILE", "gmail_token.json")

    if not secrets.exists():
        print(f"ERROR: missing {secrets}")
        print()
        print("Create an OAuth Desktop client in Google Cloud Console:")
        print("  1. APIs & Services → Library → enable Gmail API")
        print("  2. APIs & Services → Credentials → Create Credentials")
        print("     → OAuth client ID → Application type: Desktop app")
        print("  3. Download JSON → save as client_secrets.json in project root")
        print("  4. Re-run: python -m pipeline.setup_gmail_oauth")
        return 1

    print(f"Using client secrets: {secrets}")
    print("A browser window will open — sign in as your sending Gmail")
    print(f"(expected: {os.environ.get('GMAIL_EMAIL', 'GMAIL_EMAIL not set')})")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    token_file.write_text(creds.to_json(), encoding="utf-8")
    print()
    print(f"SUCCESS — token saved to {token_file}")
    print("Token refresh is automatic on future sends.")
    print("Do NOT commit gmail_token.json or client_secrets.json.")
    print()
    print("Test with:")
    print("  DRY_RUN=true python -m pipeline.main send --limit 1")
    print("  (then DRY_RUN=false for a real send)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

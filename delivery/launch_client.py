"""Auto-launch delivery after a client completes onboarding.

Flow:
  form OK → Clients row → research dental practices → send if Workspace connected
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
TARGETS_DIR = ROOT / "targets"

CALENDLY_RE = re.compile(r"calendly\.com/", re.I)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", re.I)

DEFAULT_RESEARCH_LIMIT = 50
DEFAULT_SEND_LIMIT = 25


def _safe(email: str) -> str:
    return email.strip().lower().replace("@", "_at_")


def _targets_path(client_email: str) -> Path:
    return TARGETS_DIR / f"{_safe(client_email)}_targets.csv"


def workspace_connected(client_email: str) -> bool:
    """True if encrypted Workspace OAuth token exists for this client."""
    try:
        from .token_store import token_path

        return token_path(client_email).exists()
    except Exception:
        return False


def targets_ready(client_email: str) -> bool:
    path = _targets_path(client_email)
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8").strip()
        # header + at least one data row
        return text.count("\n") >= 1 and len(text.splitlines()) > 1
    except OSError:
        return False


def validate_brief(brief: dict) -> dict:
    """Check onboarding form fields. Returns {ok, errors, warnings}."""
    errors: list[str] = []
    warnings: list[str] = []

    email = (brief.get("Email") or "").strip()
    company = (brief.get("Company") or "").strip()
    name = (brief.get("Name") or "").strip()
    calendly = (brief.get("Calendly") or "").strip()
    geo = (brief.get("Geo") or "").strip()
    icp = (brief.get("ICP") or "").strip()
    workspace = (brief.get("Workspace Email") or "").strip()
    services = (brief.get("Services") or "").strip()

    if not email or not EMAIL_RE.match(email):
        errors.append("Valid email is required")
    if not company or len(company) < 2:
        errors.append("Company name is required")
    if not name or len(name) < 2:
        errors.append("Your name is required")

    if not calendly:
        errors.append("Calendly link is required (practices book meetings with you)")
    else:
        url = calendly if "://" in calendly else "https://" + calendly
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            host = ""
        if "calendly.com" not in host and not CALENDLY_RE.search(calendly):
            errors.append(
                "Calendly must be a calendly.com link "
                "(example: https://calendly.com/your-name/meeting)"
            )

    if not geo and not icp:
        errors.append("Add geographic target and/or types of dental practices (ICP)")

    if not workspace:
        warnings.append(
            "Google Workspace email missing — we'll still research practices, "
            "but outreach waits until you connect Workspace"
        )
    elif email and workspace.lower() != email.lower():
        # Often workspace differs from personal — just note it
        if not EMAIL_RE.match(workspace):
            errors.append("Workspace Email looks invalid")
        else:
            warnings.append(
                f"Workspace Email ({workspace}) differs from form Email ({email}) — OK if intentional"
            )

    if not services:
        warnings.append("Services field empty — outreach will use a generic pitch")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


def _update_client_status(client_email: str, status: str, note: str = "") -> None:
    from .client_tracker import _clients_ws, _find_matches, _get, ensure_tracker_columns

    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    values = ws.get_all_values()
    hits = _find_matches(values, cols, client_email, "")
    if not hits:
        return
    row_index, row = hits[-1]
    if "Status" in cols:
        ws.update_cell(row_index, cols["Status"], status)
    if note and "Notes" in cols:
        prev = _get(row, cols, "Notes")
        marker = note.strip()
        if marker and marker not in prev:
            new_notes = f"{prev}; {marker}".strip("; ").strip() if prev else marker
            ws.update_cell(row_index, cols["Notes"], new_notes)


def send_form_fix_email(email: str, name: str, errors: list[str]) -> bool:
    """Ask the lead to resubmit with corrected fields."""
    from pipeline.email_sender import send_email
    from pipeline.templates import SIGNATURE

    first = (name or "").strip().split()[0] if (name or "").strip() else "there"
    bullets = "\n".join(f"• {e}" for e in errors)
    body = (
        f"Hi {first},\n\n"
        f"Thanks for submitting the onboarding form — a few fields need a quick fix "
        f"before we can launch:\n\n"
        f"{bullets}\n\n"
        f"Please resubmit the form with corrections "
        f"(or reply to this email with the missing info).\n\n"
        f"{SIGNATURE}"
    )
    return send_email(
        email,
        "almost there — need a couple details to launch",
        body,
    )


def research_practices(client_email: str, limit: int = DEFAULT_RESEARCH_LIMIT) -> dict:
    """Build dental target list for the client (Apollo → CSV)."""
    from .research_dental import build_target_list

    rows = build_target_list(client_email, limit=limit)
    return {"targets": len(rows), "path": str(_targets_path(client_email))}


def send_practice_emails(
    client_email: str,
    limit: int = DEFAULT_SEND_LIMIT,
) -> dict:
    """Send first-touch dental emails from the client's Workspace."""
    from .send_as_client import run_campaign

    return run_campaign(client_email, daily_limit=limit)


def launch_client(
    client_email: str,
    *,
    name: str = "",
    send_connect_link: bool = True,
    research_limit: int = DEFAULT_RESEARCH_LIMIT,
    send_limit: int = DEFAULT_SEND_LIMIT,
    force_send: bool = False,
) -> dict:
    """
    After form acceptance:
      1) research dental practices
      2) if Workspace connected → start sending
      3) else → email OAuth connect link and wait
    """
    email = client_email.strip().lower()
    result: dict = {
        "email": email,
        "researched": False,
        "sent": 0,
        "workspace": False,
        "status": "",
        "errors": [],
    }

    connected = workspace_connected(email)
    result["workspace"] = connected

    # Always try to build / refresh targets
    try:
        if not targets_ready(email) or force_send:
            print(f"launch: researching practices for {email}…")
            r = research_practices(email, limit=research_limit)
            result["researched"] = True
            result["targets"] = r["targets"]
            print(f"launch: {r['targets']} targets ready")
        else:
            result["targets"] = sum(
                1 for _ in _targets_path(email).read_text(encoding="utf-8").splitlines()[1:]
                if _.strip()
            )
            print(f"launch: using existing targets ({result['targets']})")
    except Exception as e:
        result["errors"].append(f"research failed: {e}")
        print(f"launch research error {email}: {e}")
        _update_client_status(email, "research_failed", f"research_error {date.today()}")
        result["status"] = "research_failed"
        return result

    if not connected:
        if send_connect_link:
            try:
                from .connect_workspace import send_connect_instructions

                send_connect_instructions(email, name)
                print(f"launch: Workspace connect link sent → {email}")
            except Exception as e:
                result["errors"].append(f"connect link failed: {e}")
                print(f"launch connect link error: {e}")
        _update_client_status(
            email,
            "awaiting_workspace",
            f"awaiting_workspace {date.today().isoformat()}",
        )
        result["status"] = "awaiting_workspace"
        return result

    # Workspace ready → send
    try:
        summary = send_practice_emails(email, limit=send_limit)
        result["sent"] = int(summary.get("sent") or 0)
        result["send_summary"] = summary
        _update_client_status(
            email,
            "running",
            f"campaign_started {date.today().isoformat()} sent={result['sent']}",
        )
        result["status"] = "running"
        print(f"launch: sent {result['sent']} practice emails for {email}")
    except Exception as e:
        result["errors"].append(f"send failed: {e}")
        _update_client_status(email, "send_failed", f"send_error {date.today()}")
        result["status"] = "send_failed"
        print(f"launch send error {email}: {e}")

    return result


def _client_rows() -> list[dict]:
    """All Clients rows as dicts with email/status/created/type."""
    from pipeline.to_sheets import connect

    gc = connect()
    sh = gc.open(os.environ.get("SHEET_NAME", "Pilot CRM"))
    try:
        ws = sh.worksheet("Clients")
    except Exception:
        return []
    values = ws.get_all_values()
    if not values:
        return []
    headers = [h.strip() for h in values[0]]
    out = []
    for raw in values[1:]:
        row = {headers[i]: (raw[i] if i < len(raw) else "") for i in range(len(headers))}
        email = (row.get("Email") or "").strip().lower()
        if email:
            out.append(row)
    return out


def process_pending_launches(
    *,
    research_limit: int = DEFAULT_RESEARCH_LIMIT,
    send_limit: int = DEFAULT_SEND_LIMIT,
) -> dict:
    """
    Cron-friendly: for each active / awaiting_workspace client:
      - build targets if missing
      - send practice emails once Workspace is connected
    Skips repeat clients still awaiting upfront payment.
    """
    stats = {"checked": 0, "launched": 0, "waiting_workspace": 0, "skipped": 0, "errors": 0}

    for row in _client_rows():
        email = (row.get("Email") or "").strip().lower()
        status = (row.get("Status") or "").strip().lower()
        ctype = (row.get("Client Type") or "").strip().lower()
        upfront = (row.get("Upfront Paid") or "").strip().lower()
        name = (row.get("Name") or "").strip()

        stats["checked"] += 1

        # Don't launch unfinished / completed / paid-finished clients
        if status in ("completed", "paid", "needs_info", "invalid", "cancelled"):
            stats["skipped"] += 1
            continue
        if ctype == "repeat" and upfront in ("pending", "no", ""):
            if status == "awaiting_upfront":
                stats["skipped"] += 1
                continue

        # Candidates: new active pilots, awaiting workspace, research_failed retry, running (top-up)
        if status not in (
            "active",
            "onboarded",
            "awaiting_workspace",
            "research_failed",
            "send_failed",
            "running",
            "",
        ):
            stats["skipped"] += 1
            continue

        try:
            # For already-running: only send more if targets exist (warmup daily cap handles volume)
            force_research = status in ("research_failed", "") or not targets_ready(email)
            result = launch_client(
                email,
                name=name,
                send_connect_link=(status != "running"),
                research_limit=research_limit,
                send_limit=send_limit,
                force_send=force_research and not targets_ready(email),
            )
            if result.get("status") == "running" and result.get("sent", 0) > 0:
                stats["launched"] += 1
            elif result.get("status") == "awaiting_workspace":
                stats["waiting_workspace"] += 1
            if result.get("errors"):
                stats["errors"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"pending launch fail {email}: {e}")

    print(
        f"pending launches: checked={stats['checked']} launched={stats['launched']} "
        f"waiting_ws={stats['waiting_workspace']} skipped={stats['skipped']} "
        f"errors={stats['errors']}"
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-launch client delivery")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("launch", help="Research + send for one client")
    p.add_argument("client_email")
    p.add_argument("--research-limit", type=int, default=DEFAULT_RESEARCH_LIMIT)
    p.add_argument("--send-limit", type=int, default=DEFAULT_SEND_LIMIT)

    sub.add_parser("pending", help="Process all clients waiting to launch / send")

    args = parser.parse_args(argv)
    if args.cmd == "launch":
        out = launch_client(
            args.client_email,
            research_limit=args.research_limit,
            send_limit=args.send_limit,
            force_send=True,
        )
        print(out)
        return 0 if not out.get("errors") else 1
    if args.cmd == "pending":
        process_pending_launches()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""CLI entry: research, send, follow-up, or reply check.

  python -m pipeline.main              # research (default)
  python -m pipeline.main research
  python -m pipeline.main send
  python -m pipeline.main send --limit 10
  python -m pipeline.main followup 1
  python -m pipeline.main followup 2
  python -m pipeline.main replies
  python -m pipeline.main replies --days 7
  python -m pipeline.main check_forms
  python -m pipeline.main launch_pending
  python -m pipeline.main draft_reply EMAIL --body "their question"
  python -m pipeline.main verify_payment TXID [--email client@agency.com]
"""

from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

load_dotenv()

from .personalize import parse_signal
from .research import find_agencies
from .to_sheets import HEADERS, add_row_with_retry


def collect_leads(
    page: int = 1,
    require_email: bool = True,
    *,
    limit: int | None = None,
    enrich: int | None = None,
) -> list[dict]:
    """Apollo/CSV people — no website scraping (lazy at send time)."""
    people = find_agencies(
        page=page, require_email=require_email, limit=limit, enrich=enrich
    )
    rows = []
    for person in people:
        org = person.get("organization") or {}
        domain = (org.get("primary_domain") or "").strip()
        rows.append(
            {
                "person": person,
                "domain": domain,
                "signal": "",
                "signal_parts": parse_signal(""),
                "opener": "",
            }
        )
    return rows


def push_leads(rows: list[dict]) -> None:
    """Write rows to Sheets with retry/backoff; mark local jsonl synced on success."""
    from .enriched_store import append_enriched_lead, mark_synced

    for i, row in enumerate(rows, start=1):
        person = row["person"]
        email = person.get("email", "") or row.get("domain", "")
        # Ensure local-first even for credit-safe / CSV paths
        if (person.get("email") or "").strip():
            append_enriched_lead(person, synced=False)
        try:
            opener = (row.get("opener") or "").strip()
            added = add_row_with_retry(person, opener)
            status = "added" if added else "skipped"
            print(f"[{i}/{len(rows)}] {status}: {email}", flush=True)
            if added or status == "skipped":
                mark_synced(
                    person_id=str(person.get("person_id") or person.get("id") or ""),
                    email=str(person.get("email") or ""),
                )
        except Exception as e:
            print(f"[{i}/{len(rows)}] error for {email}: {e}", flush=True)
            print(
                "  (lead kept in data/enriched_leads.jsonl — run sync-leads later)",
                flush=True,
            )


def run_sync_leads(args: argparse.Namespace) -> None:
    from .enriched_store import sync_leads_to_sheet

    limit = getattr(args, "limit", None)
    summary = sync_leads_to_sheet(limit=limit)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def run_research(args: argparse.Namespace) -> None:
    if args.from_csv:
        os.environ["APOLLO_KEY"] = ""

    limit = getattr(args, "limit", None)
    enrich = getattr(args, "enrich", None)

    # --debug-dedup: small search + dedup diagnostics only (no enrich / no writes)
    if getattr(args, "debug_dedup", False):
        from .research import run_debug_dedup

        run_debug_dedup()
        return

    # --dry-run: print search plan only (no Apollo / Sheets)
    if args.dry_run:
        from .research import print_research_plan

        print_research_plan(limit=limit, enrich=enrich)
        return

    rows = collect_leads(
        page=args.page,
        require_email=not args.allow_no_email,
        limit=limit,
        enrich=enrich,
    )
    print(f"found {len(rows)} people", flush=True)

    for row in rows:
        row.setdefault("opener", "")
        p = row["person"]
        preview = {
            "email": p.get("email"),
            "first_name": p.get("first_name"),
            "last_name": p.get("last_name"),
            "title": p.get("title"),
            "company": (p.get("organization") or {}).get("name"),
            "domain": row["domain"],
            "verified_email": p.get("verified_email"),
            "enriched": p.get("enriched"),
            "person_id": p.get("person_id") or p.get("id"),
        }
        print(json.dumps(preview, ensure_ascii=False), flush=True)

    push_leads(rows)


def run_send(args: argparse.Namespace) -> None:
    from .campaign import run_first_touch

    run_first_touch(limit=args.limit)


def run_followup(args: argparse.Namespace) -> None:
    from .followup import run_followups

    run_followups(followup_number=args.wave)


def run_replies(args: argparse.Namespace) -> None:
    from .replies import run_reply_check

    run_reply_check(days=args.days)


def run_check_forms(_args: argparse.Namespace) -> None:
    from .async_onboarding import check_form_responses

    check_form_responses()


def run_launch_pending(_args: argparse.Namespace) -> None:
    """Research + send practice emails for clients waiting to launch."""
    from delivery.launch_client import process_pending_launches

    process_pending_launches()


def run_draft_reply(args: argparse.Namespace) -> None:
    from .ai_reply_assistant import draft_reply, save_draft
    from .to_sheets import get_worksheet, is_email

    email = (args.email or "").strip().lower()
    body = (args.body or "").strip()
    if not body:
        print("Provide --body \"their message text\"")
        return

    lead_context = {"email": email, "contact": "", "company": "", "title": ""}
    if is_email(email):
        try:
            ws = get_worksheet("Agencies")
            for row in ws.get_all_values()[1:]:
                row = (row + [""] * len(HEADERS))[: len(HEADERS)]
                if row[3].strip().lower() == email:
                    lead_context = {
                        "email": email,
                        "company": row[0],
                        "contact": row[1],
                        "title": row[2],
                    }
                    break
        except Exception as e:
            print(f"sheet lookup skipped: {e}")

    draft = draft_reply(body, lead_context)
    path = save_draft(email or "unknown", body, draft, lead_context)
    print("--- draft ---")
    print(draft)
    print(f"--- saved {path} ---")


def run_verify_payment(args: argparse.Namespace) -> None:
    from .payment_tracker import mark_client_as_paid, verify_payment

    txid = args.txid.strip()
    if args.email:
        result = mark_client_as_paid(args.email, txid)
    else:
        result = verify_payment(txid)
    print(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="DentalMeet lead pipeline")
    sub = parser.add_subparsers(dest="command")

    p_research = sub.add_parser("research", help="Find leads and write to Sheets")
    p_research.add_argument(
        "--dry-run",
        action="store_true",
        help="Print today's search plan only (no Apollo API / Sheets writes)",
    )
    p_research.add_argument(
        "--allow-no-email",
        action="store_true",
        help="Keep Apollo search hits even without revealed emails",
    )
    p_research.add_argument("--page", type=int, default=1)
    p_research.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max NEW leads this run (default daily cap 30)",
    )
    p_research.add_argument(
        "--enrich",
        type=int,
        default=None,
        metavar="N",
        help="BATCH MODE: enrich up to N new decision-makers (spends credits). "
        "Default/cron: enrichment OFF.",
    )
    p_research.add_argument(
        "--from-csv",
        action="store_true",
        help="Force CSV mode (ignore APOLLO_KEY)",
    )
    p_research.add_argument(
        "--debug-dedup",
        action="store_true",
        help="Diagnose batch dedup (1–2 search variants, no enrich, no sheet writes)",
    )

    p_sync = sub.add_parser(
        "sync-leads",
        help="Push unsynced data/enriched_leads.jsonl rows to Agencies (retry/backoff)",
    )
    p_sync.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max unsynced leads to write this run",
    )

    p_send = sub.add_parser("send", help="Send first-touch emails to unsent leads")
    p_send.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Max emails this run (default 5, hard cap 12)",
    )

    p_fu = sub.add_parser("followup", help="Send follow-up wave 1 or 2")
    p_fu.add_argument(
        "wave",
        type=int,
        choices=[1, 2],
        help="Follow-up wave: 1 (3+ days) or 2 (7+ days)",
    )

    p_replies = sub.add_parser("replies", help="Check Gmail IMAP for replies and update Sheet")
    p_replies.add_argument(
        "--days",
        type=int,
        default=3,
        help="Look back N days in inbox (default 3)",
    )

    sub.add_parser(
        "check_forms",
        help="Ingest Google Form onboarding responses into Clients + auto-launch",
    )
    sub.add_parser(
        "launch_pending",
        help="Build dental lists + send for clients with Workspace connected",
    )

    p_draft = sub.add_parser("draft_reply", help="Draft an AI reply for a lead question")
    p_draft.add_argument("email", help="Lead email (for context lookup)")
    p_draft.add_argument(
        "--body",
        required=True,
        help="Incoming message text to answer",
    )

    p_pay = sub.add_parser("verify_payment", help="Verify USDT TRC20 payment via TronScan")
    p_pay.add_argument("txid", help="Transaction hash")
    p_pay.add_argument(
        "--email",
        default="",
        help="If set, mark this client paid in Clients sheet after verify",
    )

    # Research flags at top level when no subcommand
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-no-email", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--page", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--from-csv", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()

    if args.command == "send":
        run_send(args)
        return

    if args.command == "sync-leads":
        run_sync_leads(args)
        return

    if args.command == "followup":
        run_followup(args)
        return

    if args.command == "replies":
        run_replies(args)
        return

    if args.command == "check_forms":
        run_check_forms(args)
        return

    if args.command == "launch_pending":
        run_launch_pending(args)
        return

    if args.command == "draft_reply":
        run_draft_reply(args)
        return

    if args.command == "verify_payment":
        run_verify_payment(args)
        return

    # Default / research
    if args.command is None:
        args = argparse.Namespace(
            dry_run=args.dry_run,
            allow_no_email=args.allow_no_email,
            page=args.page if args.page is not None else 1,
            from_csv=args.from_csv,
        )
    elif getattr(args, "page", None) is None and args.command == "research":
        args.page = 1

    run_research(args)


if __name__ == "__main__":
    main()

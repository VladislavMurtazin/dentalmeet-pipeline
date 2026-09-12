"""CLI entry: research, send, follow-up, or reply check.

  python -m pipeline.main              # research (default)
  python -m pipeline.main research
  python -m pipeline.main send
  python -m pipeline.main send --limit 10
  python -m pipeline.main reengage
  python -m pipeline.main reengage --apply
  python -m pipeline.main followup 1
  python -m pipeline.main followup 2
  python -m pipeline.main replies
  python -m pipeline.main replies --days 7
  python -m pipeline.main reply-inboxes
  python -m pipeline.main test-classify --text "yes, interested!"
  python -m pipeline.main reply-stats
  python -m pipeline.main show-templates
  python -m pipeline.main warmup-matrix
  python -m pipeline.main warmup-health
  python -m pipeline.main test-smtp --email vladislav@meet-agency.com
  python -m pipeline.main account-health
  python -m pipeline.main throttle-status
  python -m pipeline.main check_forms
  python -m pipeline.main launch_pending
  python -m pipeline.main draft_reply EMAIL --body "their question"
  python -m pipeline.main credit-stats
  python -m pipeline.main daily_summary [--force]
  python -m pipeline.main alert-test [--times 3]
  python -m pipeline.main deliverability [--record inbox|spam]
  python -m pipeline.main deliverability-test [--wait 300]
  python -m pipeline.main deliverability-report [--days 30]
  python -m pipeline.main bounce scan|cleanup|stats|record|test EMAIL
  python -m pipeline.main queue-status
  python -m pipeline.main timezone-test --state CA [--email x@y.com]
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
    from .reengage import run_reengage as _list_reengage

    # Log 2-month-silent candidates (no sheet writes). send includes them.
    try:
        _list_reengage(apply=False)
    except Exception as e:
        print(f"reengage scan skipped: {e}")
    run_first_touch(limit=args.limit)


def run_followup(args: argparse.Namespace) -> None:
    from .followup import run_followups

    run_followups(followup_number=args.wave)


def run_reengage(args: argparse.Namespace) -> None:
    from .reengage import run_reengage as _run

    _run(apply=bool(getattr(args, "apply", False)), limit=getattr(args, "limit", None))


def run_replies(args: argparse.Namespace) -> None:
    from .replies import run_reply_check

    run_reply_check(days=args.days)


def run_reply_inboxes(args: argparse.Namespace) -> None:
    from .replies import diagnose_reply_inboxes

    diagnose_reply_inboxes(days=getattr(args, "days", 3))


def run_test_classify(args: argparse.Namespace) -> None:
    from .reply_classifier import classify_reply

    text = args.text or ""
    result = classify_reply(text)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def run_reply_stats(_args: argparse.Namespace) -> None:
    from .reply_classifier import print_reply_stats

    print_reply_stats()


def run_show_templates(_args: argparse.Namespace) -> None:
    from .replies import print_response_templates

    print_response_templates()


def run_account_health(_args: argparse.Namespace) -> None:
    from .send_throttle import print_account_health

    print_account_health()


def run_throttle_status(_args: argparse.Namespace) -> None:
    from .send_throttle import print_throttle_status

    print_throttle_status()


def run_account_temperature(_args: argparse.Namespace) -> None:
    from .send_throttle import print_account_temperature

    print_account_temperature()


def run_blacklist_check(args: argparse.Namespace) -> None:
    from .blacklist_check import print_recent, run_blacklist_check

    if getattr(args, "history", False):
        print_recent(int(getattr(args, "limit", 5) or 5))
        return
    result = run_blacklist_check(pause_on_hit=not getattr(args, "no_pause", False))
    print(json.dumps(result, indent=2))


def run_validate_email(args: argparse.Namespace) -> None:
    from .email_validation import validate_recipient

    meta = {}
    if getattr(args, "verified", False):
        meta["Verified"] = "yes"
    result = validate_recipient(args.email, meta=meta, force=getattr(args, "force", False))
    print(json.dumps(result, indent=2))


def run_send_pause(args: argparse.Namespace) -> None:
    from .email_validation import is_send_paused, set_send_pause

    if args.action == "status":
        paused, reason = is_send_paused()
        print(json.dumps({"paused": paused, "reason": reason}, indent=2))
        return
    if args.action == "on":
        set_send_pause(True, args.reason or "manual pause")
        print("send paused")
        return
    if args.action == "off":
        set_send_pause(False, "")
        print("send pause cleared")
        return


def run_check_forms(_args: argparse.Namespace) -> None:
    from .async_onboarding import check_form_responses

    check_form_responses()


def run_launch_pending(_args: argparse.Namespace) -> None:
    """Research + send practice emails for clients waiting to launch."""
    from delivery.launch_client import process_pending_launches

    process_pending_launches()


def run_draft_reply(args: argparse.Namespace) -> None:
    from .ai_reply_assistant import draft_reply, save_draft
    from .to_sheets import get_all_values_safe, get_worksheet_safe, is_email

    email = (args.email or "").strip().lower()
    body = (args.body or "").strip()
    if not body:
        print("Provide --body \"their message text\"")
        return

    lead_context = {"email": email, "contact": "", "company": "", "title": ""}
    if is_email(email):
        try:
            ws = get_worksheet_safe("Agencies")
            for row in get_all_values_safe(ws)[1:]:
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


def run_payment_status(_args: argparse.Namespace) -> None:
    from delivery.payment_tracker import payment_status_all

    print(json.dumps(payment_status_all(), indent=2))


def run_crm_export(_args: argparse.Namespace) -> None:
    from .crm_export import export_crm

    export_crm()


def run_case_studies(args: argparse.Namespace) -> None:
    from delivery.case_studies import (
        approve_case_study,
        check_triggers,
        export_to_site,
        list_case_studies,
        simulate_case_study,
    )

    cmd = getattr(args, "case_cmd", "list")
    if cmd == "list":
        print(json.dumps(list_case_studies(), indent=2))
    elif cmd == "check":
        print(json.dumps(check_triggers(dry_run=getattr(args, "dry_run", False)), indent=2))
    elif cmd == "simulate":
        print(
            json.dumps(
                simulate_case_study(
                    getattr(args, "email", "demo@agency.com"),
                    meetings=getattr(args, "meetings", 12),
                    location=getattr(args, "location", "Los Angeles, CA"),
                ),
                indent=2,
            )
        )
    elif cmd == "approve":
        print(
            json.dumps(
                approve_case_study(
                    args.case_id,
                    quote=getattr(args, "quote", None) or None,
                    show_name=getattr(args, "show_name", False),
                    show_logo=getattr(args, "show_logo", False),
                ),
                indent=2,
            )
        )
    elif cmd == "export":
        export_to_site()


def run_credit_stats(_args: argparse.Namespace) -> None:
    from .credit_budget import print_credit_stats

    print_credit_stats()


def run_daily_summary(_args: argparse.Namespace) -> None:
    from .alerts import send_daily_summary

    send_daily_summary(force=getattr(_args, "force", False))


def run_alert_test(_args: argparse.Namespace) -> None:
    """Simulate 3 consecutive job failures to verify alerting."""
    from .alerts import simulate_job_failures

    times = getattr(_args, "times", 3)
    job = getattr(_args, "job", "alert_test")
    simulate_job_failures(job, times=times)
    print(f"simulated {times} failures for job={job!r}")


def run_deliverability(_args: argparse.Namespace) -> None:
    from .deliverability import record_test_result, run_deliverability_test

    if getattr(_args, "record", None):
        record_test_result(_args.record)
        return
    result = run_deliverability_test(check_imap=not getattr(_args, "no_imap", False))
    print(json.dumps(result, ensure_ascii=False), flush=True)


def run_deliverability_test_cmd(args: argparse.Namespace) -> None:
    from .deliverability_check import run_daily_check, run_scheduled_check, run_weekly_deep_check

    wait = getattr(args, "wait", 300)
    if getattr(args, "weekly", False):
        result = run_weekly_deep_check(wait_seconds=wait)
    elif getattr(args, "scheduled", False):
        result = run_scheduled_check(wait_seconds=wait)
    else:
        result = run_daily_check(
            wait_seconds=wait,
            skip_send=getattr(args, "skip_send", False),
        )
    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)


def run_deliverability_report(args: argparse.Namespace) -> None:
    from .deliverability_check import print_deliverability_report

    print_deliverability_report(days=getattr(args, "days", 30))


def run_bounce(_args: argparse.Namespace) -> None:
    from .bounce_handler import (
        mark_bounced_leads_in_sheet,
        print_bounce_stats,
        record_bounced_email,
        scan_all_bounces,
        simulate_bounce,
    )

    cmd = getattr(_args, "bounce_cmd", "stats")
    if cmd == "scan":
        result = scan_all_bounces(days=getattr(_args, "days", 7))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    elif cmd == "cleanup":
        result = mark_bounced_leads_in_sheet()
        print(json.dumps(result, ensure_ascii=False), flush=True)
    elif cmd == "stats":
        print_bounce_stats()
    elif cmd == "record":
        ok = record_bounced_email(_args.email, source="manual")
        print(json.dumps({"recorded": ok, "email": _args.email}), flush=True)
    elif cmd == "test":
        ok = simulate_bounce(_args.email)
        print(json.dumps({"recorded": ok, "email": _args.email}), flush=True)
    else:
        print(f"unknown bounce command: {cmd}")


def run_queue_status(_args: argparse.Namespace) -> None:
    from .send_queue import print_queue_status

    print_queue_status()


def run_timezone_test(args: argparse.Namespace) -> None:
    """Test timezone window: e.g. CA lead at 3am PT should not send."""
    from .send_queue import split_by_send_window
    from .timezone_utils import (
        can_send_now,
        format_local_datetime_safe,
        get_recipient_timezone,
        next_send_datetime,
        recipient_local_time,
    )

    state = getattr(args, "state", "CA") or "CA"
    lead = {"Email": args.email or "test@example.com", "state": state, "Company": f"Test Co {state}"}
    tz = get_recipient_timezone(lead)
    local = recipient_local_time(tz)
    print(f"lead state={state} timezone={tz}")
    print(f"local time: {local.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"can_send_now: {can_send_now(tz)}")
    if not can_send_now(tz):
        nxt = next_send_datetime(tz)
        print(f"next send: {format_local_datetime_safe(nxt, tz)}")
    item = {
        "to": lead["Email"],
        "subject": "test",
        "body": "test",
        "lead": lead,
    }
    ready, to_queue = split_by_send_window([item])
    print(f"ready: {len(ready)}, queued: {len(to_queue)}")
    if to_queue:
        print("PASS: would queue (not send now)")
    else:
        print("SEND: would send immediately")


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

    p_reengage = sub.add_parser(
        "reengage",
        help="List Agencies leads silent 60+ days (no reply) for another first-touch",
    )
    p_reengage.add_argument(
        "--apply",
        action="store_true",
        help="Clear Sent and write sent_was/reengage_reset in Notes (DRY_RUN skips writes)",
    )
    p_reengage.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max eligible leads to list/reset this run",
    )

    p_fu = sub.add_parser("followup", help="Send follow-up wave 1 or 2")
    p_fu.add_argument(
        "wave",
        type=int,
        choices=[1, 2],
        help="Follow-up wave: 1 (3+ days) or 2 (7+ days)",
    )

    p_replies = sub.add_parser(
        "replies",
        help="Check all sending-account IMAP inboxes for replies and update Sheet",
    )
    p_replies.add_argument(
        "--days",
        type=int,
        default=3,
        help="Look back N days in inbox (default 3)",
    )

    p_rin = sub.add_parser(
        "reply-inboxes",
        help="Diagnose IMAP connectivity for every reply inbox",
    )
    p_rin.add_argument(
        "--days",
        type=int,
        default=3,
        help="Look back N days when counting new messages (default 3)",
    )

    p_tc = sub.add_parser(
        "test-classify",
        help="Classify a reply text (multilingual keyword detector)",
    )
    p_tc.add_argument("--text", required=True, help="Reply body to classify")

    sub.add_parser(
        "reply-stats",
        help="Show reply classification breakdown by category/language",
    )
    sub.add_parser(
        "show-templates",
        help="Preview auto-reply templates (interested/question/etc.)",
    )
    sub.add_parser(
        "warmup-matrix",
        help="Print from→to warmup matrix for last 24h (all Zoho+Google)",
    )
    sub.add_parser(
        "warmup-health",
        help="Warmup health: active accounts, cross-provider, external",
    )
    p_tsmtp = sub.add_parser(
        "test-smtp",
        help="Test SMTP connection/auth/send for one sending account",
    )
    p_tsmtp.add_argument("--email", required=True, help="Account email to test")
    p_tsmtp.add_argument(
        "--no-send",
        action="store_true",
        help="Skip self-send probe (login only)",
    )

    sub.add_parser(
        "account-health",
        help="Show SMTP account health scores and 30d rates",
    )
    sub.add_parser(
        "account-temperature",
        help="Show account temperature 0-100 and send bands",
    )
    sub.add_parser(
        "throttle-status",
        help="Show per-account daily send limits and remaining capacity",
    )

    p_bl = sub.add_parser("blacklist-check", help="Check domain/IPs on DNS blacklists")
    p_bl.add_argument("--history", action="store_true", help="Show recent check log")
    p_bl.add_argument("--limit", type=int, default=5)
    p_bl.add_argument("--no-pause", action="store_true", help="Do not pause sends on hit")

    p_val = sub.add_parser("validate-email", help="Validate one email (ZeroBounce / Apollo trust)")
    p_val.add_argument("email")
    p_val.add_argument("--verified", action="store_true", help="Treat as Apollo-verified")
    p_val.add_argument("--force", action="store_true", help="Ignore cache")

    p_pause = sub.add_parser("send-pause", help="Global outbound pause flag")
    p_pause.add_argument("action", choices=["status", "on", "off"])
    p_pause.add_argument("--reason", default="")

    sub.add_parser(
        "check_forms",
        help="Ingest Google Form onboarding responses into Clients + auto-launch",
    )
    sub.add_parser(
        "launch_pending",
        help="Build dental lists + send for clients with Workspace connected",
    )

    sub.add_parser("credit-stats", help="Show Apollo credit budget usage this month")

    p_summary = sub.add_parser("daily_summary", help="Send daily pipeline summary alert")
    p_summary.add_argument("--force", action="store_true", help="Send even if already sent today")

    p_alert = sub.add_parser("alert-test", help="Simulate job failures to test alerting")
    p_alert.add_argument("--times", type=int, default=3)
    p_alert.add_argument("--job", default="alert_test")

    p_deliv = sub.add_parser("deliverability", help="Run or record deliverability test")
    p_deliv.add_argument(
        "--record",
        choices=["inbox", "spam"],
        help="Manually record last test folder placement",
    )
    p_deliv.add_argument("--no-imap", action="store_true", help="Send only; skip IMAP check")

    p_dtest = sub.add_parser(
        "deliverability-test",
        help="Send probe from each SMTP account and check Inbox vs Spam",
    )
    p_dtest.add_argument(
        "--wait",
        type=int,
        default=300,
        help="Seconds to wait before IMAP check (default 300)",
    )
    p_dtest.add_argument(
        "--skip-send",
        action="store_true",
        help="Only check IMAP for today's subjects (no send)",
    )
    p_dtest.add_argument(
        "--weekly",
        action="store_true",
        help="Run multi-provider weekly deep check",
    )
    p_dtest.add_argument(
        "--scheduled",
        action="store_true",
        help="Daily check + weekly deep check on Sundays",
    )

    p_drep = sub.add_parser(
        "deliverability-report",
        help="Show deliverability inbox/spam history",
    )
    p_drep.add_argument("--days", type=int, default=30)

    p_bounce = sub.add_parser("bounce", help="Bounce scan, cleanup, and stats")
    bounce_sub = p_bounce.add_subparsers(dest="bounce_cmd", required=True)
    p_b_scan = bounce_sub.add_parser("scan", help="Scan IMAP for bounce notifications")
    p_b_scan.add_argument("--days", type=int, default=7)
    bounce_sub.add_parser("cleanup", help="Mark bounced leads in Agencies sheet")
    bounce_sub.add_parser("stats", help="Print weekly bounce rate stats")
    p_b_rec = bounce_sub.add_parser("record", help="Manually record a bounced email")
    p_b_rec.add_argument("email")
    p_b_test = bounce_sub.add_parser("test", help="Simulate bounce for testing")
    p_b_test.add_argument("email")

    sub.add_parser("queue-status", help="Show emails waiting for recipient-local send window")

    p_tz = sub.add_parser("timezone-test", help="Test timezone send window for a state")
    p_tz.add_argument("--state", default="CA", help="US state code (default CA)")
    p_tz.add_argument("--email", default="test@example.com")

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

    sub.add_parser("payment-status", help="Show all clients with payment status")
    sub.add_parser("crm-export", help="Export CRM snapshot for /crm dashboard")

    p_cs = sub.add_parser("case-studies", help="Case study generation and publishing")
    cs_sub = p_cs.add_subparsers(dest="case_cmd", required=True)
    cs_sub.add_parser("list", help="List case studies and approval status")
    p_cs_check = cs_sub.add_parser("check", help="Trigger drafts for 10+ meeting clients")
    p_cs_check.add_argument("--dry-run", action="store_true")
    p_cs_sim = cs_sub.add_parser("simulate", help="Simulate case study draft")
    p_cs_sim.add_argument("--email", default="demo@agency.com")
    p_cs_sim.add_argument("--meetings", type=int, default=12)
    p_cs_sim.add_argument("--location", default="Los Angeles, CA")
    p_cs_ap = cs_sub.add_parser("approve", help="Approve and publish case study")
    p_cs_ap.add_argument("case_id")
    p_cs_ap.add_argument("--quote", default="")
    p_cs_ap.add_argument("--show-name", action="store_true")
    p_cs_ap.add_argument("--show-logo", action="store_true")
    cs_sub.add_parser("export", help="Export approved case studies to website JSON")

    # Research flags at top level when no subcommand
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-no-email", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--page", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--from-csv", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()

    from .alerts import run_monitored

    if args.command == "send":
        run_monitored("send", run_send, args)
        return

    if args.command == "sync-leads":
        run_monitored("sync-leads", run_sync_leads, args)
        return

    if args.command == "followup":
        run_monitored(f"followup_{args.wave}", run_followup, args)
        return

    if args.command == "reengage":
        run_monitored("reengage", run_reengage, args)
        return

    if args.command == "replies":
        run_monitored("replies", run_replies, args)
        return

    if args.command == "reply-inboxes":
        run_reply_inboxes(args)
        return

    if args.command == "test-classify":
        run_test_classify(args)
        return

    if args.command == "reply-stats":
        run_reply_stats(args)
        return

    if args.command == "show-templates":
        run_show_templates(args)
        return

    if args.command == "warmup-matrix":
        from .warmup import print_warmup_matrix

        print_warmup_matrix()
        return

    if args.command == "warmup-health":
        from .warmup import print_warmup_health

        print_warmup_health()
        return

    if args.command == "test-smtp":
        from .warmup import test_smtp_account

        test_smtp_account(args.email, send_probe=not args.no_send)
        return

    if args.command == "account-health":
        run_account_health(args)
        return

    if args.command == "account-temperature":
        run_account_temperature(args)
        return

    if args.command == "blacklist-check":
        run_monitored("blacklist_check", run_blacklist_check, args)
        return

    if args.command == "validate-email":
        run_validate_email(args)
        return

    if args.command == "send-pause":
        run_send_pause(args)
        return

    if args.command == "throttle-status":
        run_throttle_status(args)
        return

    if args.command == "check_forms":
        run_monitored("check_forms", run_check_forms, args)
        return

    if args.command == "launch_pending":
        run_monitored("launch_pending", run_launch_pending, args)
        return

    if args.command == "draft_reply":
        run_monitored("draft_reply", run_draft_reply, args)
        return

    if args.command == "verify_payment":
        run_monitored("verify_payment", run_verify_payment, args)
        return

    if args.command == "payment-status":
        run_payment_status(args)
        return

    if args.command == "crm-export":
        run_monitored("crm-export", run_crm_export, args)
        return

    if args.command == "case-studies":
        run_monitored("case-studies", run_case_studies, args)
        return

    if args.command == "credit-stats":
        run_monitored("credit-stats", run_credit_stats, args)
        return

    if args.command == "daily_summary":
        run_daily_summary(args)
        return

    if args.command == "alert-test":
        run_alert_test(args)
        return

    if args.command == "deliverability":
        run_monitored("deliverability", run_deliverability, args)
        return

    if args.command == "deliverability-test":
        run_monitored("deliverability_test", run_deliverability_test_cmd, args)
        return

    if args.command == "deliverability-report":
        run_deliverability_report(args)
        return

    if args.command == "bounce":
        run_bounce(args)
        return

    if args.command == "queue-status":
        run_queue_status(args)
        return

    if args.command == "timezone-test":
        run_timezone_test(args)
        return

    # Default / research
    if args.command is None:
        args = argparse.Namespace(
            dry_run=args.dry_run,
            allow_no_email=args.allow_no_email,
            page=args.page if args.page is not None else 1,
            from_csv=args.from_csv,
            limit=None,
            enrich=None,
            debug_dedup=False,
        )
        run_monitored("research", run_research, args)
        return
    elif getattr(args, "page", None) is None and args.command == "research":
        args.page = 1

    if args.command == "research":
        run_monitored("research", run_research, args)
        return

    parser.print_help()


if __name__ == "__main__":
    main()

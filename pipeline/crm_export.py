"""Export CRM snapshot from Google Sheets → data/crm.json + lead site public/crm.json."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
CRM_PATH = ROOT / "data" / "crm.json"
LEAD_PUBLIC = ROOT.parent.parent / "lead" / "public"
if not LEAD_PUBLIC.parent.exists():
    LEAD_PUBLIC = ROOT.parent / "lead" / "public"

REGISTRY_PATH = ROOT / "data" / "invoices" / "registry.json"
INTERESTED_RE = re.compile(r"interested|yes|positive|call|meeting", re.I)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _lead_status(sent: str, replied: str, reply_type: str) -> str:
    rt = (reply_type or "").strip().lower()
    if rt in ("client", "converted"):
        return "client"
    if INTERESTED_RE.search(rt or ""):
        return "interested"
    if (replied or "").strip().lower() in ("yes", "y", "true", "1"):
        return "replied"
    if (sent or "").strip():
        return "contacted"
    return "new"


def _lead_geo(row: list[str], headers: dict[str, int]) -> str:
    for key in ("Signal", "Notes"):
        idx = headers.get(key)
        if idx is not None and idx < len(row):
            val = (row[idx] or "").strip()
            if val:
                from .timezone_utils import get_recipient_timezone

                lead = {"notes": val, "signal": val}
                tz = get_recipient_timezone(lead)
                if tz:
                    return tz.split("/")[-1].replace("_", " ")
                return val[:40]
    return ""


def _parse_money(raw: str) -> float:
    try:
        return float(str(raw or "0").replace("$", "").replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _agency_rows() -> tuple[list[dict], dict[str, int]]:
    from .to_sheets import get_all_values_safe, get_worksheet_safe

    ws = get_worksheet_safe("Agencies")
    values = get_all_values_safe(ws)
    if not values:
        return [], {}
    headers = {h.strip(): i for i, h in enumerate(values[0]) if h.strip()}
    leads: list[dict] = []
    for row in values[1:]:
        if not row or not any(c.strip() for c in row):
            continue
        email = row[headers.get("Email", 3)] if len(row) > headers.get("Email", 3) else ""
        if not email.strip():
            continue
        sent = row[headers["Sent"]] if "Sent" in headers and len(row) > headers["Sent"] else ""
        replied = row[headers["Replied"]] if "Replied" in headers and len(row) > headers["Replied"] else ""
        reply_type = (
            row[headers["Reply type"]]
            if "Reply type" in headers and len(row) > headers["Reply type"]
            else ""
        )
        notes = row[headers["Notes"]] if "Notes" in headers and len(row) > headers["Notes"] else ""
        company = row[headers["Company"]] if "Company" in headers and len(row) > headers["Company"] else ""
        status = _lead_status(sent, replied, reply_type)
        last_contact = sent or replied or ""
        date_added = ""
        m = re.search(r"(\d{4}-\d{2}-\d{2})", notes or sent or "")
        if m:
            date_added = m.group(1)
        leads.append(
            {
                "company": company.strip(),
                "email": email.strip().lower(),
                "status": status,
                "last_contact": last_contact.strip(),
                "notes": (notes or "").strip()[:500],
                "geo": _lead_geo(row, headers),
                "date_added": date_added,
                "sent": bool((sent or "").strip()),
                "replied": bool((replied or "").strip().lower() in ("yes", "y", "true", "1")),
                "reply_type": (reply_type or "").strip(),
            }
        )
    return leads, headers


def _client_rows() -> list[dict]:
    from .to_sheets import get_all_values_safe, open_sheet_safe, open_worksheet_safe

    sh = open_sheet_safe(os.environ.get("SHEET_NAME", "Pilot CRM"))
    ws = open_worksheet_safe(sh, "Clients")
    values = get_all_values_safe(ws)
    if not values:
        return []
    headers = {h.strip(): i for i, h in enumerate(values[0]) if h.strip()}
    clients: list[dict] = []
    today = date.today()
    registry = _load_json(REGISTRY_PATH, {"invoices": []})
    next_invoice_by_email: dict[str, str] = {}
    for inv in registry.get("invoices") or []:
        if inv.get("status") != "unpaid":
            continue
        email = (inv.get("client_email") or "").lower()
        due = inv.get("due_date") or ""
        if email and due:
            prev = next_invoice_by_email.get(email)
            if not prev or due < prev:
                next_invoice_by_email[email] = due

    active_statuses = {
        "running",
        "paid",
        "active",
        "awaiting_workspace",
        "awaiting_upfront",
        "pilot",
    }

    for row in values[1:]:
        if not row or len(row) < 3:
            continue
        email = row[headers.get("Email", 2)].strip().lower() if "Email" in headers else row[2].strip().lower()
        if not email:
            continue
        name = row[headers.get("Name", 1)] if "Name" in headers and len(row) > headers["Name"] else row[1]
        company = row[headers.get("Company", 0)] if "Company" in headers and len(row) > headers["Company"] else ""
        status = row[headers["Status"]] if "Status" in headers and len(row) > headers["Status"] else ""
        if (status or "").strip().lower() not in active_statuses:
            continue
        ws_status = (
            row[headers["workspace_status"]]
            if "workspace_status" in headers and len(row) > headers["workspace_status"]
            else "unknown"
        )
        meetings = 0
        if "meetings_this_month" in headers and len(row) > headers["meetings_this_month"]:
            try:
                meetings = int(str(row[headers["meetings_this_month"]] or 0))
            except ValueError:
                meetings = 0
        revenue = _parse_money(
            row[headers["Total Paid"]]
            if "Total Paid" in headers and len(row) > headers["Total Paid"]
            else ""
        )
        payment_status = (
            row[headers["Payment Status"]]
            if "Payment Status" in headers and len(row) > headers["Payment Status"]
            else ""
        )
        clients.append(
            {
                "name": (name or "").strip(),
                "company": (company or name or "").strip(),
                "email": email,
                "status": (status or "").strip(),
                "workspace_status": (ws_status or "unknown").strip(),
                "meetings_this_month": meetings,
                "revenue_generated": revenue,
                "next_invoice_date": next_invoice_by_email.get(email, ""),
                "payment_status": (payment_status or "").strip(),
            }
        )
    return clients


def _revenue_summary() -> dict[str, float]:
    registry = _load_json(REGISTRY_PATH, {"invoices": []})
    today = date.today()
    month_start = today.replace(day=1)
    pending = 0.0
    paid_month = 0.0
    recurring = 0.0
    for inv in registry.get("invoices") or []:
        try:
            amount = float(inv.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        st = (inv.get("status") or "").lower()
        if st == "unpaid":
            pending += amount
        elif st == "paid":
            paid_raw = inv.get("paid_date") or inv.get("invoice_date") or ""
            try:
                paid_d = date.fromisoformat(str(paid_raw)[:10])
                if paid_d >= month_start:
                    paid_month += amount
            except ValueError:
                pass
    try:
        from delivery.client_tracker import get_client_payment_model
        from delivery.client_reports import active_clients

        for c in active_clients():
            email = c.get("email") or ""
            if not email:
                continue
            model = get_client_payment_model(email)
            upfront = float(model.get("upfront") or 0)
            per = float(model.get("per_meeting") or 0)
            recurring += upfront / 12 if upfront else per * 2
    except Exception:
        pass
    return {
        "monthly_recurring": round(recurring, 2),
        "pending_invoices": round(pending, 2),
        "paid_this_month": round(paid_month, 2),
    }


def build_crm_snapshot() -> dict:
    leads, _ = _agency_rows()
    clients = _client_rows()
    revenue = _revenue_summary()

    total = len(leads)
    contacted = sum(1 for l in leads if l["status"] != "new")
    replies = sum(1 for l in leads if l["status"] in ("replied", "interested", "client"))
    interested = sum(1 for l in leads if l["status"] == "interested")
    client_count = len(clients)
    monthly_revenue = revenue["paid_this_month"] + revenue["monthly_recurring"]

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overview": {
            "total_leads": total,
            "leads_contacted": contacted,
            "replies": replies,
            "interested": interested,
            "clients": client_count,
            "monthly_revenue": round(monthly_revenue, 2),
        },
        "leads": leads,
        "clients": clients,
        "revenue": revenue,
    }


def export_crm() -> dict:
    data = build_crm_snapshot()
    _save_json(CRM_PATH, data)
    if LEAD_PUBLIC.parent.exists():
        _save_json(LEAD_PUBLIC / "crm.json", data)
    print(f"crm export: {data['overview']['total_leads']} leads, {data['overview']['clients']} clients")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export CRM data for private dashboard")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("export", help="Fetch sheets and write crm.json")
    sub.add_parser("print", help="Print snapshot JSON to stdout")
    args = parser.parse_args(argv)
    cmd = args.cmd or "export"
    if cmd == "print":
        print(json.dumps(build_crm_snapshot(), indent=2))
        return 0
    export_crm()
    return 0


if __name__ == "__main__":
    sys.exit(main())

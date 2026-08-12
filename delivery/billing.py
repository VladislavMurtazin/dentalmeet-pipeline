"""Billing: qualify → invoice PDF → email client (Pilot / Repeat)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from calendar import month_name
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
INVOICES_DIR = ROOT / "data" / "invoices"
REGISTRY_PATH = INVOICES_DIR / "registry.json"

RATE_PER_MEETING = 150
PILOT_MAX = 750
REPEAT_UPFRONT = 300
REPEAT_FLAT = 1000
FLAT_PILOT = PILOT_MAX


def get_client_payment_model(client_email: str) -> dict:
    from .client_tracker import get_client_payment_model as _get

    return _get(client_email)


def compute_amount(client_email: str, meetings: int) -> tuple[float, str]:
    model = get_client_payment_model(client_email)
    return _amount_from_model(model, meetings)


def _amount_from_model(model: dict, qualified_meetings: int) -> tuple[float, str]:
    ctype = (model.get("client_type") or "Pilot").lower()
    q = max(0, int(qualified_meetings))

    if model.get("flat_meetings") or (
        ctype == "repeat" and model.get("upfront") == REPEAT_FLAT
    ):
        return float(REPEAT_FLAT), "repeat_flat_1000"

    if ctype == "repeat":
        return float(REPEAT_UPFRONT + q * RATE_PER_MEETING), "repeat_300_plus_150"

    return float(min(q * RATE_PER_MEETING, PILOT_MAX)), "pilot_150_cap_750"


def format_qualified_invoice(
    qualified_meetings: int,
    *,
    amount: float | None = None,
    model: str = "pilot_150_cap_750",
) -> str:
    q = int(qualified_meetings)
    if model == "repeat_flat_1000":
        return (
            f"Repeat flat: ${REPEAT_FLAT:,.0f} for up to 7 meetings "
            f"(qualified so far: {q})"
        )
    if model == "repeat_300_plus_150":
        total = REPEAT_UPFRONT + q * RATE_PER_MEETING if amount is None else float(amount)
        return (
            f"Setup ${REPEAT_UPFRONT} + Qualified meetings: {q} × "
            f"${RATE_PER_MEETING} = ${total:,.0f}"
        )
    uncapped = q * RATE_PER_MEETING
    total = min(uncapped, PILOT_MAX) if amount is None else float(amount)
    if uncapped > PILOT_MAX:
        return (
            f"Qualified meetings: {q} × ${RATE_PER_MEETING} = ${uncapped:,.0f} "
            f"(pilot cap ${PILOT_MAX:,.0f} → ${total:,.0f})"
        )
    return f"Qualified meetings: {q} × ${RATE_PER_MEETING} = ${total:,.0f}"


def invoice_summary(client_email: str, meetings: int) -> dict:
    amount, model = compute_amount(client_email, meetings)
    pay = get_client_payment_model(client_email)
    return {
        "client_email": client_email,
        "meetings": meetings,
        "qualified_meetings": meetings,
        "amount": amount,
        "model": model,
        "client_type": pay.get("client_type"),
        "invoice_line": format_qualified_invoice(
            meetings, amount=amount, model=model
        ),
        "wallet": os.environ.get("TRC20_WALLET_ADDRESS", ""),
    }


def _safe(email: str) -> str:
    return email.strip().lower().replace("@", "_at_")


def _invoice_number(client_email: str, when: date | None = None) -> str:
    when = when or date.today()
    slug = _safe(client_email).replace(".", "")[:18]
    return f"INV-{when.strftime('%Y%m%d')}-{slug}"


def _period_label(period: str, ref: date | None = None) -> str:
    ref = ref or date.today()
    if period == "week":
        start = ref - timedelta(days=ref.weekday() + 7)
        end = start + timedelta(days=6)
        return f"{start.isoformat()} to {end.isoformat()}"
    # previous calendar month for monthly invoices on the 1st; else current month
    if ref.day == 1:
        first = (ref.replace(day=1) - timedelta(days=1)).replace(day=1)
    else:
        first = ref.replace(day=1)
    if first.month == 12:
        last = first.replace(year=first.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        last = first.replace(month=first.month + 1, day=1) - timedelta(days=1)
    return f"{month_name[first.month]} {first.day}-{last.day}, {first.year}"


def _load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"invoices": []}
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"invoices": []}


def _save_registry(data: dict) -> None:
    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _register_invoice(record: dict) -> None:
    data = _load_registry()
    invoices = data.setdefault("invoices", [])
    # upsert by invoice_number
    for i, inv in enumerate(invoices):
        if inv.get("invoice_number") == record["invoice_number"]:
            invoices[i] = {**inv, **record}
            _save_registry(data)
            return
    invoices.append(record)
    _save_registry(data)


def calculate_invoice(
    client_email: str,
    period: str = "month",
    *,
    write_files: bool = True,
) -> dict:
    """
    Qualify meetings → amount by Pilot/Repeat model.

    Pilot: min(q * 150, 750)
    Repeat: 300 + q * 150 (or $1000 flat)
    """
    from .qualify_meetings import qualify_all_meetings

    qualified = qualify_all_meetings(client_email, period=period)
    q = qualified["qualified_meetings"]
    pay = get_client_payment_model(client_email)
    amount, model = _amount_from_model(pay, q)
    line = format_qualified_invoice(q, amount=amount, model=model)
    today = date.today()
    due = today + timedelta(days=7)
    inv_no = _invoice_number(client_email, today)

    result = {
        "client_email": client_email,
        "period": period,
        "period_label": _period_label(period, today),
        "client_type": pay.get("client_type"),
        "payment_model": pay,
        "total_meetings": qualified["total_meetings"],
        "qualified_meetings": q,
        "disqualified_meetings": qualified["disqualified_meetings"],
        "breakdown": qualified["breakdown"],
        "amount": amount,
        "model": model,
        "invoice_line": line,
        "invoice_number": inv_no,
        "invoice_date": today.isoformat(),
        "due_date": due.isoformat(),
        "wallet": os.environ.get("TRC20_WALLET_ADDRESS", ""),
        "due": "after_pilot"
        if (pay.get("client_type") or "").lower() == "pilot"
        else "upfront_plus_meetings",
        "html_path": "",
        "pdf_path": "",
        "status": "unpaid",
    }

    print(line)
    if write_files:
        result["html_path"] = str(_write_invoice_html(result))
        pdf = generate_invoice_pdf(client_email, period=period, inv=result)
        result["pdf_path"] = str(pdf) if pdf else ""
        _register_invoice(
            {
                "invoice_number": inv_no,
                "client_email": client_email,
                "invoice_date": result["invoice_date"],
                "due_date": result["due_date"],
                "amount": amount,
                "qualified_meetings": q,
                "period": period,
                "period_label": result["period_label"],
                "status": "unpaid",
                "pdf_path": result["pdf_path"],
                "html_path": result["html_path"],
                "reminder_sent": False,
            }
        )
        print(f"invoice HTML: {result['html_path']}")
        if result["pdf_path"]:
            print(f"invoice PDF: {result['pdf_path']}")
    return result


def generate_invoice_pdf(
    client_email: str,
    period: str = "month",
    inv: dict | None = None,
) -> Path | None:
    """Professional PDF invoice → data/invoices/{email}_{date}.pdf"""
    if inv is None:
        inv = calculate_invoice(client_email, period=period, write_files=False)

    try:
        from fpdf import FPDF
    except ImportError:
        print("fpdf2 not installed — HTML only (pip install fpdf2)")
        _write_invoice_html(inv)
        return None

    # Resolve client name
    name = client_email
    try:
        from pipeline.to_sheets import connect

        gc = connect()
        sh = gc.open(os.environ.get("SHEET_NAME", "Pilot CRM"))
        ws = sh.worksheet("Clients")
        for row in ws.get_all_values()[1:]:
            if row and len(row) > 2 and row[2].strip().lower() == client_email.lower():
                name = row[1] or client_email
                break
    except Exception:
        pass

    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    today = inv.get("invoice_date") or date.today().isoformat()
    path = INVOICES_DIR / f"{_safe(client_email)}_{today}.pdf"

    def _pdf_text(s: str) -> str:
        return (
            (s or "")
            .replace("×", "x")
            .replace("—", "-")
            .replace("–", "-")
            .replace("✅", "[OK]")
            .encode("latin-1", "replace")
            .decode("latin-1")
        )

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "DentalMeet Invoice", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=11)
    lines = [
        f"Invoice #: {inv.get('invoice_number', '')}",
        f"Client: {_pdf_text(str(name))}",
        f"Email: {client_email}",
        f"Period: {_pdf_text(str(inv.get('period_label', period)))}",
        f"Invoice date: {today}",
        f"Due date: {inv.get('due_date', '')}",
        "",
        "Line items:",
        f"  {_pdf_text(str(inv.get('invoice_line', '')))}",
        f"TOTAL DUE: ${float(inv.get('amount') or 0):,.0f} USD",
        "",
        "Payment instructions:",
        "  Method: USDT (TRC20 network)",
        f"  Wallet: {inv.get('wallet') or os.environ.get('TRC20_WALLET_ADDRESS', 'n/a')}",
        "  Reply to your invoice email with the TXID after sending.",
        "",
        f"Qualified meetings: {inv.get('qualified_meetings', 0)} / "
        f"{inv.get('total_meetings', 0)} total",
    ]
    for line in lines:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(w=0, h=6, text=_pdf_text(line))

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_x(pdf.l_margin)
    pdf.cell(0, 8, "Qualification breakdown", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    for item in (inv.get("breakdown") or [])[:40]:
        m = item.get("meeting") or {}
        flag = "Q" if item.get("qualified") else "X"
        row = (
            f"[{flag}] {m.get('event_date') or m.get('date', '')} "
            f"{m.get('attendee_email', '')} - {item.get('reason', '')}"
        )
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(w=0, h=5, text=_pdf_text(row))

    pdf.output(str(path))
    inv["pdf_path"] = str(path)
    return path


def _write_invoice_html(inv: dict) -> Path:
    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    today = inv.get("invoice_date") or date.today().isoformat()
    path = INVOICES_DIR / f"{_safe(inv['client_email'])}_{today}.html"

    rows = []
    for item in inv.get("breakdown") or []:
        m = item.get("meeting") or {}
        rows.append(
            "<tr>"
            f"<td>{m.get('event_date') or m.get('date', '')}</td>"
            f"<td>{m.get('attendee_email', '')}</td>"
            f"<td>{'Yes' if item.get('qualified') else 'No'}</td>"
            f"<td>{item.get('reason', '')}</td>"
            "</tr>"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Invoice {inv.get('invoice_number', '')}</title>
<style>
body{{font-family:Georgia,serif;max-width:720px;margin:2rem auto;color:#1a1a1a;
background:#f7f4ef;padding:1.5rem}}
h1{{font-size:1.5rem}} .muted{{color:#666}}
.box{{background:#fff;padding:1rem 1.25rem;margin:1rem 0;border-left:4px solid #222}}
table{{width:100%;border-collapse:collapse;background:#fff}}
th,td{{text-align:left;padding:0.5rem;border-bottom:1px solid #ddd;font-size:0.9rem}}
.total{{font-size:1.25rem;font-weight:700}}
</style></head><body>
<h1>DentalMeet Invoice</h1>
<p class="muted">{inv.get('invoice_number')} · {inv['client_email']} · {inv.get('period_label')}</p>
<div class="box">
  <p>{inv['invoice_line']}</p>
  <p class="total">Amount due: ${inv['amount']:,.0f} USD</p>
  <p>Pay via USDT (TRC20): <code>{inv.get('wallet') or '(set TRC20_WALLET_ADDRESS)'}</code></p>
  <p class="muted">Due date: {inv.get('due_date')}</p>
</div>
<p>Qualified {inv['qualified_meetings']} / {inv['total_meetings']}
({inv['disqualified_meetings']} disqualified).</p>
<table>
<thead><tr><th>Date</th><th>Attendee</th><th>OK</th><th>Reason</th></tr></thead>
<tbody>
{"".join(rows) if rows else "<tr><td colspan='4'>No meetings in period</td></tr>"}
</tbody>
</table>
</body></html>
"""
    path.write_text(html, encoding="utf-8")
    return path


def send_invoice(client_email: str, period: str = "month") -> dict:
    """Generate invoice PDF and email the client with TRC20 payment details."""
    from pipeline.email_sender import send_email
    from pipeline.to_sheets import connect

    inv = calculate_invoice(client_email, period=period, write_files=True)
    if float(inv.get("amount") or 0) <= 0:
        print(f"skip send: amount is $0 for {client_email}")
        return {**inv, "sent": False, "reason": "zero_amount"}

    first = "there"
    try:
        gc = connect()
        sh = gc.open(os.environ.get("SHEET_NAME", "Pilot CRM"))
        ws = sh.worksheet("Clients")
        for row in ws.get_all_values()[1:]:
            if row and len(row) > 2 and row[2].strip().lower() == client_email.lower():
                first = (row[1] or "").split()[0] or "there"
                break
    except Exception:
        pass

    # Month/year for subject from period_label
    label = inv.get("period_label") or period
    subject = f"Invoice — {label}"
    wallet = inv.get("wallet") or os.environ.get("TRC20_WALLET_ADDRESS", "")
    body = (
        f"Hi {first},\n"
        f"\n"
        f"Here's your invoice for {label}:\n"
        f"\n"
        f"Invoice #: {inv.get('invoice_number')}\n"
        f"Qualified meetings: {inv.get('qualified_meetings')}\n"
        f"Amount due: ${float(inv.get('amount') or 0):,.0f}\n"
        f"\n"
        f"{inv.get('invoice_line')}\n"
        f"\n"
        f"Payment details:\n"
        f"USDT (TRC20): {wallet}\n"
        f"Due date: {inv.get('due_date')}\n"
        f"\n"
        f"After sending, reply with the TXID so I can confirm automatically.\n"
        f"\n"
        f"Full breakdown is in the HTML invoice on our side "
        f"({Path(inv.get('html_path') or '').name}). "
        f"Reply if you want the PDF attached next time.\n"
        f"\n"
        f"Questions? Just reply here.\n"
        f"\n"
        f"— Vladislav\n"
        f"Founder, DentalMeet\n"
    )
    # Gmail API path is plain text for now — include wallet clearly
    ok = send_email(client_email, subject, body)

    # Mark Payment Status unpaid on Clients
    try:
        from delivery.client_tracker import ensure_tracker_columns, _clients_ws

        ws = _clients_ws()
        cols = ensure_tracker_columns(ws)
        values = ws.get_all_values()
        for i, row in enumerate(values[1:], start=2):
            if len(row) > 2 and row[2].strip().lower() == client_email.lower():
                if "Payment Status" in cols:
                    ws.update_cell(i, cols["Payment Status"], "unpaid")
                if "Total Billed" in cols:
                    ws.update_cell(i, cols["Total Billed"], str(int(inv["amount"])))
                break
    except Exception as e:
        print(f"Clients payment status update skipped: {e}")

    print(f"invoice email {'sent' if ok else 'FAILED'} → {client_email}")
    return {**inv, "sent": ok}


def send_invoices_for_active_clients(period: str = "month") -> dict:
    """Monthly job: invoice every active/completed-unpaid client with meetings."""
    from delivery.weekly_report import active_clients

    sent, skipped = [], []
    # Also include Completed pilots awaiting payment
    clients = list(active_clients())
    try:
        from pipeline.to_sheets import connect

        gc = connect()
        sh = gc.open(os.environ.get("SHEET_NAME", "Pilot CRM"))
        ws = sh.worksheet("Clients")
        headers = [h.strip().lower() for h in ws.row_values(1)]
        email_i = headers.index("email") if "email" in headers else 2
        status_i = headers.index("status") if "status" in headers else None
        pay_i = headers.index("payment status") if "payment status" in headers else None
        for row in ws.get_all_values()[1:]:
            if len(row) <= email_i:
                continue
            email = row[email_i].strip()
            if not email:
                continue
            status = (
                row[status_i].strip().lower()
                if status_i is not None and status_i < len(row)
                else ""
            )
            pay = (
                row[pay_i].strip().lower()
                if pay_i is not None and pay_i < len(row)
                else ""
            )
            if status in ("completed", "active", "onboarded", "running") and pay != "paid":
                if email not in clients:
                    clients.append(email)
    except Exception as e:
        print(f"extra clients scan: {e}")

    for email in clients:
        try:
            result = send_invoice(email, period=period)
            if result.get("sent"):
                sent.append(email)
            else:
                skipped.append({"email": email, "reason": result.get("reason")})
        except Exception as e:
            skipped.append({"email": email, "reason": str(e)})
            print(f"invoice fail {email}: {e}")
    print(f"invoices sent={len(sent)} skipped={len(skipped)}")
    return {"sent": sent, "skipped": skipped}


PILOT_INVOICE_DAYS = 21  # bill each client 3 weeks after Created


def _parse_created(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw[:10] if fmt.startswith("%Y") else raw, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def clients_due_for_pilot_invoice(days: int = PILOT_INVOICE_DAYS) -> list[dict]:
    """Clients whose Created date is >= `days` ago and not yet billed/completed."""
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
    today = date.today()
    due: list[dict] = []

    for raw in values[1:]:
        row = {headers[i]: (raw[i] if i < len(raw) else "") for i in range(len(headers))}
        email = (row.get("Email") or "").strip().lower()
        if not email:
            continue

        pilot_done = (row.get("Pilot Completed") or "").strip().lower()
        if pilot_done in ("yes", "true", "1"):
            continue

        pay = (row.get("Payment Status") or "").strip().lower()
        if pay == "paid":
            continue

        status = (row.get("Status") or "").strip().lower()
        if status in ("needs_info", "invalid", "cancelled", "awaiting_upfront"):
            continue

        created = _parse_created(row.get("Created") or "")
        if created is None:
            continue
        age = (today - created).days
        if age < int(days):
            continue

        due.append(
            {
                "email": email,
                "created": created.isoformat(),
                "days_since_start": age,
                "company": (row.get("Company") or "").strip(),
                "status": status,
            }
        )
    return due


def send_pilot_invoices_due(days: int = PILOT_INVOICE_DAYS) -> dict:
    """
    For each client started >= `days` ago: qualify meetings → send invoice →
    mark pilot completed (via update_client_after_pilot).
    """
    from delivery.client_tracker import update_client_after_pilot

    due = clients_due_for_pilot_invoice(days=days)
    print(f"pilot invoices due ({days}+ days): {len(due)}")
    sent, skipped = [], []

    for item in due:
        email = item["email"]
        try:
            result = update_client_after_pilot(email)
            # Tag the invoice in registry for audit
            inv_no = (result.get("invoice") or {}).get("number")
            if inv_no:
                data = _load_registry()
                for inv in data.get("invoices") or []:
                    if inv.get("invoice_number") == inv_no:
                        inv["pilot_invoice"] = True
                        inv["kind"] = "pilot_3w"
                        inv["days_since_start"] = item["days_since_start"]
                        break
                _save_registry(data)

            sent.append(email)
            print(
                f"pilot invoice → {email} "
                f"(started {item['created']}, day {item['days_since_start']})"
            )
        except Exception as e:
            skipped.append({"email": email, "reason": str(e)})
            print(f"pilot invoice fail {email}: {e}")

    print(f"pilot invoices sent={len(sent)} skipped={len(skipped)}")
    return {"sent": sent, "skipped": skipped, "due": due}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DentalMeet invoicing")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inv = sub.add_parser("invoice", help="Generate invoice HTML+PDF")
    p_inv.add_argument("client_email")
    p_inv.add_argument("--period", default="month", choices=["week", "month"])

    p_send = sub.add_parser("send", help="Generate + email invoice")
    p_send.add_argument("client_email")
    p_send.add_argument("--period", default="month", choices=["week", "month"])

    p_all = sub.add_parser("send_all", help="Email invoices to active clients")
    p_all.add_argument("--period", default="month", choices=["week", "month"])

    p_due = sub.add_parser(
        "pilot_due",
        help="Invoice clients whose pilot started N+ days ago (default 21)",
    )
    p_due.add_argument("--days", type=int, default=PILOT_INVOICE_DAYS)

    # Back-compat: `python -m delivery.billing email --period month`
    args = parser.parse_args(argv)

    if args.cmd == "invoice":
        inv = calculate_invoice(args.client_email, period=args.period)
        print(json.dumps({k: inv[k] for k in (
            "invoice_number", "qualified_meetings", "amount", "due_date", "pdf_path"
        ) if k in inv}, indent=2))
        return 0
    if args.cmd == "send":
        send_invoice(args.client_email, period=args.period)
        return 0
    if args.cmd == "send_all":
        send_invoices_for_active_clients(period=args.period)
        return 0
    if args.cmd == "pilot_due":
        print(json.dumps(send_pilot_invoices_due(days=args.days), indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    # Allow legacy: python -m delivery.billing client@x.com
    if len(sys.argv) > 1 and "@" in sys.argv[1] and not sys.argv[1].startswith("-"):
        calculate_invoice(sys.argv[1], period=(
            "month" if "--period" not in sys.argv else sys.argv[sys.argv.index("--period") + 1]
        ))
        raise SystemExit(0)
    raise SystemExit(main())

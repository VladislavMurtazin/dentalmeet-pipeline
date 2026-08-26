"""Verify USDT TRC20 payments (TronScan) and mark invoices paid."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
INVOICES_DIR = ROOT / "data" / "invoices"
REGISTRY_PATH = INVOICES_DIR / "registry.json"

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def _wallet() -> str:
    return os.environ.get("TRC20_WALLET_ADDRESS", "").strip()


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


def _latest_unpaid_amount(client_email: str) -> float | None:
    email = client_email.strip().lower()
    for inv in reversed(_load_registry().get("invoices") or []):
        if inv.get("client_email", "").lower() == email and inv.get("status") == "unpaid":
            try:
                return float(inv.get("amount") or 0)
            except (TypeError, ValueError):
                return None
    return None


def verify_payment(txid: str, min_amount: float | None = None) -> dict:
    """
    GET TronScan transaction and validate confirmed + amount + recipient.
    """
    txid = (txid or "").strip()
    wallet = _wallet()
    if not txid:
        return {"status": "error", "error": "empty txid"}
    if not wallet:
        return {"status": "error", "error": "TRC20_WALLET_ADDRESS not set"}

    if min_amount is None:
        raw = os.environ.get("PAYMENT_MIN_USDT", "").strip()
        try:
            min_amount = float(raw) if raw else 150.0
        except ValueError:
            min_amount = 150.0

    urls = [
        f"https://apilist.tronscanapi.com/api/transaction-info?hash={txid}",
        f"https://apilist.tronscanapi.com/api/transaction?hash={txid}",
    ]
    data = None
    last_err = None
    for url in urls:
        try:
            r = requests.get(url, timeout=30)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue
            payload = r.json()
            if isinstance(payload, dict) and (
                payload.get("hash") or payload.get("contractData") or payload.get("data")
            ):
                data = payload
                if isinstance(payload.get("data"), list) and payload["data"]:
                    data = payload["data"][0]
                break
            data = payload
            break
        except Exception as e:
            last_err = str(e)

    if not data:
        return {"status": "error", "error": last_err or "no data from TronScan"}

    confirmed = bool(
        data.get("confirmed")
        or data.get("contractRet") == "SUCCESS"
        or data.get("contract_ret") == "SUCCESS"
        or str(data.get("result", "")).lower() in ("success", "true", "1")
    )
    amount = _extract_usdt_amount(data)
    to_addr = _extract_to_address(data)
    wallet_ok = to_addr.lower() == wallet.lower() if to_addr else False
    amount_ok = amount is not None and amount + 1e-6 >= float(min_amount)

    ts = data.get("timestamp") or data.get("block_timestamp") or ""
    if isinstance(ts, (int, float)):
        try:
            ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
        except Exception:
            ts = str(ts)

    status = "confirmed" if (confirmed and wallet_ok and amount_ok) else "rejected"
    return {
        "status": status,
        "amount": amount,
        "timestamp": ts,
        "to": to_addr,
        "wallet_expected": wallet,
        "wallet_match": wallet_ok,
        "amount_ok": amount_ok,
        "min_amount": min_amount,
        "confirmed_on_chain": confirmed,
        "txid": txid,
    }


def _extract_usdt_amount(data: dict) -> float | None:
    from pipeline.payment_tracker import _extract_usdt_amount as _legacy

    return _legacy(data)


def _extract_to_address(data: dict) -> str:
    from pipeline.payment_tracker import _extract_to_address as _legacy

    return _legacy(data)


def mark_invoice_as_paid(
    client_email: str,
    txid: str,
    invoice_date: str = "",
) -> dict:
    """Verify TX against latest unpaid invoice amount, update sheet + registry."""
    from pipeline.email_sender import send_email
    from pipeline.to_sheets import is_email

    from .client_tracker import ensure_tracker_columns, _clients_ws

    email = (client_email or "").strip().lower()
    if not is_email(email):
        return {"ok": False, "error": f"bad email {client_email!r}"}

    expected = _latest_unpaid_amount(email)
    if expected is None:
        # fall back to sheet Total Billed
        try:
            from pipeline.to_sheets import get_all_values_safe

            ws = _clients_ws()
            cols = ensure_tracker_columns(ws)
            for row in get_all_values_safe(ws)[1:]:
                if len(row) > 2 and row[2].strip().lower() == email:
                    raw = row[cols["Total Billed"] - 1] if "Total Billed" in cols else ""
                    expected = float(str(raw).replace("$", "").replace(",", "") or 150)
                    break
        except Exception:
            expected = 150.0
    if expected is None:
        expected = 150.0

    result = verify_payment(txid, min_amount=float(expected) * 0.98)  # small tolerance
    if result.get("status") != "confirmed":
        print(f"payment not confirmed: {result}")
        return {"ok": False, "verify": result}

    from pipeline.to_sheets import get_all_values_safe, update_cell_safe

    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    values = get_all_values_safe(ws)
    row_index = None
    name = ""
    for i, row in enumerate(values[1:], start=2):
        if len(row) > 2 and row[2].strip().lower() == email:
            row_index = i
            name = row[1].strip() if len(row) > 1 else ""
            break
    if row_index is None:
        return {"ok": False, "error": f"client not found: {email}", "verify": result}

    today = date.today().isoformat()
    amount = result.get("amount") or expected
    if "Payment Status" in cols:
        update_cell_safe(ws, row_index, cols["Payment Status"], "paid")
    if "Payment TXID" in cols:
        update_cell_safe(ws, row_index, cols["Payment TXID"], txid)
    if "Payment Date" in cols:
        update_cell_safe(ws, row_index, cols["Payment Date"], today)
    if "Payment Amount" in cols:
        update_cell_safe(ws, row_index, cols["Payment Amount"], str(amount))
    if "Total Paid" in cols:
        update_cell_safe(ws, row_index, cols["Total Paid"], str(amount))
    if "Total Lifetime Value" in cols:
        try:
            prev = float(
                str(values[row_index - 1][cols["Total Lifetime Value"] - 1] or 0)
                .replace("$", "")
                .replace(",", "")
            )
        except Exception:
            prev = 0.0
        update_cell_safe(
            ws, row_index, cols["Total Lifetime Value"], str(int(prev + float(amount)))
        )
    if "Status" in cols:
        # keep Completed for finished pilots; else paid
        cur = values[row_index - 1][cols["Status"] - 1] if cols["Status"] <= len(values[row_index - 1]) else ""
        if (cur or "").lower() not in ("completed", "churned"):
            update_cell_safe(ws, row_index, cols["Status"], "paid")
    if "Upfront Paid" in cols:
        update_cell_safe(ws, row_index, cols["Upfront Paid"], "Yes")

    # Registry
    data = _load_registry()
    for inv in data.get("invoices") or []:
        if inv.get("client_email", "").lower() != email:
            continue
        if inv.get("status") != "unpaid":
            continue
        if invoice_date and inv.get("invoice_date") != invoice_date:
            continue
        inv["status"] = "paid"
        inv["txid"] = txid
        inv["paid_date"] = today
        if not invoice_date:
            break  # mark latest unpaid only
    _save_registry(data)

    first = name.split()[0] if name else "there"
    send_email(
        email,
        "payment confirmed — thank you",
        (
            f"Hi {first},\n\n"
            f"Payment confirmed! Thanks.\n\n"
            f"TXID: {txid}\n"
            f"Amount: {amount} USDT\n\n"
            f"— Vladislav\nDentalMeet\n"
        ),
    )
    print(f"marked paid: {email} txid={txid} amount={amount}")
    return {"ok": True, "row_index": row_index, "verify": result}


def check_unpaid_invoices() -> dict:
    """Remind clients whose unpaid invoices are past due (7+ days)."""
    from pipeline.email_sender import send_email

    today = date.today()
    reminded = []
    data = _load_registry()
    name_cache: dict[str, str] = {}

    try:
        from pipeline.to_sheets import get_all_values_safe, open_sheet_safe, open_worksheet_safe

        sh = open_sheet_safe(os.environ.get("SHEET_NAME", "Pilot CRM"))
        ws = open_worksheet_safe(sh, "Clients")
        for row in get_all_values_safe(ws)[1:]:
            if row and len(row) > 2:
                name_cache[row[2].strip().lower()] = (row[1] or "").split()[0] or "there"
    except Exception:
        pass

    for inv in data.get("invoices") or []:
        if inv.get("status") != "unpaid":
            continue
        if inv.get("reminder_sent"):
            continue
        due_raw = inv.get("due_date") or ""
        try:
            due = date.fromisoformat(due_raw[:10])
        except Exception:
            continue
        if today < due:
            continue

        email = inv.get("client_email") or ""
        first = name_cache.get(email.lower(), "there")
        amount = inv.get("amount")
        number = inv.get("invoice_number")
        body = (
            f"Hi {first},\n\n"
            f"Just a reminder that invoice #{number} for ${amount} is overdue.\n"
            f"Payment details are in the original invoice email "
            f"(USDT TRC20: {_wallet()}).\n\n"
            f"Reply with your TXID once paid.\n\n"
            f"— Vladislav\nDentalMeet\n"
        )
        if send_email(email, f"Reminder — invoice #{number} overdue", body):
            inv["reminder_sent"] = True
            reminded.append(email)
            print(f"overdue reminder → {email} ({number})")

    _save_registry(data)
    print(f"overdue reminders sent: {len(reminded)}")
    return {"reminded": reminded}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="USDT payment verification")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_v = sub.add_parser("verify", help="Verify a TXID on TronScan")
    p_v.add_argument("txid")
    p_v.add_argument("--min", type=float, default=None, help="Minimum USDT expected")

    p_m = sub.add_parser("mark_paid", help="Verify TX and mark client invoice paid")
    p_m.add_argument("client_email")
    p_m.add_argument("txid")
    p_m.add_argument("--invoice-date", default="")

    sub.add_parser("check_overdue", help="Remind overdue unpaid invoices")

    args = parser.parse_args(argv)

    if args.cmd == "verify":
        print(json.dumps(verify_payment(args.txid, min_amount=args.min), indent=2))
        return 0
    if args.cmd == "mark_paid":
        print(
            json.dumps(
                mark_invoice_as_paid(
                    args.client_email, args.txid, invoice_date=args.invoice_date
                ),
                indent=2,
                default=str,
            )
        )
        return 0
    if args.cmd == "check_overdue":
        print(json.dumps(check_unpaid_invoices(), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

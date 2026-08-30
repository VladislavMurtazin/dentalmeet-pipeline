"""Verify USDT TRC20 payments (TronScan), poll incoming txs, reminders."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
INVOICES_DIR = ROOT / "data" / "invoices"
REGISTRY_PATH = INVOICES_DIR / "registry.json"
PROCESSED_TXIDS_PATH = INVOICES_DIR / "processed_txids.json"
PAYMENTS_LOG_PATH = INVOICES_DIR / "payments_log.json"

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
AMOUNT_TOLERANCE = 0.50


def _wallet() -> str:
    return (
        os.environ.get("USDT_WALLET_ADDRESS", "").strip()
        or os.environ.get("USDT_WALLET_TRC20", "").strip()
        or os.environ.get("TRC20_WALLET_ADDRESS", "").strip()
    )


def _load_payments_log() -> dict:
    if not PAYMENTS_LOG_PATH.exists():
        return {"payments": []}
    try:
        data = json.loads(PAYMENTS_LOG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("payments", [])
            return data
    except Exception:
        pass
    return {"payments": []}


def _save_payments_log(data: dict) -> None:
    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    PAYMENTS_LOG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _log_payment(entry: dict) -> None:
    data = _load_payments_log()
    payments = data.setdefault("payments", [])
    txid = (entry.get("txid") or "").strip()
    if txid and any(p.get("txid") == txid for p in payments if isinstance(p, dict)):
        return
    payments.append(entry)
    data["payments"] = payments[-500:]
    _save_payments_log(data)


def _load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"invoices": [], "processed_txids": []}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"invoices": [], "processed_txids": []}
    data.setdefault("invoices", [])
    data.setdefault("processed_txids", [])
    return data


def _save_registry(data: dict) -> None:
    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    existing = _load_registry()
    if not data.get("invoices") and existing.get("invoices"):
        data["invoices"] = existing["invoices"]
    REGISTRY_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _processed_txids() -> set[str]:
    data = _load_registry()
    return set(data.get("processed_txids") or [])


def _mark_txid_processed(txid: str) -> None:
    data = _load_registry()
    txids = set(data.get("processed_txids") or [])
    txids.add(txid.strip())
    data["processed_txids"] = sorted(txids)[-500:]
    _save_registry(data)


def _latest_unpaid_amount(client_email: str) -> float | None:
    email = client_email.strip().lower()
    for inv in reversed(_load_registry().get("invoices") or []):
        if inv.get("client_email", "").lower() == email and inv.get("status") == "unpaid":
            try:
                return float(inv.get("amount") or 0)
            except (TypeError, ValueError):
                return None
    return None


def fetch_incoming_transactions(limit: int = 20) -> list[dict]:
    """Poll TronScan for recent incoming USDT TRC20 transfers."""
    wallet = _wallet()
    if not wallet:
        return []

    txs: list[dict] = []
    seen: set[str] = set()

    # TRC20 transfers (best for USDT)
    try:
        r = requests.get(
            "https://apilist.tronscanapi.com/api/token_trc20/transfers",
            params={
                "relatedAddress": wallet,
                "limit": limit,
                "start": 0,
                "direction": 2,
                "tokens": USDT_CONTRACT,
            },
            timeout=30,
        )
        if r.status_code == 200:
            for item in r.json().get("token_transfers") or r.json().get("data") or []:
                txid = item.get("transaction_id") or item.get("hash") or ""
                if not txid or txid in seen:
                    continue
                to_addr = item.get("to_address") or item.get("toAddress") or ""
                if to_addr.lower() != wallet.lower():
                    continue
                quant = item.get("quant") or item.get("amount") or 0
                decimals = int(item.get("tokenInfo", {}).get("tokenDecimal") or item.get("decimals") or 6)
                try:
                    amount = float(quant) / (10**decimals)
                except (TypeError, ValueError):
                    amount = None
                seen.add(txid)
                txs.append(
                    {
                        "txid": txid,
                        "amount": amount,
                        "timestamp": item.get("block_ts") or item.get("timestamp"),
                        "from": item.get("from_address") or item.get("fromAddress") or "",
                        "to": to_addr,
                        "confirmed": True,
                    }
                )
    except Exception as e:
        print(f"token_trc20/transfers error: {e}")

    # Fallback: transaction list endpoint (user-specified)
    if len(txs) < limit:
        try:
            r = requests.get(
                "https://apilist.tronscanapi.com/api/transaction",
                params={"sort": "-timestamp", "address": wallet, "limit": limit},
                timeout=30,
            )
            if r.status_code == 200:
                payload = r.json()
                items = payload if isinstance(payload, list) else payload.get("data") or []
                for item in items:
                    txid = item.get("hash") or item.get("transactionHash") or ""
                    if not txid or txid in seen:
                        continue
                    amount = _extract_usdt_amount(item)
                    to_addr = _extract_to_address(item)
                    if to_addr and to_addr.lower() != wallet.lower():
                        continue
                    if amount is None or amount <= 0:
                        continue
                    seen.add(txid)
                    txs.append(
                        {
                            "txid": txid,
                            "amount": amount,
                            "timestamp": item.get("timestamp") or item.get("block_timestamp"),
                            "from": item.get("ownerAddress") or "",
                            "to": to_addr or wallet,
                            "confirmed": bool(item.get("confirmed", True)),
                        }
                    )
        except Exception as e:
            print(f"transaction list error: {e}")

    return txs[:limit]


def verify_payment(txid: str, min_amount: float | None = None) -> dict:
    """
    GET TronScan transaction and validate confirmed + amount + recipient.
    """
    txid = (txid or "").strip()
    wallet = _wallet()
    if not txid:
        return {"status": "error", "error": "empty txid"}
    if not wallet:
        return {"status": "error", "error": "USDT_WALLET_TRC20 / TRC20_WALLET_ADDRESS not set"}

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


def _find_unpaid_by_amount(amount: float) -> dict | None:
    """Match incoming amount to a unique unpaid invoice."""
    if amount is None or amount <= 0:
        return None
    matches = []
    for inv in _load_registry().get("invoices") or []:
        if inv.get("status") != "unpaid":
            continue
        try:
            expected = float(inv.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if abs(expected - amount) <= AMOUNT_TOLERANCE:
            matches.append(inv)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        matches.sort(key=lambda x: x.get("due_date") or "")
        return matches[0]
    return None


def _send_alert(subject: str, body: str) -> None:
    try:
        from pipeline.alerts import send_alert

        send_alert(subject, body, severity="info")
        return
    except Exception:
        pass
    alert_to = os.environ.get("ALERT_EMAIL", os.environ.get("GMAIL_EMAIL", "")).strip()
    if not alert_to:
        print(f"ALERT: {subject}\n{body}")
        return
    from pipeline.email_sender import send_email

    send_email(alert_to, subject, body, skip_unsubscribe=True, skip_throttle=True)


def _update_sheet_payment(
    client_email: str,
    payment_status: str,
    txid: str = "",
    amount: float | None = None,
) -> int | None:
    from pipeline.to_sheets import get_all_values_safe, update_cell_safe

    from .client_tracker import ensure_tracker_columns, _clients_ws

    email = client_email.strip().lower()
    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    values = get_all_values_safe(ws)
    row_index = None
    for i, row in enumerate(values[1:], start=2):
        if len(row) > 2 and row[2].strip().lower() == email:
            row_index = i
            break
    if row_index is None:
        return None
    today = date.today().isoformat()
    if "Payment Status" in cols:
        update_cell_safe(ws, row_index, cols["Payment Status"], payment_status)
    if txid and "Payment TXID" in cols:
        update_cell_safe(ws, row_index, cols["Payment TXID"], txid)
    if "Payment Date" in cols:
        update_cell_safe(ws, row_index, cols["Payment Date"], today)
    if amount is not None and "Payment Amount" in cols:
        update_cell_safe(ws, row_index, cols["Payment Amount"], str(amount))
    if payment_status == "paid" and "Status" in cols:
        cur = values[row_index - 1][cols["Status"] - 1] if cols["Status"] <= len(values[row_index - 1]) else ""
        if (cur or "").lower() not in ("completed", "churned"):
            update_cell_safe(ws, row_index, cols["Status"], "paid")
    return row_index


def _pause_client_campaign(client_email: str) -> None:
    from pipeline.to_sheets import get_all_values_safe, update_cell_safe

    from .client_tracker import _clients_ws, ensure_tracker_columns

    email = client_email.strip().lower()
    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    for i, row in enumerate(get_all_values_safe(ws)[1:], start=2):
        if len(row) > 2 and row[2].strip().lower() == email:
            if "Status" in cols:
                update_cell_safe(ws, i, cols["Status"], "payment_paused")
            break


def mark_invoice_as_paid(
    client_email: str,
    txid: str,
    invoice_date: str = "",
    *,
    amount_override: float | None = None,
    skip_verify: bool = False,
) -> dict:
    """Verify TX against latest unpaid invoice amount, update sheet + registry."""
    from pipeline.email_sender import send_email
    from pipeline.to_sheets import get_all_values_safe, is_email, update_cell_safe

    from .client_tracker import ensure_tracker_columns, _clients_ws

    email = (client_email or "").strip().lower()
    if not is_email(email):
        return {"ok": False, "error": f"bad email {client_email!r}"}

    expected = _latest_unpaid_amount(email)
    if expected is None:
        try:
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

    if skip_verify and amount_override is not None:
        result = {
            "status": "confirmed",
            "amount": amount_override,
            "txid": txid,
            "simulated": True,
        }
    else:
        result = verify_payment(txid, min_amount=float(expected) * 0.98)
        if result.get("status") != "confirmed":
            print(f"payment not confirmed: {result}")
            return {"ok": False, "verify": result}

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
    amount = amount_override if amount_override is not None else (result.get("amount") or expected)
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
        cur = values[row_index - 1][cols["Status"] - 1] if cols["Status"] <= len(values[row_index - 1]) else ""
        if (cur or "").lower() not in ("completed", "churned"):
            update_cell_safe(ws, row_index, cols["Status"], "paid")
    if "Upfront Paid" in cols:
        update_cell_safe(ws, row_index, cols["Upfront Paid"], "Yes")

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
            break
    if txid:
        txids = set(data.get("processed_txids") or [])
        txids.add(txid)
        data["processed_txids"] = sorted(txids)[-500:]
    _save_registry(data)

    first = name.split()[0] if name else "there"
    send_email(
        email,
        "Payment received, thank you!",
        (
            f"Hi {first},\n\n"
            f"Payment received, thank you!\n\n"
            f"TXID: {txid}\n"
            f"Amount: {amount} USDT\n\n"
            f"— Vladislav\nDentalMeet\n"
        ),
    )
    print(f"marked paid: {email} txid={txid} amount={amount}")
    return {"ok": True, "row_index": row_index, "verify": result}


def match_incoming_payment(tx: dict) -> dict:
    """Match one incoming tx to client by unique invoice amount."""
    txid = (tx.get("txid") or "").strip()
    amount = tx.get("amount")
    if not txid or txid in _processed_txids():
        return {"matched": False, "reason": "already_processed_or_no_txid"}

    try:
        amount_f = float(amount)
    except (TypeError, ValueError):
        return {"matched": False, "reason": "bad_amount"}

    inv = _find_unpaid_by_amount(amount_f)
    if not inv:
        _mark_txid_processed(txid)
        _log_payment(
            {
                "txid": txid,
                "amount": amount_f,
                "from": tx.get("from") or "",
                "timestamp": tx.get("timestamp"),
                "status": "unknown",
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        _send_alert(
            f"Unknown payment of ${amount_f:,.2f} received",
            (
                f"Incoming USDT did not match any unpaid invoice.\n\n"
                f"Amount: {amount_f} USDT\n"
                f"From: {tx.get('from') or 'n/a'}\n"
                f"TXID: {txid}\n"
                f"Verify on TronScan and match manually if needed."
            ),
        )
        return {
            "matched": False,
            "reason": "no_invoice_for_amount",
            "amount": amount_f,
            "txid": txid,
        }

    expected = float(inv.get("amount") or 0)
    email = inv.get("client_email") or ""
    number = inv.get("invoice_number") or ""

    if amount_f < expected - AMOUNT_TOLERANCE:
        _update_sheet_payment(email, "partial", txid=txid, amount=amount_f)
        inv["partial_amount"] = amount_f
        inv["partial_txid"] = txid
        data = _load_registry()
        _save_registry(data)
        from pipeline.email_sender import send_email

        send_email(
            email,
            f"Partial payment received — invoice #{number}",
            (
                f"Hi,\n\nWe received {amount_f} USDT but invoice #{number} is for ${expected}.\n"
                f"Please send the remaining ${expected - amount_f:.2f} USDT.\n\n"
                f"TXID: {txid}\nWallet: {_wallet()}\n\n— DentalMeet\n"
            ),
        )
        _mark_txid_processed(txid)
        _log_payment(
            {
                "txid": txid,
                "amount": amount_f,
                "client_email": email,
                "invoice_number": number,
                "status": "partial",
                "from": tx.get("from") or "",
                "timestamp": tx.get("timestamp"),
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        return {"matched": True, "status": "partial", "email": email, "amount": amount_f}

    if amount_f > expected + AMOUNT_TOLERANCE:
        _send_alert(
            f"Overpayment: invoice #{number} ({email})",
            f"Expected ${expected}, received {amount_f} USDT.\nTXID: {txid}\nManual review required.",
        )
        _mark_txid_processed(txid)
        _log_payment(
            {
                "txid": txid,
                "amount": amount_f,
                "client_email": email,
                "invoice_number": number,
                "status": "overpayment",
                "from": tx.get("from") or "",
                "timestamp": tx.get("timestamp"),
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        return {"matched": True, "status": "overpayment", "email": email, "amount": amount_f}

    result = mark_invoice_as_paid(
        email,
        txid,
        invoice_date=inv.get("invoice_date") or "",
        amount_override=amount_f,
        skip_verify=True,
    )
    _log_payment(
        {
            "txid": txid,
            "amount": amount_f,
            "client_email": email,
            "invoice_number": number,
            "status": "paid",
            "from": tx.get("from") or "",
            "timestamp": tx.get("timestamp"),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    _send_alert(
        f"Payment received: ${amount_f:,.0f} from {email}",
        (
            f"Matched invoice #{number}.\n"
            f"Amount: {amount_f} USDT\n"
            f"Client: {email}\n"
            f"TXID: {txid}\n"
        ),
    )
    return {"matched": True, "status": "paid", "email": email, "result": result}


def poll_incoming_payments(limit: int = 20) -> dict:
    """Fetch recent incoming txs and auto-match to unpaid invoices."""
    txs = fetch_incoming_transactions(limit=limit)
    results = []
    unknown = []
    for tx in txs:
        if not tx.get("confirmed", True):
            continue
        r = match_incoming_payment(tx)
        if r.get("matched"):
            results.append(r)
            print(f"payment match: {r}")
        elif r.get("reason") == "no_invoice_for_amount":
            unknown.append(r)
            print(f"unknown payment: {r}")
    return {"checked": len(txs), "matched": results, "unknown": unknown}


def payment_report() -> dict:
    """Summary of pending invoices + received payments for CLI/CRM."""
    registry = _load_registry()
    log = _load_payments_log()
    today = date.today()
    month_start = today.replace(day=1).isoformat()

    pending = [
        inv
        for inv in (registry.get("invoices") or [])
        if (inv.get("status") or "").lower() == "unpaid"
    ]
    received = [
        p
        for p in (log.get("payments") or [])
        if isinstance(p, dict) and (p.get("status") or "") in ("paid", "partial", "overpayment")
    ]
    # Also include paid invoices from registry if log is empty
    paid_inv = [
        {
            "txid": inv.get("txid") or "",
            "amount": inv.get("amount"),
            "client_email": inv.get("client_email"),
            "invoice_number": inv.get("invoice_number"),
            "status": "paid",
            "timestamp": inv.get("paid_date") or inv.get("invoice_date"),
        }
        for inv in (registry.get("invoices") or [])
        if (inv.get("status") or "").lower() == "paid"
    ]
    if not received and paid_inv:
        received = paid_inv

    def _amt(x) -> float:
        try:
            return float(x.get("amount") or 0)
        except (TypeError, ValueError):
            return 0.0

    month_rev = 0.0
    all_time = 0.0
    for p in received:
        a = _amt(p)
        all_time += a
        ts = str(p.get("timestamp") or p.get("recorded_at") or p.get("paid_date") or "")
        if ts[:10] >= month_start:
            month_rev += a
    for inv in paid_inv:
        # avoid double-count if already in received with same txid
        pass
    # Prefer registry paid for all-time if log incomplete
    reg_all = sum(
        _amt(inv)
        for inv in (registry.get("invoices") or [])
        if (inv.get("status") or "").lower() == "paid"
    )
    reg_month = 0.0
    for inv in registry.get("invoices") or []:
        if (inv.get("status") or "").lower() != "paid":
            continue
        paid_raw = str(inv.get("paid_date") or inv.get("invoice_date") or "")
        if paid_raw[:10] >= month_start:
            reg_month += _amt(inv)

    return {
        "wallet": _wallet() or None,
        "pending_invoices": pending,
        "pending_total": round(sum(_amt(i) for i in pending), 2),
        "received_payments": received[-50:],
        "revenue_this_month": round(max(month_rev, reg_month), 2),
        "revenue_all_time": round(max(all_time, reg_all), 2),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def print_payment_report() -> None:
    data = payment_report()
    print("=== USDT PAYMENT REPORT ===")
    print(f"Wallet: {data.get('wallet') or '(not set)'}")
    print(f"Pending invoices: {len(data.get('pending_invoices') or [])} "
          f"(${data.get('pending_total', 0):,.2f})")
    print(f"Revenue this month: ${data.get('revenue_this_month', 0):,.2f}")
    print(f"Revenue all time:   ${data.get('revenue_all_time', 0):,.2f}")
    print("Recent payments:")
    payments = data.get("received_payments") or []
    if not payments:
        print("  (none yet)")
    for p in payments[-20:]:
        print(
            f"  {p.get('status', '?'):10} ${float(p.get('amount') or 0):,.2f}  "
            f"{p.get('client_email') or 'unknown'}  "
            f"txid={(p.get('txid') or '')[:16]}…"
        )
    print("===========================")


def simulate_payment_match(amount: float, client_email: str | None = None) -> dict:
    """Test amount matching without on-chain TX (dry-run friendly)."""
    txid = f"SIM-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    tx = {"txid": txid, "amount": float(amount), "confirmed": True, "simulated": True}

    if client_email:
        inv_amount = _latest_unpaid_amount(client_email)
        if inv_amount is None:
            return {"ok": False, "error": f"no unpaid invoice for {client_email}"}
        tx["amount"] = float(amount)
        return match_incoming_payment(tx)

    return match_incoming_payment(tx)


def payment_status_all() -> list[dict]:
    """All clients with payment status from sheet + registry."""
    from pipeline.to_sheets import get_all_values_safe

    from .client_tracker import ensure_tracker_columns, _clients_ws

    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    rows = []
    unpaid_by_email: dict[str, list] = {}
    for inv in _load_registry().get("invoices") or []:
        email = (inv.get("client_email") or "").lower()
        unpaid_by_email.setdefault(email, []).append(inv)

    for row in get_all_values_safe(ws)[1:]:
        if len(row) < 3:
            continue
        email = row[2].strip().lower()
        if not email:
            continue
        name = row[1].strip() if len(row) > 1 else ""
        payment_status = (
            row[cols["Payment Status"] - 1] if "Payment Status" in cols and len(row) >= cols["Payment Status"] else ""
        )
        status = row[cols["Status"] - 1] if "Status" in cols and len(row) >= cols["Status"] else ""
        unpaid = [i for i in unpaid_by_email.get(email, []) if i.get("status") == "unpaid"]
        rows.append(
            {
                "name": name,
                "email": email,
                "status": (status or "").strip(),
                "payment_status": (payment_status or "").strip() or ("unpaid" if unpaid else "—"),
                "unpaid_invoices": len(unpaid),
                "unpaid_total": round(sum(float(i.get("amount") or 0) for i in unpaid), 2),
                "latest_due": unpaid[0].get("due_date") if unpaid else "",
            }
        )
    return rows


def run_payment_reminders() -> dict:
    """
    Reminder schedule:
    - 3 days before due
    - on due date
    - 3 days overdue
    - 7 days overdue: pause campaign + alert
    """
    from pipeline.email_sender import send_email

    today = date.today()
    sent: dict[str, list] = {"reminder_3d": [], "due_today": [], "overdue_3d": [], "overdue_7d": []}
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
        due_raw = inv.get("due_date") or ""
        try:
            due = date.fromisoformat(due_raw[:10])
        except Exception:
            continue

        email = inv.get("client_email") or ""
        first = name_cache.get(email.lower(), "there")
        amount = inv.get("amount")
        number = inv.get("invoice_number")
        wallet = _wallet()
        days = (today - due).days

        def _send(key: str, flag: str, subject: str, body: str) -> None:
            if inv.get(flag):
                return
            if send_email(email, subject, body):
                inv[flag] = True
                sent[key].append(email)
                print(f"{key} → {email} (#{number})")

        if due - today == timedelta(days=3):
            _send(
                "reminder_3d",
                "reminder_3d_sent",
                f"Invoice #{number} due in 3 days",
                (
                    f"Hi {first},\n\nInvoice #{number} for ${amount} is due in 3 days ({due}).\n"
                    f"Pay via USDT TRC20: {wallet}\n\n— DentalMeet\n"
                ),
            )
        elif due == today:
            _send(
                "due_today",
                "reminder_due_sent",
                f"Invoice #{number} due today",
                (
                    f"Hi {first},\n\nInvoice #{number} for ${amount} is due today.\n"
                    f"USDT TRC20: {wallet}\n\n— DentalMeet\n"
                ),
            )
        elif days == 3:
            _send(
                "overdue_3d",
                "reminder_3over_sent",
                f"Invoice #{number} overdue — please pay",
                (
                    f"Hi {first},\n\nInvoice #{number} for ${amount} is 3 days overdue.\n"
                    f"Please pay via USDT TRC20: {wallet}\n\n— DentalMeet\n"
                ),
            )
        elif days >= 7:
            if not inv.get("paused_at_7over"):
                _pause_client_campaign(email)
                inv["paused_at_7over"] = True
                sent["overdue_7d"].append(email)
                _send_alert(
                    f"Campaign paused — 7+ days overdue ({email})",
                    f"Invoice #{number} (${amount}) overdue since {due}. Campaign paused.",
                )
                _send(
                    "overdue_7d",
                    "reminder_7over_sent",
                    f"Invoice #{number} — campaign paused",
                    (
                        f"Hi {first},\n\nInvoice #{number} is 7+ days overdue. "
                        f"Your campaign is paused until payment is received.\n"
                        f"USDT TRC20: {wallet}\n\n— DentalMeet\n"
                    ),
                )

    _save_registry(data)
    total = sum(len(v) for v in sent.values())
    print(f"payment reminders sent: {total}")
    return sent


def check_unpaid_invoices() -> dict:
    """Legacy wrapper — runs full reminder schedule."""
    return run_payment_reminders()


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

    sub.add_parser("check_overdue", help="Run payment reminder schedule")
    sub.add_parser("poll", help="Poll wallet for incoming USDT and auto-match")
    p_poll = sub.add_parser("poll_incoming", help="Alias for poll")
    p_poll.add_argument("--limit", type=int, default=20)
    p_check = sub.add_parser("check", help="Poll for new USDT payments (alias of poll)")
    p_check.add_argument("--limit", type=int, default=20)

    p_sim = sub.add_parser("simulate", help="Simulate incoming USDT tx for testing")
    p_sim.add_argument("amount", type=float)
    p_sim.add_argument("--email", default="", help="Optional client email hint")

    sub.add_parser("payment-status", help="Show all clients payment status")
    sub.add_parser("report", help="Show pending invoices + payment history")
    sub.add_parser("reminders", help="Run full reminder schedule")

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
    if args.cmd in ("check_overdue", "reminders"):
        print(json.dumps(run_payment_reminders(), indent=2))
        return 0
    if args.cmd in ("poll", "poll_incoming", "check"):
        limit = getattr(args, "limit", 20)
        print(json.dumps(poll_incoming_payments(limit=limit), indent=2, default=str))
        return 0
    if args.cmd == "simulate":
        print(
            json.dumps(
                simulate_payment_match(args.amount, client_email=args.email or None),
                indent=2,
                default=str,
            )
        )
        return 0
    if args.cmd == "payment-status":
        print(json.dumps(payment_status_all(), indent=2))
        return 0
    if args.cmd == "report":
        print_payment_report()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

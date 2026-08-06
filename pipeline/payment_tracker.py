"""Verify USDT (TRC20) payments via TronScan and mark Clients as paid."""

from __future__ import annotations

import os
from datetime import date, datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

from .email_sender import send_email
from .to_sheets import connect, is_email

# USDT TRC20 contract on Tron
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
DEFAULT_MIN_USDT = 700.0

PAYMENT_HEADERS = [
    "Payment Status",
    "Payment TXID",
    "Payment Date",
    "Payment Amount",
]


def _wallet() -> str:
    return os.environ.get("TRC20_WALLET_ADDRESS", "").strip()


def _min_amount() -> float:
    raw = os.environ.get("PAYMENT_MIN_USDT", "").strip()
    try:
        return float(raw) if raw else DEFAULT_MIN_USDT
    except ValueError:
        return DEFAULT_MIN_USDT


def verify_payment(txid: str) -> dict:
    """Fetch TX from TronScan and validate amount + recipient wallet."""
    txid = (txid or "").strip()
    wallet = _wallet()
    if not txid:
        return {"status": "error", "error": "empty txid"}
    if not wallet:
        return {"status": "error", "error": "TRC20_WALLET_ADDRESS not set"}

    # Primary: transaction-info (richer payload)
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
                break
            # list wrapper
            if isinstance(payload, dict) and payload.get("data"):
                data = payload["data"][0] if isinstance(payload["data"], list) else payload
                break
            data = payload
            break
        except Exception as e:
            last_err = str(e)

    if not data:
        return {"status": "error", "error": last_err or "no data from TronScan"}

    # Confirmed?
    confirmed = bool(
        data.get("confirmed")
        or data.get("contractRet") == "SUCCESS"
        or str(data.get("result", "")).lower() in ("success", "true", "1")
    )
    # Some payloads nest under contract_ret
    if data.get("contract_ret") == "SUCCESS":
        confirmed = True

    amount = _extract_usdt_amount(data)
    to_addr = _extract_to_address(data)
    wallet_ok = to_addr.lower() == wallet.lower() if to_addr else False
    amount_ok = amount is not None and amount + 1e-6 >= _min_amount()

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
        "confirmed_on_chain": confirmed,
        "txid": txid,
        "raw_keys": list(data.keys())[:20],
    }


def _extract_usdt_amount(data: dict) -> float | None:
    """Best-effort USDT amount from TronScan payload (sun / 1e6)."""
    # trc20TransferInfo list
    transfers = data.get("trc20TransferInfo") or data.get("trc20_transfer_info") or []
    if isinstance(transfers, list):
        for t in transfers:
            contract = (
                t.get("contract_address")
                or t.get("contractAddress")
                or t.get("token_info", {}).get("address")
                or ""
            )
            if contract and contract != USDT_CONTRACT:
                # still accept if symbol is USDT
                sym = (
                    t.get("symbol")
                    or t.get("token_info", {}).get("symbol")
                    or ""
                ).upper()
                if sym and sym != "USDT":
                    continue
            quant = t.get("amount_str") or t.get("amount") or t.get("quant")
            decimals = int(
                t.get("decimals")
                or t.get("token_info", {}).get("decimals")
                or 6
            )
            try:
                q = float(quant)
                if q > 1_000_000:  # likely in sun/base units
                    return q / (10**decimals)
                # amount_str sometimes already human
                if decimals == 6 and q >= 1:
                    # Heuristic: values like 700000000 => 700 USDT
                    if q >= 10_000:
                        return q / (10**decimals)
                    return q
            except (TypeError, ValueError):
                continue

    # contractData
    cd = data.get("contractData") or {}
    if isinstance(cd, dict):
        for key in ("amount", "quant"):
            if key in cd:
                try:
                    q = float(cd[key])
                    return q / 1_000_000 if q > 10_000 else q
                except (TypeError, ValueError):
                    pass
    return None


def _extract_to_address(data: dict) -> str:
    transfers = data.get("trc20TransferInfo") or data.get("trc20_transfer_info") or []
    if isinstance(transfers, list) and transfers:
        t = transfers[0]
        return (
            t.get("to_address")
            or t.get("toAddress")
            or t.get("to")
            or ""
        )
    cd = data.get("contractData") or {}
    if isinstance(cd, dict):
        return cd.get("to_address") or cd.get("toAddress") or cd.get("to") or ""
    return data.get("toAddress") or data.get("to_address") or ""


def _clients_ws():
    import gspread

    gc = connect()
    sheet_name = os.environ.get("SHEET_NAME", "Pilot CRM")
    sh = gc.open(sheet_name)
    try:
        return sh.worksheet("Clients")
    except gspread.WorksheetNotFound as e:
        raise RuntimeError("Clients worksheet missing — run check_forms first") from e


def _ensure_payment_columns(ws) -> dict[str, int]:
    """Ensure payment columns exist; return header→1-based col index."""
    headers = ws.row_values(1)
    header_map = {h.strip(): i + 1 for i, h in enumerate(headers) if h.strip()}
    for name in PAYMENT_HEADERS:
        if name not in header_map:
            col = len(headers) + 1
            ws.update_cell(1, col, name)
            headers.append(name)
            header_map[name] = col
    return header_map


def mark_client_as_paid(client_email: str, txid: str) -> dict:
    """Verify TX then update Clients + send confirmation email."""
    email = (client_email or "").strip().lower()
    if not is_email(email):
        return {"ok": False, "error": f"bad email {client_email!r}"}

    result = verify_payment(txid)
    if result.get("status") != "confirmed":
        print(f"payment not confirmed: {result}")
        return {"ok": False, "verify": result}

    ws = _clients_ws()
    cols = _ensure_payment_columns(ws)
    values = ws.get_all_values()
    headers = values[0] if values else []
    email_col = None
    name_col = None
    status_col = None
    for i, h in enumerate(headers):
        hl = h.strip().lower()
        if hl == "email":
            email_col = i
        if hl == "name":
            name_col = i
        if hl == "status":
            status_col = i

    if email_col is None:
        return {"ok": False, "error": "Clients sheet has no Email column"}

    row_index = None
    name = ""
    for i, row in enumerate(values[1:], start=2):
        if len(row) > email_col and row[email_col].strip().lower() == email:
            row_index = i
            if name_col is not None and len(row) > name_col:
                name = row[name_col].strip()
            break

    if row_index is None:
        return {"ok": False, "error": f"client not found: {email}", "verify": result}

    today = date.today().isoformat()
    amount = result.get("amount") or ""
    ws.update_cell(row_index, cols["Payment Status"], "paid")
    ws.update_cell(row_index, cols["Payment TXID"], txid)
    ws.update_cell(row_index, cols["Payment Date"], today)
    ws.update_cell(row_index, cols["Payment Amount"], str(amount))
    if status_col is not None:
        ws.update_cell(row_index, status_col + 1, "paid")

    first = name.split()[0] if name else "there"
    subject = "payment confirmed — launching soon"
    body = (
        f"Hi {first},\n"
        f"\n"
        f"Payment confirmed! Your campaign launches within 24 hours.\n"
        f"\n"
        f"TXID: {txid}\n"
        f"\n"
        f"— Vladislav\n"
        f"DentalMeet\n"
    )
    send_email(email, subject, body)
    print(f"marked paid: {email} txid={txid} amount={amount}")
    return {"ok": True, "row_index": row_index, "verify": result}

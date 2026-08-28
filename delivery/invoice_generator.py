"""Generate PDF invoices with USDT wallet + QR code and email on due dates."""

from __future__ import annotations

import io
import os
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
INVOICES_DIR = ROOT / "data" / "invoices"


def _wallet() -> str:
    return (
        os.environ.get("USDT_WALLET_TRC20", "").strip()
        or os.environ.get("TRC20_WALLET_ADDRESS", "").strip()
    )


def _qr_url(data: str, size: int = 180) -> str:
    from urllib.parse import quote

    return (
        f"https://api.qrserver.com/v1/create-qr-code/?size={size}x{size}&data="
        f"{quote(data)}"
    )


def generate_invoice_pdf_with_qr(
    client_email: str,
    period: str = "month",
    inv: dict | None = None,
) -> Path | None:
    """PDF invoice with embedded USDT wallet QR (TRC20)."""
    from .billing import calculate_invoice, generate_invoice_pdf

    if inv is None:
        inv = calculate_invoice(client_email, period=period, write_files=False)

    wallet = _wallet() or inv.get("wallet") or ""
    inv["wallet"] = wallet

    base = generate_invoice_pdf(client_email, period=period, inv=inv)
    if not base or not wallet:
        return base

    try:
        from fpdf import FPDF
    except ImportError:
        return base

    qr_resp = requests.get(_qr_url(wallet), timeout=20)
    if qr_resp.status_code != 200:
        return base

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "USDT Payment QR (TRC20)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    pdf.multi_cell(0, 6, f"Send exactly ${float(inv.get('amount') or 0):,.2f} USDT to:\n{wallet}")
    img = io.BytesIO(qr_resp.content)
    pdf.image(img, x=pdf.l_margin, y=pdf.get_y() + 2, w=50)
    pdf.ln(55)
    pdf.set_font("Helvetica", size=9)
    pdf.multi_cell(
        0,
        5,
        "Scan with your wallet app. Network: TRC20 only. Reply with TXID after payment.",
    )
    qr_path = INVOICES_DIR / f"{base.stem}_qr.pdf"
    pdf.output(str(qr_path))
    return qr_path


def send_invoice_email(client_email: str, period: str = "month") -> dict:
    """Generate invoice (PDF + QR) and email the client."""
    from .billing import calculate_invoice, send_invoice

    inv = calculate_invoice(client_email, period=period, write_files=True)
    qr_pdf = generate_invoice_pdf_with_qr(client_email, period=period, inv=inv)
    result = send_invoice(client_email, period=period)
    if qr_pdf:
        result["qr_pdf_path"] = str(qr_pdf)
    return result


def send_due_invoices(days_before: int = 0) -> dict:
    """Email invoices due in N days (0 = due today)."""
    import json

    from pipeline.email_sender import send_email

    registry_path = INVOICES_DIR / "registry.json"
    if not registry_path.exists():
        return {"sent": []}
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    today = date.today()
    target = today + timedelta(days=days_before)
    sent: list[str] = []

    for inv in data.get("invoices") or []:
        if inv.get("status") != "unpaid":
            continue
        due_raw = inv.get("due_date") or ""
        try:
            due = date.fromisoformat(due_raw[:10])
        except ValueError:
            continue
        if due != target:
            continue
        email = inv.get("client_email") or ""
        if not email:
            continue
        send_invoice_email(email, period=inv.get("period") or "month")
        sent.append(email)

    return {"sent": sent, "target_date": target.isoformat()}

"""Daily backup of Google Sheets to local CSV files."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "data" / "backups"
RETENTION_DAYS = 30

FILENAME_RE = re.compile(r"^(agencies|clients|forms)_(\d{4}-\d{2}-\d{2})\.csv$", re.I)

PREFIX_TO_WORKSHEET = {
    "agencies": "Agencies",
    "clients": "Clients",
}


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _backup_path(prefix: str, day: date | None = None) -> Path:
    d = day or _utc_today()
    return BACKUP_DIR / f"{prefix}_{d.isoformat()}.csv"


def _write_csv(path: Path, rows: list[list[str]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)
    return max(0, len(rows) - 1)


def _read_csv(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def _fetch_main_worksheet(name: str):
    from .to_sheets import get_all_values_safe, open_sheet_safe, open_worksheet_safe

    sheet_name = os.environ.get("SHEET_NAME", "Pilot CRM")
    sh = open_sheet_safe(sheet_name)
    ws = open_worksheet_safe(sh, name)
    return get_all_values_safe(ws)


def _fetch_form_responses():
    from .async_onboarding import _form_responses_ws
    from .to_sheets import get_all_values_safe

    ws = _form_responses_ws()
    return get_all_values_safe(ws)


def _fetch_sheet_rows(prefix: str) -> list[list[str]]:
    if prefix == "forms":
        return _fetch_form_responses()
    worksheet = PREFIX_TO_WORKSHEET[prefix]
    return _fetch_main_worksheet(worksheet)


def prune_old_backups(retention_days: int = RETENTION_DAYS) -> list[str]:
    """Delete backup files older than retention_days. Returns deleted filenames."""
    if not BACKUP_DIR.exists():
        return []

    cutoff = _utc_today() - timedelta(days=retention_days)
    deleted: list[str] = []
    for path in sorted(BACKUP_DIR.glob("*.csv")):
        match = FILENAME_RE.match(path.name)
        if not match:
            continue
        try:
            file_day = date.fromisoformat(match.group(2))
        except ValueError:
            continue
        if file_day < cutoff:
            path.unlink(missing_ok=True)
            deleted.append(path.name)
    return deleted


def run_backup(day: date | None = None) -> dict:
    """Download all sheets as dated CSV files under data/backups/."""
    day = day or _utc_today()
    results: dict[str, dict] = {}
    errors: list[str] = []

    for prefix in ("agencies", "clients", "forms"):
        path = _backup_path(prefix, day)
        try:
            rows = _fetch_sheet_rows(prefix)
            data_rows = _write_csv(path, rows)
            results[prefix] = {"path": str(path), "rows": data_rows}
            print(f"backup {prefix}: {data_rows} rows -> {path.name}", flush=True)
        except Exception as e:
            errors.append(f"{prefix}: {e}")
            print(f"backup {prefix} FAILED: {e}")

    deleted = prune_old_backups()
    if deleted:
        print(f"pruned {len(deleted)} old backup(s): {', '.join(deleted)}")

    if errors:
        raise RuntimeError("Backup failed:\n" + "\n".join(errors))

    return {"date": day.isoformat(), "backups": results, "pruned": deleted}


def _resolve_backup_file(file_arg: str) -> Path:
    path = Path(file_arg)
    if not path.is_absolute():
        path = BACKUP_DIR / path.name
    if not path.exists():
        raise FileNotFoundError(f"Backup file not found: {path}")
    match = FILENAME_RE.match(path.name)
    if not match:
        raise ValueError(
            f"Invalid backup filename {path.name!r}; "
            "expected agencies_YYYY-MM-DD.csv, clients_..., or forms_..."
        )
    return path


def _col_letter(n: int) -> str:
    """1-based column index -> Excel letter (1=A)."""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def restore_backup(file_arg: str) -> dict:
    """Restore a sheet from a local CSV backup (overwrites the target worksheet)."""
    from .async_onboarding import _form_responses_ws
    from .to_sheets import open_sheet_safe, open_worksheet_safe, retry_sheets_call, sheets_update_safe

    path = _resolve_backup_file(file_arg)
    match = FILENAME_RE.match(path.name)
    assert match is not None
    prefix = match.group(1).lower()
    backup_day = match.group(2)

    rows = _read_csv(path)
    if not rows:
        raise ValueError(f"Backup file is empty: {path}")

    n_cols = max(len(r) for r in rows)
    n_rows = len(rows)

    if prefix == "forms":
        ws = _form_responses_ws()
        target = f"form responses ({os.environ.get('FORM_RESPONSES_WORKSHEET', 'Form Responses 1')})"
    else:
        sheet_name = os.environ.get("SHEET_NAME", "Pilot CRM")
        sh = open_sheet_safe(sheet_name)
        ws = open_worksheet_safe(sh, PREFIX_TO_WORKSHEET[prefix])
        target = f"{PREFIX_TO_WORKSHEET[prefix]} in {sheet_name!r}"

    print(f"WARNING: restoring {target} from {path.name} ({n_rows} rows, {n_cols} cols)")

    retry_sheets_call(ws.clear)
    retry_sheets_call(ws.resize, rows=max(n_rows + 10, 100), cols=max(n_cols, 1))

    end_col = _col_letter(n_cols)
    sheets_update_safe(
        ws,
        range_name=f"A1:{end_col}{n_rows}",
        values=rows,
        value_input_option="USER_ENTERED",
    )

    print(f"restore complete: {path.name} -> {target}")
    return {"file": path.name, "prefix": prefix, "backup_date": backup_day, "rows": n_rows}


def _handle_failure(exc: BaseException) -> None:
    from .alerts import send_alert

    body = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    send_alert("Backup failed", body, severity="critical")
    print(body, file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backup / restore Google Sheets as CSV")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="Run daily backup (default)")

    p_restore = sub.add_parser("restore", help="Restore a sheet from a CSV backup")
    p_restore.add_argument("--file", required=True, help="e.g. agencies_2026-09-01.csv")

    args = parser.parse_args(argv)
    command = args.command or "run"

    try:
        if command == "restore":
            restore_backup(args.file)
        else:
            run_backup()
    except Exception as exc:
        if command != "restore":
            _handle_failure(exc)
        else:
            print(f"restore failed: {exc}", file=sys.stderr)
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

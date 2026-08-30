"""Automated analytics — daily metrics, trends, dashboard export, email reports."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
ANALYTICS_PATH = ROOT / "data" / "analytics.json"
LEAD_PUBLIC = ROOT.parent.parent / "lead" / "public"
if not LEAD_PUBLIC.parent.exists():
    LEAD_PUBLIC = ROOT.parent / "lead" / "public"

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://dentalmeet.agency/dashboard").strip()
RETENTION_DAYS = 90
TEMPLATE_RE = re.compile(r"tpl=([AD]\d+)", re.I)

TZ_TO_REGION: dict[str, str] = {
    "America/Los_Angeles": "Pacific",
    "America/Denver": "Mountain",
    "America/Chicago": "Central",
    "America/New_York": "Eastern",
    "Europe/London": "UK",
    "Australia/Sydney": "AU",
    "Pacific/Honolulu": "Pacific",
    "America/Anchorage": "Pacific",
}
DEFAULT_REGION = "Other"

METRIC_KEYS = (
    "emails_sent",
    "emails_opened",
    "emails_replied",
    "replies_interested",
    "replies_questions",
    "replies_not_interested",
    "bounces",
    "meetings_booked",
)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _load_json(path: Path, default: Any) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _empty_metrics() -> dict[str, int]:
    return {k: 0 for k in METRIC_KEYS}


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(100.0 * numerator / denominator, 1)


def _with_rates(metrics: dict[str, Any]) -> dict[str, Any]:
    sent = int(metrics.get("emails_sent") or 0)
    opened = int(metrics.get("emails_opened") or 0)
    replied = int(metrics.get("emails_replied") or 0)
    bounces = int(metrics.get("emails_bounced") or metrics.get("bounces") or 0)
    out = dict(metrics)
    out["bounces"] = bounces
    out["open_rate"] = _rate(opened, sent)
    out["reply_rate"] = _rate(replied, sent)
    out["bounce_rate"] = _rate(bounces, sent)
    return out


def load_analytics() -> dict:
    raw = _load_json(ANALYTICS_PATH, {"days": {}})
    if not isinstance(raw, dict):
        return {"days": {}}
    raw.setdefault("days", {})
    return raw


def save_analytics(data: dict) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_json(ANALYTICS_PATH, data)


def _lead_region(lead: dict) -> str:
    from .timezone_utils import get_recipient_timezone

    tz = get_recipient_timezone(lead)
    return TZ_TO_REGION.get(tz, DEFAULT_REGION)


def _template_from_notes(notes: str) -> str | None:
    match = TEMPLATE_RE.search(notes or "")
    if match:
        return match.group(1).upper()
    return None


def _bounces_on_day(day: str) -> int:
    from .bounce_handler import BOUNCED_PATH

    raw = _load_json(BOUNCED_PATH, {})
    if not isinstance(raw, dict):
        return 0
    count = 0
    for info in raw.values():
        if not isinstance(info, dict):
            continue
        if str(info.get("date") or "")[:10] == day:
            count += 1
    return count


def _scan_agencies_breakdown(day: str) -> dict[str, Any]:
    """Sheet scan for template / region / day-of-week breakdown on sends."""
    from .to_sheets import HEADERS, get_all_values_safe, get_worksheet_safe

    by_template: dict[str, dict[str, int]] = defaultdict(_empty_metrics)
    by_region: dict[str, dict[str, int]] = defaultdict(_empty_metrics)
    by_dow: dict[str, dict[str, int]] = defaultdict(_empty_metrics)
    sheet_totals = _empty_metrics()
    dow_name = datetime.fromisoformat(day).strftime("%A")

    ws = get_worksheet_safe("Agencies")
    values = get_all_values_safe(ws)

    for i, raw in enumerate(values):
        if i == 0:
            continue
        cells = list(raw) + [""] * max(0, len(HEADERS) - len(raw))
        fields = {name: (cells[j] if j < len(cells) else "") for j, name in enumerate(HEADERS)}
        sent = (fields.get("Sent") or "").strip()[:10]
        if sent != day:
            continue

        lead = dict(fields)
        tpl = _template_from_notes(fields.get("Notes") or "") or "unknown"
        region = _lead_region(lead)
        reply_type = (fields.get("Reply type") or "").strip().lower()
        meeting = (fields.get("Meeting") or "").strip()
        replied = (fields.get("Replied") or "").strip().lower() == "yes"

        def bump(target: dict[str, dict[str, int]], key: str) -> None:
            target[key]["emails_sent"] += 1
            if replied:
                target[key]["emails_replied"] += 1
            if reply_type == "interested":
                target[key]["replies_interested"] += 1
            elif reply_type == "question":
                target[key]["replies_questions"] += 1
            elif reply_type == "not_interested":
                target[key]["replies_not_interested"] += 1
            if meeting:
                target[key]["meetings_booked"] += 1

        bump(by_template, tpl)
        bump(by_region, region)
        bump(by_dow, dow_name)
        sheet_totals["emails_sent"] += 1
        if replied:
            sheet_totals["emails_replied"] += 1
        if reply_type == "interested":
            sheet_totals["replies_interested"] += 1
        elif reply_type == "question":
            sheet_totals["replies_questions"] += 1
        elif reply_type == "not_interested":
            sheet_totals["replies_not_interested"] += 1
        if meeting:
            sheet_totals["meetings_booked"] += 1

    return {
        "sheet_totals": sheet_totals,
        "by_template": dict(by_template),
        "by_region": dict(by_region),
        "by_dow": dict(by_dow),
    }


def collect_day_metrics(day: str | None = None) -> dict[str, Any]:
    """Build metrics record for one UTC day."""
    day = day or _utc_today().isoformat()
    from .alerts import load_daily_metrics

    daily = load_daily_metrics().get(day, {})
    if not isinstance(daily, dict):
        daily = {}

    metrics = _empty_metrics()
    metrics["emails_sent"] = int(daily.get("emails_sent") or 0)
    metrics["emails_opened"] = int(daily.get("emails_opened") or daily.get("presentation_clicks") or 0)
    metrics["emails_replied"] = int(daily.get("replies") or 0)
    metrics["replies_interested"] = int(daily.get("interested") or daily.get("replies_interested") or 0)
    metrics["replies_questions"] = int(daily.get("replies_questions") or 0)
    metrics["replies_not_interested"] = int(daily.get("replies_not_interested") or 0)
    metrics["meetings_booked"] = int(daily.get("meetings_booked") or 0)
    metrics["bounces"] = _bounces_on_day(day)

    breakdown: dict[str, Any] = {"by_template": {}, "by_region": {}, "by_dow": {}}
    try:
        breakdown = _scan_agencies_breakdown(day)
        sheet = breakdown.get("sheet_totals") or {}
        if int(metrics["emails_sent"] or 0) == 0:
            metrics["emails_sent"] = int(sheet.get("emails_sent") or 0)
        if int(metrics["emails_replied"] or 0) == 0:
            metrics["emails_replied"] = int(sheet.get("emails_replied") or 0)
        if int(metrics["replies_interested"] or 0) == 0:
            metrics["replies_interested"] = int(sheet.get("replies_interested") or 0)
        if int(metrics["replies_questions"] or 0) == 0:
            metrics["replies_questions"] = int(sheet.get("replies_questions") or 0)
        if int(metrics["replies_not_interested"] or 0) == 0:
            metrics["replies_not_interested"] = int(sheet.get("replies_not_interested") or 0)
        if int(metrics["meetings_booked"] or 0) == 0:
            metrics["meetings_booked"] = int(sheet.get("meetings_booked") or 0)
    except Exception as e:
        print(f"sheet breakdown skipped: {e}")

    record = _with_rates({**metrics, "date": day})
    record["by_template"] = {
        k: _with_rates(v) for k, v in (breakdown.get("by_template") or {}).items()
    }
    record["by_region"] = {
        k: _with_rates(v) for k, v in (breakdown.get("by_region") or {}).items()
    }
    record["by_dow"] = {
        k: _with_rates(v) for k, v in (breakdown.get("by_dow") or {}).items()
    }
    return record


def update_daily(day: str | None = None) -> dict[str, Any]:
    """Collect today's metrics and persist to analytics.json."""
    day = day or _utc_today().isoformat()
    data = load_analytics()
    record = collect_day_metrics(day)
    data["days"][day] = record
    _prune_old_days(data)
    save_analytics(data)
    export_to_site(data)
    from .template_ab import maybe_generate_monthly_variants

    maybe_generate_monthly_variants()
    return record


def _prune_old_days(data: dict, retention_days: int = RETENTION_DAYS) -> None:
    cutoff = _utc_today() - timedelta(days=retention_days)
    days = data.get("days") or {}
    for key in list(days.keys()):
        try:
            if date.fromisoformat(key) < cutoff:
                del days[key]
        except ValueError:
            del days[key]


def _days_in_range(data: dict, start: date, end: date) -> list[dict]:
    days = data.get("days") or {}
    out: list[dict] = []
    cur = start
    while cur <= end:
        key = cur.isoformat()
        if key in days:
            out.append(days[key])
        cur += timedelta(days=1)
    return out


def _aggregate(records: list[dict]) -> dict[str, Any]:
    totals = _empty_metrics()
    for rec in records:
        for k in METRIC_KEYS:
            totals[k] += int(rec.get(k) or 0)
    return _with_rates(totals)


def weekly_summary(end: date | None = None) -> dict[str, Any]:
    end = end or _utc_today()
    start = end - timedelta(days=6)
    data = load_analytics()
    records = _days_in_range(data, start, end)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": len(records),
        **_aggregate(records),
    }


def monthly_summary(end: date | None = None) -> dict[str, Any]:
    end = end or _utc_today()
    start = end - timedelta(days=29)
    data = load_analytics()
    records = _days_in_range(data, start, end)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": len(records),
        **_aggregate(records),
    }


def _template_aggregate(data: dict, days: int = 30) -> dict[str, dict[str, Any]]:
    end = _utc_today()
    start = end - timedelta(days=days - 1)
    records = _days_in_range(data, start, end)
    agg: dict[str, dict[str, int]] = defaultdict(_empty_metrics)
    for rec in records:
        for tpl, stats in (rec.get("by_template") or {}).items():
            if not str(tpl).startswith("A"):
                continue
            for k in METRIC_KEYS:
                agg[tpl][k] += int(stats.get(k) or 0)
    return {k: _with_rates(v) for k, v in agg.items()}


def top_templates(n: int = 5, days: int = 30) -> list[dict[str, Any]]:
    data = load_analytics()
    agg = _template_aggregate(data, days=days)
    ranked = sorted(
        agg.items(),
        key=lambda x: (x[1].get("reply_rate", 0), x[1].get("open_rate", 0), x[1].get("emails_sent", 0)),
        reverse=True,
    )
    return [{"template": tpl, **stats} for tpl, stats in ranked[:n]]


def bottom_templates(n: int = 5, days: int = 30) -> list[dict[str, Any]]:
    data = load_analytics()
    agg = _template_aggregate(data, days=days)
    with_sends = [(t, s) for t, s in agg.items() if int(s.get("emails_sent") or 0) > 0]
    ranked = sorted(
        with_sends,
        key=lambda x: (x[1].get("reply_rate", 0), x[1].get("open_rate", 0)),
    )
    return [{"template": tpl, **stats} for tpl, stats in ranked[:n]]


def trends(days: int = 30) -> dict[str, Any]:
    data = load_analytics()
    end = _utc_today()
    start = end - timedelta(days=days - 1)
    series: list[dict[str, Any]] = []
    for rec in _days_in_range(data, start, end):
        series.append(
            {
                "date": rec.get("date"),
                "emails_sent": rec.get("emails_sent", 0),
                "open_rate": rec.get("open_rate", 0),
                "reply_rate": rec.get("reply_rate", 0),
                "emails_replied": rec.get("emails_replied", 0),
            }
        )
    return {"start": start.isoformat(), "end": end.isoformat(), "series": series}


def build_site_payload(data: dict | None = None) -> dict:
    """Dashboard JSON: today, week, month, trends, template rankings."""
    data = data or load_analytics()
    today_key = _utc_today().isoformat()
    today = data.get("days", {}).get(today_key) or collect_day_metrics(today_key)
    try:
        from .deliverability_check import deliverability_trend

        deliverability = deliverability_trend(30)
    except Exception as e:
        deliverability = {"error": str(e), "series": []}
    return {
        "updated_at": data.get("updated_at"),
        "today": today,
        "week": weekly_summary(),
        "month": monthly_summary(),
        "trends": trends(30),
        "top_templates": top_templates(5),
        "bottom_templates": bottom_templates(5),
        "deliverability": deliverability,
    }


def export_to_site(data: dict | None = None) -> None:
    """Write analytics.json to the Next.js public folder."""
    payload = build_site_payload(data)
    targets = [LEAD_PUBLIC / "analytics.json"] if LEAD_PUBLIC.parent.exists() else []
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        _save_json(path, payload)
        print(f"exported dashboard data -> {path}")


def _format_daily_report(record: dict[str, Any]) -> tuple[str, str]:
    sent = int(record.get("emails_sent") or 0)
    interested = int(record.get("replies_interested") or 0)
    open_rate = record.get("open_rate", 0)
    subject = f"Daily Report: {sent} sent, {interested} interested, {open_rate}% open rate"
    body = (
        f"Date: {record.get('date')}\n\n"
        f"Emails sent:     {sent}\n"
        f"Emails opened:   {record.get('emails_opened', 0)} ({open_rate}% open rate)\n"
        f"Emails replied:  {record.get('emails_replied', 0)} ({record.get('reply_rate', 0)}% reply rate)\n"
        f"  Interested:    {interested}\n"
        f"  Questions:     {record.get('replies_questions', 0)}\n"
        f"  Not interested:{record.get('replies_not_interested', 0)}\n"
        f"Bounces:         {record.get('bounces', 0)} ({record.get('bounce_rate', 0)}%)\n"
        f"Meetings booked: {record.get('meetings_booked', 0)}\n\n"
        f"Dashboard: {DASHBOARD_URL}\n"
    )
    return subject, body


def send_daily_report(record: dict[str, Any] | None = None) -> bool:
    from .alerts import send_alert

    record = record or collect_day_metrics()
    subject, body = _format_daily_report(record)
    return send_alert(subject, body, severity="info")


def print_daily(record: dict[str, Any] | None = None) -> None:
    record = record or update_daily()
    print(f"=== Daily analytics ({record.get('date')}) ===")
    for key in METRIC_KEYS:
        print(f"  {key}: {record.get(key, 0)}")
    print(f"  open_rate:  {record.get('open_rate', 0)}%")
    print(f"  reply_rate: {record.get('reply_rate', 0)}%")
    print(f"  bounce_rate:{record.get('bounce_rate', 0)}%")
    templates = record.get("by_template") or {}
    if templates:
        print("\n  By template:")
        for tpl in sorted(templates.keys()):
            s = templates[tpl]
            print(
                f"    {tpl}: sent={s.get('emails_sent', 0)} "
                f"open={s.get('open_rate', 0)}% reply={s.get('reply_rate', 0)}%"
            )
    print("========================================")


def print_weekly() -> None:
    summary = weekly_summary()
    print(f"=== Weekly summary ({summary['start']} -> {summary['end']}) ===")
    for key in METRIC_KEYS:
        print(f"  {key}: {summary.get(key, 0)}")
    print(f"  open_rate:  {summary.get('open_rate', 0)}%")
    print(f"  reply_rate: {summary.get('reply_rate', 0)}%")
    print("========================================")


def print_top_templates() -> None:
    print("=== Top templates (30d) ===")
    for row in top_templates(5):
        print(
            f"  {row['template']}: sent={row.get('emails_sent', 0)} "
            f"open={row.get('open_rate', 0)}% reply={row.get('reply_rate', 0)}%"
        )
    print("\n=== Bottom templates (30d) ===")
    for row in bottom_templates(5):
        print(
            f"  {row['template']}: sent={row.get('emails_sent', 0)} "
            f"open={row.get('open_rate', 0)}% reply={row.get('reply_rate', 0)}%"
        )
    print("========================================")


def print_trends(days: int = 30) -> None:
    t = trends(days)
    print(f"=== Trends ({t['start']} -> {t['end']}) ===")
    for row in t["series"]:
        print(
            f"  {row['date']}: sent={row['emails_sent']} "
            f"open={row['open_rate']}% reply={row['reply_rate']}%"
        )
    print("========================================")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="DentalMeet pipeline analytics")
    sub = parser.add_subparsers(dest="command")

    p_daily = sub.add_parser("daily", help="Collect and show today's metrics")
    p_daily.add_argument("--email", action="store_true", help="Email report to ALERT_EMAIL")
    p_daily.add_argument("--day", help="UTC date YYYY-MM-DD (default today)")

    sub.add_parser("weekly", help="Week summary")
    sub.add_parser("top-templates", help="Best/worst templates (30d)")
    sub.add_parser("template-scores", help="A/B template scores and rankings")
    sub.add_parser("reset-templates", help="Reset A/B template performance stats")
    p_trends = sub.add_parser("trends", help="30-day trend lines")
    p_trends.add_argument("--days", type=int, default=30)

    args = parser.parse_args(argv)
    command = args.command or "daily"

    try:
        if command == "daily":
            record = update_daily(getattr(args, "day", None))
            print_daily(record)
            if getattr(args, "email", False):
                send_daily_report(record)
        elif command == "weekly":
            update_daily()
            print_weekly()
        elif command == "top-templates":
            update_daily()
            print_top_templates()
        elif command == "template-scores":
            from .template_ab import maybe_generate_monthly_variants, print_template_scores

            maybe_generate_monthly_variants()
            print_template_scores()
        elif command == "reset-templates":
            from .template_ab import reset_templates

            reset_templates()
        elif command == "trends":
            update_daily()
            print_trends(getattr(args, "days", 30))
        else:
            parser.print_help()
            sys.exit(1)
    except Exception as exc:
        from .alerts import send_alert

        send_alert("Analytics failed", str(exc), severity="critical")
        raise


if __name__ == "__main__":
    main()

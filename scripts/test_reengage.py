"""Unit checks for Agencies 2-month re-engage + Google-preferred agency send."""

from __future__ import annotations

import io
import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.reengage import (  # noqa: E402
    DEFAULT_REENGAGE_DAYS,
    append_reengage_notes,
    apply_reengage_reset,
    is_reengage_eligible,
    last_reengage_reset,
    reengage_ineligibility_reason,
    reengage_notes_marker,
)
from pipeline.smtp_accounts import prefer_google_senders  # noqa: E402


TODAY = date(2026, 9, 12)
STALE = (TODAY - timedelta(days=DEFAULT_REENGAGE_DAYS)).isoformat()  # 2026-07-14
TOO_NEW = (TODAY - timedelta(days=DEFAULT_REENGAGE_DAYS - 1)).isoformat()
OLDER = (TODAY - timedelta(days=90)).isoformat()


def _lead(**overrides) -> dict:
    row = {
        "Company": "Bright Dental Ads",
        "Contact": "Sam Founder",
        "Title": "Founder",
        "Email": "sam@agency.example",
        "Verified": "yes",
        "Signal": "Page: agency.example",
        "Sent": STALE,
        "Replied": "",
        "Reply type": "",
        "Meeting": "",
        "Notes": "",
        "Approved": "",
        "Enriched": "",
        "PersonID": "",
        "LinkedIn": "",
    }
    row.update(overrides)
    return row


class ReengageEligibilityTests(unittest.TestCase):
    def test_stale_silent_lead_is_eligible(self) -> None:
        self.assertTrue(is_reengage_eligible(_lead(), today=TODAY))
        self.assertIsNone(reengage_ineligibility_reason(_lead(), today=TODAY))

    def test_exactly_60_days_is_eligible(self) -> None:
        self.assertTrue(is_reengage_eligible(_lead(Sent=STALE), today=TODAY))

    def test_59_days_is_not_eligible(self) -> None:
        self.assertEqual(
            reengage_ineligibility_reason(_lead(Sent=TOO_NEW), today=TODAY),
            "sent_too_recent",
        )

    def test_empty_sent_not_eligible(self) -> None:
        self.assertEqual(
            reengage_ineligibility_reason(_lead(Sent=""), today=TODAY),
            "no_sent_date",
        )

    def test_replied_blocks(self) -> None:
        self.assertEqual(
            reengage_ineligibility_reason(_lead(Replied="yes"), today=TODAY),
            "replied",
        )
        self.assertEqual(
            reengage_ineligibility_reason(_lead(Replied="2026-08-01"), today=TODAY),
            "replied",
        )

    def test_meeting_column_blocks(self) -> None:
        self.assertEqual(
            reengage_ineligibility_reason(_lead(Meeting="response_sent"), today=TODAY),
            "meeting_active",
        )
        self.assertEqual(
            reengage_ineligibility_reason(_lead(Meeting="booked"), today=TODAY),
            "meeting_active",
        )

    def test_reply_type_blocks(self) -> None:
        for kind in ("interested", "question", "not_interested", "unsubscribe", "bounced"):
            self.assertEqual(
                reengage_ineligibility_reason(_lead(**{"Reply type": kind}), today=TODAY),
                f"reply_type:{kind}",
                msg=kind,
            )

    def test_notes_bounce_unsub_dnc_block(self) -> None:
        cases = (
            "status=bounced 2026-06-01",
            "unsubscribed 2026-06-01",
            "tpl=A1; not_interested",
            "do_not_contact 2026-05-01",
            "bounced",
        )
        for notes in cases:
            self.assertEqual(
                reengage_ineligibility_reason(_lead(Notes=notes), today=TODAY),
                "notes_block",
                msg=notes,
            )

    def test_verified_required(self) -> None:
        self.assertEqual(
            reengage_ineligibility_reason(_lead(Verified="no"), today=TODAY),
            "not_verified",
        )

    def test_recent_reengage_marker_caps(self) -> None:
        notes = f"sent_was 2026-04-01; reengage_reset {TOO_NEW}"
        self.assertEqual(
            reengage_ineligibility_reason(_lead(Sent=STALE, Notes=notes), today=TODAY),
            "reengage_cap",
        )

    def test_old_reengage_marker_allows_another_cycle(self) -> None:
        notes = f"sent_was 2026-01-01; reengage_reset {OLDER}"
        self.assertTrue(is_reengage_eligible(_lead(Sent=STALE, Notes=notes), today=TODAY))

    def test_last_reengage_reset_picks_latest(self) -> None:
        notes = "reengage_reset 2026-01-01; other; reengage_reset 2026-06-01"
        self.assertEqual(last_reengage_reset(notes), date(2026, 6, 1))
        self.assertIsNone(last_reengage_reset("tpl=A3; FU1 sent 2026-01-01"))

    def test_notes_marker_preserves_sent_history(self) -> None:
        marker = reengage_notes_marker("2026-01-15", TODAY)
        self.assertEqual(marker, "sent_was 2026-01-15; reengage_reset 2026-09-12")
        combined = append_reengage_notes("tpl=A1", "2026-01-15", TODAY)
        self.assertIn("tpl=A1", combined)
        self.assertIn("sent_was 2026-01-15", combined)
        self.assertIn("reengage_reset 2026-09-12", combined)


class ReengageSheetApplyTests(unittest.TestCase):
    def test_dry_run_apply_does_not_write(self) -> None:
        lead = _lead(row_index=4)
        lead["sent_was"] = STALE
        with (
            patch.dict(os.environ, {"DRY_RUN": "true"}, clear=False),
            patch("pipeline.reengage.get_worksheet_safe") as ws,
            patch("pipeline.reengage.update_cell_safe") as upd,
        ):
            notes = apply_reengage_reset(lead, today=TODAY)
        ws.assert_not_called()
        upd.assert_not_called()
        self.assertIn("sent_was", notes)
        self.assertIn("reengage_reset 2026-09-12", notes)


class GetUnsentLeadsMergeTests(unittest.TestCase):
    def test_fresh_first_then_reengage(self) -> None:
        from pipeline.campaign import get_unsent_leads
        from pipeline.to_sheets import HEADERS

        def row(**fields) -> list[str]:
            return [fields.get(name, "") for name in HEADERS]

        header = list(HEADERS)
        values = [
            header,
            row(Email="new@agency.example", Verified="yes"),
            row(Email="old@agency.example", Verified="yes", Sent=STALE),
            row(Email="mid@agency.example", Verified="yes", Sent=TOO_NEW),
            row(
                Email="replied@agency.example",
                Verified="yes",
                Sent=STALE,
                Replied="yes",
            ),
        ]

        class _FixedDate(date):
            @classmethod
            def today(cls):
                return TODAY

        with (
            patch("pipeline.campaign.get_worksheet_safe"),
            patch("pipeline.campaign.get_all_values_safe", return_value=values),
            patch(
                "pipeline.bounce_handler.check_recipient_sendable",
                return_value=(True, ""),
            ),
            patch("pipeline.reengage.date", _FixedDate),
        ):
            leads = get_unsent_leads()

        emails = [item["Email"] for item in leads]
        self.assertEqual(emails, ["new@agency.example", "old@agency.example"])
        self.assertFalse(leads[0]["reengage"])
        self.assertTrue(leads[1]["reengage"])
        self.assertEqual(leads[1]["sent_was"], STALE)


class PreferGoogleSendersTests(unittest.TestCase):
    def test_flag_defaults_on(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PREFER_GOOGLE_SENDERS", None)
            self.assertTrue(prefer_google_senders())

    def test_flag_can_disable(self) -> None:
        with patch.dict(os.environ, {"PREFER_GOOGLE_SENDERS": "0"}, clear=False):
            self.assertFalse(prefer_google_senders())

    def test_send_email_ignores_zoho_owner_when_preferring_google(self) -> None:
        from pipeline.email_sender import send_email

        buf = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "PREFER_GOOGLE_SENDERS": "1",
                    "DRY_RUN": "true",
                    "SKIP_EMAIL_VALIDATION": "1",
                },
                clear=False,
            ),
            patch(
                "pipeline.conversation_owner.get_conversation_owner",
                return_value="hello@dentalmeet.agency",
            ),
            patch(
                "pipeline.conversation_owner.google_workspace_smtp_accounts",
                return_value=[("vladislav@meet-agency.com", "pw")],
            ),
            patch(
                "pipeline.email_sender.load_smtp_accounts",
                return_value=[("hello@dentalmeet.agency", "zoho")],
            ),
            patch(
                "pipeline.email_validation.is_send_paused",
                return_value=(False, ""),
            ),
            patch(
                "pipeline.bounce_handler.check_recipient_sendable",
                return_value=(True, ""),
            ),
            patch("pipeline.send_throttle.record_send"),
            patch("pipeline.email_sender._advance_rotation"),
            patch("pipeline.email_sender._remember_thread_owner"),
            patch(
                "pipeline.send_throttle.select_next_account",
                return_value=("vladislav@meet-agency.com", "pw", 0),
            ),
            patch("sys.stdout", buf),
        ):
            ok = send_email("lead@agency.example", "Hi", "Body")
        self.assertTrue(ok)
        log = buf.getvalue()
        self.assertIn("ignoring non-Google owner", log)
        self.assertIn("vladislav@meet-agency.com", log)
        self.assertNotIn("From (fixed): hello@dentalmeet.agency", log)

    def test_send_email_refuses_zoho_when_no_google_password(self) -> None:
        from pipeline.email_sender import send_email

        buf = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "PREFER_GOOGLE_SENDERS": "1",
                    "DRY_RUN": "",
                    "SKIP_EMAIL_VALIDATION": "1",
                },
                clear=False,
            ),
            patch("pipeline.email_sender.load_smtp_accounts", return_value=[]),
            patch(
                "pipeline.conversation_owner.google_workspace_smtp_accounts",
                return_value=[],
            ),
            patch(
                "pipeline.email_validation.is_send_paused",
                return_value=(False, ""),
            ),
            patch(
                "pipeline.bounce_handler.check_recipient_sendable",
                return_value=(True, ""),
            ),
            patch("sys.stdout", buf),
        ):
            ok = send_email("lead@agency.example", "Hi", "Body")
        self.assertFalse(ok)
        log = buf.getvalue()
        self.assertIn("Refusing Zoho fallback", log)
        self.assertIn("PREFER_GOOGLE_SENDERS", log)


if __name__ == "__main__":
    unittest.main()

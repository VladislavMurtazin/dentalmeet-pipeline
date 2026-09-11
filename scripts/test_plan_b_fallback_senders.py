"""Unit checks for Plan B clinic Google Workspace senders."""

from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from delivery.send_as_fallback import (  # noqa: E402
    load_fallback_senders,
    pick_next_fallback_sender,
    send_as_fallback,
)


GOOGLE_A = "vladislav@meet-agency.com"
GOOGLE_B = "founder@meet-agency.com"
GOOGLE_C = "outreach@meet-agency.com"
ZOHO = "hello@dentalmeet.agency"
CLINIC = {"email": "clinic@example.com"}


class PlanBFallbackSenderTests(unittest.TestCase):
    def test_load_uses_google_not_zoho(self) -> None:
        pairs = [(GOOGLE_A, "pw1"), (GOOGLE_B, "pw2")]
        with (
            patch.dict(os.environ, {"FALLBACK_ROTATE": "1"}, clear=False),
            patch(
                "pipeline.conversation_owner.google_workspace_smtp_accounts",
                return_value=pairs,
            ),
        ):
            senders = load_fallback_senders()
        self.assertEqual(senders, [GOOGLE_A, GOOGLE_B])
        self.assertTrue(all(e.endswith("@meet-agency.com") for e in senders))
        self.assertNotIn(ZOHO, senders)

    def test_no_google_password_does_not_fall_back_to_zoho(self) -> None:
        buf = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "FALLBACK_ROTATE": "1",
                    "FALLBACK_FROM_EMAIL": ZOHO,
                    "DRY_RUN": "",
                },
                clear=False,
            ),
            patch(
                "pipeline.conversation_owner.google_workspace_smtp_accounts",
                return_value=[],
            ),
            patch("sys.stdout", buf),
        ):
            senders = load_fallback_senders()
        self.assertEqual(senders, [])
        log = buf.getvalue()
        self.assertIn("Refusing Zoho fallback", log)
        self.assertIn("ERROR", log)

    def test_rotate_off_zoho_from_refused(self) -> None:
        buf = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "FALLBACK_ROTATE": "0",
                    "FALLBACK_FROM_EMAIL": ZOHO,
                    "DRY_RUN": "",
                },
                clear=False,
            ),
            patch(
                "pipeline.conversation_owner.google_workspace_smtp_accounts",
                return_value=[(GOOGLE_A, "pw")],
            ),
            patch("sys.stdout", buf),
        ):
            senders = load_fallback_senders()
        self.assertEqual(senders, [])
        self.assertIn("Refusing Zoho fallback", buf.getvalue())

    def test_rotate_off_uses_google_from(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "FALLBACK_ROTATE": "0",
                    "FALLBACK_FROM_EMAIL": GOOGLE_C,
                },
                clear=False,
            ),
            patch(
                "pipeline.conversation_owner.google_workspace_smtp_accounts",
                return_value=[(GOOGLE_A, "pw1"), (GOOGLE_C, "pw3")],
            ),
        ):
            senders = load_fallback_senders()
        self.assertEqual(senders, [GOOGLE_C])

    def test_pool_filters_to_google_only(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "FALLBACK_ROTATE": "1",
                    "FALLBACK_FROM_POOL": f"{GOOGLE_A},{ZOHO}",
                },
                clear=False,
            ),
            patch(
                "pipeline.conversation_owner.google_workspace_smtp_accounts",
                return_value=[(GOOGLE_A, "pw1"), (GOOGLE_B, "pw2")],
            ),
        ):
            senders = load_fallback_senders()
        self.assertEqual(senders, [GOOGLE_A])

    def test_pick_uses_select_next_account(self) -> None:
        pairs = [(GOOGLE_A, "pw1"), (GOOGLE_B, "pw2")]
        with (
            patch(
                "pipeline.conversation_owner.google_workspace_smtp_accounts",
                return_value=pairs,
            ),
            patch(
                "pipeline.send_throttle.select_next_account",
                return_value=(GOOGLE_B, "pw2", 1),
            ) as select,
        ):
            picked = pick_next_fallback_sender([GOOGLE_A, GOOGLE_B])
        self.assertEqual(picked, GOOGLE_B)
        select.assert_called_once()
        self.assertEqual(select.call_args.args[0], pairs)

    def test_pick_skips_zoho_and_per_run_cap(self) -> None:
        pairs = [(GOOGLE_A, "pw1"), (GOOGLE_B, "pw2")]
        with (
            patch(
                "pipeline.conversation_owner.google_workspace_smtp_accounts",
                return_value=pairs,
            ),
            patch(
                "pipeline.send_throttle.select_next_account",
                return_value=(GOOGLE_B, "pw2", 1),
            ) as select,
        ):
            picked = pick_next_fallback_sender(
                [ZOHO, GOOGLE_A, GOOGLE_B],
                per_run_counts={GOOGLE_A: 3},
                max_per_account=3,
            )
        self.assertEqual(picked, GOOGLE_B)
        self.assertEqual(select.call_args.args[0], [(GOOGLE_B, "pw2")])

    def test_send_refuses_zoho_from(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            ok = send_as_fallback(
                "client@example.com",
                CLINIC,
                "subj",
                "body",
                from_email=ZOHO,
            )
        self.assertFalse(ok)
        self.assertIn("refusing non-Google", buf.getvalue())

    def test_send_refuses_when_no_google_sender(self) -> None:
        buf = io.StringIO()
        with (
            patch(
                "delivery.send_as_fallback.load_fallback_senders",
                return_value=[],
            ),
            patch(
                "delivery.send_as_fallback.pick_next_fallback_sender",
                return_value=None,
            ),
            patch("sys.stdout", buf),
        ):
            ok = send_as_fallback(
                "client@example.com", CLINIC, "subj", "body"
            )
        self.assertFalse(ok)
        log = buf.getvalue()
        self.assertIn("ERROR", log)
        self.assertIn("Zoho fallback", log)

    def test_dry_run_can_list_catalog_google_without_password(self) -> None:
        env = {
            "DRY_RUN": "true",
            "FALLBACK_ROTATE": "1",
            "FALLBACK_FROM_POOL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            senders = load_fallback_senders()
        self.assertTrue(senders)
        self.assertTrue(all(e.endswith("@meet-agency.com") for e in senders))
        self.assertFalse(any(e.endswith("@dentalmeet.agency") for e in senders))


if __name__ == "__main__":
    unittest.main()

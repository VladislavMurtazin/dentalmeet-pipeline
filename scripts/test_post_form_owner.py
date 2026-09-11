"""Unit checks for post-form Google Workspace conversation owner."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.conversation_owner import (  # noqa: E402
    get_conversation_owner,
    is_google_workspace_sender,
    resolve_from_email,
    resolve_post_form_from_email,
    set_conversation_owner,
)


GOOGLE_A = "vladislav@meet-agency.com"
GOOGLE_B = "founder@meet-agency.com"
ZOHO = "hello@dentalmeet.agency"
LEAD = "agency.founder@example.com"


class PostFormOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.owner_path = Path(self._tmpdir.name) / "conversation_owners.json"
        self._owner_patch = patch(
            "pipeline.conversation_owner.OWNER_PATH", self.owner_path
        )
        self._sheets_write = patch(
            "pipeline.conversation_owner._write_clients_owner"
        )
        self._sheets_read = patch(
            "pipeline.conversation_owner._read_clients_owner", return_value=None
        )
        self._agencies = patch(
            "pipeline.conversation_owner._read_agencies_via", return_value=None
        )
        self._owner_patch.start()
        self._sheets_write.start()
        self._sheets_read.start()
        self._agencies.start()

    def tearDown(self) -> None:
        self._owner_patch.stop()
        self._sheets_write.stop()
        self._sheets_read.stop()
        self._agencies.stop()
        self._tmpdir.cleanup()

    def test_is_google_workspace_sender(self) -> None:
        self.assertTrue(is_google_workspace_sender(GOOGLE_A))
        self.assertTrue(is_google_workspace_sender(GOOGLE_B))
        self.assertFalse(is_google_workspace_sender(ZOHO))
        self.assertFalse(is_google_workspace_sender("not-an-email"))

    def test_keeps_sticky_google_owner(self) -> None:
        set_conversation_owner(LEAD, GOOGLE_B)
        with (
            patch(
                "pipeline.conversation_owner._google_sender_ready",
                return_value=True,
            ),
            patch(
                "pipeline.conversation_owner.pick_google_workspace_sender",
                return_value=GOOGLE_A,
            ) as pick,
        ):
            owner = resolve_post_form_from_email(LEAD)
        self.assertEqual(owner, GOOGLE_B)
        pick.assert_not_called()
        self.assertEqual(get_conversation_owner(LEAD), GOOGLE_B)

    def test_replaces_zoho_owner_with_google(self) -> None:
        set_conversation_owner(LEAD, ZOHO)
        self.assertEqual(resolve_from_email(LEAD), ZOHO)
        with (
            patch(
                "pipeline.conversation_owner._google_sender_ready",
                return_value=False,
            ),
            patch(
                "pipeline.conversation_owner.pick_google_workspace_sender",
                return_value=GOOGLE_A,
            ),
        ):
            owner = resolve_post_form_from_email(LEAD)
        self.assertEqual(owner, GOOGLE_A)
        self.assertEqual(get_conversation_owner(LEAD), GOOGLE_A)
        self.assertEqual(resolve_from_email(LEAD), GOOGLE_A)

    def test_no_google_password_does_not_fall_back_to_zoho(self) -> None:
        set_conversation_owner(LEAD, ZOHO)
        buf = io.StringIO()
        with (
            patch(
                "pipeline.conversation_owner.google_workspace_smtp_accounts",
                return_value=[],
            ),
            patch("sys.stdout", buf),
        ):
            owner = resolve_post_form_from_email(LEAD)
        self.assertIsNone(owner)
        self.assertEqual(get_conversation_owner(LEAD), ZOHO)
        log = buf.getvalue()
        self.assertIn("Refusing Zoho fallback", log)
        self.assertIn("ERROR", log)

    def test_refuses_zoho_overwrite_of_google_owner(self) -> None:
        set_conversation_owner(LEAD, GOOGLE_A)
        kept = set_conversation_owner(LEAD, ZOHO)
        self.assertEqual(kept, GOOGLE_A)
        self.assertEqual(get_conversation_owner(LEAD), GOOGLE_A)

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
            from pipeline.conversation_owner import pick_google_workspace_sender

            picked = pick_google_workspace_sender()
        self.assertEqual(picked, GOOGLE_B)
        select.assert_called_once()
        self.assertEqual(select.call_args.args[0], pairs)

    def test_persisted_owner_file(self) -> None:
        set_conversation_owner(LEAD, GOOGLE_A)
        data = json.loads(self.owner_path.read_text(encoding="utf-8"))
        self.assertEqual(data[LEAD], GOOGLE_A)

    def test_dry_run_can_pick_catalog_google_without_password(self) -> None:
        with patch.dict(os.environ, {"DRY_RUN": "true"}, clear=False):
            from pipeline.conversation_owner import google_workspace_smtp_accounts

            emails = [e for e, _ in google_workspace_smtp_accounts()]
        self.assertTrue(emails)
        self.assertTrue(all(e.endswith("@meet-agency.com") for e in emails))
        self.assertFalse(any(e.endswith("@dentalmeet.agency") for e in emails))


if __name__ == "__main__":
    unittest.main()

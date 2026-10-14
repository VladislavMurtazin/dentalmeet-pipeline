"""Unit checks for follow-up sheet row width (HEADERS grew past 12 columns)."""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.followup import _pad, get_followup_candidates  # noqa: E402
from pipeline.to_sheets import HEADERS, N_COLS, _pad_row  # noqa: E402


SENT = "2020-01-15"


def _full_row(**fields: str) -> list[str]:
    return [fields.get(name, "") for name in HEADERS]


def _legacy_12(**fields: str) -> list[str]:
    """Pre-Enriched/PersonID/LinkedIn width that crashed follow-up jobs."""
    return _full_row(**fields)[:12]


def _non_string_email_row() -> list:
    """Email cell is not a string, so .strip() would have aborted the job."""
    row = _legacy_12(
        Company="Num Co",
        Contact="Num Owner",
        Email="num@agency.example",
        Sent=SENT,
    )
    row[3] = 404
    return row


class PadTests(unittest.TestCase):
    def test_default_width_tracks_headers(self) -> None:
        self.assertGreater(len(HEADERS), 12)
        self.assertEqual(len(_pad(["only"])), len(HEADERS))
        self.assertEqual(len(_pad(["only"])), N_COLS)

    def test_matches_to_sheets_pad_row(self) -> None:
        sample = ["Acme", None, "Founder", "a@b.co", "yes"] + ["z"] * (N_COLS + 4)
        self.assertEqual(_pad(sample), _pad_row(sample))
        self.assertEqual(_pad(sample, n=4), _pad_row(sample, 4))

    def test_short_row_keeps_leading_cells_and_blanks_the_rest(self) -> None:
        short = _legacy_12(
            Company="Bright Dental Ads",
            Contact="Sam Founder",
            Email="sam@agency.example",
            Sent=SENT,
            Notes="domain.example",
        )
        self.assertEqual(len(short), 12)
        padded = _pad(short)
        self.assertEqual(padded[:12], short)
        self.assertEqual(padded[12:], ["", "", ""])
        fields = {name: padded[j] for j, name in enumerate(HEADERS)}
        self.assertEqual(fields["Email"], "sam@agency.example")
        self.assertEqual(fields["Enriched"], "")
        self.assertEqual(fields["PersonID"], "")
        self.assertEqual(fields["LinkedIn"], "")

    def test_none_cell_becomes_empty_string(self) -> None:
        padded = _pad(["Co", None, "Title", "ann@agency.example"])
        self.assertEqual(padded[1], "")
        self.assertNotEqual(padded[1], "None")


class FollowupCandidateTests(unittest.TestCase):
    def _values(self) -> list:
        return [
            list(HEADERS),
            _legacy_12(
                Company="Short Co",
                Contact="Ann Owner",
                Email="short@agency.example",
                Sent=SENT,
                Notes="short.example",
            ),
            None,
            _full_row(
                Company="Full Co",
                Contact="Bea Owner",
                Email="full@agency.example",
                Sent=SENT,
                Notes="full.example",
                Enriched="TRUE",
                PersonID="pid-1",
                LinkedIn="https://linkedin.com/in/bea",
            ),
            _non_string_email_row(),
            _legacy_12(
                Company="Replied Co",
                Contact="Cy Owner",
                Email="replied@agency.example",
                Sent=SENT,
                Replied="yes",
            ),
            _full_row(
                Company="Extra Co",
                Contact="Dee Owner",
                Email="extra@agency.example",
                Sent=SENT,
            )
            + ["trailing", "junk"],
            _legacy_12(
                Company="Fu1 Co",
                Contact="Eve Owner",
                Email="fu1@agency.example",
                Sent=SENT,
                Notes="FU1 sent 2020-01-20",
            ),
        ]

    def test_short_and_wide_rows_are_candidates_malformed_are_skipped(self) -> None:
        buf = io.StringIO()
        with (
            patch("pipeline.followup.get_worksheet_safe"),
            patch(
                "pipeline.followup.get_all_values_safe",
                return_value=self._values(),
            ),
            patch("sys.stdout", buf),
        ):
            found = get_followup_candidates(3, 1)

        emails = [item["Email"] for item in found]
        self.assertEqual(
            emails,
            [
                "short@agency.example",
                "full@agency.example",
                "extra@agency.example",
            ],
        )
        self.assertEqual(found[0]["row_index"], 2)
        self.assertEqual(found[0]["Company"], "Short Co")
        self.assertEqual(found[0]["Contact"], "Ann Owner")
        self.assertEqual(found[1]["row_index"], 4)
        log = buf.getvalue()
        self.assertIn("followup: skip malformed row 3:", log)
        self.assertIn("followup: skip malformed row 5:", log)
        self.assertNotIn("skip malformed row 2:", log)
        self.assertNotIn("skip malformed row 6:", log)

    def test_fu2_still_requires_fu1_after_padding(self) -> None:
        buf = io.StringIO()
        with (
            patch("pipeline.followup.get_worksheet_safe"),
            patch(
                "pipeline.followup.get_all_values_safe",
                return_value=self._values(),
            ),
            patch("sys.stdout", buf),
        ):
            found = get_followup_candidates(7, 2)

        self.assertEqual([item["Email"] for item in found], ["fu1@agency.example"])
        self.assertIn("skip malformed row 3:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

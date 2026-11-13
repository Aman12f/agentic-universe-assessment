"""
Unit Tests for Normalization Functions.

Validates:
- Header fuzzy normalization and whitespace/underscore collapsing.
- Indian mobile phone validation, prefix stripping (91, 0), and canonical formatting (+91XXXXXXXXXX).
- Day-first date parsing and rejection of impossible dates (e.g. 31/02).
- Decimal stripping of currency symbols (₹, Rs, Rs., INR), commas, and rounding to 2 places.
- Enum case-insensitivity.
- Timestamp conversion across UTC, offset, and naive (naive = IST).
"""

from __future__ import annotations

import unittest
from datetime import datetime

from solution.config import IST
from solution.normalizers import (
    format_ist_iso,
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_header,
    normalize_phone,
    normalize_string,
    parse_timestamp_to_ist,
)


class TestNormalizers(unittest.TestCase):
    """Test suite for value cleaners and parsers."""

    def test_normalize_header(self) -> None:
        """Verifies header lower-casing, trimming, and underscore/space collapsing."""
        self.assertEqual(normalize_header("Name"), "name")
        self.assertEqual(normalize_header("  Customer__Name  "), "customer name")
        self.assertEqual(normalize_header("mobile_no"), "mobile no")
        self.assertEqual(normalize_header("POLICY   NUMBER"), "policy number")
        self.assertEqual(normalize_header(""), "")

    def test_normalize_phone_valid_cases(self) -> None:
        """Verifies valid Indian mobile numbers in multiple input formats."""
        cases = [
            ("9876543210", "+919876543210"),
            ("+91 98765 43210", "+919876543210"),
            ("09876543210", "+919876543210"),
            ("91-98765-43210", "+919876543210"),
            ("(+91) 9876543210", "+919876543210"),
            ("+919876543210", "+919876543210"),
            ("6123456789", "+916123456789"),
            ("7123456789", "+917123456789"),
            ("8123456789", "+918123456789"),
        ]
        for raw, expected in cases:
            valid, norm = normalize_phone(raw)
            self.assertTrue(valid, f"Failed on raw={raw}")
            self.assertEqual(norm, expected)

    def test_normalize_phone_invalid_cases(self) -> None:
        """Verifies invalid phone rejection (bad prefixes, short lengths, landlines, non-numeric)."""
        invalid_cases = [
            "5123456789",        # Invalid prefix (must start with 6,7,8,9)
            "987654321",         # 9 digits (too short)
            "022-2456-7890",     # Landline (starts with 2 after prefix)
            "N/A",               # Non-numeric
            "+1 415 555 0134",   # US number
            "",                  # Empty string
            "   ",               # Whitespace only
            None,                # None object
        ]
        for raw in invalid_cases:
            valid, norm = normalize_phone(raw)
            self.assertFalse(valid, f"Expected invalid for raw={raw}")
            self.assertIsNone(norm)

    def test_normalize_date(self) -> None:
        """Verifies day-first date parsing across YYYY-MM-DD, DD/MM/YYYY, DD-Mon-YYYY."""
        self.assertEqual(normalize_date("2026-10-05"), (True, "2026-10-05"))
        self.assertEqual(normalize_date("05/10/2026"), (True, "2026-10-05"))
        self.assertEqual(normalize_date("05-10-2026"), (True, "2026-10-05"))
        self.assertEqual(normalize_date("01-Dec-2026"), (True, "2026-12-01"))
        self.assertEqual(normalize_date("01-dec-2026"), (True, "2026-12-01"))
        self.assertEqual(normalize_date("01-DEC-2026"), (True, "2026-12-01"))

        # Impossible and unparseable dates must return (False, None)
        self.assertEqual(normalize_date("31/02/2026"), (False, None))
        self.assertEqual(normalize_date("soon"), (False, None))
        self.assertEqual(normalize_date("TBD"), (False, None))
        self.assertEqual(normalize_date("2026-13-01"), (False, None))

    def test_normalize_decimal(self) -> None:
        """Verifies currency prefix stripping, comma removal, non-negative checks, and 2-decimal rounding."""
        self.assertEqual(normalize_decimal("1200"), (True, "1200.00"))
        self.assertEqual(normalize_decimal("1200.5"), (True, "1200.50"))
        self.assertEqual(normalize_decimal("₹12,500.50"), (True, "12500.50"))
        self.assertEqual(normalize_decimal("Rs. 4000"), (True, "4000.00"))
        self.assertEqual(normalize_decimal("rs 4000"), (True, "4000.00"))
        self.assertEqual(normalize_decimal("INR 5000.5"), (True, "5000.50"))

        # Invalid decimals
        self.assertEqual(normalize_decimal("N/A"), (False, None))
        self.assertEqual(normalize_decimal("-"), (False, None))
        self.assertEqual(normalize_decimal("-500"), (False, None))
        self.assertEqual(normalize_decimal("twelve thousand"), (False, None))

    def test_normalize_enum(self) -> None:
        """Verifies case-insensitive enum validation and lower-case normalization."""
        langs = ["en", "hi", "ta", "kn", "te", "mr"]
        self.assertEqual(normalize_enum("en", langs), (True, "en"))
        self.assertEqual(normalize_enum("EN", langs), (True, "en"))
        self.assertEqual(normalize_enum("  Hi  ", langs), (True, "hi"))
        self.assertEqual(normalize_enum("french", langs), (False, None))

    def test_parse_timestamp_to_ist(self) -> None:
        """Verifies timestamp conversion from UTC, offset, and naive formats to IST."""
        # UTC format
        t_utc = parse_timestamp_to_ist("2026-10-05T13:20:21Z")
        self.assertEqual(format_ist_iso(t_utc), "2026-10-05T18:50:21+05:30")

        # Explicit offset format
        t_off = parse_timestamp_to_ist("2026-10-05T18:50:21+05:30")
        self.assertEqual(format_ist_iso(t_off), "2026-10-05T18:50:21+05:30")

        # Naive format: per spec "Naive means IST"
        t_naive = parse_timestamp_to_ist("2026-10-05 18:50:21")
        self.assertEqual(format_ist_iso(t_naive), "2026-10-05T18:50:21+05:30")


if __name__ == "__main__":
    unittest.main()

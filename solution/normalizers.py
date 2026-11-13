"""
Value Normalizers and Parsers for Messy Ingestion Data.

This module strictly implements the data transformation rules defined in Assignment A:
- Header matching: lower-case, trim, replace runs of '_' or whitespace with single space.
- Phone normalisation: Indian mobile numbers canonicalized to '+91XXXXXXXXXX'.
- Date normalisation: day-first multi-format parsing, rejecting impossible dates.
- Decimal normalisation: currency prefix stripping (₹, Rs, Rs., INR), comma handling, non-negative rounding.
- Enum normalisation: case-insensitive validation and lower-casing.
- Timestamp parsing: converts UTC, offset, and naive strings (naive = IST) into aware IST datetimes.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

from solution.config import IST


def normalize_header(header: str) -> str:
    """
    Normalizes a CSV header string for schema field matching.

    Spec Rule:
        "to normalise a header, lower-case it, trim it, and replace every run of '_'
         or whitespace with a single space. Match the result against the field name
         or any of its aliases (normalised the same way)."

    Examples:
        - "Customer Name" -> "customer name"
        - "  Insured__Name " -> "insured name"
        - "Mobile_No" -> "mobile no"

    Args:
        header: Raw header column name from CSV.

    Returns:
        Cleaned, normalized string.
    """
    if not header:
        return ""
    return re.sub(r"[\s_]+", " ", header.strip().lower())


def normalize_phone(raw: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Validates and canonicalizes an Indian mobile phone number.

    Spec Rule:
        "an Indian mobile number. Strip every non-digit. If 12 digits starting with 91,
         drop the 91. If 11 digits starting with 0, drop the 0. The result is valid
         only if it is exactly 10 digits and starts with 6, 7, 8 or 9.
         Store it canonically as +91XXXXXXXXXX."

    Valid Examples:
        - "9876543210"        -> (True, "+919876543210")
        - "+91 98765 43210"   -> (True, "+919876543210")
        - "09876543210"       -> (True, "+919876543210")
        - "91-77187-50036"    -> (True, "+917718750036")

    Invalid Examples:
        - "5123456789" (bad prefix: does not start with 6, 7, 8, or 9) -> (False, None)
        - "987654321" (only 9 digits)                                  -> (False, None)
        - "022-2456-7890" (landline, starts with 2 after prefix)       -> (False, None)
        - "N/A" (non-numeric string)                                   -> (False, None)

    Args:
        raw: Raw phone string from CSV or JSON.

    Returns:
        Tuple of (is_valid: bool, canonical_phone: Optional[str]).
    """
    if raw is None:
        return False, None
    s = raw.strip()
    if not s:
        return False, None

    # Strip every non-digit character (hyphens, spaces, parens, plus signs)
    digits = re.sub(r"\D", "", s)

    # 12 digits starting with 91 -> drop leading 91
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    # 11 digits starting with 0 -> drop leading 0
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    # Result is valid only if exactly 10 digits starting with 6, 7, 8, or 9
    if len(digits) == 10 and digits[0] in "6789":
        return True, f"+91{digits}"

    return False, None


def normalize_date(raw: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Parses dates in supported formats and outputs canonical 'YYYY-MM-DD'.

    Spec Rule:
        "accept YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY and DD-Mon-YYYY (for example
         01-Dec-2026; the month name is case-insensitive). Dates are day-first.
         Impossible dates (like 31/02) are invalid."

    Args:
        raw: Raw date string from CSV.

    Returns:
        Tuple of (is_valid: bool, canonical_date: Optional[str]).
        If raw is empty/None, returns (True, None) because empty values are missing (not invalid).
        If raw contains an unparseable or impossible date, returns (False, None).
    """
    if raw is None:
        return True, None
    s = raw.strip()
    if not s:
        return True, None

    # Supported format specifications (all day-first per spec)
    formats = [
        ("%Y-%m-%d", False),  # ISO date: 2026-10-05
        ("%d/%m/%Y", False),  # Slash separated: 05/10/2026
        ("%d-%m-%Y", False),  # Dash separated: 05-10-2026
        ("%d-%b-%Y", True),   # Mon-named: 01-Dec-2026 (case-insensitive)
    ]

    for fmt, is_named_month in formats:
        try:
            val = s
            if is_named_month:
                # Standardize casing for month abbreviations (e.g. 01-dec-2026 -> 01-Dec-2026)
                parts = s.split("-")
                if len(parts) == 3:
                    val = f"{parts[0]}-{parts[1].title()}-{parts[2]}"
            # strptime automatically detects impossible dates like 31/02/2026 and raises ValueError
            dt = datetime.strptime(val, fmt)
            return True, dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return False, None


def normalize_decimal(raw: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Parses currency and numeric amounts into canonical 2-decimal strings.

    Spec Rule:
        "optionally prefixed by ₹, Rs, Rs. or INR (case-insensitive, optional space).
         Commas are thousands separators. Must be >= 0. Round to 2 places."

    Valid Examples:
        - "₹12,500.50" -> (True, "12500.50")
        - "Rs. 4000"   -> (True, "4000.00")
        - "INR 5000.5" -> (True, "5000.50")
        - "4000"       -> (True, "4000.00")

    Invalid Examples:
        - "twelve thousand" -> (False, None)
        - "-500"            -> (False, None)
        - "N/A"             -> (False, None)

    Args:
        raw: Raw amount string from CSV.

    Returns:
        Tuple of (is_valid: bool, canonical_decimal_string: Optional[str]).
    """
    if raw is None:
        return True, None
    s = raw.strip()
    if not s:
        return True, None

    # Match optional currency prefix followed by digits, commas, and optional decimal point
    pattern = re.compile(r"^(?:₹|rs\.|rs|inr)?\s*([0-9,]+(?:\.[0-9]+)?)$", re.IGNORECASE)
    match = pattern.match(s)
    if not match:
        return False, None

    # Remove thousands separator commas
    num_str = match.group(1).replace(",", "")
    try:
        val = Decimal(num_str)
        if val < 0:
            return False, None
        rounded = val.quantize(Decimal("0.01"))
        return True, str(rounded)
    except (InvalidOperation, ValueError):
        return False, None


def normalize_enum(raw: Optional[str], allowed_values: List[str]) -> Tuple[bool, Optional[str]]:
    """
    Validates a categorical value against an allowed set and stores it lowercase.

    Spec Rule:
        "compared case-insensitively against values and stored lower-case."

    Args:
        raw: Raw enum string from CSV.
        allowed_values: Allowed choices from schema (e.g., ['en', 'hi', 'ta']).

    Returns:
        Tuple of (is_valid: bool, lowercase_choice: Optional[str]).
    """
    if raw is None:
        return True, None
    s = raw.strip().lower()
    if not s:
        return True, None

    lookup = {v.lower(): v.lower() for v in allowed_values}
    if s in lookup:
        return True, lookup[s]
    return False, None


def normalize_string(raw: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Trims a generic string field. Empty or whitespace-only inputs count as missing (None).

    Args:
        raw: Raw string from CSV.

    Returns:
        Tuple of (is_valid: bool, trimmed_string_or_none: Optional[str]).
    """
    if raw is None:
        return True, None
    s = raw.strip()
    if not s:
        return True, None
    return True, s


def parse_timestamp_to_ist(ts_str: str) -> datetime:
    """
    Parses event timestamps and converts them to aware Indian Standard Time (IST).

    Spec Rule:
        "Timestamps come in three forms: UTC (...Z), offset (...+05:30), and naive
         (2026-10-03 10:01:00). Naive means IST. All dates in reports are IST dates."

    Args:
        ts_str: ISO-8601 or naive timestamp string.

    Returns:
        Timezone-aware datetime object anchored to IST (UTC+05:30).
    """
    s = ts_str.strip()

    # UTC format ending in 'Z'
    if s.endswith("Z"):
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(IST)

    # ISO format with explicit offset (e.g. +05:30)
    if "T" in s:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=IST)
        return dt.astimezone(IST)

    # Naive space-separated format: 'YYYY-MM-DD HH:MM:SS' -> Naive means IST per spec
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=IST)


def format_ist_iso(dt: datetime) -> str:
    """
    Formats an aware datetime as ISO-8601 with '+05:30' offset for final reports.

    Example:
        datetime(2026, 10, 14, 17, 55, 24, tzinfo=IST) -> "2026-10-14T17:55:24+05:30"

    Args:
        dt: Datetime object.

    Returns:
        ISO-8601 string formatted with IST timezone offset.
    """
    ist_dt = dt.astimezone(IST)
    return ist_dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")

"""
Cryptographic Utilities for PII Protection, Masking, and Blind Indexing.

This module enforces Rule 4 of Assignment A:
    "Fields marked 'pii': true must not be stored in plaintext.
     You still need to match events to leads by phone number, so think about how."

Key Mechanisms:
1. Blind Indexing (HMAC-SHA256):
   - Computes a deterministic hash of the canonical phone number using a secret pepper.
   - Allows O(1) indexed SQL equality lookups and joins without revealing plaintext phone numbers.
2. Masking (+91******XXXX):
   - Renders the phone number compliant with customer privacy standards for human-facing reports.
3. Authenticated Symmetric Encryption (CTR Stream Cipher + HMAC):
   - Standard-library implementation providing authenticated encryption for sensitive PII
     payloads (names, full numbers) before storage on disk, guaranteeing zero plaintext PII at rest.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Optional, Tuple

from solution.config import PII_PEPPER


def hash_phone(canonical_phone: str, secret: str = PII_PEPPER) -> str:
    """
    Computes a deterministic HMAC-SHA256 blind index for a canonical phone number.

    Why a Blind Index?
    Standard randomized encryption (like AES-GCM with a random IV) outputs a different
    ciphertext each time, preventing database indexes from finding matching leads.
    A keyed HMAC provides a cryptographically secure, one-way deterministic token
    allowing fast indexed SQL joins (WHERE phone_hash = ?) without plaintext exposure.

    Args:
        canonical_phone: The normalized Indian phone number (+91XXXXXXXXXX).
        secret: Secret pepper key used for HMAC calculation.

    Returns:
        Hexadecimal SHA-256 digest (64 characters), or empty string if input is falsy.
    """
    if not canonical_phone:
        return ""
    return hmac.new(
        secret.encode("utf-8"),
        canonical_phone.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def mask_phone(canonical_phone: str) -> str:
    """
    Masks a canonical Indian mobile phone (+91XXXXXXXXXX) for client reports.

    Per Output Contract:
        "phone_masked: '+91******' followed by the last 4 digits (e.g., +91******5036)."

    Args:
        canonical_phone: The normalized phone number (+91XXXXXXXXXX).

    Returns:
        Masked string '+91******XXXX', or empty string if input is invalid/too short.
    """
    if not canonical_phone or len(canonical_phone) < 4:
        return ""
    last_four = canonical_phone[-4:]
    return f"+91******{last_four}"


def _derive_keys(secret: str) -> Tuple[bytes, bytes]:
    """
    Derives independent encryption and MAC keys from the master secret.

    Uses domain separation with SHA-256:
    - Encryption Key: SHA-256(secret || b":enc")
    - Authentication Key: SHA-256(secret || b":auth")

    Args:
        secret: Master secret pepper string.

    Returns:
        Tuple of (enc_key, auth_key) each 32 bytes.
    """
    enc_key = hashlib.sha256(secret.encode("utf-8") + b":enc").digest()
    auth_key = hashlib.sha256(secret.encode("utf-8") + b":auth").digest()
    return enc_key, auth_key


def encrypt_pii(plaintext: Optional[str], secret: str = PII_PEPPER) -> Optional[str]:
    """
    Encrypts sensitive PII fields using an authenticated HMAC-SHA256 CTR stream cipher.

    Design:
    - Standard library only (no external C-extensions required for portable deployment).
    - Generates a cryptographically secure random 16-byte initialization vector (IV).
    - Keystream generated via HMAC-SHA256(enc_key, IV || 32-bit counter).
    - Ciphertext = plaintext XOR keystream.
    - Tag = HMAC-SHA256(auth_key, IV || Ciphertext) for Encrypt-then-MAC authentication.
    - Output format: base64(IV [16B] + Tag [32B] + Ciphertext).

    Args:
        plaintext: The JSON or string containing sensitive customer information.
        secret: Master secret pepper key.

    Returns:
        Base64-encoded encrypted envelope string, or None if plaintext is None.
    """
    if plaintext is None:
        return None
    data = plaintext.encode("utf-8")
    enc_key, auth_key = _derive_keys(secret)
    iv = secrets.token_bytes(16)

    # Generate keystream blocks
    keystream = bytearray()
    counter = 0
    while len(keystream) < len(data):
        block = hmac.new(enc_key, iv + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        keystream.extend(block)
        counter += 1

    # XOR plaintext with keystream
    ciphertext = bytes(a ^ b for a, b in zip(data, keystream[:len(data)]))

    # Encrypt-then-MAC authentication tag
    tag = hmac.new(auth_key, iv + ciphertext, hashlib.sha256).digest()

    return base64.b64encode(iv + tag + ciphertext).decode("ascii")


def decrypt_pii(encrypted_data: Optional[str], secret: str = PII_PEPPER) -> Optional[str]:
    """
    Verifies authenticity and decrypts ciphertext encrypted by `encrypt_pii`.

    Integrity Verification:
    - First verifies the HMAC-SHA256 authentication tag in constant time using `hmac.compare_digest`.
    - If the ciphertext or IV has been tampered with, decryption aborts immediately returning None.

    Args:
        encrypted_data: Base64-encoded envelope (IV + Tag + Ciphertext).
        secret: Master secret pepper key.

    Returns:
        Decrypted UTF-8 plaintext string, or None if decryption/verification fails.
    """
    if encrypted_data is None:
        return None
    try:
        raw = base64.b64decode(encrypted_data.encode("ascii"))
        # Header requires at least 16 bytes IV + 32 bytes Tag = 48 bytes
        if len(raw) < 48:
            return None

        iv = raw[:16]
        expected_tag = raw[16:48]
        ciphertext = raw[48:]

        enc_key, auth_key = _derive_keys(secret)

        # Constant-time MAC comparison
        actual_tag = hmac.new(auth_key, iv + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_tag, actual_tag):
            return None

        # Recompute keystream and XOR to recover plaintext
        keystream = bytearray()
        counter = 0
        while len(keystream) < len(ciphertext):
            block = hmac.new(enc_key, iv + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            keystream.extend(block)
            counter += 1

        plaintext_bytes = bytes(a ^ b for a, b in zip(ciphertext, keystream[:len(ciphertext)]))
        return plaintext_bytes.decode("utf-8")
    except Exception:
        return None

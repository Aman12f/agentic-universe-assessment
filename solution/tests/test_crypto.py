"""
Unit Tests for Cryptographic Utilities and PII Protection.

Validates:
- Correct masking of phone numbers (+91******XXXX).
- Deterministic nature of HMAC-SHA256 blind index hashing.
- Authenticated CTR encryption and decryption of sensitive customer PII.
- Integrity verification failure upon ciphertext tampering.
"""

from __future__ import annotations

import unittest

from solution.crypto import decrypt_pii, encrypt_pii, hash_phone, mask_phone


class TestCrypto(unittest.TestCase):
    """Test suite for PII cryptographic routines."""

    def test_mask_phone(self) -> None:
        """Verifies phone number masking produces '+91******' followed by the last 4 digits."""
        self.assertEqual(mask_phone("+919876543210"), "+91******3210")
        self.assertEqual(mask_phone("+917718750036"), "+91******0036")
        self.assertEqual(mask_phone(""), "")

    def test_hash_phone_deterministic(self) -> None:
        """Verifies deterministic output for blind index joins and uniqueness across different numbers."""
        h1 = hash_phone("+919876543210")
        h2 = hash_phone("+919876543210")
        h3 = hash_phone("+919876543211")

        # Same input must always produce identical hash
        self.assertEqual(h1, h2)
        # Different input must produce different hash
        self.assertNotEqual(h1, h3)
        # Length of SHA-256 hex digest is 64 characters
        self.assertEqual(len(h1), 64)

    def test_encrypt_decrypt_pii(self) -> None:
        """Verifies encrypted ciphertext hides plaintext and decrypts losslessly."""
        original = '{"name": "Aarav Sharma", "phone": "+919876543210"}'
        encrypted = encrypt_pii(original)

        self.assertIsNotNone(encrypted)
        self.assertNotEqual(original, encrypted)
        # Sensitive values must NOT appear in ciphertext
        self.assertNotIn("Aarav", encrypted)
        self.assertNotIn("9876543210", encrypted)

        # Decrypts back to exact original string
        decrypted = decrypt_pii(encrypted)
        self.assertEqual(original, decrypted)

    def test_tampered_pii_decryption_fails(self) -> None:
        """Verifies that ciphertext tampering causes MAC check failure and returns None."""
        original = "Secret Customer Data"
        encrypted = encrypt_pii(original)
        assert encrypted is not None

        # Alter base64 characters
        tampered = encrypted[:-2] + ("AA" if not encrypted.endswith("AA") else "BB")
        self.assertIsNone(decrypt_pii(tampered))


if __name__ == "__main__":
    unittest.main()

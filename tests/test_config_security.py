"""UIMRS Task 11B — configuration and production-safety tests.

These tests exercise secret-key and CORS resolution without starting the
application or touching any operational database.
"""
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import (
    DEFAULT_ALLOWED_ORIGINS,
    DEFAULT_DEV_SECRET_KEY,
    CorsConfigurationError,
    InsecureSecretKeyError,
    is_insecure_secret_key,
    resolve_allowed_origins,
    resolve_environment,
    resolve_secret_key,
)


class TestSecretKeyResolution(unittest.TestCase):
    def test_missing_secret_in_development_returns_dev_default(self):
        self.assertEqual(
            resolve_secret_key({}),
            DEFAULT_DEV_SECRET_KEY,
        )

    def test_missing_secret_in_production_raises(self):
        with self.assertRaises(InsecureSecretKeyError):
            resolve_secret_key({"UIMRS_ENV": "production"})

    def test_default_secret_in_production_raises(self):
        with self.assertRaises(InsecureSecretKeyError):
            resolve_secret_key(
                {"UIMRS_ENV": "production", "SECRET_KEY": DEFAULT_DEV_SECRET_KEY}
            )

    def test_short_secret_in_production_raises(self):
        with self.assertRaises(InsecureSecretKeyError):
            resolve_secret_key({"UIMRS_ENV": "prod", "SECRET_KEY": "too-short"})

    def test_insecure_named_secret_in_production_raises(self):
        with self.assertRaises(InsecureSecretKeyError):
            resolve_secret_key(
                {"UIMRS_ENV": "production", "SECRET_KEY": "CHANGE_ME_IN_PRODUCTION"}
            )

    def test_valid_secret_in_production_is_accepted(self):
        secret = "a" * 48
        self.assertEqual(
            resolve_secret_key({"UIMRS_ENV": "production", "SECRET_KEY": secret}),
            secret,
        )

    def test_valid_secret_in_development_is_accepted(self):
        secret = "development-secret-value-with-enough-length"
        self.assertEqual(
            resolve_secret_key({"SECRET_KEY": secret}),
            secret,
        )

    def test_is_insecure_secret_key_helper(self):
        self.assertTrue(is_insecure_secret_key(""))
        self.assertTrue(is_insecure_secret_key("short"))
        self.assertTrue(is_insecure_secret_key(DEFAULT_DEV_SECRET_KEY))
        self.assertFalse(is_insecure_secret_key("a" * 40))

    def test_environment_resolution(self):
        self.assertEqual(resolve_environment({}), "development")
        self.assertEqual(resolve_environment({"APP_ENV": "Production"}), "production")


class TestCorsResolution(unittest.TestCase):
    def test_default_origins_include_documented_dev_server(self):
        origins = resolve_allowed_origins({})
        self.assertIn("http://localhost:8000", origins)
        self.assertIn("http://127.0.0.1:8000", origins)
        self.assertEqual(origins, DEFAULT_ALLOWED_ORIGINS)

    def test_explicit_origins_are_parsed_and_trimmed(self):
        origins = resolve_allowed_origins(
            {"ALLOWED_ORIGINS": " https://app.example.com , https://admin.example.com "}
        )
        self.assertEqual(
            origins,
            ["https://app.example.com", "https://admin.example.com"],
        )

    def test_cors_allowed_origins_alias_is_supported(self):
        origins = resolve_allowed_origins(
            {"CORS_ALLOWED_ORIGINS": "https://example.com"}
        )
        self.assertEqual(origins, ["https://example.com"])

    def test_empty_origin_list_falls_back_to_defaults(self):
        self.assertEqual(
            resolve_allowed_origins({"ALLOWED_ORIGINS": " , "}),
            DEFAULT_ALLOWED_ORIGINS,
        )

    def test_wildcard_origin_is_rejected(self):
        with self.assertRaises(CorsConfigurationError):
            resolve_allowed_origins({"ALLOWED_ORIGINS": "*"})

    def test_wildcard_among_explicit_origins_is_rejected(self):
        with self.assertRaises(CorsConfigurationError):
            resolve_allowed_origins({"ALLOWED_ORIGINS": "https://example.com,*"})


if __name__ == "__main__":
    unittest.main()

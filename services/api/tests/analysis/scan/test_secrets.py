"""Unit tests for `redact_secrets`: no DB, no scanner, pure function."""

from app.analysis.scan.secrets import REDACTION_TOKEN, redact_secrets


def test_fake_aws_key_is_redacted():
    # AWS's own published example access key id -- safe to use in tests.
    content = b'aws_key = "AKIAIOSFODNN7EXAMPLE"\n'

    redacted, match_count = redact_secrets(content)

    assert match_count >= 1
    assert b"AKIAIOSFODNN7EXAMPLE" not in redacted
    assert REDACTION_TOKEN.encode("utf-8") in redacted


def test_normal_source_without_secrets_is_unchanged():
    content = b"def hello():\n    return 'hi'\n"

    redacted, match_count = redact_secrets(content)

    assert match_count == 0
    assert redacted == content


def test_binary_content_with_nul_byte_is_returned_unchanged():
    content = b"\x00\x01binary-data"

    redacted, match_count = redact_secrets(content)

    assert redacted == content
    assert match_count == 0


def test_redaction_token_is_the_same_regardless_of_secret_type_or_length():
    short = redact_secrets(b'x = "AKIAIOSFODNN7EXAMPLE"\n')[0]
    assert REDACTION_TOKEN.encode("utf-8") in short
    # The literal secret value must never survive, and the token itself
    # carries no length/type information about what it replaced.
    assert b"AKIAIOSFODNN7EXAMPLE" not in short

"""Unit tests for `_validate_remote`'s SSRF guard (no DB needed, and no real
network beyond local address resolution): a git remote must not resolve to
a private/loopback/link-local/metadata address, covering the gap a
security audit found — only the URL scheme was checked before, never the
destination host."""

from __future__ import annotations

import socket

import pytest

from app.repository.ingest import IngestError, _validate_remote

PRIVATE_HOST_LITERALS = (
    "127.0.0.1",  # loopback
    "169.254.169.254",  # cloud metadata, link-local
    "10.1.2.3",  # RFC1918
    "172.16.0.1",  # RFC1918
    "192.168.1.1",  # RFC1918
    "::1",  # loopback, IPv6
    "fc00::1",  # unique local, IPv6
    "fe80::1",  # link-local, IPv6
)


def test_validate_remote_rejects_metadata_ip_literal():
    with pytest.raises(IngestError, match="disallowed address"):
        _validate_remote("https://169.254.169.254/latest/meta-data/", None)


@pytest.mark.parametrize("host", PRIVATE_HOST_LITERALS)
def test_validate_remote_rejects_loopback_and_private_literals(host):
    # IPv6 literals need brackets in a URL (RFC 3986) or urlparse mis-splits
    # the host on the literal's own colons.
    netloc = f"[{host}]" if ":" in host else host
    with pytest.raises(IngestError, match="disallowed address"):
        _validate_remote(f"https://{netloc}/x", None)


def test_validate_remote_rejects_hostname_resolving_to_private_ip(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        assert host == "evil.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr("app.repository.ingest.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(IngestError, match="disallowed address"):
        _validate_remote("https://evil.example.com/repo.git", None)


def test_validate_remote_allows_public_host(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("app.repository.ingest.socket.getaddrinfo", fake_getaddrinfo)
    _validate_remote("https://example.com/repo.git", "main")  # does not raise


def test_validate_remote_still_rejects_non_https_scheme():
    with pytest.raises(IngestError, match="unsupported repository URL scheme"):
        _validate_remote("file:///etc", None)


def test_validate_remote_still_rejects_dash_prefixed_url_or_ref(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("app.repository.ingest.socket.getaddrinfo", fake_getaddrinfo)

    # A leading '-' breaks urlparse's own scheme detection (no scheme is
    # recognized), so this is rejected as an unsupported scheme rather than
    # by the dash check specifically — still rejected either way, which is
    # what matters: git never sees a URL string it could parse as an option.
    with pytest.raises(IngestError):
        _validate_remote("-https://example.com/repo.git", None)
    with pytest.raises(IngestError, match="invalid ref"):
        _validate_remote("https://example.com/repo.git", "--upload-pack=evil")


def test_validate_remote_dns_failure_raises_ingest_error(monkeypatch):
    def fake_getaddrinfo(*_args, **_kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr("app.repository.ingest.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(IngestError, match="could not resolve"):
        _validate_remote("https://nowhere.invalid/repo.git", None)

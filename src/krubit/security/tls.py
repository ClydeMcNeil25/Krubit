"""Validated TLS context backed by the operating system trust store."""

from __future__ import annotations

import ssl

import truststore


def system_ssl_context() -> ssl.SSLContext:
    """Use native roots while tolerating legacy non-critical CA extensions."""

    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context

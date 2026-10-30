"""TLS settings for outbound HTTP to brokers and feeds.

Certificate verification is never disabled. On a network whose proxy re-signs TLS with a corporate
root certificate, point MP_CA_BUNDLE at that root (PEM, from your IT team) or set
MP_TLS_TRUSTSTORE=true to use the operating system's certificate store.
"""

from __future__ import annotations

import os
import ssl

LOCAL_HOSTS = ("127.0.0.1", "localhost")


def allowed_hosts() -> list[str]:
    """Host names the gateway and MCP servers answer to (DNS-rebinding protection): this machine,
    plus MP_ALLOWED_HOSTS (comma-separated, e.g. a Tailscale name) when you open it up deliberately."""
    extra = [h.strip().lower() for h in os.environ.get("MP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return [*LOCAL_HOSTS, *extra]


def ssl_verify() -> bool | str | ssl.SSLContext:
    bundle = os.environ.get("MP_CA_BUNDLE")
    if bundle:
        if not os.path.exists(bundle):
            raise FileNotFoundError(f"MP_CA_BUNDLE={bundle} does not exist")
        return bundle
    if os.environ.get("MP_TLS_TRUSTSTORE", "").lower() in ("1", "true", "yes"):
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return True

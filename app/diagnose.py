"""Outbound connectivity probes.

The SDK reports every network failure as "Connection error." A DNS failure, a
missing CA bundle, an IPv6 address with no route and a dropped socket are four
different problems with four different fixes and one identical message.

These probes separate them without making a paid call, so the answer costs
nothing and can be read from outside the access gate. They deliberately return
class names and short reasons only — never the API key, never a token, never
anything about the user's data.
"""

from __future__ import annotations

import os
import re
import socket
import ssl
from typing import Any

HOST = "api.anthropic.com"
PORT = 443
TIMEOUT = 6.0


SECRET = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def redact(text: str) -> str:
    """Scrub anything secret-shaped out of diagnostic text.

    Written after this file leaked an API key: httpx puts the offending header
    *value* in its exception message, the probe printed the exception, and the
    probe answers without a cookie. An exception message is untrusted output,
    not a description of a failure — it can contain whatever the failing call
    was holding.
    """
    live = os.getenv("ANTHROPIC_API_KEY") or ""
    if live:
        text = text.replace(live.strip(), "<redacted>").replace(live, "<redacted>")
    return SECRET.sub("<redacted>", text)


def _err(e: BaseException) -> str:
    return redact(f"{type(e).__name__}: {str(e)[:160]}")


def probe() -> dict[str, Any]:
    out: dict[str, Any] = {"host": HOST}

    # 1. DNS. Report the families separately: a container that gets an AAAA
    #    record but has no IPv6 route fails at connect, not here, and that is
    #    the one failure people misread as a firewall.
    try:
        infos = socket.getaddrinfo(HOST, PORT, proto=socket.IPPROTO_TCP)
        out["dns"] = "ok"
        out["v4"] = sum(1 for i in infos if i[0] == socket.AF_INET)
        out["v6"] = sum(1 for i in infos if i[0] == socket.AF_INET6)
    except Exception as e:
        out["dns"] = _err(e)
        return out

    # 2. TCP, per family, so "IPv6 unreachable but IPv4 fine" is visible rather
    #    than hidden behind whichever one the resolver happened to return first.
    for family, label in ((socket.AF_INET, "tcp_v4"), (socket.AF_INET6, "tcp_v6")):
        addr = next((i[4] for i in infos if i[0] == family), None)
        if addr is None:
            out[label] = "no address"
            continue
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        try:
            s.connect(addr)
            out[label] = "ok"
        except Exception as e:
            out[label] = _err(e)
        finally:
            s.close()

    # 3. TLS. This is where a missing or stale CA bundle shows up, and it is the
    #    failure that looks least like itself from the outside.
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((HOST, PORT), timeout=TIMEOUT) as raw:
            with ctx.wrap_socket(raw, server_hostname=HOST) as tls:
                out["tls"] = "ok"
                out["tls_version"] = tls.version()
    except Exception as e:
        out["tls"] = _err(e)

    # 4. Where OpenSSL looks for roots, and whether anything is actually there.
    paths = ssl.get_default_verify_paths()
    out["ca_file"] = bool(paths.cafile and os.path.exists(paths.cafile))
    out["ca_dir"] = bool(paths.capath and os.path.isdir(paths.capath))

    # 5. truststore replaces OpenSSL's roots with the OS store. httpx2 pulls it
    #    in, so if it is installed but the OS store is empty, TLS above can pass
    #    while the SDK's own client fails.
    try:
        import truststore

        tctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with socket.create_connection((HOST, PORT), timeout=TIMEOUT) as raw:
            with tctx.wrap_socket(raw, server_hostname=HOST):
                out["truststore"] = "ok"
    except ImportError:
        out["truststore"] = "not installed"
    except Exception as e:
        out["truststore"] = _err(e)

    # Booleans only. The value never leaves the process.
    out["key_set"] = bool(os.getenv("ANTHROPIC_API_KEY"))

    # 6. The real stack. Everything above tests sockets the way this file opens
    #    them, which is not necessarily the way httpx2 opens them — and the
    #    difference between those two is exactly what is being hunted. models.list
    #    is a GET that consumes no tokens, so this costs nothing and still
    #    exercises the client, the transport and the TLS path the SDK uses.
    try:
        import anthropic

        from app.ai.client import _why

        models = anthropic.Anthropic(timeout=20.0, max_retries=0).models.list(limit=1)
        out["sdk"] = f"ok ({len(models.data)} model listed)"
    except Exception as e:  # noqa: BLE001 - the class is the finding
        from app.ai.client import _why

        out["sdk"] = redact(_why(e, depth=6))[:400]

    return out

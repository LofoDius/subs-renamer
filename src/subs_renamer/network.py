"""Explicit SOCKS routing, independent of proxy environment variables."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


def normalize_proxy(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" not in value:
        value = "socks5://" + value
    try:
        parsed = urlsplit(value)
        port, host = parsed.port, parsed.hostname
        if parsed.scheme not in {"socks5", "socks5h"}:
            raise ValueError
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise ValueError
        if not host or port is None or not 1 <= port <= 65535:
            raise ValueError
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError
        if ":" in host:
            host = f"[{ipaddress.IPv6Address(host)}]"
        else:
            host = host.encode("idna").decode("ascii").lower()
            if not re.fullmatch(r"[a-z0-9.-]+", host):
                raise ValueError
        return f"socks5://{host}:{port}"
    except (ValueError, UnicodeError):
        raise ValueError(
            "Укажите SOCKS5 без авторизации: socks5://host:port или host:port. "
            "Логин, пароль, путь и параметры не поддерживаются."
        ) from None


def gemini_http_options(proxy: str = ""):
    from google.genai import types

    address = normalize_proxy(proxy)
    arguments = {"trust_env": False}
    if address:
        arguments["proxy"] = address
    return types.HttpOptions(
        timeout=90000,
        retry_options=types.HttpRetryOptions(attempts=1),
        client_args=arguments.copy(),
        async_client_args=arguments.copy(),
    )


async def close_failed_stream(request):
    """Close even a half-established connection when HTTPX is cancelled.

    httpcore 1.0's pool does not yet own streams during SOCKS/TLS negotiation.
    Its public trace extension lets us release these sockets on failure too.
    No request headers, bodies or connection details are logged.
    """
    stream = None

    async def trace(event, info):
        nonlocal stream
        if event.endswith((".connect_tcp.complete", ".start_tls.complete")):
            stream = info["return_value"]
        elif event.endswith(".failed") and stream is not None:
            closing, stream = stream, None
            await closing.aclose()

    request.extensions["trace"] = trace

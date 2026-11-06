import json
import socket
import struct
import threading

import pytest

from subs_renamer import gemini
from subs_renamer.gemini import Extracted, GeminiAnalyzer
from subs_renamer.network import gemini_http_options, normalize_proxy


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        (" localhost:2080 ", "socks5://localhost:2080"),
        ("socks5://127.0.0.1:1080/", "socks5://127.0.0.1:1080"),
        ("socks5h://host:1080", "socks5://host:1080"),
        ("[::1]:2080", "socks5://[::1]:2080"),
    ],
)
def test_proxy_normalization(value, expected):
    assert normalize_proxy(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://host:8080",
        "socks4://host:1080",
        "socks5://user:pw@host:1080",
        "host",
        "host:0",
        "host:65536",
        "host:abc",
        "socks5://:1080",
        "host:1080/path",
        "host:1080?x=y",
        "host:1080#fragment",
        "bad host:1080",
    ],
)
def test_invalid_proxy_is_rejected_without_echoing_input(value):
    with pytest.raises(ValueError) as error:
        normalize_proxy(value)
    assert "user:pw" not in str(error.value)


def test_proxy_applies_to_both_clients_and_disables_environment():
    options = gemini_http_options("localhost:2080")
    assert (
        options.client_args
        == options.async_client_args
        == {"proxy": "socks5://localhost:2080", "trust_env": False}
    )
    assert gemini_http_options().client_args == {"trust_env": False}


def receive_exact(connection, length):
    data = b""
    while len(data) < length:
        block = connection.recv(length - len(data))
        if not block:
            raise EOFError("Incomplete test SOCKS request")
        data += block
    return data


def test_real_sdk_uses_unauthenticated_socks_and_remote_dns(tmp_path, monkeypatch):
    # All traffic stays on loopback. The SOCKS endpoint emulates the upstream response.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)
    observed, failures = {}, []

    def server():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(10)
                version, count = receive_exact(connection, 2)
                observed["methods"] = receive_exact(connection, count)
                assert version == 5
                connection.sendall(b"\x05\x00")
                version, command, _, kind = receive_exact(connection, 4)
                assert version == 5 and command == 1 and kind == 3
                length = receive_exact(connection, 1)[0]
                observed["host"] = receive_exact(connection, length).decode()
                observed["port"] = struct.unpack("!H", receive_exact(connection, 2))[0]
                connection.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
                request = b""
                while b"\r\n\r\n" not in request:
                    request += connection.recv(65536)
                headers, body = request.split(b"\r\n\r\n", 1)
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                body += receive_exact(connection, length - len(body))
                observed["body"] = json.loads(body)
                response = json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": '{"mentions": []}'}]},
                                "finishReason": "STOP",
                            }
                        ]
                    }
                ).encode()
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                    + f"Content-Length: {len(response)}\r\n\r\n".encode()
                    + response
                )
        except Exception as error:
            failures.append(error)
        finally:
            listener.close()

    worker = threading.Thread(target=server, daemon=True)
    worker.start()
    original = gemini.gemini_http_options

    def options(proxy):
        result = original(proxy)
        result.base_url = "http://gemini-test.invalid"
        return result

    monkeypatch.setattr(gemini, "gemini_http_options", options)
    monkeypatch.setenv("ALL_PROXY", "http://invalid-environment-proxy.invalid:1")
    monkeypatch.setenv("NO_PROXY", "*")
    client = GeminiAnalyzer(
        "test-model",
        proxy=f"127.0.0.1:{listener.getsockname()[1]}",
        api_key="synthetic-test-key",
        cache=tmp_path,
    )
    try:
        assert client.request("test", {"subtitles": []}, Extracted, threading.Event()).mentions == []
    finally:
        client.close()
        worker.join(timeout=10)
    assert not failures and not worker.is_alive()
    assert observed["methods"] == b"\x00"
    assert observed["host"] == "gemini-test.invalid" and observed["port"] == 80


def test_unavailable_proxy_fails_without_direct_fallback(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    client = GeminiAnalyzer(
        "test-model", proxy=f"127.0.0.1:{port}", api_key="synthetic-test-key", cache=tmp_path
    )
    try:
        # Raw SDK connection is used here to avoid application retry waits.
        import httpx

        with pytest.raises(httpx.TransportError):
            client.client.models.generate_content(model="test-model", contents="synthetic")
    finally:
        client.close()

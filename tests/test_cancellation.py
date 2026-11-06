"""Exercise the real SDK against stalled sockets, not a cooperative transport mock."""

import os
import socket
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication

from subs_renamer import gemini, gui
from subs_renamer.models import Project
from subs_renamer.subtitles import parse


@pytest.mark.parametrize("stage", ["response", "socks_handshake", "tls_handshake"])
def test_cancel_stalled_network_restores_ui_immediately(tmp_path, monkeypatch, stage, record_property):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        gui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    received, disconnected, release = threading.Event(), threading.Event(), threading.Event()
    server_errors = []

    def stalled_server():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(0.05)
                while not release.is_set():
                    try:
                        data = connection.recv(65536)
                    except TimeoutError:
                        continue
                    except ConnectionResetError:
                        disconnected.set()
                        break
                    if not data:
                        disconnected.set()
                        break
                    received.set()
                    # Intentionally never send an HTTP response, TLS or SOCKS handshake.
        except Exception as error:
            server_errors.append(error)
        finally:
            listener.close()

    server = threading.Thread(target=stalled_server, daemon=True)
    server.start()
    original_options = gemini.gemini_http_options

    def options(proxy):
        result = original_options(proxy)
        result.base_url = (
            "https://gemini-test.invalid"
            if stage == "socks_handshake"
            else f"{'https' if stage == 'tls_handshake' else 'http'}://127.0.0.1:{port}"
        )
        return result

    monkeypatch.setattr(gemini, "gemini_http_options", options)

    class LocalAnalyzer(gemini.GeminiAnalyzer):
        def __init__(self, model, *, proxy=""):
            super().__init__(model, proxy=proxy, api_key="synthetic-test-key", cache=tmp_path / "cache")

    monkeypatch.setattr(gui, "GeminiAnalyzer", LocalAnalyzer)
    window = gui.Window()
    window.project = Project(
        documents=[parse("synthetic.srt", content=("1\n00:00:01,000 --> 00:00:02,000\nРэн — это Лис.\n"))]
    )
    window.pages.setCurrentWidget(window.workspace)
    window.settings.setValue(
        "gemini_proxy", f"socks5://127.0.0.1:{port}" if stage == "socks_handshake" else ""
    )
    errors, ticks = [], []
    monkeypatch.setattr(window, "error", errors.append)
    timer = QTimer(window)
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        window.analyze_button.click()
        deadline = time.monotonic() + 5
        while not received.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert received.is_set(), f"SDK did not reach stalled {stage}: {server_errors} {errors}"
        assert window.analyze_button.text() == "Отменить"
        tick_count = len(ticks)
        until = time.monotonic() + 0.08
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(0.005)
        assert len(ticks) >= tick_count + 3, "UI event loop stalled while waiting for the SDK"

        start = time.monotonic()
        window.analyze_button.click()
        assert time.monotonic() - start < 0.1, "Cancel slot blocked the UI thread"
        while window.worker is not None and time.monotonic() - start < 1:
            app.processEvents()
            time.sleep(0.005)
        elapsed = time.monotonic() - start
        record_property(f"cancel_{stage}_ms", round(elapsed * 1000, 1))
        print(f"{stage}: cancel-to-ready {elapsed * 1000:.1f} ms")
        assert window.worker is None, "Cancelled request still keeps the interface disabled"
        assert window.body.isEnabled() and window.change_folder_button.isEnabled()
        assert window.analyze_button.text() == "Найти имена" and window.analyze_button.isEnabled()
        assert disconnected.wait(0.5), "Cancelled request socket was left running"
        assert not errors and not window.project.groups
        assert not list((tmp_path / "cache").glob("*.json")), "A cancelled response entered the cache"

        results = []
        window.task(lambda cancel, progress: "new task", results.append)
        deadline = time.monotonic() + 1
        while window.worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert results == ["new task"] and not errors
    finally:
        timer.stop()
        release.set()
        if window.worker:
            window.worker.cancel.set()
            deadline = time.monotonic() + 5
            while window.worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
        server.join(timeout=5)
        window.dirty = False
        window.close()
    assert not server_errors

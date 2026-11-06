"""Headless in-process Qt tests for worker sequencing and editing state."""

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLineEdit, QPushButton

from subs_renamer import gui
from subs_renamer.models import Alias, CharacterGroup


def test_gemini_settings_refresh_uses_edited_proxy(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        gui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    calls = []

    def models(proxy):
        calls.append(proxy)
        return ["gemini-a", "gemini-b"]

    monkeypatch.setattr(gui, "available_models", models)
    window = gui.Window()
    failures = []
    timer = QTimer()

    def check():
        dialog = window.findChild(QDialog)
        if dialog is None:
            return
        refresh = dialog.findChild(QPushButton, "refreshModels")
        if not calls or refresh is None or not refresh.isEnabled():
            return
        try:
            combo = dialog.findChild(QComboBox)
            assert not combo.isEditable()
            assert combo.count() == 2
            if len(calls) == 1:
                combo.setCurrentText("gemini-b")
                dialog.findChild(QLineEdit).setText("localhost:2080")
                refresh.click()
                return
            assert calls == ["", "socks5://localhost:2080"]
            assert combo.currentText() == "gemini-b"
        except AssertionError as error:
            failures.append(error)
        timer.stop()
        dialog.accept()

    timer.timeout.connect(check)
    timer.start(10)
    QTimer.singleShot(5000, lambda: window.findChild(QDialog).reject() if timer.isActive() else None)
    window.configure()
    timer.stop()
    assert not failures
    assert calls == ["", "socks5://localhost:2080"]
    assert window.project.model == "gemini-b"
    window.dirty = False
    window.close()
    app.processEvents()


def wait_for_worker(app, window):
    deadline = time.monotonic() + 10
    while window.worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window.worker is None


def test_mkv_import_reuses_selected_id_and_skips_missing_tracks(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        gui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    window = gui.Window()

    def track(id_, language="rus", codec="S_TEXT/UTF8"):
        return {"id": id_, "type": "subtitles", "properties": {"language": language, "codec_id": codec}}

    containers = [
        ("first.mkv", {"tracks": [track(0), track(7)]}),
        ("second.mkv", {"tracks": [track(3), track(7, "eng")]}),
        ("missing.mkv", {"tracks": [track(3)]}),
        ("image.mkv", {"tracks": [track(7, codec="S_HDMV/PGS"), track(3)]}),
    ]
    prompts, extracted, results = [], [], []

    def choose(*args):
        prompts.append(args)
        return args[3][1], True

    monkeypatch.setattr(gui.QInputDialog, "getItem", choose)
    monkeypatch.setattr(gui.MKVTools, "extract", lambda self, path, info, id_, cancel: extracted.append((path, id_)))
    monkeypatch.setattr(window, "task", lambda work, done: results.append(work(threading.Event(), lambda *a: None)))
    try:
        window.pending_import = ([], containers, [], False, "")
        window.extract_pending()
        assert len(prompts) == 1
        assert extracted == [("first.mkv", 7), ("second.mkv", 7)]
        assert len(results[0][1]) == 2
        assert all("ID 7" in error for error in results[0][1])

        # A new import gets its own selection, including ID zero.
        monkeypatch.setattr(gui.QInputDialog, "getItem", lambda *args: (args[3][0], True))
        window.pending_import = ([], containers[:1] * 2, [], False, "")
        window.extract_pending()
        assert extracted[-2:] == [("first.mkv", 0), ("first.mkv", 0)]
    finally:
        window.dirty = False
        window.close()
        app.processEvents()


def test_generate_name_forms_success_failure_and_cancel(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        gui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    window = gui.Window()
    group = CharacterGroup(label="Рэн", aliases=[Alias(text="Рэн"), Alias(text="Рэна")])
    window.project.groups = [group]
    window.current_group = group.id
    window.refresh()
    errors = []
    monkeypatch.setattr(window, "error", errors.append)
    monkeypatch.setattr(gui.GeminiAnalyzer, "__init__", lambda *args, **kwargs: None)
    monkeypatch.setattr(gui.GeminiAnalyzer, "close", lambda *args: None)
    mode = ["success"]

    def generate(self, project, selected, cancel):
        assert selected.replacement_name == "Иван"
        assert selected is not group
        if mode[0] == "failure":
            raise RuntimeError("Synthetic failure")
        if mode[0] == "cancel":
            cancel.wait(5)
            raise InterruptedError("Генерация отменена")
        return dict(zip([a.id for a in selected.aliases], ["Иван", "Ивана"]))

    monkeypatch.setattr(gui.GeminiAnalyzer, "generate_replacements", generate)
    try:
        assert not window.generate_button.isEnabled()
        window.replacement_name.setText("Иван")
        assert group.replacement_name == "Иван"
        window.generate_button.click()
        wait_for_worker(app, window)
        assert [a.replacement for a in group.aliases] == ["Иван", "Ивана"]
        assert window.aliases.item(1, 1).text() == "Ивана"
        window.aliases.item(1, 1).setText("Ручная правка")
        for state in ["failure", "cancel"]:
            mode[0] = state
            window.generate_button.click()
            if state == "cancel":
                window.cancel_task()
            wait_for_worker(app, window)
            assert group.aliases[1].replacement == "Ручная правка"
        assert errors == ["Synthetic failure"]
    finally:
        wait_for_worker(app, window)
        window.dirty = False
        window.close()


def test_fully_blocked_files_are_red_and_allow_next_step(tmp_path, monkeypatch):
    from subs_renamer.subtitles import parse

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        gui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    window = gui.Window()
    doc = parse("blocked.srt", content="1\n00:00:01,000 --> 00:00:02,000\nТекст\n")
    window.project.documents = [doc]
    errors = []
    monkeypatch.setattr(window, "error", errors.append)
    monkeypatch.setattr(gui.GeminiAnalyzer, "__init__", lambda *args, **kwargs: None)
    monkeypatch.setattr(gui.GeminiAnalyzer, "close", lambda *args: None)

    def analyze(self, project, *args):
        project.analysis_skipped_document_ids = {doc.id}
        return []

    monkeypatch.setattr(gui.GeminiAnalyzer, "analyze", analyze)
    window.analyze()
    wait_for_worker(app, window)
    assert not errors
    assert window.steps.currentIndex() == 1
    assert window.files.item(0, 0).foreground().color().name() == "#ff7777"
    assert "Gemini" in window.files.item(0, 0).toolTip()
    window.set_step(2)
    assert window.steps.currentIndex() == 2
    window.project.analysis_skipped_document_ids.clear()
    window.refresh()
    assert window.files.item(0, 0).foreground().color().name() != "#ff7777"
    window.dirty = False
    window.close()
    app.processEvents()


def test_import_worker_chain_and_preview(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        gui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    window = gui.Window()
    assert window.pages.currentWidget() is window.welcome
    assert window.steps.count() == 3
    assert window.steps.currentIndex() == 0
    errors = []
    monkeypatch.setattr(window, "error", errors.append)
    source = tmp_path / "nested" / "episode.srt"
    source.parent.mkdir()
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nРэн, привет!\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("Не субтитры", encoding="utf-8")
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    window.welcome_button.click()
    wait_for_worker(app, window)
    assert not errors and len(window.project.documents) == 1
    assert window.pages.currentWidget() is window.workspace
    assert window.body.isEnabled()
    group = CharacterGroup(label="Рэн", aliases=[Alias(text="Рэн", replacement="Рен")])
    window.project.groups = [group]
    window.current_group = group.id
    window.refresh()
    assert window.aliases.rowCount() == 1
    assert window.contexts.rowCount() == 1
    assert window.make_preview()
    assert window.review.rowCount() == 1
    assert "Рен" in window.review.item(0, 3).text()
    window.review.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert len(window.project.excluded) == 1
    assert window.make_preview()
    assert window.review.item(0, 0).checkState() == Qt.CheckState.Unchecked
    window.dirty = False
    window.close()


def test_welcome_folder_change_replaces_season_and_preserves_on_cancel_or_failure(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        gui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    window = gui.Window()
    errors, notices = [], []
    monkeypatch.setattr(window, "error", errors.append)
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *args: notices.append(args[-1]))
    selected = [""]
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *args: selected[0])
    first, second, empty, invalid = [tmp_path / name for name in ["first", "second", "empty", "invalid"]]
    for folder in [first, second, empty, invalid]:
        folder.mkdir()
    for folder, name in [(first, "Рэн"), (second, "Аки")]:
        (folder / "nested").mkdir()
        (folder / "nested" / "episode.srt").write_text(
            f"1\n00:00:01,000 --> 00:00:02,000\n{name}!\n", encoding="utf-8"
        )
    (invalid / "broken.srt").write_text("Не субтитры", encoding="utf-8")
    try:
        assert [b.text() for b in window.welcome.findChildren(QPushButton)] == ["Выбрать папку с субтитрами"]
        window.welcome_button.click()
        assert window.pages.currentWidget() is window.welcome and window.worker is None

        selected[0] = str(first)
        window.welcome_button.click()
        wait_for_worker(app, window)
        assert window.pages.currentWidget() is window.workspace
        original = window.project
        original.groups = [CharacterGroup(label="Рэн", aliases=[Alias(text="Рэн", replacement="Рен")])]
        original.excluded.add("synthetic-exclusion")
        window.project_path = "old-project.json"
        window.current_group = original.groups[0].id
        window.refresh()
        window.make_preview()
        assert window.review.rowCount() == 1

        selected[0] = ""
        window.change_folder_button.click()
        assert window.project is original and window.project_path == "old-project.json"

        selected[0] = str(second)
        monkeypatch.setattr(window, "can_discard", lambda: False)
        window.change_folder_button.click()
        assert window.project is original and window.worker is None
        monkeypatch.setattr(window, "can_discard", lambda: True)
        window.change_folder_button.click()
        assert window.project is original and not window.change_folder_button.isEnabled()
        wait_for_worker(app, window)
        assert window.project is not original
        assert len(window.project.documents) == 1 and "Аки" in window.project.documents[0].content
        assert not window.project.groups and not window.project.excluded
        assert window.project_path is None and window.current_group is None
        assert window.review.rowCount() == 0 and window.project.base_root == str(second.resolve())

        replacement = window.project
        selected[0] = str(empty)
        window.change_folder_button.click()
        assert notices and window.project is replacement
        selected[0] = str(invalid)
        window.change_folder_button.click()
        wait_for_worker(app, window)
        assert errors and window.project is replacement
        assert window.project.base_root == str(second.resolve())
        assert window.pages.currentWidget() is window.workspace
    finally:
        wait_for_worker(app, window)
        window.dirty = False
        window.close()


def test_analysis_button_cancels_and_recovers_after_all_outcomes(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        gui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    window = gui.Window()
    errors, results = [], []
    monkeypatch.setattr(window, "error", errors.append)
    release = threading.Event()

    def cancellable(cancel, progress):
        while not release.wait(0.01):
            if cancel.is_set():
                raise InterruptedError("Анализ отменён")
        return "completed"

    monkeypatch.setattr(window, "analyze", lambda: window.task(cancellable, results.append))
    try:
        # The button remains accessible while the main controls are disabled.
        window.analyze_button.click()
        assert window.analyze_button.text() == "Отменить"
        assert window.analyze_button.isEnabled() and not window.body.isEnabled()
        window.analyze_button.click()
        assert window.worker.cancel.is_set() and not window.analyze_button.isEnabled()
        wait_for_worker(app, window)
        assert window.analyze_button.text() == "Найти имена"
        assert window.analyze_button.isEnabled() and window.body.isEnabled()
        assert not errors and not results

        # A successful subsequent run restores the same action and delivers the result.
        release.set()
        window.analyze_button.click()
        wait_for_worker(app, window)
        assert results == ["completed"] and window.analyze_button.text() == "Найти имена"

        def failing(cancel, progress):
            raise RuntimeError("Synthetic failure")

        window.task(failing, results.append)
        wait_for_worker(app, window)
        assert errors == ["Synthetic failure"]
        assert window.analyze_button.text() == "Найти имена" and window.analyze_button.isEnabled()
        assert window.body.isEnabled()
    finally:
        if window.worker:
            release.set()
            wait_for_worker(app, window)
        window.dirty = False
        window.close()


def test_sequential_flow_and_export_actions(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        gui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    source = tmp_path / "episode.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nРэн!\n", encoding="utf-8")
    window = gui.Window()
    monkeypatch.setattr(gui.GeminiAnalyzer, "__init__", lambda *args, **kwargs: None)
    monkeypatch.setattr(gui.GeminiAnalyzer, "close", lambda *args: None)
    monkeypatch.setattr(gui.GeminiAnalyzer, "analyze", lambda *args: [
        CharacterGroup(label="Рэн", aliases=[Alias(text="Рэн", replacement="Рен")])
    ])
    monkeypatch.setattr(window, "error", lambda message: (_ for _ in ()).throw(AssertionError(message)))
    notices = []
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *args: notices.append(args[-1]))
    monkeypatch.setattr(gui.QMessageBox, "question", lambda *args: gui.QMessageBox.StandardButton.Yes)
    try:
        window.import_paths([str(source)])
        wait_for_worker(app, window)
        assert window.steps.currentIndex() == 0
        assert window.analyze_button.text() == "Найти имена"
        window.analyze_button.click()
        wait_for_worker(app, window)
        assert window.steps.currentIndex() == 1
        assert window.analyze_button.text() == "Заменить имена"
        window.analyze_button.click()
        assert window.steps.currentIndex() == 2 and window.review.rowCount() == 1
        assert window.analyze_button.isHidden()
        assert not window.save_button.isHidden() and not window.replace_button.isHidden()
        window.back_button.click()
        assert window.steps.currentIndex() == 1
        window.analyze_button.click()
        destination = tmp_path / "output"
        monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *args: str(destination))
        window.save_button.click()
        wait_for_worker(app, window)
        assert "Рен" in (destination / "episode.renamed.srt").read_text(encoding="utf-8")
        assert "Рэн" in source.read_text(encoding="utf-8")
        assert window.analyze_button.isHidden()
        window.replace_button.click()
        wait_for_worker(app, window)
        assert "Рен" in source.read_text(encoding="utf-8")
        assert len(notices) == 2
    finally:
        wait_for_worker(app, window)
        window.dirty = False
        window.close()

def test_contexts_deduplicate_dialogue_and_toggle_all_matches(tmp_path, monkeypatch):
    from subs_renamer.subtitles import parse

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        gui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    window = gui.Window()
    doc = parse("episode.srt", content=(
        "1\n00:00:01,000 --> 00:00:02,000\nРэн, Рэн!\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nРэн, Рэн!\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\nРэн, Рэн!\n"
    ))
    group = CharacterGroup(label="Рэн", aliases=[Alias(text="Рэн", replacement="Рен")])
    window.project.documents = [doc]
    window.project.groups = [group]
    window.current_group = group.id
    window.refresh()
    assert len(window.found) == 6
    assert window.contexts.rowCount() == 2
    ids = {o.id for o in window.found if o.cue_id in {c.id for c in doc.cues[:2]}}
    window.contexts.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert window.project.excluded == ids
    window.show_group()
    assert window.contexts.item(0, 0).checkState() == Qt.CheckState.Unchecked
    window.contexts.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert not window.project.excluded
    window.project.excluded.add(next(iter(ids)))
    window.show_group()
    assert window.contexts.item(0, 0).checkState() == Qt.CheckState.PartiallyChecked
    window.contexts.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert not window.project.excluded
    window.dirty = False
    window.close()
    app.processEvents()


def test_character_sorting_preserves_selection_and_refresh_order(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        gui, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    window = gui.Window()
    groups = [CharacterGroup(label=name) for name in ["Борис", "анна", "Яна"]]
    counts = dict(zip([group.id for group in groups], [2, 10, 1]))
    monkeypatch.setattr(gui, "frequency", lambda *_: counts)
    window.project.groups = groups
    window.current_group = groups[0].id

    def names():
        return [window.groups.topLevelItem(i).text(0) for i in range(3)]

    try:
        window.refresh()
        assert names() == ["анна", "Борис", "Яна"]
        window.groups.sortItems(0, Qt.SortOrder.AscendingOrder)
        assert names() == ["анна", "Борис", "Яна"]
        window.groups.sortItems(0, Qt.SortOrder.DescendingOrder)
        assert names() == ["Яна", "Борис", "анна"]
        window.refresh()
        assert names() == ["Яна", "Борис", "анна"]
        assert window.selected_group().id == groups[0].id
        assert window.groups.currentItem().text(0) == "Борис"
        window.groups.sortItems(1, Qt.SortOrder.AscendingOrder)
        assert names() == ["Яна", "Борис", "анна"]
        window.groups.sortItems(1, Qt.SortOrder.DescendingOrder)
        assert names() == ["анна", "Борис", "Яна"]
    finally:
        window.dirty = False
        window.close()
        app.processEvents()

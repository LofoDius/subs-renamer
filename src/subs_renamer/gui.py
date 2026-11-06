from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .engine import export, frequency, occurrences, preview, replace_originals
from .gemini import RECOMMENDED_MODELS, GeminiAnalyzer, available_models
from .mkv import MKVTools
from .models import Alias, CharacterGroup, Project
from .network import normalize_proxy
from .subtitles import parse, visible

SUPPORTED = {".ass", ".srt", ".crt", ".mkv"}


class Worker(QThread):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, function):
        super().__init__()
        self.function = function
        self.cancel = threading.Event()

    def run(self):
        try:
            self.result.emit(self.function(self.cancel, self.progress.emit))
        except InterruptedError as error:
            self.failed.emit(str(error))
        except Exception as error:
            self.failed.emit(str(error))


def item(text: str, *, editable=False):
    result = QTableWidgetItem(text)
    if not editable:
        result.setFlags(result.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return result


class CharacterTreeItem(QTreeWidgetItem):
    def __lt__(self, other):
        column = self.treeWidget().sortColumn()
        if column == 1:
            return int(self.text(column)) < int(other.text(column))
        return self.text(column).casefold().replace("ё", "е") < other.text(column).casefold().replace("ё", "е")


class ColumnTable(QTableWidget):
    """Keep column boundaries visible below the last row, including an empty table."""

    def paintEvent(self, event):
        super().paintEvent(event)
        header = self.horizontalHeader()
        columns = [
            header.logicalIndex(i)
            for i in range(header.count())
            if not header.isSectionHidden(header.logicalIndex(i))
        ]
        painter = QPainter(self.viewport())
        painter.setClipRect(event.rect())
        painter.setPen(QColor("#50627b"))
        for column in columns[:-1]:
            x = header.sectionViewportPosition(column) + header.sectionSize(column) - 1
            if 0 <= x < self.viewport().width():
                painter.drawLine(x, 0, x, self.viewport().height() - 1)
        painter.end()


def table(headers):
    widget = ColumnTable(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setAlternatingRowColors(True)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.verticalHeader().hide()
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    widget.horizontalHeader().setStretchLastSection(True)
    widget.horizontalHeader().sectionResized.connect(lambda *_: widget.viewport().update())
    widget.horizontalScrollBar().valueChanged.connect(lambda _: widget.viewport().update())
    return widget


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Subs Renamer")
        self.resize(1360, 900)
        self.setMinimumSize(1000, 700)
        self.setAcceptDrops(True)
        self.settings = QSettings("SubsRenamer", "SubsRenamer")
        self.project = Project(model=self.settings.value("model", "gemini-3.8-flash"))
        self.project_path = None
        self.dirty = False
        self.worker = None
        self.current_group = None
        self.updating = False
        self.found = []
        self.pages = QStackedWidget()
        self.setCentralWidget(self.pages)
        self.welcome = QWidget()
        welcome_layout = QVBoxLayout(self.welcome)
        welcome_layout.addStretch()
        self.welcome_button = QPushButton("Выбрать папку с субтитрами")
        self.welcome_button.setObjectName("primary")
        self.welcome_button.setMinimumSize(320, 52)
        self.welcome_button.clicked.connect(self.change_folder)
        welcome_layout.addWidget(self.welcome_button, alignment=Qt.AlignmentFlag.AlignCenter)
        welcome_layout.addStretch()
        self.pages.addWidget(self.welcome)
        self.workspace = QWidget()
        self.pages.addWidget(self.workspace)
        root = self.workspace
        layout = QVBoxLayout(root)
        layout.setContentsMargins(26, 22, 26, 18)
        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 8)
        self.steps = QStackedWidget()
        body_layout.addWidget(self.steps)
        layout.addWidget(self.body, 1)
        # Files
        files_page = QWidget()
        fl = QVBoxLayout(files_page)
        self.files = table(["Файл", "Формат / дорожка", "Реплик", "Кодировка"])
        self.files.setColumnWidth(0, 650)
        self.files.setColumnWidth(1, 220)
        self.files.setColumnWidth(2, 100)
        fl.addWidget(self.files)
        remove = QPushButton("Убрать выбранные файлы")
        remove.clicked.connect(self.remove_files)
        fl.addWidget(remove, alignment=Qt.AlignmentFlag.AlignLeft)
        self.steps.addWidget(files_page)
        # Character cards
        group_page = QWidget()
        gl = QVBoxLayout(group_page)
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        self.groups = QTreeWidget()
        self.groups.setHeaderLabels(["Персонаж / обращение", "Упоминаний"])
        self.groups.headerItem().setToolTip(0, "Нажмите для сортировки по имени; повторное нажатие меняет порядок")
        self.groups.headerItem().setToolTip(1, "Нажмите для сортировки по числу упоминаний; повторное нажатие меняет порядок")
        self.groups.setSortingEnabled(True)
        self.groups.sortItems(1, Qt.SortOrder.DescendingOrder)
        self.groups.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.groups.setColumnWidth(0, 260)
        self.groups.itemSelectionChanged.connect(self.select_group)
        self.groups.itemChanged.connect(self.rename_group)
        ll.addWidget(self.groups)
        for label, callback in [
            ("Добавить персонажа", self.add_group),
            ("Объединить выбранные карточки", self.merge_groups),
            ("Удалить выбранные карточки", self.delete_groups),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            ll.addWidget(button)
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        self.group_title = QLabel("Выберите карточку слева")
        self.group_title.setObjectName("section")
        rl.addWidget(self.group_title)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Новое имя (именительный падеж):"))
        self.replacement_name = QLineEdit()
        self.replacement_name.setPlaceholderText("Например: Рен")
        self.replacement_name.textChanged.connect(self.replacement_name_changed)
        name_row.addWidget(self.replacement_name, 1)
        self.generate_button = QPushButton("Составить варианты с Gemini")
        self.generate_button.clicked.connect(self.generate_replacements)
        name_row.addWidget(self.generate_button)
        rl.addLayout(name_row)
        hint = QLabel("Введите одно новое имя, составьте варианты и проверьте замены в таблице ниже.")
        hint.setWordWrap(True)
        rl.addWidget(hint)
        self.aliases = table(["Найденное написание", "Желаемое написание", "Упоминаний"])
        self.aliases.setColumnWidth(0, 240)
        self.aliases.setColumnWidth(1, 240)
        self.aliases.itemChanged.connect(self.alias_changed)
        rl.addWidget(self.aliases, 2)
        buttons = QHBoxLayout()
        for label, callback in [
            ("Добавить вариант", self.add_alias),
            ("Отделить выбранные", self.split_aliases),
            ("Удалить вариант", self.delete_aliases),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        rl.addLayout(buttons)
        self.context_toggle = QPushButton("Контекст")
        self.context_toggle.setCheckable(True)
        self.context_toggle.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowRight))
        rl.addWidget(self.context_toggle)
        self.context_panel = QWidget()
        context_layout = QVBoxLayout(self.context_panel)
        context_layout.setContentsMargins(0, 0, 0, 0)
        context_hint = QLabel("Снимите галочку, если здесь слово обозначает что-то другое.")
        context_hint.setWordWrap(True)
        context_layout.addWidget(context_hint)
        self.contexts = table(["Вкл.", "Файл / время", "Реплика"])
        self.contexts.setColumnWidth(0, 50)
        self.contexts.setColumnWidth(1, 240)
        self.contexts.itemChanged.connect(self.context_changed)
        context_layout.addWidget(self.contexts, 1)
        self.evidence = QTextEdit()
        self.evidence.setReadOnly(True)
        self.evidence.setMaximumHeight(90)
        self.evidence.setPlaceholderText("Примеры, на которых Gemini предложил объединение")
        context_layout.addWidget(self.evidence)
        rl.addWidget(self.context_panel, 3)
        self.context_panel.hide()

        def toggle_context(expanded):
            self.context_panel.setVisible(expanded)
            arrow = QStyle.StandardPixmap.SP_ArrowDown if expanded else QStyle.StandardPixmap.SP_ArrowRight
            self.context_toggle.setIcon(self.style().standardIcon(arrow))

        self.context_toggle.toggled.connect(toggle_context)
        split.addWidget(right)
        split.setSizes([360, 850])
        gl.addWidget(split)
        self.steps.addWidget(group_page)
        # Review
        review = QWidget()
        vl = QVBoxLayout(review)
        row = QHBoxLayout()
        row.addWidget(QLabel("03  /  ПРОВЕРКА     Исключите нежелательные изменения перед сохранением."))
        row.addStretch()
        button = QPushButton("Обновить предпросмотр")
        button.clicked.connect(self.make_preview)
        row.addWidget(button)
        vl.addLayout(row)
        self.review = table(["Вкл.", "Файл / время", "До", "После / причина пропуска"])
        self.review.setColumnWidth(0, 50)
        self.review.setColumnWidth(1, 230)
        self.review.setColumnWidth(2, 400)
        self.review.itemChanged.connect(self.preview_changed)
        vl.addWidget(self.review)
        self.review_summary = QLabel("Задайте замены на шаге «Имена» и обновите предпросмотр.")
        vl.addWidget(self.review_summary)
        self.steps.addWidget(review)
        footer = QHBoxLayout()
        self.back_button = QPushButton()
        self.back_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        self.back_button.setToolTip("Назад")
        self.back_button.setAccessibleName("Назад")
        self.back_button.setFixedWidth(46)
        self.back_button.clicked.connect(lambda: self.set_step(self.steps.currentIndex() - 1))
        footer.addWidget(self.back_button)
        self.change_folder_button = QPushButton("Изменить папку")
        self.change_folder_button.clicked.connect(self.change_folder)
        footer.addWidget(self.change_folder_button)
        self.settings_button = QPushButton("Настройки Gemini")
        self.settings_button.clicked.connect(self.configure)
        footer.addWidget(self.settings_button)
        footer.addStretch(1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(260)
        self.progress_bar.hide()
        footer.addWidget(self.progress_bar)
        self.analyze_button = QPushButton("Найти имена")
        self.analyze_button.setObjectName("primary")
        self.analyze_button.clicked.connect(self.analyze_or_cancel)
        footer.addWidget(self.analyze_button)
        self.save_button = QPushButton("Сохранить отдельными файлами")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.export_files)
        footer.addWidget(self.save_button)
        self.replace_button = QPushButton("Заменить оригинальные файлы")
        self.replace_button.clicked.connect(self.replace_files)
        footer.addWidget(self.replace_button)
        self.steps.currentChanged.connect(self.update_actions)
        self.update_actions()
        layout.addLayout(footer)
        for name, shortcut, callback in [
            ("Открыть проект…", "Ctrl+O", self.open_project),
            ("Сохранить проект…", "Ctrl+S", self.save_project),
        ]:
            action = QAction(name, self)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
            self.addAction(action)
        self.refresh()

    def set_step(self, index):
        self.steps.setCurrentIndex(max(0, min(index, 2)))
        self.update_actions()

    def update_actions(self):
        step = self.steps.currentIndex()
        busy = self.worker is not None
        self.back_button.setVisible(step > 0)
        self.back_button.setEnabled(not busy)
        self.settings_button.setEnabled(not busy)
        self.analyze_button.setVisible(busy or step < 2)
        self.analyze_button.setText("Отменить" if busy else ("Найти имена" if step == 0 else "Заменить имена"))
        self.analyze_button.setEnabled(True)
        self.save_button.setVisible(step == 2 and not busy)
        self.replace_button.setVisible(step == 2 and not busy)

    def mark_dirty(self):
        self.dirty = True
        self.setWindowTitle("Subs Renamer *")

    def error(self, text):
        self.set_status(text)
        QMessageBox.warning(self, "Subs Renamer", text)

    def set_status(self, message):
        self.progress_bar.setToolTip(message)
        self.progress_bar.setAccessibleDescription(message)
        self.analyze_button.setToolTip(message)

    def task(self, function, done):
        if self.worker is not None:
            return
        self.body.setEnabled(False)
        self.change_folder_button.setEnabled(False)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.analyze_button.setText("Отменить")
        self.analyze_button.setEnabled(True)
        self.worker = Worker(function)
        self.update_actions()
        self.task_callback = done
        self.task_value = None
        self.task_error = None
        self.worker.progress.connect(self.on_progress)
        self.worker.result.connect(self.receive_result)
        self.worker.failed.connect(self.receive_error)
        self.worker.finished.connect(self.task_finished)
        self.worker.start()

    def receive_result(self, value):
        self.task_value = value

    def receive_error(self, message):
        self.task_error = message

    def task_finished(self):
        worker = self.worker
        self.worker = None
        self.body.setEnabled(True)
        self.change_folder_button.setEnabled(True)
        self.progress_bar.hide()
        self.update_actions()
        cancelled = worker is not None and worker.cancel.is_set()
        if worker:
            worker.deleteLater()
        if cancelled:
            self.set_status(self.task_error or "Операция отменена")
            if not self.project.documents:
                self.pages.setCurrentWidget(self.welcome)
        elif self.task_error is not None:
            if not self.project.documents:
                self.pages.setCurrentWidget(self.welcome)
            self.error(self.task_error)
        else:
            self.task_callback(self.task_value)

    def on_progress(self, current, total, message):
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(current)
        self.set_status(message)

    def analyze_or_cancel(self):
        if self.worker is not None:
            self.cancel_task()
        elif self.steps.currentIndex() == 0:
            self.analyze()
        else:
            self.make_preview()

    def cancel_task(self):
        if self.worker:
            self.worker.cancel.set()
            self.analyze_button.setEnabled(False)
            self.set_status("Отмена… Прерывание текущей операции.")

    def configure(self):
        if self.worker:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Настройки Gemini")
        dialog.resize(620, 150)
        form = QFormLayout(dialog)
        model = QComboBox()
        model.addItem(
            self.project.model if self.project.model in RECOMMENDED_MODELS else RECOMMENDED_MODELS[0]
        )
        proxy = QLineEdit(self.settings.value("gemini_proxy", ""))
        proxy.setPlaceholderText("socks5://localhost:2080")
        proxy.setClearButtonEnabled(True)
        model_row = QHBoxLayout()
        model_row.addWidget(model, 1)
        refresh = QPushButton()
        refresh.setObjectName("refreshModels")
        refresh.setIcon(dialog.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        refresh.setToolTip("Обновить список моделей")
        refresh.setAccessibleName("Обновить список моделей")
        model_row.addWidget(refresh)
        form.addRow("Модель Gemini", model_row)
        form.addRow("SOCKS5-прокси", proxy)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        state = {"closed": False, "worker": None}

        def loaded(names):
            if state["closed"]:
                return
            selected = model.currentText()
            model.clear()
            model.addItems(names)
            index = model.findText(selected)
            model.setCurrentIndex(max(index, 0))

        def failed(message):
            if not state["closed"]:
                QMessageBox.warning(dialog, "Модели Gemini", message)

        def finished():
            state["worker"] = None
            if not state["closed"]:
                refresh.setEnabled(True)
                refresh.setToolTip("Обновить список моделей")
                model.setEnabled(True)

        def reload_models():
            if state["closed"] or state["worker"] is not None:
                return
            try:
                address = normalize_proxy(proxy.text())
            except ValueError as error:
                QMessageBox.warning(dialog, "Некорректный прокси", str(error))
                return
            refresh.setEnabled(False)
            refresh.setToolTip("Загрузка списка моделей…")
            model.setEnabled(False)
            worker = Worker(lambda *_: available_models(address))
            worker.setParent(self)
            state["worker"] = worker
            worker.result.connect(loaded)
            worker.failed.connect(failed)
            worker.finished.connect(finished)
            worker.finished.connect(worker.deleteLater)
            worker.start()

        refresh.clicked.connect(reload_models)
        dialog.finished.connect(lambda _: state.update(closed=True))
        QTimer.singleShot(0, reload_models)

        def accept():
            try:
                normalized = normalize_proxy(proxy.text())
            except ValueError as error:
                QMessageBox.warning(dialog, "Некорректный прокси", str(error))
                return
            proxy.setText(normalized)
            dialog.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings.setValue("gemini_proxy", proxy.text())
            self.settings.remove("key_path")
            self.settings.remove("mkv_dir")
            self.project.model = model.currentText()
            self.settings.setValue("model", self.project.model)
            self.mark_dirty()

    def change_folder(self):
        if self.worker:
            return
        path = QFileDialog.getExistingDirectory(self, "Папка с субтитрами", self.project.base_root)
        if not path:
            return
        if (self.project.documents or self.project.groups) and not self.can_discard():
            return
        self.import_paths([path], replace=True, selected_folder=str(Path(path).resolve()))

    def import_paths(self, paths, *, replace=False, selected_folder=""):
        if self.worker or not paths:
            return
        expanded = []
        for path in map(Path, paths):
            expanded.extend(path.rglob("*") if path.is_dir() else [path])
        existing = (
            set()
            if replace
            else {str(Path(d.source_path).resolve()).casefold() for d in self.project.documents}
        )
        paths = sorted(
            {
                str(p.resolve())
                for p in expanded
                if p.is_file()
                and p.suffix.lower() in SUPPORTED
                and ".renamed." not in p.name.lower()
                and str(p.resolve()).casefold() not in existing
            }
        )
        if not paths:
            QMessageBox.information(
                self,
                "Субтитры не найдены",
                "В выбранной папке нет новых поддерживаемых ASS, SRT, CRT или MKV.",
            )
            return
        self.pages.setCurrentWidget(self.workspace)
        tools = MKVTools()

        def inspect(cancel, progress):
            docs, containers, errors = [], [], []
            for i, path in enumerate(paths):
                if cancel.is_set():
                    break
                progress(i, len(paths), f"Чтение: {Path(path).name}")
                try:
                    if Path(path).suffix.lower() == ".mkv":
                        containers.append((path, tools.probe(path, cancel)))
                    else:
                        docs.append(parse(path))
                except InterruptedError:
                    break
                except Exception as error:
                    errors.append(f"{Path(path).name}: {error}")
            return docs, containers, errors, replace, selected_folder

        self.task(inspect, self.inspected)

    def inspected(self, result):
        self.pending_import = result
        self.extract_pending()

    def extract_pending(self):
        docs, containers, errors, replace, selected_folder = self.pending_import
        tools = MKVTools()
        selected = []
        selected_track_id = None
        for path, info in containers:
            tracks = tools.tracks(info)
            text_tracks = [t for t in tracks if tools.text_format(t)]
            if not text_tracks:
                errors.append(f"{Path(path).name}: нет текстовых субтитров; PGS/VobSub требуют OCR.")
                continue
            track_id = selected_track_id
            if track_id is not None and not any(t["id"] == track_id for t in text_tracks):
                errors.append(f"{Path(path).name}: нет текстовой дорожки с выбранным ID {track_id}.")
                continue
            if track_id is None:
                track_id = tools.automatic_track(info)
            if track_id is None:
                labels = []
                for track in text_tracks:
                    props = track.get("properties", {})
                    labels.append(
                        f"ID {track['id']} · {props.get('language', 'und')} · "
                        f"{track.get('codec', '')} · {props.get('track_name', 'Без названия')}"
                    )
                label, ok = QInputDialog.getItem(
                    self, "Выберите дорожку",
                    f"{Path(path).name}\nЭтот ID будет использован для всех MKV в текущем импорте.",
                    labels, 0, False,
                )
                if not ok:
                    errors.append("Импорт MKV отменён: дорожка не выбрана.")
                    break
                track_id = text_tracks[labels.index(label)]["id"]
            selected_track_id = track_id
            selected.append((path, info, track_id))

        def extract_selected(cancel, progress):
            for i, (path, info, track_id) in enumerate(selected):
                if cancel.is_set():
                    break
                progress(i, len(selected), f"Извлечение: {Path(path).name}")
                try:
                    docs.append(tools.extract(path, info, track_id, cancel))
                except InterruptedError:
                    break
                except Exception as error:
                    errors.append(f"{Path(path).name}: {error}")
            return docs, errors, replace, selected_folder

        self.task(extract_selected, self.imported)

    def imported(self, result):
        docs, errors, replace, selected_folder = result
        if replace and docs:
            self.project = Project(model=self.project.model, base_root=selected_folder)
            self.project_path = None
            self.current_group = None
            self.dirty = False
            self.review.setRowCount(0)
            self.review_summary.setText("Задайте замены на шаге «Имена» и обновите предпросмотр.")
            self.setWindowTitle("Subs Renamer")
        self.project.documents.extend(docs)
        if docs and self.project.documents and not (replace and selected_folder):
            try:
                self.project.base_root = os.path.commonpath(
                    [str(Path(d.source_path).parent) for d in self.project.documents]
                )
            except ValueError:
                self.project.base_root = ""
        if docs:
            self.mark_dirty()
        self.refresh()
        if docs:
            self.set_step(0)
        if not self.project.documents:
            self.pages.setCurrentWidget(self.welcome)
        self.set_status(f"Добавлено файлов: {len(docs)}")
        if errors:
            self.error("Некоторые файлы пропущены:\n\n" + "\n".join(errors))

    def remove_files(self):
        rows = {i.row() for i in self.files.selectedIndexes()}
        self.project.documents = [d for i, d in enumerate(self.project.documents) if i not in rows]
        self.mark_dirty()
        self.refresh()
        self.files.clearSelection()
        self.files.setCurrentItem(None)

    def analyze(self):
        if not self.project.documents:
            self.error("Сначала добавьте субтитры")
            return
        if (
            self.project.groups
            and QMessageBox.question(
                self,
                "Повторный анализ",
                "Пересоздать группы? Замены совпадающих написаний сохранятся, ручная группировка будет обновлена.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        snapshot = self.project.model_copy(deep=True)
        proxy = self.settings.value("gemini_proxy", "")

        def run(cancel, progress):
            analyzer = GeminiAnalyzer(snapshot.model, proxy=proxy)
            try:
                return analyzer.analyze(snapshot, cancel, progress)
            finally:
                analyzer.close()

        def done(groups):
            self.project.groups = groups
            self.project.analysis_skipped_document_ids = snapshot.analysis_skipped_document_ids
            self.mark_dirty()
            self.refresh()
            self.set_step(1)
            self.set_status(f"Готово. Карточек: {len(groups)}. Проверьте имена и связи между никами.")
            if snapshot.analysis_skipped_document_ids:
                self.set_status(
                    f"Готово. Карточек: {len(groups)}. "
                    f"Файлов полностью пропущено из-за блокировки Gemini: "
                    f"{len(snapshot.analysis_skipped_document_ids)} (выделены красным в списке файлов)."
                )

        self.task(run, done)

    def refresh(self):
        self.updating = True
        self.change_folder_button.setToolTip(self.project.base_root or "Выбрать другую папку с субтитрами")
        self.files.setRowCount(len(self.project.documents))
        for row, doc in enumerate(self.project.documents):
            values = [
                doc.source_path,
                f"{doc.format.upper()} {doc.track_label}",
                str(len(doc.cues)),
                doc.encoding,
            ]
            for col, value in enumerate(values):
                cell = item(value)
                if doc.id in self.project.analysis_skipped_document_ids:
                    cell.setForeground(QColor("#ff7777"))
                    cell.setToolTip("Gemini заблокировал все блоки этого файла; файл пропущен при анализе.")
                self.files.setItem(row, col, cell)
        self.found = occurrences(self.project)
        counts = frequency(self.project, self.found)
        self.groups.setSortingEnabled(False)
        self.groups.clear()
        for group in self.project.groups:
            node = CharacterTreeItem([group.label, str(counts[group.id])])
            node.setData(0, Qt.ItemDataRole.UserRole, group.id)
            node.setFlags(node.flags() | Qt.ItemFlag.ItemIsEditable)
            self.groups.addTopLevelItem(node)
            for alias in group.aliases:
                count = sum(o.alias_id == alias.id and o.id not in self.project.excluded for o in self.found)
                child = CharacterTreeItem([alias.text, str(count)])
                child.setData(0, Qt.ItemDataRole.UserRole, group.id)
                node.addChild(child)
            if group.id == self.current_group:
                self.groups.setCurrentItem(node)
        self.groups.setSortingEnabled(True)
        self.updating = False
        self.show_group()

    def selected_group(self):
        return next((g for g in self.project.groups if g.id == self.current_group), None)

    def select_group(self):
        if self.updating:
            return
        node = self.groups.currentItem()
        self.current_group = node.data(0, Qt.ItemDataRole.UserRole) if node else None
        self.show_group()

    def show_group(self):
        self.updating = True
        group = self.selected_group()
        self.aliases.setRowCount(0)
        self.contexts.setRowCount(0)
        self.evidence.clear()
        self.group_title.setText(group.label if group else "Выберите карточку слева")
        self.replacement_name.setEnabled(group is not None)
        self.replacement_name.setText(group.replacement_name if group else "")
        self.generate_button.setEnabled(bool(group and group.replacement_name.strip() and group.aliases))
        if group:
            self.aliases.setRowCount(len(group.aliases))
            for row, alias in enumerate(group.aliases):
                for col, value in enumerate(
                    [
                        alias.text,
                        alias.replacement,
                        str(
                            sum(
                                o.alias_id == alias.id and o.id not in self.project.excluded
                                for o in self.found
                            )
                        ),
                    ]
                ):
                    cell = item(value, editable=col in (0, 1))
                    cell.setData(Qt.ItemDataRole.UserRole, alias.id)
                    self.aliases.setItem(row, col, cell)
            docs = {d.id: d for d in self.project.documents}
            alias_ids = {a.id for a in group.aliases}
            cues = {(d.id, c.id): c for d in self.project.documents for c in d.cues}
            contexts = {}
            for occurrence in self.found:
                if occurrence.alias_id not in alias_ids:
                    continue
                doc = docs[occurrence.document_id]
                cue = cues[doc.id, occurrence.cue_id]
                text = visible(doc, cue)[0]
                key = (doc.id, cue.timing, text)
                contexts.setdefault(key, []).append(occurrence)
            self.contexts.setRowCount(len(contexts))
            for row, ((document_id, timing, text), matches) in enumerate(contexts.items()):
                doc = docs[document_id]
                occurrence_ids = list(dict.fromkeys(o.id for o in matches))
                excluded_count = sum(oid in self.project.excluded for oid in occurrence_ids)
                check = item("")
                check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(
                    Qt.CheckState.Unchecked
                    if excluded_count == len(occurrence_ids)
                    else Qt.CheckState.PartiallyChecked
                    if excluded_count
                    else Qt.CheckState.Checked
                )
                check.setData(Qt.ItemDataRole.UserRole, occurrence_ids)
                self.contexts.setItem(row, 0, check)
                self.contexts.setItem(row, 1, item(f"{Path(doc.source_path).name}\n{timing}"))
                cell = item(text)
                cell.setToolTip("\n".join(dict.fromkeys(o.reason for o in matches if o.reason)))
                self.contexts.setItem(row, 2, cell)
            self.contexts.resizeRowsToContents()
            self.evidence.setPlainText("\n".join(group.evidence))
        self.updating = False

    def replacement_name_changed(self, text):
        if self.updating:
            return
        group = self.selected_group()
        if group:
            group.replacement_name = text.strip()
            self.generate_button.setEnabled(bool(text.strip() and group.aliases))
            self.mark_dirty()

    def generate_replacements(self):
        group = self.selected_group()
        if self.worker or not group or not group.replacement_name.strip() or not group.aliases:
            return
        snapshot = self.project.model_copy(deep=True)
        selected = next(g for g in snapshot.groups if g.id == group.id)
        proxy = self.settings.value("gemini_proxy", "")

        def run(cancel, progress):
            progress(0, 1, "Gemini составляет варианты нового имени…")
            analyzer = GeminiAnalyzer(snapshot.model, proxy=proxy)
            try:
                return analyzer.generate_replacements(snapshot, selected, cancel)
            finally:
                analyzer.close()

        def done(replacements):
            for alias in group.aliases:
                alias.replacement = replacements[alias.id]
            self.mark_dirty()
            self.refresh()
            self.set_status("Варианты составлены. Проверьте замены и контексты перед сохранением.")

        self.task(run, done)

    def rename_group(self, node, col):
        if self.updating or node.parent() is not None or col != 0:
            return
        group = next(g for g in self.project.groups if g.id == node.data(0, Qt.ItemDataRole.UserRole))
        group.label = node.text(0).strip() or "Персонаж"
        self.mark_dirty()

    def alias_changed(self, cell):
        if self.updating:
            return
        group = self.selected_group()
        if not group:
            return
        alias = next(a for a in group.aliases if a.id == cell.data(Qt.ItemDataRole.UserRole))
        if cell.column() == 0:
            alias.text = cell.text().strip()
        elif cell.column() == 1:
            alias.replacement = cell.text().strip()
        self.mark_dirty()
        if cell.column() == 0:
            self.refresh()

    def context_changed(self, cell):
        if self.updating or cell.column() != 0:
            return
        for occurrence_id in cell.data(Qt.ItemDataRole.UserRole):
            self.set_excluded(occurrence_id, cell.checkState() != Qt.CheckState.Checked)

    def set_excluded(self, occurrence_id, excluded):
        if excluded:
            self.project.excluded.add(occurrence_id)
        else:
            self.project.excluded.discard(occurrence_id)
        self.mark_dirty()

    def add_group(self):
        text, ok = QInputDialog.getText(self, "Новый персонаж", "Имя или прозвище из субтитров:")
        if ok and text.strip():
            group = CharacterGroup(label=text.strip(), aliases=[Alias(text=text.strip())])
            self.project.groups.append(group)
            self.current_group = group.id
            self.mark_dirty()
            self.refresh()

    def add_alias(self):
        group = self.selected_group()
        if not group:
            self.error("Выберите персонажа")
            return
        text, ok = QInputDialog.getText(self, "Добавить вариант", "Точное написание из субтитров:")
        if ok and text.strip():
            group.aliases.append(Alias(text=text.strip()))
            self.mark_dirty()
            self.refresh()

    def selected_group_ids(self):
        return {node.data(0, Qt.ItemDataRole.UserRole) for node in self.groups.selectedItems()}

    def merge_groups(self):
        ids = self.selected_group_ids()
        groups = [g for g in self.project.groups if g.id in ids]
        if len(groups) < 2:
            self.error("Выберите несколько карточек с Ctrl или Shift")
            return
        target = groups[0]
        for group in groups[1:]:
            target.aliases.extend(group.aliases)
            target.evidence.extend(group.evidence)
        self.project.groups = [g for g in self.project.groups if g.id not in ids or g.id == target.id]
        self.current_group = target.id
        self.mark_dirty()
        self.refresh()

    def delete_groups(self):
        ids = self.selected_group_ids()
        self.project.groups = [g for g in self.project.groups if g.id not in ids]
        self.current_group = None
        self.mark_dirty()
        self.refresh()

    def selected_alias_ids(self):
        return {
            self.aliases.item(i.row(), 0).data(Qt.ItemDataRole.UserRole)
            for i in self.aliases.selectedIndexes()
        }

    def split_aliases(self):
        group = self.selected_group()
        ids = self.selected_alias_ids()
        if not group or not ids:
            return
        selected = [a for a in group.aliases if a.id in ids]
        group.aliases = [a for a in group.aliases if a.id not in ids]
        created = CharacterGroup(label=selected[0].text, aliases=selected)
        self.project.groups.append(created)
        self.project.groups = [g for g in self.project.groups if g.aliases]
        self.current_group = created.id
        self.mark_dirty()
        self.refresh()

    def delete_aliases(self):
        group = self.selected_group()
        if group:
            ids = self.selected_alias_ids()
            group.aliases = [a for a in group.aliases if a.id not in ids]
            self.mark_dirty()
            self.refresh()

    def make_preview(self):
        try:
            changes, skipped = preview(self.project)
        except ValueError as error:
            self.error(str(error))
            return False
        self.updating = True
        # Include excluded changes so users can turn them back on from the review screen.
        full = self.project.model_copy(deep=True)
        full.excluded = set()
        all_changes, all_skipped = preview(full)
        docs = {d.id: d for d in full.documents}
        rows = [(c.occurrence, c.replacement) for c in all_changes] + [(o, None) for o in all_skipped]
        self.review.setRowCount(len(rows))
        for row, (o, replacement) in enumerate(rows):
            doc = docs[o.document_id]
            cue = next(c for c in doc.cues if c.id == o.cue_id)
            check = item("")
            check.setData(Qt.ItemDataRole.UserRole, o.id)
            if replacement is not None:
                check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(
                    Qt.CheckState.Unchecked if o.id in self.project.excluded else Qt.CheckState.Checked
                )
            else:
                check.setToolTip(o.reason)
            self.review.setItem(row, 0, check)
            self.review.setItem(row, 1, item(f"{Path(doc.source_path).name}\n{cue.timing}"))
            before = doc.content[cue.start : cue.end]
            self.review.setItem(row, 2, item(before))
            after = (
                doc.content[cue.start : o.start] + replacement + doc.content[o.end : cue.end]
                if replacement is not None
                else o.reason
            )
            self.review.setItem(row, 3, item(after))
            self.review.item(row, 3).setToolTip(f"{o.text} → {replacement}" if replacement else o.reason)
            if replacement is None:
                self.review.item(row, 3).setForeground(QColor("#e6b55d"))
        self.review.resizeRowsToContents()
        self.review_summary.setText(
            f"Изменений: {len(changes)} · Пропущено сложных участков: {len(skipped)} · "
            "Каждая строка показывает одно отдельное изменение реплики."
        )
        self.updating = False
        self.set_step(2)
        return True

    def preview_changed(self, cell):
        if self.updating or cell.column() != 0:
            return
        self.set_excluded(cell.data(Qt.ItemDataRole.UserRole), cell.checkState() != Qt.CheckState.Checked)

    def export_files(self):
        if not self.project.documents:
            self.error("Нет субтитров для сохранения")
            return
        if not self.make_preview():
            return
        directory = QFileDialog.getExistingDirectory(self, "Папка исправленных субтитров")
        if directory:
            snapshot = self.project.model_copy(deep=True)
            self.task(
                lambda cancel, progress: export(snapshot, directory, cancel, progress),
                lambda saved: QMessageBox.information(
                    self,
                    "Сохранено",
                    f"Сохранено файлов: {len(saved)}\nПапка: {directory}\nОригиналы не изменены.",
                ),
            )

    def replace_files(self):
        if not self.project.documents:
            self.error("Нет субтитров для сохранения")
            return
        if not self.make_preview():
            return
        if QMessageBox.question(
            self,
            "Заменить оригинальные файлы?",
            "Исходные файлы будут перезаписаны. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        snapshot = self.project.model_copy(deep=True)
        self.task(
            lambda cancel, progress: replace_originals(snapshot, cancel, progress),
            lambda saved: QMessageBox.information(
                self, "Сохранено", f"Заменено оригинальных файлов: {len(saved)}"
            ),
        )

    def save_project(self):
        if self.worker:
            return False
        path = self.project_path
        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self, "Сохранить проект", "season.subsproject.json", "Проект (*.subsproject.json)"
            )
        if path:
            try:
                self.project.save(path)
                self.project_path = path
                self.dirty = False
                self.setWindowTitle("Subs Renamer — " + Path(path).name)
                self.set_status("Проект сохранён вместе с текстом субтитров и вашими исправлениями")
                return True
            except Exception as error:
                self.error(str(error))
        return False

    def can_discard(self):
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Сохранить проект?",
            "В проекте есть несохранённые изменения.",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard or (
            answer == QMessageBox.StandardButton.Save and self.save_project()
        )

    def open_project(self):
        if self.worker:
            return
        if not self.can_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть проект", "", "Проект (*.subsproject.json *.json)"
        )
        if path:
            try:
                project = Project.load(path)
                self.project = project
                self.project_path = path
                self.current_group = None
                self.dirty = False
                self.refresh()
                self.pages.setCurrentWidget(self.workspace)
                self.setWindowTitle("Subs Renamer — " + Path(path).name)
                self.make_preview()
            except Exception as error:
                self.error(str(error))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and not self.worker:
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.import_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])

    def closeEvent(self, event):
        if self.worker:
            self.cancel_task()
            event.ignore()
            return
        if any(worker.isRunning() for worker in self.findChildren(Worker)):
            event.ignore()
            return
        if self.can_discard():
            event.accept()
        else:
            event.ignore()


STYLE = """
QWidget { background: #141923; color: #e4e9f2; font-family: 'Segoe UI'; font-size: 13px; }
QLabel#section { font-size: 19px; font-weight: 600; }
QPushButton { background: #263246; border: 1px solid #394962; border-radius: 6px; padding: 9px 14px; }
QPushButton:hover { background: #354762; }
QPushButton:disabled { color: #6c7a8e; }
QPushButton#primary { background: #497ced; color: white; border-color: #6b96f5; font-weight: 600; }
QPushButton#primary:hover { background: #5c8cf4; }
QLineEdit, QTextEdit { background: #1b2330; border: 1px solid #35445a; border-radius: 4px; padding: 6px; }
QTableWidget, QTreeWidget { background: #1b2330; alternate-background-color: #202b3a; border: 1px solid #303e53; gridline-color: #50627b; selection-background-color: #34589b; }
QHeaderView::section { background: #263246; color: #aebed3; padding: 9px; border: 0; border-right: 1px solid #50627b; border-bottom: 1px solid #50627b; }
QProgressBar { border: 1px solid #394962; border-radius: 4px; text-align: center; }
QProgressBar::chunk { background: #497ced; }
QToolTip { background: #263246; color: #e4e9f2; border: 1px solid #497ced; }
"""


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(STYLE)
    window = Window()
    window.show()
    sys.exit(app.exec())

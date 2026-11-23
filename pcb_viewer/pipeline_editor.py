"""Desktop editor and local library for routing pipelines."""
import json
from urllib.parse import quote

from PySide6.QtCore import QIODevice, QPersistentModelIndex, QSaveFile, QSettings, Qt
from PySide6.QtGui import QCursor, QDrag
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QVBoxLayout, QWidget)

from .pipelines import PIPELINE_1, ROUTING_BLOCKS, RoutingPipeline


BLOCK_ROLE = Qt.ItemDataRole.UserRole
SETTINGS_GROUP = 'routing/pipelines'


class PipelineBlocks(QListWidget):
    def __init__(self):
        super().__init__()
        self.dragged_index = QPersistentModelIndex()

    def startDrag(self, _supported_actions):
        item = self.currentItem()
        if item is None or not item.isSelected():
            return
        drag = QDrag(self)
        drag.setMimeData(self.mimeData([item]))
        rect = self.visualItemRect(item)
        drag.setPixmap(self.viewport().grab(rect))
        drag.setHotSpot(self.viewport().mapFromGlobal(QCursor.pos()) - rect.topLeft())
        self.dragged_index = QPersistentModelIndex(self.currentIndex())
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self.dragged_index = QPersistentModelIndex()

    def remove_selected(self):
        items = self.selectedItems()
        if items:
            self.takeItem(self.row(items[0]))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_selected()
            event.accept()
        else:
            super().keyPressEvent(event)


class BlockPalette(QListWidget):
    def __init__(self, sequence):
        super().__init__()
        self.sequence = sequence
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDropIndicatorShown(False)

    def startDrag(self, _supported_actions):
        super().startDrag(Qt.DropAction.CopyAction)

    def _accept_removal(self, event):
        if (event.source() is self.sequence and self.sequence.dragged_index.isValid() and
                event.possibleActions() & Qt.DropAction.MoveAction):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return True
        event.ignore()
        return False

    def dragEnterEvent(self, event):
        self._accept_removal(event)

    def dragMoveEvent(self, event):
        self._accept_removal(event)

    def dropEvent(self, event):
        if self._accept_removal(event):
            self.sequence.takeItem(self.sequence.dragged_index.row())


class PipelineEditor(QWidget):
    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.settings = settings if settings is not None else QSettings('PCB Studio', 'PCB Studio')
        self.saved, settings_error = self._read_library()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        library = QHBoxLayout()
        library.addWidget(QLabel('Saved pipelines'))
        self.saved_list = QComboBox()
        library.addWidget(self.saved_list, 1)
        self._button(library, 'Load', self.load_saved)
        self._button(library, 'New', self.new_pipeline)
        self._button(library, 'Duplicate', self.duplicate)
        self._button(library, 'Import…', self.import_pipeline)
        layout.addLayout(library)

        naming = QHBoxLayout()
        naming.addWidget(QLabel('Pipeline name'))
        self.name = QLineEdit()
        naming.addWidget(self.name, 1)
        self._button(naming, 'Save', self.save)
        self._button(naming, 'Export…', self.export_pipeline)
        layout.addLayout(naming)

        columns = QHBoxLayout()
        self.blocks = PipelineBlocks()
        available = QVBoxLayout()
        available.addWidget(QLabel('Available blocks'))
        self.palette = BlockPalette(self.blocks)
        self.palette.setAccessibleName('Available routing blocks')
        for block in ROUTING_BLOCKS:
            self.palette.addItem(self._block_item(block))
        available.addWidget(self.palette, 1)
        columns.addLayout(available, 1)

        sequence = QVBoxLayout()
        sequence.addWidget(QLabel('Execution order'))
        self.blocks.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.blocks.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.blocks.setAlternatingRowColors(True)
        self.blocks.setAccessibleName('Pipeline execution order')
        sequence.addWidget(self.blocks, 1)
        columns.addLayout(sequence, 1)
        layout.addLayout(columns, 1)
        for block_list in (self.palette, self.blocks):
            block_list.setStyleSheet('QListWidget::item { padding: 5px 8px; }')

        controls = QHBoxLayout()
        self._button(controls, 'Add block →', self.add_block)
        controls.addStretch()
        self._button(controls, 'Move up', lambda: self.move_block(-1))
        self._button(controls, 'Move down', lambda: self.move_block(1))
        self._button(controls, 'Remove block', self.blocks.remove_selected)
        layout.addLayout(controls)
        note = QLabel('Drag blocks into Execution order to add them. Drag them back to Available blocks to remove them. '
                      'Consecutive Straighten, A*, and Drag blocks repeat together until stable.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.palette.itemDoubleClicked.connect(lambda _item: self.add_block())
        self.name.textChanged.connect(self.update_status)
        model = self.blocks.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.rowsMoved):
            signal.connect(self.update_status)
        self.baseline = None
        self._load_draft(PIPELINE_1, saved=True)
        if settings_error:
            self.status.setText(f'Could not read saved pipelines. {settings_error}')

    @staticmethod
    def _button(layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)

    @staticmethod
    def _block_item(block):
        label, description = ROUTING_BLOCKS[block]
        item = QListWidgetItem(label)
        item.setData(BLOCK_ROLE, block)
        item.setToolTip(description)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
        return item

    def block_ids(self):
        return tuple(self.blocks.item(row).data(BLOCK_ROLE) for row in range(self.blocks.count()))

    def _read_library(self):
        saved = {PIPELINE_1.name.casefold(): PIPELINE_1}
        settings_error = None
        self.settings.sync()
        self.settings.beginGroup(SETTINGS_GROUP)
        try:
            for key in self.settings.childKeys():
                try:
                    pipeline = RoutingPipeline.from_dict(json.loads(self.settings.value(key)))
                    saved.setdefault(pipeline.name.casefold(), pipeline)
                except (TypeError, ValueError) as error:
                    settings_error = str(error)
        finally:
            self.settings.endGroup()
        return saved, settings_error

    def pipelines(self):
        return tuple(self.saved.values())

    def _refresh_saved(self, name):
        self.saved_list.clear()
        self.saved_list.addItems([pipeline.name for pipeline in self.pipelines()])
        self.saved_list.setCurrentText(name)

    def _load_draft(self, pipeline, saved=False):
        self.baseline = pipeline if saved else None
        self.name.setText(pipeline.name)
        self.blocks.clear()
        for block in pipeline.blocks:
            self.blocks.addItem(self._block_item(block))
        self._refresh_saved(pipeline.name if saved else self.saved_list.currentText())
        self.update_status()

    def has_changes(self):
        return (self.baseline is None or self.name.text().strip() != self.baseline.name
                or self.block_ids() != self.baseline.blocks)

    def update_status(self, *_args):
        if not self.has_changes():
            self.status.setText('Built-in pipeline. Duplicate it to save a variation.'
                                if self.baseline == PIPELINE_1 else 'Saved in your pipeline library.')
        elif self.name.text().strip().casefold() == PIPELINE_1.name.casefold():
            self.status.setText('Unsaved changes. Enter a new name to save a variation of Pipeline 1.')
        else:
            self.status.setText('Unsaved changes')

    def allow_discard(self):
        if not self.has_changes():
            return True
        buttons = QMessageBox.StandardButton
        choice = QMessageBox.question(self, 'Unsaved pipeline', 'Save changes to the current pipeline?',
                                      buttons.Save | buttons.Discard | buttons.Cancel, buttons.Save)
        return choice == buttons.Discard or (choice == buttons.Save and self.save())

    def _available_name(self, name):
        candidate = name
        suffix = 2
        while candidate.casefold() in self.saved:
            candidate = f'{name} {suffix}'
            suffix += 1
        return candidate

    def load_saved(self):
        pipeline = self.saved[self.saved_list.currentText().casefold()]
        if self.allow_discard():
            self._load_draft(self.saved[pipeline.name.casefold()], saved=True)

    def new_pipeline(self):
        if self.allow_discard():
            self._load_draft(RoutingPipeline(self._available_name('Pipeline 2'), ()))
            self.name.selectAll()
            self.name.setFocus()

    def duplicate(self):
        name = self.name.text().strip() or 'Pipeline'
        blocks = self.block_ids()
        if self.allow_discard():
            self._load_draft(RoutingPipeline(self._available_name(f'{name} copy'), blocks))
            self.name.selectAll()
            self.name.setFocus()

    def add_block(self):
        item = self.palette.currentItem()
        if item is not None:
            item = item.clone()
            row = self.blocks.currentRow()
            self.blocks.insertItem(row + 1 if row >= 0 else self.blocks.count(), item)
            self.blocks.setCurrentItem(item)

    def move_block(self, offset):
        row = self.blocks.currentRow()
        if row >= 0 and 0 <= row + offset < self.blocks.count():
            item = self.blocks.takeItem(row)
            self.blocks.insertItem(row + offset, item)
            self.blocks.setCurrentItem(item)

    def draft(self):
        return RoutingPipeline(self.name.text(), self.block_ids())

    def save(self):
        if not self.has_changes():
            return True
        try:
            pipeline = self.draft()
            key = pipeline.name.casefold()
            saved, _ = self._read_library()
            existing = saved.get(key)
            if existing is not None and (existing == PIPELINE_1 or existing != self.baseline):
                raise ValueError('This pipeline name is already in use. Choose another name.')
            self.settings.setValue(f'{SETTINGS_GROUP}/{quote(key, safe="")}',
                                   json.dumps(pipeline.to_dict(), ensure_ascii=False))
            self.settings.sync()
            if self.settings.status() != QSettings.Status.NoError:
                raise OSError('Could not write the pipeline library.')
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, 'Cannot save pipeline', str(error))
            return False
        self.saved = {**saved, key: pipeline}
        self._load_draft(pipeline, saved=True)
        return True

    def import_pipeline(self):
        filename, _ = QFileDialog.getOpenFileName(self, 'Import pipeline', '', 'Pipeline JSON (*.json)')
        if not filename:
            return
        try:
            pipeline = RoutingPipeline.load(filename)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, 'Cannot import pipeline', str(error))
            return
        if self.allow_discard():
            self._load_draft(RoutingPipeline(self._available_name(pipeline.name), pipeline.blocks))

    def export_pipeline(self):
        try:
            pipeline = self.draft()
        except ValueError as error:
            QMessageBox.warning(self, 'Cannot export pipeline', str(error))
            return
        filename, _ = QFileDialog.getSaveFileName(self, 'Export pipeline', '', 'Pipeline JSON (*.json)')
        if not filename:
            return
        if not filename.lower().endswith('.json'):
            filename += '.json'
        data = (json.dumps(pipeline.to_dict(), ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        target = QSaveFile(filename)
        if (not target.open(QIODevice.OpenModeFlag.WriteOnly) or
                target.write(data) != len(data) or not target.commit()):
            QMessageBox.warning(self, 'Cannot export pipeline', target.errorString())

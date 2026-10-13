"""Qt desktop interface; display coordinates invert Y, stored coordinates do not."""
import copy
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from PySide6.QtCore import Qt, QPointF, QRectF, QTimer, QSettings
from PySide6.QtGui import QAction, QColor, QPainter, QPainterPath, QPen, QBrush, QPolygonF
from PySide6.QtWidgets import (QApplication, QMainWindow, QGraphicsScene, QGraphicsView,
    QGraphicsItem, QDockWidget, QWidget, QVBoxLayout, QLabel, QComboBox, QLineEdit,
    QCheckBox, QPushButton, QPlainTextEdit, QFileDialog, QMessageBox, QDoubleSpinBox,
    QToolBar, QDialog, QDialogButtonBox, QSpinBox, QProgressBar, QTabBar,
    QSizePolicy, QHBoxLayout, QGroupBox, QFormLayout)
from .model import Document, COLLECTIONS
from .router import extract_jobs

COLORS = ['#58c8ff', '#f6c85f', '#b291ff', '#74dfa5', '#ff8c87', '#7bdad0', '#dbaaed', '#bbc976', '#ee9fce', '#839fff']


def auto_route_statistics(data):
    """Summarize routed copper against route requests, including older files."""
    jobs = extract_jobs(data)
    jobs_by_id = {job.id: job for job in jobs}
    nets = {}
    for job in jobs:
        nets.setdefault(job.net, set()).add(job.id)
    routed = set()
    wire_length = 0.0
    for wire in data.get('wires', []):
        # Read-only compatibility: old documents can still contain hand-drawn
        # copper even though this app no longer offers a manual-routing mode.
        if wire.get('route_type') not in ('autoroute', 'manualroute'):
            continue
        points = wire.get('points', [])
        wire_length += sum(math.dist(a, b) for a, b in zip(points, points[1:]))
        job_ids = wire.get('route_jobs', wire.get('autoroute_jobs'))
        if not isinstance(job_ids, list):
            job_ids = [wire.get('route_job', wire.get('autoroute_job'))]
        for job_id in job_ids:
            if (isinstance(job_id, int) and not isinstance(job_id, bool) and
                    job_id in jobs_by_id and wire.get('net') == jobs_by_id[job_id].net and
                    wire.get('layer') == jobs_by_id[job_id].layer):
                routed.add(job_id)
    return {
        'nets_total': len(nets),
        'nets_routed': sum(ids <= routed for ids in nets.values()),
        'nets_partial': sum(bool(ids & routed) and not ids <= routed for ids in nets.values()),
        'connections_total': len(jobs),
        'connections_routed': len(routed),
        'wire_length': wire_length,
    }


def path_for(points, close=False):
    path = QPainterPath()
    if points:
        path.moveTo(points[0][0], -points[0][1])
        for x,y in points[1:]:
            path.lineTo(x, -y)
        if close:
            path.closeSubpath()
    return path


class BoardView(QGraphicsView):
    def __init__(self, window):
        super().__init__(window)
        self.setScene(QGraphicsScene(self))
        self.window = window
        self.mode = 'Select'
        self.points = []
        self.preview = None
        self.pan = None
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QColor('#101923'))
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setMouseTracking(True)

    def set_mode(self, mode):
        self.cancel()
        self.mode = mode
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag if mode == 'Select' else QGraphicsView.DragMode.NoDrag)
        self.setCursor(Qt.CursorShape.ArrowCursor if mode == 'Select' else Qt.CursorShape.CrossCursor)
        self.window.statusBar().showMessage({
            'Select': 'Select an object to inspect or edit. Middle/right drag pans; wheel zooms.',
            'Rectangle': 'Click two opposite corners to place a keepout. Esc cancels.',
            'Polygon': 'Click vertices; Enter finishes the keepout. Esc cancels.',
        }[mode])

    def cancel(self):
        self.points = []
        if self.preview is not None:
            self.scene().removeItem(self.preview)
            self.preview = None

    def coordinate(self, event):
        p = self.mapToScene(event.position().toPoint())
        step = self.window.grid.value()
        x,y = p.x(), -p.y()
        if step:
            x,y = round(x/step)*step, round(y/step)*step
        return [x,y]

    def wheelEvent(self, event):
        factor = 1.2 if event.angleDelta().y() > 0 else 1/1.2
        if 0.005 < self.transform().m11()*factor < 100:
            self.scale(factor, factor)

    def mousePressEvent(self, event):
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self.pan = event.position().toPoint()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.mode != 'Select':
            point = self.coordinate(event)
            if not self.points or point != self.points[-1]:
                self.points.append(point)
            if self.mode == 'Rectangle' and len(self.points) == 2:
                a,b = self.points
                self.finish([a,[b[0],a[1]],b,[a[0],b[1]]])
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.pan is not None:
            delta = event.position().toPoint()-self.pan
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value()-delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value()-delta.y())
            self.pan = event.position().toPoint()
            return
        x,y = self.coordinate(event)
        self.window.coords.setText(f'X {x:.2f}   Y {y:.2f}   •   source units')
        if self.points:
            if self.preview is not None:
                self.scene().removeItem(self.preview)
            pts = self.points+[[x,y]]
            if self.mode == 'Rectangle':
                a = self.points[0]
                pts = [a,[x,a[1]],[x,y],[a[0],y]]
            pen = QPen(QColor('#ff6584'), 0, Qt.PenStyle.DashLine)
            self.preview = self.scene().addPath(path_for(pts, True), pen,
                                                QBrush(QColor(255,70,110,35)))
            self.preview.setZValue(100)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.pan is not None:
            self.pan = None
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.mode == 'Polygon':
            self.finish(self.points)
        else:
            super().keyPressEvent(event)

    def finish(self, points):
        try:
            layer = self.window.active_layer.currentText()
            obj = {'type': self.window.keepout_type.currentText(), 'layer':layer,
                   'shape':'polygon', 'points': points}
            self.window.document.change(
                'keepouts', len(self.window.document.data.get('keepouts', [])), obj)
            self.cancel()
            self.window.refresh()
        except ValueError as e:
            QMessageBox.warning(self, 'Invalid keepout', str(e))
            if self.mode == 'Rectangle':
                self.cancel()


class MainWindow(QMainWindow):
    def __init__(self, filename=None):
        super().__init__()
        self.document = Document()
        self.sessions = []
        self.current_session = -1
        self.resize(1440, 900)
        self.view = BoardView(self)
        self.setCentralWidget(self.view)
        self._routing_preview_items = {}
        self._routing_preview_airwires = {}
        self._routing_preview_hidden = []
        self._routing_preview_round = None
        self._routing_preview_document = None
        self._routing_preview_labels = None
        self._routing_preview_last_paint = 0.0
        self._routing_in_progress = False
        self._route_progress_label = None
        self._route_progress_bar = None
        self._route_cancel_button = None
        self.coords = QLabel('Source coordinates')
        self.statusBar().addPermanentWidget(self.coords)

        tab_toolbar = QToolBar('Open boards')
        self.tab_toolbar = tab_toolbar
        tab_toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tab_toolbar)
        self.tabs = QTabBar()
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.tabs.currentChanged.connect(self.switch_tab)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        tab_toolbar.addWidget(self.tabs)
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)

        toolbar = QToolBar('Board tools')
        self.main_toolbar = toolbar
        self.addToolBar(toolbar)
        def action(name, shortcut, callback):
            a = QAction(name, self)
            a.setShortcut(shortcut)
            a.triggered.connect(callback)
            if shortcut in ('Delete', 'F'):
                a.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
                self.view.addAction(a)
            toolbar.addAction(a)
            return a
        action('New', 'Ctrl+N', self.new)
        action('Open…', 'Ctrl+O', self.open)
        action('Save', 'Ctrl+S', self.save)
        action('Save as…', 'Ctrl+Shift+S', lambda: self.save(True))
        toolbar.addSeparator()
        action('Undo', 'Ctrl+Z', lambda: self.history(False))
        action('Redo', 'Ctrl+Shift+Z', lambda: self.history(True))
        action('Fit board', 'F', self.fit)
        action('Auto route…', 'Ctrl+R', self.autoroute)
        toolbar.addSeparator()
        self.mode = QComboBox()
        self.mode.addItems(['Select', 'Rectangle', 'Polygon'])
        self.mode.currentTextChanged.connect(self.view.set_mode)
        toolbar.addWidget(self.mode)
        action('Finish polygon', 'Ctrl+Return', lambda: self.view.finish(self.view.points)
               if self.view.mode == 'Polygon' else None)
        action('Delete', 'Delete', self.delete)
        dock = QDockWidget('Board & selection', self)
        self.side_dock = dock
        dock.setMinimumWidth(310)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel('PCB STUDIO  /  JSON editor'))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        route_group = QGroupBox('Routing statistics')
        route_layout = QFormLayout(route_group)
        self.route_stat_labels = {}
        for key, title in (
            ('nets_routed', 'Nets routed'),
            ('nets_total', 'Total nets'),
            ('partial', 'Partially routed nets'),
            ('connections', 'Connections routed'),
            ('unrouted', 'Unrouted connections'),
            ('completion', 'Connection completion'),
            ('length', 'Generated copper length'),
        ):
            label = QLabel()
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            route_layout.addRow(title, label)
            self.route_stat_labels[key] = label
        route_group.setToolTip('Board-wide counts from routing requests and marked routed traces. A net is routed only when all of its requested connections are routed.')
        layout.addWidget(route_group)
        layout.addWidget(QLabel('Visible layer'))
        self.visible_layer = QComboBox()
        self.visible_layer.currentTextChanged.connect(self.layer_changed)
        layout.addWidget(self.visible_layer)
        layout.addWidget(QLabel('Editing layer'))
        self.active_layer = QComboBox()
        layout.addWidget(self.active_layer)
        layout.addWidget(QLabel('Keepout type'))
        self.keepout_type = QComboBox()
        self.keepout_type.addItems(['wire_keepout', 'via_keepout'])
        layout.addWidget(self.keepout_type)
        layout.addWidget(QLabel('Snap spacing (0 = off, source units)'))
        self.grid = QDoubleSpinBox()
        self.grid.setRange(0,1000)
        self.grid.setDecimals(3)
        self.grid.setValue(1)
        layout.addWidget(self.grid)
        self.search = QLineEdit()
        self.search.setPlaceholderText('Filter by net or component reference…')
        self.search.returnPressed.connect(self.refresh)
        layout.addWidget(self.search)
        self.show_unrouted = QCheckBox('Show unrouted connections (dashed)')
        self.show_unrouted.setChecked(False)
        self.show_unrouted.toggled.connect(self.refresh)
        layout.addWidget(self.show_unrouted)
        self.categories = {}
        for group in COLLECTIONS:
            cb = QCheckBox(group.replace('_',' ').title())
            cb.setChecked(group != 'components')
            cb.toggled.connect(self.refresh)
            layout.addWidget(cb)
            self.categories[group] = cb
        layout.addWidget(QLabel('Selected object • JSON properties'))
        self.properties = QPlainTextEdit()
        self.properties.setPlaceholderText('Select geometry to inspect. Edit values and apply.')
        layout.addWidget(self.properties, 1)
        apply = QPushButton('Apply properties')
        apply.clicked.connect(self.apply_properties)
        layout.addWidget(apply)
        move = QPushButton('Move selected by offset…')
        move.clicked.connect(self.move_selected)
        layout.addWidget(move)
        help_text = QLabel('Wheel: zoom  •  Middle/right drag: pan\nRectangle: two clicks  •  Polygon: clicks + Enter\nEsc: cancel  •  Delete: remove selection')
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.view.scene().selectionChanged.connect(self.selection_changed)
        self.add_session(self.document)
        if filename:
            self.load(filename)

    def layer_changed(self, layer):
        if layer != "All layers":
            self.active_layer.setCurrentText(layer)
        self.refresh()

    def reset_layers(self):
        self.view.cancel()
        self.mode.setCurrentText("Select")
        self.search.clear()
        self.show_unrouted.blockSignals(True)
        self.show_unrouted.setChecked(False)
        self.show_unrouted.blockSignals(False)
        for group, cb in self.categories.items():
            cb.blockSignals(True)
            cb.setChecked(group != "components")
            cb.blockSignals(False)
        self.visible_layer.blockSignals(True)
        self.visible_layer.clear()
        self.visible_layer.addItems(['All layers']+self.document.layers)
        # A single layer is immediately readable on dense full-board samples.
        self.visible_layer.setCurrentIndex(1)
        self.visible_layer.blockSignals(False)
        self.active_layer.clear()
        self.active_layer.addItems(self.document.layers)

    def session_state(self):
        return {
            'visible_layer': self.visible_layer.currentText(),
            'active_layer': self.active_layer.currentText(),
            'keepout_type': self.keepout_type.currentText(),
            'grid': self.grid.value(),
            'search': self.search.text(),
            'show_unrouted': self.show_unrouted.isChecked(),
            'categories': {group: checkbox.isChecked() for group, checkbox in self.categories.items()},
            'mode': self.mode.currentText(),
            'transform': self.view.transform(),
            'center': self.view.mapToScene(self.view.viewport().rect().center()),
        }

    def save_session_state(self):
        if 0 <= self.current_session < len(self.sessions):
            self.sessions[self.current_session]['state'] = self.session_state()

    def restore_session_state(self, state):
        self.reset_layers()
        if state is None:
            self.refresh()
            self.fit()
            return
        self.visible_layer.blockSignals(True)
        if state['visible_layer'] in [self.visible_layer.itemText(i) for i in range(self.visible_layer.count())]:
            self.visible_layer.setCurrentText(state['visible_layer'])
        self.visible_layer.blockSignals(False)
        self.active_layer.setCurrentText(state['active_layer'])
        self.keepout_type.setCurrentText(state['keepout_type'])
        self.grid.setValue(state['grid'])
        self.search.setText(state['search'])
        self.show_unrouted.blockSignals(True)
        self.show_unrouted.setChecked(state.get('show_unrouted', False))
        self.show_unrouted.blockSignals(False)
        for group, checked in state['categories'].items():
            if group in self.categories:
                self.categories[group].blockSignals(True)
                self.categories[group].setChecked(checked)
                self.categories[group].blockSignals(False)
        self.mode.setCurrentText(state['mode'])
        self.refresh()
        self.view.setTransform(state['transform'])
        self.view.centerOn(state['center'])

    def add_session(self, document):
        self.save_session_state()
        self.sessions.append({'document': document, 'state': None})
        index = len(self.sessions) - 1
        self.tabs.blockSignals(True)
        self.tabs.addTab(document.path.name if document.path else 'Untitled')
        self.tabs.setCurrentIndex(index)
        self.tabs.blockSignals(False)
        self.activate_tab(index)

    def activate_tab(self, index):
        if not 0 <= index < len(self.sessions):
            return
        if index != self.current_session:
            self.save_session_state()
        self.current_session = index
        self.document = self.sessions[index]['document']
        self.restore_session_state(self.sessions[index]['state'])
        self.title()

    def switch_tab(self, index):
        if index != self.current_session:
            self.activate_tab(index)

    def allowed(self):
        if not self.document.dirty:
            return True
        name = self.document.path.name if self.document.path else 'Untitled'
        answer = QMessageBox.question(self, 'Unsaved changes', f'Save changes to {name}?',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def new(self):
        self.add_session(Document())

    def open(self):
        filenames,_ = QFileDialog.getOpenFileNames(self, 'Open PCB JSON', 'pcb_example', 'PCB JSON (*.json)')
        for filename in filenames:
            self.open_in_tab(filename)

    def open_in_tab(self, filename):
        try:
            path = Path(filename).resolve()
            for index, session in enumerate(self.sessions):
                existing = session['document'].path
                if existing and existing.resolve() == path:
                    self.tabs.setCurrentIndex(index)
                    return True
            candidate = Document.load(filename)
            if self.document.path is None and not self.document.dirty:
                self.document = candidate
                self.sessions[self.current_session] = {'document': candidate, 'state': None}
                self.restore_session_state(None)
                self.title()
            else:
                self.add_session(candidate)
            return True
        except (OSError, ValueError, TypeError, KeyError) as e:
            QMessageBox.critical(self, 'Cannot open board', str(e))
            return False

    def load(self, filename):
        """Replace the active tab; retained for startup and programmatic use."""
        try:
            candidate = Document.load(filename)
            self.document = candidate
            if self.current_session < 0:
                self.add_session(candidate)
            else:
                self.sessions[self.current_session] = {'document': candidate, 'state': None}
                self.restore_session_state(None)
                self.title()
        except (OSError, ValueError, TypeError, KeyError) as e:
            QMessageBox.critical(self, 'Cannot open board', str(e))

    def close_tab(self, index):
        if not 0 <= index < len(self.sessions):
            return
        if index != self.current_session:
            self.tabs.setCurrentIndex(index)
        if not self.allowed():
            return
        self.sessions.pop(index)
        self.tabs.blockSignals(True)
        self.tabs.removeTab(index)
        self.tabs.blockSignals(False)
        self.current_session = -1
        if self.sessions:
            target = min(index, len(self.sessions) - 1)
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(target)
            self.tabs.blockSignals(False)
            self.activate_tab(target)
        else:
            self.add_session(Document())

    def save(self, save_as=False):
        filename = self.document.path
        if save_as or filename is None:
            filename,_ = QFileDialog.getSaveFileName(self, 'Save PCB JSON', str(filename or 'board.json'), 'PCB JSON (*.json)')
            if not filename:
                return False
            if not str(filename).lower().endswith('.json'):
                filename += '.json'
        try:
            self.document.save(filename)
            self.title()
            self.statusBar().showMessage(f'Saved {filename}', 5000)
            return True
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, 'Cannot save board', str(e))
            return False

    def title(self):
        name = self.document.path.name if self.document.path else 'Untitled'
        label = f'{"* " if self.document.dirty else ""}{name}'
        if 0 <= self.current_session < self.tabs.count():
            self.tabs.setTabText(self.current_session, label)
            self.tabs.setTabToolTip(self.current_session, str(self.document.path or 'Unsaved board'))
        self.setWindowTitle(f'{label} — PCB Studio')

    def closeEvent(self, event):
        if self._routing_in_progress:
            self.statusBar().showMessage('Cancel routing before closing the board.', 5000)
            event.ignore()
            return
        original = self.current_session
        for index, session in enumerate(self.sessions):
            if not session['document'].dirty:
                continue
            self.tabs.setCurrentIndex(index)
            if not self.allowed():
                event.ignore()
                return
        if 0 <= original < len(self.sessions):
            self.tabs.setCurrentIndex(original)
        event.accept()

    def history(self, redo):
        self.document.redo() if redo else self.document.undo()
        self.refresh()

    def fit(self):
        rect = self.view.scene().itemsBoundingRect()
        if rect.isEmpty():
            rect = QRectF(-100,-100,200,200)
        self.view.setSceneRect(rect.adjusted(-100,-100,100,100))
        self.view.fitInView(rect.adjusted(-20,-20,20,20), Qt.AspectRatioMode.KeepAspectRatio)

    def _begin_routing_preview(self):
        """Hide old generated copper without changing the document or undo stack."""
        self._end_routing_preview()
        self._routing_preview_document = self.document
        old_indices = {index for index, wire in
                       enumerate(self.document.data.get('wires', []))
                       if wire.get('route_type') == 'autoroute'}
        for item in self.view.scene().items():
            identity = item.data(0)
            if (isinstance(identity, tuple) and len(identity) == 2 and
                    ((identity[0] == 'wires' and identity[1] in old_indices) or
                     identity[0] == 'unrouted')):
                item.setVisible(False)
                self._routing_preview_hidden.append(item)
        self._routing_preview_labels = (
            self.summary.text(),
            {key: label.text() for key, label in self.route_stat_labels.items()},
        )

    def _end_routing_preview(self):
        scene = self.view.scene()
        for _, item, _ in self._routing_preview_items.values():
            if item.scene() is scene:
                scene.removeItem(item)
        self._routing_preview_items.clear()
        for item in self._routing_preview_airwires.values():
            if item.scene() is scene:
                scene.removeItem(item)
        self._routing_preview_airwires.clear()
        for item in self._routing_preview_hidden:
            if item.scene() is scene:
                item.setVisible(True)
        self._routing_preview_hidden.clear()
        if self._routing_preview_labels is not None:
            summary, labels = self._routing_preview_labels
            self.summary.setText(summary)
            for key, value in labels.items():
                self.route_stat_labels[key].setText(value)
        self._routing_preview_labels = None
        self._routing_preview_round = None
        self._routing_preview_document = None
        self._routing_preview_last_paint = 0.0

    def _draw_routing_preview(self, router, round_number, total_rounds, message):
        """Incrementally paint actual provisional grid routes on the open board."""
        if self.document is not self._routing_preview_document:
            return
        now = time.monotonic()
        changed_round = round_number != self._routing_preview_round
        if changed_round:
            for _, item, _ in self._routing_preview_items.values():
                self.view.scene().removeItem(item)
            self._routing_preview_items.clear()
            for item in self._routing_preview_airwires.values():
                self.view.scene().removeItem(item)
            self._routing_preview_airwires.clear()
            self._routing_preview_round = round_number
        if (not changed_round and message != 'Routing complete' and
                set(router.routes) == set(self._routing_preview_items) and
                now - self._routing_preview_last_paint < 0.15):
            return
        self._routing_preview_last_paint = now
        scene = self.view.scene()
        for job_id in list(self._routing_preview_items):
            if job_id not in router.routes:
                scene.removeItem(self._routing_preview_items.pop(job_id)[1])
        for job_id, cells in router.routes.items():
            signature = tuple(cells)
            previous = self._routing_preview_items.get(job_id)
            if previous is not None and previous[0] == signature:
                continue
            if previous is not None:
                scene.removeItem(previous[1])
            job = router.jobs_by_id[job_id]
            points = router._polyline(job, cells)
            pen = QPen(QColor('#54ffd2'), job.width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            item = scene.addPath(path_for(points), pen)
            item.setZValue(8)
            item.setToolTip(f'Provisional route: {job.net} (connection {job.id})')
            length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
            self._routing_preview_items[job_id] = (signature, item, length)
        routed = set(router.routes)
        if self.show_unrouted.isChecked():
            for job_id in list(self._routing_preview_airwires):
                if job_id in routed:
                    scene.removeItem(self._routing_preview_airwires.pop(job_id))
            for job in router.jobs:
                if job.id in routed or job.id in self._routing_preview_airwires:
                    continue
                pen = QPen(QColor('#ffcf66'), 0, Qt.PenStyle.DashLine)
                item = scene.addPath(path_for((job.start, job.goal)), pen)
                item.setZValue(12)
                item.setToolTip(f'Unrouted (preview): {job.net} (connection {job.id})')
                self._routing_preview_airwires[job.id] = item
        net_jobs = {}
        for job in router.jobs:
            net_jobs.setdefault(job.net, set()).add(job.id)
        labels = self.route_stat_labels
        labels['nets_routed'].setText(str(sum(ids <= routed for ids in net_jobs.values())))
        labels['nets_total'].setText(str(len(net_jobs)))
        labels['partial'].setText(str(sum(bool(ids & routed) and not ids <= routed
                                          for ids in net_jobs.values())))
        labels['connections'].setText(f'{len(routed)} / {len(router.jobs)} (preview)')
        labels['unrouted'].setText(str(len(router.jobs) - len(routed)))
        labels['completion'].setText(f'{len(routed) / len(router.jobs):.1%}')
        labels['length'].setText(
            f'≈ {sum(entry[2] for entry in self._routing_preview_items.values()):,.1f} units')
        self.summary.setText(
            f'Live routing preview · round {round_number}/{total_rounds}\n'
            f'{len(routed)}/{len(router.jobs)} provisional connections · {message}')

    def selected(self):
        return sorted(set(tuple(item.data(0)) for item in self.view.scene().selectedItems() if item.data(0)))

    def selection_changed(self):
        selected = self.selected()
        self.properties.setPlainText(json.dumps(self.document.data[selected[0][0]][selected[0][1]], ensure_ascii=False, indent=2) if len(selected) == 1 else '')

    def apply_properties(self):
        selected = self.selected()
        if len(selected) != 1:
            return
        try:
            obj = json.loads(self.properties.toPlainText())
            self.document.change(*selected[0], obj)
            self.refresh()
        except (ValueError, TypeError) as e:
            QMessageBox.warning(self, 'Invalid properties', str(e))

    def delete(self):
        # Text editors retain their own Delete behavior through Qt shortcut context.
        for group,index in reversed(self.selected()):
            self.document.change(group,index,None)
        self.refresh()

    def move_selected(self):
        selected = self.selected()
        if not selected:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Move selection in source coordinates')
        layout = QVBoxLayout(dialog)
        fields = []
        for label in ('X offset', 'Y offset'):
            layout.addWidget(QLabel(label))
            field = QDoubleSpinBox()
            field.setRange(-1e7,1e7)
            field.setDecimals(4)
            layout.addWidget(field)
            fields.append(field)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec():
            dx,dy = [field.value() for field in fields]
            for group,index in selected:
                obj = copy.deepcopy(self.document.data[group][index])
                if 'x' in obj:
                    obj['x'] += dx
                    obj['y'] += dy
                for key in ('points','vertices'):
                    if key in obj:
                        obj[key] = [[x+dx,y+dy] for x,y in obj[key]]
                self.document.change(group,index,obj)
            self.refresh()

    def autoroute(self):
        from .router import RouterConfig, autoroute_data
        from .freerouting import freeroute_data
        dialog = QDialog(self)
        dialog.setWindowTitle('PCB auto router')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('Algorithm'))
        algorithm = QComboBox()
        algorithm.addItems(['Built-in (TritonRoute-inspired)', 'Freerouting benchmark'])
        layout.addWidget(algorithm)
        layout.addWidget(QLabel('Built-in routing priority'))
        ordering = QComboBox()
        ordering.addItems(['Higher completion (dense areas first)', 'Faster (short routes first)'])
        layout.addWidget(ordering)
        layout.addWidget(QLabel("Congestion calculation"))
        compute_backend = QComboBox()
        for label, value in [("Python (default)", "python"), ("PyTorch automatic device", "torch-auto"),
                             ("PyTorch CPU", "torch-cpu"), ("PyTorch Apple GPU (MPS)", "torch-mps"),
                             ("PyTorch NVIDIA GPU (CUDA)", "torch-cuda")]:
            compute_backend.addItem(label, value)
        layout.addWidget(compute_backend)
        rounds_row = QHBoxLayout()
        rounds_label = QLabel('Routing-order trials (first uses baseline order)')
        rounds_row.addWidget(rounds_label)
        random_rounds = QSpinBox()
        random_rounds.setRange(1, 16)
        random_rounds.setValue(1)
        rounds_row.addWidget(random_rounds)
        layout.addLayout(rounds_row)
        seed_row = QHBoxLayout()
        seed_label = QLabel('Random seed (reproducible)')
        seed_row.addWidget(seed_label)
        random_seed = QSpinBox()
        random_seed.setRange(-2_147_483_647, 2_147_483_647)
        random_seed.setValue(0)
        seed_row.addWidget(random_seed)
        layout.addLayout(seed_row)
        layout.addWidget(QLabel('Freerouting JAR (required for benchmark)'))
        jar_row = QHBoxLayout()
        jar_field = QLineEdit()
        jar_field.setPlaceholderText('Select a Freerouting .jar file…')
        jar_field.setText(os.environ.get('FREEROUTING_JAR',
                          QSettings('PCB Studio', 'PCB Studio').value('freerouting_jar', '')))
        jar_row.addWidget(jar_field)
        browse_jar = QPushButton('Browse…')
        browse_jar.clicked.connect(lambda: jar_field.setText(
            QFileDialog.getOpenFileName(dialog, 'Freerouting JAR', jar_field.text(),
                                        'Java archives (*.jar)')[0] or jar_field.text()))
        jar_row.addWidget(browse_jar)
        layout.addLayout(jar_row)
        layout.addWidget(QLabel('Java runtime (leave blank for automatic selection)'))
        java_row = QHBoxLayout()
        java_field = QLineEdit()
        java_field.setPlaceholderText('Java 25 bin/java for current Freerouting releases…')
        java_field.setText(os.environ.get('FREEROUTING_JAVA',
                           QSettings('PCB Studio', 'PCB Studio').value('freerouting_java', '')))
        java_row.addWidget(java_field)
        browse_java = QPushButton('Browse…')
        browse_java.clicked.connect(lambda: java_field.setText(
            QFileDialog.getOpenFileName(dialog, 'Java executable', java_field.text())[0]
            or java_field.text()))
        java_row.addWidget(browse_java)
        layout.addLayout(java_row)
        fields = {}
        defaults = {
            'Grid spacing': 2.0,
            'Clearance': self.document.data.get('metadata', {}).get('design_rules', {}).get('clearance_mil', 2.0),
        }
        for label, value in defaults.items():
            layout.addWidget(QLabel(label + ' (source units)'))
            field = QDoubleSpinBox()
            field.setRange(0.1 if label == 'Grid spacing' else 0.0, 1000.0)
            field.setDecimals(3)
            field.setValue(float(value))
            layout.addWidget(field)
            fields[label] = field
        layout.addWidget(QLabel('Initial congestion-repair passes'))
        iterations = QSpinBox()
        iterations.setRange(1, 64)
        iterations.setValue(6)
        layout.addWidget(iterations)
        early_row = QHBoxLayout()
        early_label = QLabel('Early refinement interval (attempted jobs; 0 disables)')
        early_row.addWidget(early_label)
        early_interval = QSpinBox()
        early_interval.setRange(0, 1000)
        early_interval.setValue(192)
        early_row.addWidget(early_interval)
        layout.addLayout(early_row)
        force_row = QHBoxLayout()
        force_label = QLabel('Straighten + repair + balance iterations (built-in; 0 disables)')
        force_row.addWidget(force_label)
        force_iterations = QSpinBox()
        force_iterations.setRange(0, 20)
        force_iterations.setValue(6)
        force_row.addWidget(force_iterations)
        layout.addLayout(force_row)
        detail_row = QHBoxLayout()
        detail_label = QLabel('Detailed-routing pass limit (0 disables)')
        detail_row.addWidget(detail_label)
        detail_passes = QSpinBox()
        detail_passes.setRange(0, 64)
        detail_passes.setValue(32)
        detail_row.addWidget(detail_passes)
        layout.addLayout(detail_row)
        drag_row = QHBoxLayout()
        drag_label = QLabel('Detailed drag search states (0 disables)')
        drag_row.addWidget(drag_label)
        drag_states = QSpinBox()
        drag_states.setRange(0, 64)
        drag_states.setValue(12)
        drag_row.addWidget(drag_states)
        layout.addLayout(drag_row)
        attraction_row = QHBoxLayout()
        attraction_label = QLabel('Inter-net attraction (0 disables)')
        attraction_row.addWidget(attraction_label)
        attraction_strength = QDoubleSpinBox()
        attraction_strength.setRange(0.0, 2.0)
        attraction_strength.setDecimals(2)
        attraction_strength.setSingleStep(0.1)
        attraction_strength.setValue(0.5)
        attraction_row.addWidget(attraction_strength)
        layout.addLayout(attraction_row)
        detour_row = QHBoxLayout()
        detour_label = QLabel('Maximum route stretch (0 disables)')
        detour_row.addWidget(detour_label)
        max_detour_ratio = QDoubleSpinBox()
        max_detour_ratio.setRange(0.0, 20.0)
        max_detour_ratio.setSingleStep(0.5)
        max_detour_ratio.setValue(3.0)
        detour_row.addWidget(max_detour_ratio)
        layout.addLayout(detour_row)
        algorithm.currentIndexChanged.connect(lambda index: (
            compute_backend.setEnabled(index == 0),
            rounds_label.setEnabled(index == 0),
            random_rounds.setEnabled(index == 0),
            seed_label.setEnabled(index == 0),
            random_seed.setEnabled(index == 0),
            early_label.setEnabled(index == 0),
            early_interval.setEnabled(index == 0),
            force_label.setEnabled(index == 0),
            force_iterations.setEnabled(index == 0),
            detail_label.setEnabled(index == 0),
            detail_passes.setEnabled(index == 0),
            drag_label.setEnabled(index == 0),
            drag_states.setEnabled(index == 0),
            attraction_label.setEnabled(index == 0),
            attraction_strength.setEnabled(index == 0),
            detour_label.setEnabled(index == 0),
            max_detour_ratio.setEnabled(index == 0)))
        layout.addWidget(QLabel('Freerouting time limit (minutes)'))
        freerouting_minutes = QSpinBox()
        freerouting_minutes.setRange(1, 240)
        freerouting_minutes.setValue(10)
        layout.addWidget(freerouting_minutes)
        note = QLabel('Only the open JSON board is used. Deleted trace interiors are ignored. Each built-in order trial runs the full repair/refinement pipeline, so multiple trials can take several times longer. The Freerouting benchmark runs an external JAR through a temporary single-layer DSN; non-terminal copper is conservatively fixed as obstacles.')
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if not dialog.exec():
            return

        scratch = copy.deepcopy(self.document.data)
        scratch['wires'] = [wire for wire in scratch.get('wires', [])
                            if wire.get('route_type') != 'autoroute']
        scratch.pop('autoroute_report', None)
        benchmark_input = hashlib.sha256(json.dumps(
            scratch, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode('utf-8')).hexdigest()
        config = RouterConfig(grid_step=fields['Grid spacing'].value(),
                              clearance=fields['Clearance'].value(),
                              iterations=iterations.value(),
                              early_refine_interval=early_interval.value(),
                              force_iterations=force_iterations.value(),
                              detailed_max_passes=detail_passes.value(),
                              detailed_drag_states=drag_states.value(),
                              attraction_strength=attraction_strength.value(),
                              max_detour_ratio=max_detour_ratio.value(),
                              random_rounds=random_rounds.value(),
                              random_seed=random_seed.value(),
                              compute_backend=compute_backend.currentData(),
                              ordering='dense_first' if ordering.currentIndex() == 0 else 'short',
                              layer=self.visible_layer.currentText()
                              if self.visible_layer.currentText() != 'All layers' else None)
        use_freerouting = algorithm.currentIndex() == 1
        if use_freerouting:
            QSettings('PCB Studio', 'PCB Studio').setValue('freerouting_jar', jar_field.text())
            QSettings('PCB Studio', 'PCB Studio').setValue('freerouting_java', java_field.text())
        previous_mode = self.mode.currentText()
        self.mode.setCurrentText('Select')
        action_states = [(action, action.isEnabled())
                         for action in self.main_toolbar.actions()]
        for action, _ in action_states:
            action.setEnabled(False)
        self.mode.setEnabled(False)
        self.tabs.setEnabled(False)
        self.side_dock.setEnabled(False)
        self._routing_in_progress = True
        progress_label = QLabel('Preparing routing grid…')
        progress_label.setMinimumWidth(220)
        progress_label.setMaximumWidth(560)
        progress_bar = QProgressBar()
        progress_bar.setRange(0, 1000)
        progress_bar.setValue(0)
        progress_bar.setFixedWidth(170)
        cancel_button = QPushButton('Cancel routing')
        cancel_requested = [False]
        cancel_button.clicked.connect(lambda: cancel_requested.__setitem__(0, True))
        self.statusBar().addWidget(progress_label, 1)
        self.statusBar().addPermanentWidget(progress_bar)
        self.statusBar().addPermanentWidget(cancel_button)
        self._route_progress_label = progress_label
        self._route_progress_bar = progress_bar
        self._route_cancel_button = cancel_button
        preview_count = [None]
        routing_document = self.document
        if not use_freerouting:
            self._begin_routing_preview()
        def visualize(router, round_number, total_rounds, message):
            if self.document is not routing_document:
                return
            preview_count[0] = (len(router.routes), len(router.jobs))
            self._draw_routing_preview(router, round_number, total_rounds, message)
        def update(done, total, message):
            if self.document is not routing_document:
                return False
            label = message
            if preview_count[0] is not None:
                routed, requested = preview_count[0]
                label += f'\nLive board preview: {routed}/{requested} provisional connections'
            progress_label.setText(label.replace('\n', ' · '))
            progress_label.setToolTip(label)
            progress_bar.setValue(round(done / max(1, total) * 1000))
            QApplication.processEvents()
            return not cancel_requested[0]
        routing_error = None
        try:
            wires, report = (freeroute_data(scratch, config, jar_field.text(), update,
                                           freerouting_minutes.value() * 60,
                                           java_field.text() or None)
                             if use_freerouting else autoroute_data(
                                 scratch, config, update, visualize))
        except (ValueError, TypeError, KeyError, OSError) as error:
            routing_error = error
        finally:
            self._end_routing_preview()
            for widget in (progress_label, progress_bar, cancel_button):
                self.statusBar().removeWidget(widget)
                widget.deleteLater()
            self._route_progress_label = None
            self._route_progress_bar = None
            self._route_cancel_button = None
            for action, enabled in action_states:
                action.setEnabled(enabled)
            self.mode.setEnabled(True)
            self.tabs.setEnabled(True)
            self.side_dock.setEnabled(True)
            self.mode.setCurrentText(previous_mode)
            self._routing_in_progress = False
        if routing_error is not None:
            QMessageBox.warning(self, 'Auto route failed', str(routing_error))
            return
        if report.cancelled:
            self.statusBar().showMessage('Auto routing cancelled.', 5000)
            return

        existing = self.document.data.get('wires', [])
        generated = [index for index, wire in enumerate(existing)
                     if wire.get('route_type') == 'autoroute']
        changes = [('wires', index, None) for index in reversed(generated)]
        base = len(existing) - len(generated)
        changes.extend(('wires', base + index, wire) for index, wire in enumerate(wires))
        self.document.batch_change(changes)
        self.refresh()
        self.fit()
        backend = 'Freerouting' if use_freerouting else 'Built-in'
        session = self.sessions[self.current_session]
        if session.get('benchmark_input') != benchmark_input:
            session['benchmarks'] = {}
            session['benchmark_input'] = benchmark_input
        benchmarks = session['benchmarks']
        benchmarks[backend] = report
        title = 'Auto routing complete' if report.clean else 'Auto routing needs review'
        conflict_label = ('generated-trace centerline crossings' if use_freerouting
                          else 'trace-clearance conflict locations')
        detail = (f'{backend}: '
                  f'{report.routed}/{report.jobs_total} connections routed in {report.elapsed_seconds:.1f}s.\n'
                  f'{report.wire_length:.1f} source units of new copper.\n'
                  f'{report.conflict_cells} remaining {conflict_label}; '
                  f'{len(report.failed)} unrouted connections.\n\n'
                  'Generated traces are one undoable edit. Review and run final fabrication DRC before use.')
        if not use_freerouting:
            detail += (f'\nEarly refinement: {report.early_refinement_passes} checkpoints, '
                       f'{report.early_refinement_moves} accepted moves.'
                       f'\nCombined straighten/repair/balance: {report.force_iterations_run} iterations, '
                       f'{report.force_jobs_moved} accepted route moves, '
                       f'{report.force_new_connections:+d} net connection change.')
            detail += (f'\nDetailed routing: {report.detailed_passes} passes, '
                       f'{report.detailed_straightened} straightened traces, '
                       f'{report.detailed_dragged} connections made by dragging blockers, '
                       f'{report.detailed_new_connections:+d} connection change.')
            detail += (f'\nRouting-order trials: {len(report.round_results)} completed; '
                       f'best was round {report.selected_round}.')
            if len(report.round_results) > 1:
                detail += ''.join(
                    f'\n  Round {result["round"]} '
                    f'({"baseline" if result["seed"] is None else "seed " + str(result["seed"])}): '
                    f'{result["routed"]}/{result["jobs_total"]} routed, '
                    f'{result["wire_length"]:.1f} length, '
                    f'{result["elapsed_seconds"]:.1f}s'
                    for result in report.round_results)
            overlong = [entry['net'] for entry in report.failed
                        if 'detour limit' in entry['reason']]
            if overlong:
                detail += ('\nLeft unrouted to avoid excessive detours: '
                           + ', '.join(overlong[:8])
                           + ('…' if len(overlong) > 8 else '') + '.')
        if len(benchmarks) > 1:
            detail += '\n\nThis tab’s benchmark runs:\n'
            for name, result in benchmarks.items():
                detail += (f'{name}: {result.routed}/{result.jobs_total} routed, '
                           f'{result.wire_length:.1f} length, '
                           f'{result.elapsed_seconds:.1f}s\n')
        if report.failed:
            names = [entry['net'] for entry in report.failed]
            preview = ', '.join(names[:12])
            if len(names) > 12:
                preview += f' … (+{len(names) - 12} more)'
            detail += f'\n\nUnrouted: {preview}'
        QMessageBox.information(self, title, detail)

    def refresh(self, *_):
        if (self._routing_preview_document is not None and
                self.document is not self._routing_preview_document):
            self._end_routing_preview()
        self.view.cancel()
        scene = self.view.scene()
        scene.blockSignals(True)
        scene.clear()
        layer = self.visible_layer.currentText()
        all_layers = layer == 'All layers'
        search = self.search.text().strip().lower()
        data = self.document.data
        colors = dict(zip(self.document.layers, (COLORS*100)))
        count = 0
        for group in ('copper_polygons','wires','pins','pads','vias','components','keepouts'):
            if not self.categories[group].isChecked():
                continue
            for index,obj in enumerate(data.get(group, [])):
                obj_layer = obj.get('layer', data.get('layer'))
                layers = obj.get('layers', [])
                if not all_layers and ((obj_layer and obj_layer != layer) or (layers and layer not in layers)):
                    continue
                if search and group != 'keepouts' and search not in ' '.join(str(obj.get(k,'')) for k in ('net','ref','pin_ref')).lower():
                    continue
                color = QColor(colors.get(obj_layer or layer, COLORS[0]))
                brush = QBrush(Qt.BrushStyle.NoBrush)
                pen = QPen(color, 0)
                z = 2
                if group in ('wires','copper_polygons','keepouts'):
                    path = path_for(obj.get('vertices',obj.get('points', [])), group != 'wires')
                    if group == 'wires':
                        pen.setWidthF(obj.get('width', 2))
                        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    elif group == 'copper_polygons':
                        color.setAlpha(65)
                        brush = QBrush(color)
                        pen = QPen(Qt.PenStyle.NoPen)
                        z = 0
                    else:
                        color = QColor('#ff6584' if obj.get('type') != 'via_keepout' else '#ffb95c')
                        pen = QPen(color, 0)
                        color.setAlpha(100)
                        brush = QBrush(color, Qt.BrushStyle.BDiagPattern)
                        z = 10
                else:
                    x,y = obj['x'], obj['y']
                    stack = data.get('padstacks', {}).get(obj.get('padstack',obj.get('via_type')), {})
                    per_layer = stack.get('shapes_per_layer', {})
                    if per_layer and not all_layers and layer not in per_layer:
                        continue
                    if isinstance(obj.get('shape'), dict):
                        shapes = [obj['shape']]
                    elif all_layers:
                        shapes = []
                        for shape_layer, entries in per_layer.items():
                            if layers and shape_layer not in layers:
                                continue
                            for shape in entries:
                                if shape not in shapes:
                                    shapes.append(shape)
                    else:
                        shapes = per_layer.get(layer, [])
                    path = QPainterPath()
                    for shape in shapes or [{'shape':'circle','radius': 3 if group == 'vias' else 4}]:
                        if shape['shape'] == 'rect':
                            path.addRect(QRectF(x+shape['x1'], -y-shape['y2'], shape['x2']-shape['x1'],shape['y2']-shape['y1']))
                        else:
                            radius = shape.get('radius', 4)
                            path.addEllipse(QPointF(x,-y),radius,radius)
                    if group == 'vias':
                        color = QColor('#d3e5ed')
                    elif group == 'components':
                        color = QColor('#ffffff')
                    else:
                        color = QColor('#edc56d')
                    pen = QPen(color,0)
                    brush = QBrush(color) if group in ('pins','pads') else QBrush(Qt.BrushStyle.NoBrush)
                    z = 4
                item = scene.addPath(path,pen,brush)
                item.setZValue(z)
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
                item.setData(0,(group,index))
                item.setToolTip(f'{group}[{index}]\n{obj.get("net",obj.get("ref",obj.get("type","")))}\n{obj_layer or ", ".join(layers)}')
                count += 1
        unrouted_count = 0
        if self.show_unrouted.isChecked():
            routed = {job_id for wire in data.get('wires', [])
                      if wire.get('route_type') == 'autoroute'
                      for job_id in wire.get('autoroute_jobs', [wire.get('autoroute_job')])}
            for job in extract_jobs(data):
                if job.id in routed or (not all_layers and job.layer != layer):
                    continue
                if search and search not in job.net.lower():
                    continue
                pen = QPen(QColor('#ffcf66'), 0, Qt.PenStyle.DashLine)
                item = scene.addPath(path_for((job.start, job.goal)), pen)
                item.setZValue(12)
                item.setData(0, ('unrouted', job.id))
                item.setToolTip(f'Unrouted: {job.net} (connection {job.id})')
                unrouted_count += 1
        scene.blockSignals(False)
        self.properties.clear()
        overlay = f'  ·  {unrouted_count} unrouted shown' if self.show_unrouted.isChecked() else ''
        self.summary.setText(f'{len(data.get("wires", [])):,} wires  ·  {len(data.get("vias", [])):,} vias\n{len(data.get("keepouts", []))} keepouts  ·  {count:,} visible objects{overlay}')
        stats = auto_route_statistics(data)
        routed_count = stats['connections_routed']
        total = stats['connections_total']
        labels = self.route_stat_labels
        labels['nets_routed'].setText(f'{stats["nets_routed"]:,}')
        labels['nets_total'].setText(f'{stats["nets_total"]:,}')
        labels['partial'].setText(f'{stats["nets_partial"]:,}')
        labels['connections'].setText(f'{routed_count:,} / {total:,}')
        labels['unrouted'].setText(f'{total - routed_count:,}')
        labels['completion'].setText(f'{routed_count / total:.1%}' if total else '—')
        labels['length'].setText(f'{stats["wire_length"]:,.1f} units')
        self.title()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('PCB Studio')
    app.setStyle('Fusion')
    app.setStyleSheet('QMainWindow, QDockWidget, QWidget { font-size: 12px; } QToolBar { spacing: 8px; padding: 8px; } QPlainTextEdit { font-family: monospace; }')
    window = MainWindow(sys.argv[1] if len(sys.argv)>1 else None)
    window.show()
    QTimer.singleShot(0, window.fit)
    sys.exit(app.exec())

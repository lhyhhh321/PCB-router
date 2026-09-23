"""Qt desktop interface; display coordinates invert Y, stored coordinates do not."""
import copy
import json
import sys
from pathlib import Path
from PySide6.QtCore import Qt, QPointF, QRectF, QTimer
from PySide6.QtGui import QAction, QColor, QPainter, QPainterPath, QPen, QBrush, QPolygonF
from PySide6.QtWidgets import (QApplication, QMainWindow, QGraphicsScene, QGraphicsView,
    QGraphicsItem, QDockWidget, QWidget, QVBoxLayout, QLabel, QComboBox, QLineEdit,
    QCheckBox, QPushButton, QPlainTextEdit, QFileDialog, QMessageBox, QDoubleSpinBox,
    QToolBar, QDialog, QDialogButtonBox, QSpinBox, QProgressDialog)
from .model import Document, COLLECTIONS

COLORS = ['#58c8ff', '#f6c85f', '#b291ff', '#74dfa5', '#ff8c87', '#7bdad0', '#dbaaed', '#bbc976', '#ee9fce', '#839fff']


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
        self.window.statusBar().showMessage({'Select':'Select an object to inspect or edit. Middle/right drag pans; wheel zooms.', 'Rectangle':'Click two opposite corners to place a keepout. Esc cancels.', 'Polygon':'Click vertices; Enter finishes the keepout. Esc cancels.'}[mode])

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
            self.preview = self.scene().addPath(path_for(pts, True), pen, QBrush(QColor(255,70,110,35)))
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
            self.window.document.change('keepouts', len(self.window.document.data.get('keepouts', [])), obj)
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
        self.resize(1440, 900)
        self.view = BoardView(self)
        self.setCentralWidget(self.view)
        self.coords = QLabel('Source coordinates')
        self.statusBar().addPermanentWidget(self.coords)
        toolbar = QToolBar('Board tools')
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
        action('Finish polygon', 'Ctrl+Return', lambda: self.view.finish(self.view.points) if self.view.mode == 'Polygon' else None)
        action('Delete', 'Delete', self.delete)
        dock = QDockWidget('Board & selection', self)
        dock.setMinimumWidth(310)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel('PCB STUDIO  /  JSON editor'))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        layout.addWidget(QLabel('Visible layer'))
        self.visible_layer = QComboBox()
        self.visible_layer.currentTextChanged.connect(self.layer_changed)
        layout.addWidget(self.visible_layer)
        layout.addWidget(QLabel('Place keepouts on layer'))
        self.active_layer = QComboBox()
        layout.addWidget(self.active_layer)
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
        self.reset_layers()
        self.refresh()
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

    def allowed(self):
        if not self.document.dirty:
            return True
        answer = QMessageBox.question(self, 'Unsaved changes', 'Save changes before continuing?',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def new(self):
        if self.allowed():
            self.document = Document()
            self.reset_layers()
            self.refresh()
            self.fit()

    def open(self):
        if not self.allowed():
            return
        filename,_ = QFileDialog.getOpenFileName(self, 'Open PCB JSON', 'pcb_example', 'PCB JSON (*.json)')
        if filename:
            self.load(filename)

    def load(self, filename):
        try:
            candidate = Document.load(filename)
            self.document = candidate
            self.reset_layers()
            self.refresh()
            self.fit()
        except (OSError, ValueError, TypeError, KeyError) as e:
            QMessageBox.critical(self, 'Cannot open board', str(e))

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
        self.setWindowTitle(f'{"* " if self.document.dirty else ""}{self.document.path.name if self.document.path else "Untitled"} — PCB Studio')

    def closeEvent(self, event):
        event.accept() if self.allowed() else event.ignore()

    def history(self, redo):
        self.document.redo() if redo else self.document.undo()
        self.refresh()

    def fit(self):
        rect = self.view.scene().itemsBoundingRect()
        if rect.isEmpty():
            rect = QRectF(-100,-100,200,200)
        self.view.setSceneRect(rect.adjusted(-100,-100,100,100))
        self.view.fitInView(rect.adjusted(-20,-20,20,20), Qt.AspectRatioMode.KeepAspectRatio)

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
        dialog = QDialog(self)
        dialog.setWindowTitle('PCB auto router')
        layout = QVBoxLayout(dialog)
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
        layout.addWidget(QLabel('Search-and-repair passes'))
        iterations = QSpinBox()
        iterations.setRange(1, 64)
        iterations.setValue(6)
        layout.addWidget(iterations)
        note = QLabel('Connections come from routing_connections, or from the endpoints in deleted_connections.wires. Existing copper, pads, vias, traces, and wire keepouts are treated as obstacles.')
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
        config = RouterConfig(grid_step=fields['Grid spacing'].value(),
                              clearance=fields['Clearance'].value(),
                              iterations=iterations.value(),
                              layer=self.visible_layer.currentText()
                              if self.visible_layer.currentText() != 'All layers' else None)
        progress = QProgressDialog('Preparing routing grid…', 'Cancel', 0, 1000, self)
        progress.setWindowTitle('Auto routing')
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        def update(done, total, message):
            progress.setLabelText(message)
            progress.setValue(round(done / max(1, total) * 1000))
            QApplication.processEvents()
            return not progress.wasCanceled()
        try:
            wires, report = autoroute_data(scratch, config, update)
        except (ValueError, TypeError, KeyError) as error:
            progress.close()
            QMessageBox.warning(self, 'Auto route failed', str(error))
            return
        progress.close()
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
        title = 'Auto routing complete' if report.clean else 'Auto routing needs review'
        detail = (f'{report.routed}/{report.jobs_total} connections routed in {report.elapsed_seconds:.1f}s.\n'
                  f'{report.conflict_cells} remaining trace-clearance conflict locations; '
                  f'{len(report.failed)} unrouted connections.\n\n'
                  'Generated traces are one undoable edit. Review and run final fabrication DRC before use.')
        QMessageBox.information(self, title, detail)

    def refresh(self, *_):
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
        scene.blockSignals(False)
        self.properties.clear()
        self.summary.setText(f'{len(data.get("wires", [])):,} wires  ·  {len(data.get("vias", [])):,} vias\n{len(data.get("keepouts", []))} keepouts  ·  {count:,} visible objects')
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

import copy
import json
import os
from pathlib import Path
import pytest
from pcb_viewer.model import Document, polygon

SAMPLES = [Path('pcb_example') / name for name in (
    '01全板.json',
    '02第三层右侧并删去地线参考.json',
    '03第三层右侧删去线网（布线输入）.json',
    'DEMO_0511_1810-init_full_board.json',
)]

@pytest.mark.parametrize('sample', SAMPLES, ids=lambda p:p.name)
def test_roundtrip(sample, tmp_path):
    doc = Document.load(sample)
    target = tmp_path/'board.json'
    doc.save(target)
    assert json.loads(target.read_text()) == json.loads(sample.read_text())

@pytest.mark.parametrize('sample', SAMPLES, ids=lambda p:p.name)
def test_keepout_history_and_preservation(sample, tmp_path):
    doc = Document.load(sample)
    original = copy.deepcopy(doc.data)
    count = len(doc.data.get('keepouts', []))
    obj = {'type':'wire_keepout','layer':doc.layers[0], 'shape':'polygon', 'points':[[0,0],[20,0],[20,10],[0,10]]}
    doc.change('keepouts',count,obj)
    assert doc.dirty
    doc.undo()
    assert doc.data == original and not doc.dirty
    doc.redo()
    target = tmp_path/'edited.json'
    doc.save(target)
    saved = Document.load(target)
    assert saved.data['keepouts'][-1]['points'] == obj['points']+[[0,0]]
    for key in original.keys()-{'stats','keepouts'}:
        assert saved.data[key] == original[key]
    second = tmp_path/'second.json'
    doc.save(second)
    assert json.loads(second.read_text()) == json.loads(target.read_text())

@pytest.mark.parametrize('points', [ [[0,0],[1,1],[2,2]], [[0,0],[2,2],[0,2],[2,0]], [[0,0],[3,0],[1,0],[1,2],[0,2]] ])
def test_bad_polygon(points):
    with pytest.raises(ValueError):
        polygon(points)

def test_branch_and_failed_save(tmp_path):
    doc = Document()
    obj = {'points':[[0,0],[1,0],[0,1]],'layer':'TOP'}
    doc.change('keepouts',0,obj)
    doc.save(tmp_path/'x.json')
    doc.undo()
    doc.change('keepouts',0,dict(obj,layer='BOTTOM'))
    assert doc.dirty
    with pytest.raises(OSError):
        doc.save(tmp_path/'missing'/'x.json')
    assert doc.dirty


def test_batch_change_is_one_undo_step():
    doc = Document({'metadata': {'layers': [{'name': 'TOP', 'type': 'signal'}]}, 'wires': []})
    wires = [
        {'layer': 'TOP', 'width': 2, 'net': 'A', 'points': [[0, 0], [10, 0]]},
        {'layer': 'TOP', 'width': 2, 'net': 'B', 'points': [[0, 5], [10, 5]]},
    ]
    doc.batch_change(('wires', index, wire) for index, wire in enumerate(wires))
    assert doc.data['wires'] == wires and doc.position == 1
    doc.undo()
    assert doc.data['wires'] == []
    doc.redo()
    assert doc.data['wires'] == wires

@pytest.fixture(scope='module')
def qt_app():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app

@pytest.mark.parametrize('sample',SAMPLES, ids=lambda p:p.name)
def test_gui_sample(qt_app, sample):
    from pcb_viewer.app import MainWindow
    window = MainWindow(str(sample))
    window.show()
    qt_app.processEvents()
    assert window.document.path == sample
    assert window.view.scene().items()
    window.view.set_mode('Rectangle')
    before = len(window.document.data['keepouts'])
    window.view.finish([[0,0],[10,0],[10,10],[0,10]])
    assert len(window.document.data['keepouts']) == before+1
    window.history(False)
    assert len(window.document.data['keepouts']) == before
    window.close()

def test_mouse_rectangle_selection_edit_delete(qt_app, tmp_path):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    from pcb_viewer.app import MainWindow
    window = MainWindow()
    window.show()
    window.fit()
    qt_app.processEvents()
    window.mode.setCurrentText('Rectangle')
    for x,y in ((0,0),(30,20)):
        point = window.view.mapFromScene(QPointF(x,-y))
        QTest.mouseClick(window.view.viewport(),Qt.MouseButton.LeftButton, pos=point)
    assert len(window.document.data['keepouts']) == 1
    window.mode.setCurrentText('Select')
    item = next(i for i in window.view.scene().items() if i.data(0))
    item.setSelected(True)
    assert window.selected() == [('keepouts',0)]
    obj = json.loads(window.properties.toPlainText())
    obj['name'] = 'My mask'
    window.properties.setPlainText(json.dumps(obj))
    window.apply_properties()
    assert window.document.data['keepouts'][0]['name'] == 'My mask'
    next(i for i in window.view.scene().items() if i.data(0)).setSelected(True)
    window.delete()
    assert not window.document.data['keepouts']
    window.history(False)
    assert window.document.data['keepouts'][0]['name'] == 'My mask'
    window.document.save(tmp_path/'mask.json')
    window.close()

@pytest.mark.parametrize('value', [
    {'x':0,'y':0,'layers':'TOP'},
    {'x':0,'y':0,'shape':{'shape':'rect'}},
    {'x':0,'y':0,'shape':{'shape':'circle','radius':-1}},
    {'x':0,'y':0,'layer':[]},
])
def test_reject_properties_before_commit(value):
    from pcb_viewer.model import validate_record
    with pytest.raises(ValueError):
        validate_record('pads', value)


def test_open_resets_stale_controls(qt_app):
    from pcb_viewer.app import MainWindow
    w = MainWindow()
    w.search.setText('nonexistent net')
    w.mode.setCurrentText('Polygon')
    w.categories['keepouts'].setChecked(False)
    w.load(str(SAMPLES[0]))
    assert not w.search.text()
    assert w.view.mode == 'Select'
    assert w.categories['keepouts'].isChecked()
    w.close()


def test_multiple_board_tabs_preserve_document_and_view_state(qt_app):
    from pcb_viewer.app import MainWindow
    w = MainWindow()
    assert w.tabs.count() == 1
    assert w.open_in_tab(str(SAMPLES[0]))
    first = w.document
    w.search.setText('first-tab-filter')
    w.grid.setValue(3.5)
    assert w.open_in_tab(str(SAMPLES[1]))
    second = w.document
    assert w.tabs.count() == 2 and second is not first
    w.search.setText('second-tab-filter')
    w.tabs.setCurrentIndex(0)
    qt_app.processEvents()
    assert w.document is first
    assert w.search.text() == 'first-tab-filter'
    assert w.grid.value() == 3.5
    w.tabs.setCurrentIndex(1)
    qt_app.processEvents()
    assert w.document is second
    assert w.search.text() == 'second-tab-filter'
    # Opening an already-open path focuses its tab rather than duplicating it.
    assert w.open_in_tab(str(SAMPLES[0]))
    assert w.tabs.count() == 2 and w.tabs.currentIndex() == 0
    w.close()


def test_manual_routing_mode_is_removed(qt_app):
    from pcb_viewer.app import MainWindow
    w = MainWindow()
    assert [w.mode.itemText(index) for index in range(w.mode.count())] == [
        'Select', 'Rectangle', 'Polygon']
    assert not hasattr(w, 'route_net')
    assert not hasattr(w, 'route_width')
    w.close()


def test_live_routing_preview_paints_and_rolls_back_without_edit(qt_app):
    from pcb_viewer.app import MainWindow
    from pcb_viewer.router import Autorouter, RouterConfig
    w = MainWindow()
    old_wire = {'net': 'A', 'layer': 'TOP', 'width': 1,
                'route_type': 'autoroute', 'autoroute_job': 0,
                'points': [[0, 0], [20, 0]]}
    w.document.data['routing_connections'] = [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 10], 'end': [20, 10]},
    ]
    w.document.data['wires'] = [old_wire]
    w.show_unrouted.setChecked(True)
    w.refresh()
    old_item = next(item for item in w.view.scene().items()
                    if item.data(0) == ('wires', 0))
    original = copy.deepcopy(w.document.data)
    router = Autorouter(w.document.data, RouterConfig(
        grid_step=2, clearance=0, boundary_margin=10, iterations=1))
    w._begin_routing_preview()
    assert not old_item.isVisible()
    first = router._grid_line(router._cell((0, 0)), router._cell((20, 0)))
    router._commit(router.jobs[0], first)
    w._draw_routing_preview(router, 1, 1, 'Initial routing')
    qt_app.processEvents()
    assert len(w._routing_preview_items) == 1
    assert len(w._routing_preview_airwires) == 1
    assert '1 / 2 (preview)' == w.route_stat_labels['connections'].text()
    router._remove(0)
    w._draw_routing_preview(router, 1, 1, 'Routing complete')
    assert not w._routing_preview_items
    assert len(w._routing_preview_airwires) == 2
    w._end_routing_preview()
    assert old_item.isVisible()
    assert not w._routing_preview_airwires
    assert w.document.data == original and not w.document.dirty
    w.close()


def test_gui_autoroute_displays_live_preview_before_final_commit(qt_app, monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox, QSpinBox
    from pcb_viewer.app import MainWindow
    w = MainWindow()
    w.document.data['routing_connections'] = [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
        {'net': 'B', 'layer': 'TOP', 'width': 1,
         'start': [0, 10], 'end': [20, 10]},
    ]
    w.refresh()
    def accept(dialog):
        rounds = [spin for spin in dialog.findChildren(QSpinBox)
                  if spin.minimum() == 1 and spin.maximum() == 16]
        assert len(rounds) == 1 and rounds[0].value() == 1
        return 1

    monkeypatch.setattr(QDialog, 'exec', accept)
    monkeypatch.setattr(QMessageBox, 'information', lambda *_args: None)
    monkeypatch.setattr(QMessageBox, 'question',
                        lambda *_args: QMessageBox.StandardButton.Discard)
    snapshots = []
    original_draw = w._draw_routing_preview

    def observe(router, round_number, total_rounds, message):
        assert w._route_progress_bar is not None
        assert w._route_progress_bar.parentWidget() is w.statusBar()
        assert w._routing_in_progress and w.view.isEnabled()
        assert not w.mode.isEnabled() and not w.tabs.isEnabled()
        original_draw(router, round_number, total_rounds, message)
        snapshots.append((len(router.routes), len(w.document.data.get('wires', [])),
                          len(w._routing_preview_items)))

    monkeypatch.setattr(w, '_draw_routing_preview', observe)
    w.autoroute()
    assert any(routed > 0 and committed == 0 and painted > 0
               for routed, committed, painted in snapshots)
    assert not w._routing_preview_items and not w._routing_preview_airwires
    assert w._route_progress_bar is None
    assert not w._routing_in_progress
    assert w.mode.isEnabled() and w.tabs.isEnabled() and w.side_dock.isEnabled()
    assert len(w.document.data['wires']) == 2
    assert w.document.position == 1
    w.close()


def test_cancelled_gui_autoroute_restores_existing_copper(qt_app, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog
    from pcb_viewer.app import MainWindow
    w = MainWindow()
    existing = {'net': 'A', 'layer': 'TOP', 'width': 1,
                'route_type': 'autoroute', 'autoroute_job': 0,
                'points': [[0, 0], [20, 0]]}
    w.document.data['routing_connections'] = [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
    ]
    w.document.data['wires'] = [existing]
    w.refresh()
    old_item = next(item for item in w.view.scene().items()
                    if item.data(0) == ('wires', 0))
    def accept(_dialog):
        QTimer.singleShot(0, lambda: w._route_cancel_button.click())
        return 1

    monkeypatch.setattr(QDialog, 'exec', accept)
    w.autoroute()
    assert w.document.data['wires'] == [existing]
    assert not w.document.dirty
    assert old_item.isVisible()
    assert not w._routing_preview_items
    assert w._route_cancel_button is None
    assert not w._routing_in_progress
    assert w.mode.isEnabled() and w.tabs.isEnabled() and w.side_dock.isEnabled()
    w.close()


def test_switching_tabs_clears_transient_routing_preview(qt_app):
    from pcb_viewer.app import MainWindow
    from pcb_viewer.router import Autorouter, RouterConfig
    w = MainWindow()
    w.document.data['routing_connections'] = [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
    ]
    router = Autorouter(w.document.data, RouterConfig(grid_step=2,
                                                       boundary_margin=10))
    w._begin_routing_preview()
    router._commit(router.jobs[0], router._grid_line(
        router._cell((0, 0)), router._cell((20, 0))))
    w._draw_routing_preview(router, 1, 1, 'Initial routing')
    assert w._routing_preview_items
    w.new()
    assert not w._routing_preview_items
    assert w._routing_preview_document is None
    assert not any('Provisional route:' in item.toolTip()
                   for item in w.view.scene().items())
    w.close()


def test_unrouted_overlay_is_optional_and_follows_active_tab(qt_app):
    from pcb_viewer.app import MainWindow
    w = MainWindow()
    w.document.data['routing_connections'] = [
        {'net': 'N1', 'layer': 'TOP', 'width': 2, 'start': [0, 0], 'end': [20, 0]},
    ]
    w.show_unrouted.setChecked(True)
    assert any('Unrouted: N1' in item.toolTip() for item in w.view.scene().items())
    w.new()
    assert not w.show_unrouted.isChecked()
    w.tabs.setCurrentIndex(0)
    assert w.show_unrouted.isChecked()
    w.document.data['wires'] = [{'net': 'N1', 'layer': 'TOP', 'width': 2,
                                 'route_type': 'autoroute', 'autoroute_job': 0,
                                 'points': [[0, 0], [20, 0]]}]
    w.refresh()
    assert not any('Unrouted: N1' in item.toolTip() for item in w.view.scene().items())
    w.close()


def test_auto_route_statistics_distinguish_nets_from_connections():
    from pcb_viewer.app import auto_route_statistics
    data = {
        'routing_connections': [
            {'net': 'A', 'layer': 'TOP', 'start': [0, 0], 'end': [10, 0]},
            {'net': 'A', 'layer': 'TOP', 'start': [10, 0], 'end': [20, 0]},
            {'net': 'B', 'layer': 'TOP', 'start': [0, 10], 'end': [10, 10]},
        ],
        'wires': [
            {'net': 'A', 'layer': 'TOP', 'route_type': 'autoroute',
             'autoroute_jobs': [0, 0], 'points': [[0, 0], [10, 0]]},
            {'net': 'B', 'layer': 'TOP', 'route_type': 'autoroute',
             'autoroute_job': 2, 'points': [[0, 10], [10, 10]]},
            {'net': 'OTHER', 'layer': 'TOP', 'route_type': 'autoroute',
             'autoroute_job': 1, 'points': [[0, 20], [10, 20]]},
        ],
    }
    assert auto_route_statistics(data) == {
        'nets_total': 2, 'nets_routed': 1, 'nets_partial': 1,
        'connections_total': 3, 'connections_routed': 2, 'wire_length': 30.0,
    }


def test_auto_route_sidebar_tracks_tabs_and_undo(qt_app):
    from pcb_viewer.app import MainWindow
    w = MainWindow()
    w.document.data['routing_connections'] = [
        {'net': 'A', 'layer': 'TOP', 'start': [0, 0], 'end': [10, 0]},
        {'net': 'A', 'layer': 'TOP', 'start': [10, 0], 'end': [20, 0]},
    ]
    w.refresh()
    labels = w.route_stat_labels
    assert labels['nets_routed'].text() == '0'
    assert labels['nets_total'].text() == '1'
    assert labels['connections'].text() == '0 / 2'
    wire = {'net': 'A', 'layer': 'TOP', 'width': 2, 'route_type': 'autoroute',
            'autoroute_job': 0, 'points': [[0, 0], [10, 0]]}
    w.document.batch_change([('wires', 0, wire)])
    w.refresh()
    assert labels['nets_routed'].text() == '0'
    assert labels['nets_total'].text() == '1'
    assert labels['partial'].text() == '1'
    assert labels['connections'].text() == '1 / 2'
    assert labels['unrouted'].text() == '1'
    assert labels['completion'].text() == '50.0%'
    assert labels['length'].text() == '10.0 units'
    w.new()
    assert labels['nets_routed'].text() == '0'
    assert labels['nets_total'].text() == '0'
    assert labels['completion'].text() == '—'
    w.tabs.setCurrentIndex(0)
    assert labels['connections'].text() == '1 / 2'
    w.history(False)
    assert labels['connections'].text() == '0 / 2'
    w.history(True)
    assert labels['connections'].text() == '1 / 2'
    w.document.saved_position = w.document.position
    w.close()


def test_gui_stage_selection_reaches_router(qt_app, monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox, QPushButton
    from pcb_viewer.app import MainWindow, RoutingStagesDialog
    w = MainWindow()
    w.document.data['routing_connections'] = [
        {'net': 'A', 'layer': 'TOP', 'width': 1,
         'start': [0, 0], 'end': [20, 0]},
    ]
    w.refresh()

    def accept(dialog):
        if isinstance(dialog, RoutingStagesDialog):
            for stage, checkbox in dialog.checks.items():
                checkbox.setChecked(stage == 'initial')
        else:
            dialog.findChild(QPushButton, 'routing_stages').click()
        return 1

    monkeypatch.setattr(QDialog, 'exec', accept)
    monkeypatch.setattr(QMessageBox, 'information', lambda *_args: None)
    monkeypatch.setattr(QMessageBox, 'question',
                        lambda *_args: QMessageBox.StandardButton.Discard)
    w.autoroute()
    report = w.sessions[w.current_session]['benchmarks']['Built-in']
    assert report.config['enabled_stages'] == ['initial']
    assert [row['stage'] for row in report.stage_results] == ['initial']
    assert report.routed == 1
    w.close()

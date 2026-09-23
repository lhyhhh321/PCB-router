import copy
import json
import os
from pathlib import Path
import pytest
from pcb_viewer.model import Document, polygon

SAMPLES = list(Path('pcb_example').glob('*.json'))

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

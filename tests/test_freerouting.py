import zipfile

import pytest

from pcb_viewer import freerouting
from pcb_viewer.freerouting import (_connected_jobs, export_dsn,
                                    parse_routed_session, required_java_version,
                                    resolve_java)
from pcb_viewer.router import RouterConfig, extract_jobs


def test_java_25_runtime_is_selected_for_java_25_jar(tmp_path, monkeypatch):
    jar = tmp_path / 'freerouting.jar'
    with zipfile.ZipFile(jar, 'w') as archive:
        archive.writestr('META-INF/MANIFEST.MF',
                         'Manifest-Version: 1.0\r\nMain-Class: app.freerouting.Main\r\n')
        archive.writestr('app/freerouting/Main.class',
                         b'\xca\xfe\xba\xbe\x00\x00\x00\x45')
    assert required_java_version(jar) == 25
    monkeypatch.setattr(freerouting, '_java_version', lambda path: 21)
    with pytest.raises(ValueError, match='requires Java 25 or newer'):
        resolve_java(jar, 'java21')
    monkeypatch.setattr(freerouting, '_java_version', lambda path: 25)
    assert resolve_java(jar, 'java25') == ('java25', 25)


def test_dsn_uses_only_deleted_connection_endpoints():
    board = {'layer': 'TOP', 'deleted_connections': {'wires': [
        {'net': 'N', 'layer': 'TOP', 'width': 2,
         'points': [[0, 0], [7, 23], [40, 0]]},
    ]}}
    dsn = export_dsn(board, RouterConfig())
    assert '(pin PAD_P0 P0 0 0)' in dsn
    assert '(pin PAD_P1 P1 40 0)' in dsn
    assert '7 23' not in dsn
    assert '(net "N"' in dsn


def test_session_routes_are_scaled_back_to_source_units():
    session = '''(session "test" (routes (resolution mil 1000)
      (network_out (net N (wire (path TOP 2000 40000 0 0 0))))))'''
    wires = parse_routed_session(session, 'TOP')
    assert wires[0]['width'] == 2
    assert wires[0]['points'] == [[40, 0], [0, 0]]


def test_unrelated_copper_is_an_obstacle_not_an_extra_routing_net():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'N', 'layer': 'TOP', 'width': 2, 'start': [0, 0], 'end': [40, 0]},
    ], 'copper_polygons': [
        {'net': 'OTHER', 'layer': 'TOP',
         'vertices': [[10, 10], [20, 10], [20, 20], [10, 10]]},
    ]}
    dsn = export_dsn(board, RouterConfig())
    assert '(wire_keepout (polygon "TOP" 0 10 10 20 10 20 20))' in dsn
    assert '(net "OTHER"' not in dsn


def test_import_connectivity_accepts_contact_at_pad_edge():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'N', 'layer': 'TOP', 'width': 2, 'start': [0, 0], 'end': [40, 0]},
    ], 'pads': [
        {'net': 'N', 'layer': 'TOP', 'x': 0, 'y': 0,
         'shape': {'shape': 'circle', 'radius': 4}},
    ]}
    wires = [{'net': 'N', 'width': 2, 'points': [[4, 0], [40, 0]]}]
    assert _connected_jobs(extract_jobs(board), wires, board) == {0}


def test_import_connectivity_counts_same_net_crossing_junction():
    board = {'layer': 'TOP', 'routing_connections': [
        {'net': 'N', 'layer': 'TOP', 'width': 2, 'start': [0, 0], 'end': [0, 10]},
    ]}
    wires = [{'net': 'N', 'width': 2, 'points': [[0, 0], [10, 10]]},
             {'net': 'N', 'width': 2, 'points': [[0, 10], [10, 0]]}]
    assert _connected_jobs(extract_jobs(board), wires) == {0}

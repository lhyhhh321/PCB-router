"""Lossless JSON document storage and small, reversible editing commands."""
import copy
import json
import math
import os
from pathlib import Path
import tempfile

COLLECTIONS = ('wires', 'vias', 'pins', 'pads', 'copper_polygons', 'keepouts', 'components')


def validate_record(group, record):
    if not isinstance(record, dict):
        raise ValueError('Objects must be JSON dictionaries.')
    def number(n):
        return isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
    for key in ('layer', 'net', 'ref', 'pin_ref', 'padstack', 'via_type', 'type'):
        if key in record and record[key] is not None and not isinstance(record[key], str):
            raise ValueError(f'{key} must be a string.')
    if 'layers' in record and (not isinstance(record['layers'], list) or
                              not all(isinstance(x, str) for x in record['layers'])):
        raise ValueError('layers must be an array of names.')
    if 'rotation' in record and not number(record['rotation']):
        raise ValueError('rotation must be finite.')
    if group in ('pins', 'pads', 'vias', 'components') and 'shape' in record:
        shape = record['shape']
        if not isinstance(shape, dict) or shape.get('shape') not in ('circle', 'rect'):
            raise ValueError('Pad shape must be a circle or rect object.')
        if shape['shape'] == 'circle':
            if not number(shape.get('radius')) or shape['radius'] <= 0:
                raise ValueError('Pad radius must be positive.')
        elif (not all(number(shape.get(k)) for k in ('x1', 'y1', 'x2', 'y2')) or
              shape['x1'] >= shape['x2'] or shape['y1'] >= shape['y2']):
            raise ValueError('Pad rectangle requires valid bounds.')
    key = 'vertices' if group == 'copper_polygons' else 'points'
    if group in ('wires', 'copper_polygons', 'keepouts'):
        points = record.get(key)
        minimum = 2 if group == 'wires' else 3
        if not isinstance(points, list) or len(points) < minimum:
            raise ValueError(f'{group} requires at least {minimum} {key}.')
        if any(not isinstance(p, (list, tuple)) or len(p) != 2 or not all(number(n) for n in p) for p in points):
            raise ValueError('Coordinates must be finite [x, y] pairs.')
    else:
        if not all(number(record.get(k)) for k in ('x', 'y')):
            raise ValueError('x and y must be finite numbers.')
    if 'width' in record and (not number(record['width']) or record['width'] <= 0):
        raise ValueError('Width must be positive.')


def polygon(points):
    pts = [list(p) for p in points]
    if pts and pts[0] == pts[-1]:
        pts.pop()
    validate_record('keepouts', {'points': pts})
    if len(set(map(tuple, pts))) != len(pts):
        raise ValueError('Polygon vertices must be distinct.')
    def cross(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    area = sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(pts, pts[1:]+pts[:1]))
    if abs(area) < 1e-9:
        raise ValueError('Keepout must have nonzero area.')
    def on(a,b,c):
        return min(a[0],b[0]) <= c[0] <= max(a[0],b[0]) and min(a[1],b[1]) <= c[1] <= max(a[1],b[1])
    edges = list(zip(pts, pts[1:]+pts[:1]))
    for i,(a,b) in enumerate(edges):
        for j,(c,d) in enumerate(edges):
            if j <= i+1 or (i == 0 and j == len(edges)-1):
                continue
            u,v,w,z = cross(a,b,c),cross(a,b,d),cross(c,d,a),cross(c,d,b)
            if (u*v < 0 and w*z < 0) or any(t == 0 and on(e,f,g) for t,e,f,g in [(u,a,b,c),(v,a,b,d),(w,c,d,a),(z,c,d,b)]):
                raise ValueError('Keepout edges must not intersect.')
    return pts + [pts[0][:]]


class Document:
    def __init__(self, data=None, path=None):
        self.data = data if data is not None else {'metadata': {'layers': [{'name':'TOP','type':'signal'}]}, 'keepouts': []}
        self.path = Path(path) if path else None
        self.history = []
        self.position = 0
        self.saved_position = 0

    @classmethod
    def load(cls, path):
        with open(path, encoding='utf-8-sig') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError('The board must be a JSON object.')
        if not any(k in data for k in COLLECTIONS):
            raise ValueError('No PCB geometry collections found.')
        for key in COLLECTIONS:
            if key in data:
                if not isinstance(data[key], list):
                    raise ValueError(f'{key} must be an array.')
                for record in data[key]:
                    validate_record(key, record)
        return cls(data, path)

    @property
    def dirty(self):
        return self.position != self.saved_position

    @property
    def layers(self):
        names = [v['name'] for v in self.data.get('metadata', {}).get('layers', [])]
        for group in COLLECTIONS:
            for obj in self.data.get(group, []):
                for name in [obj.get('layer')] + obj.get('layers', []):
                    if name and name not in names:
                        names.append(name)
        return names or [self.data.get('layer', 'TOP')]

    def change(self, group, index, value):
        if value is not None:
            validate_record(group, value)
            if group == 'keepouts':
                value = copy.deepcopy(value)
                value['points'] = polygon(value['points'])
        records = self.data.get(group, [])
        before = copy.deepcopy(records[index]) if index < len(records) else None
        existed = group in self.data
        if self.saved_position > self.position:
            self.saved_position = -1
        del self.history[self.position:]
        self.history.append((group, index, before, copy.deepcopy(value), existed))
        self._apply(group, index, before, value)
        self.position += 1

    def batch_change(self, changes):
        """Apply several geometry edits as one undoable command."""
        normalized = []
        for group, index, value in changes:
            if value is not None:
                validate_record(group, value)
                value = copy.deepcopy(value)
                if group == 'keepouts':
                    value['points'] = polygon(value['points'])
            normalized.append((group, index, value))
        prepared = []
        for group, index, value in normalized:
            records = self.data.get(group, [])
            before = copy.deepcopy(records[index]) if index < len(records) else None
            existed = group in self.data
            prepared.append((group, index, before, value, existed))
            self._apply(group, index, before, value)
        if not prepared:
            return
        if self.saved_position > self.position:
            self.saved_position = -1
        del self.history[self.position:]
        self.history.append(('__batch__', prepared))
        self.position += 1

    def _apply(self, group, index, old, new):
        records = self.data.setdefault(group, [])
        if new is None:
            records.pop(index)
        elif old is None:
            records.insert(index, copy.deepcopy(new))
        else:
            records[index] = copy.deepcopy(new)

    def undo(self):
        if self.position:
            self.position -= 1
            command = self.history[self.position]
            commands = reversed(command[1]) if command[0] == '__batch__' else (command,)
            for g,i,old,new,existed in commands:
                self._apply(g,i,new,old)
                if not existed and not self.data[g]:
                    del self.data[g]

    def redo(self):
        if self.position < len(self.history):
            command = self.history[self.position]
            commands = command[1] if command[0] == '__batch__' else (command,)
            for g,i,old,new,_ in commands:
                self._apply(g,i,old,new)
            self.position += 1

    def save(self, path=None):
        target = Path(path or self.path)
        output = copy.deepcopy(self.data)
        if self.history and isinstance(output.get('stats'), dict):
            for group in COLLECTIONS:
                for key in (group, 'total_'+group):
                    if key in output['stats']:
                        output['stats'][key] = len(output.get(group, []))
            for group,key in [('wires','wires_by_layer'),('copper_polygons','polygons_by_layer'),('vias','vias_by_type')]:
                if key in output['stats']:
                    counts = {}
                    for obj in output.get(group, []):
                        label = obj.get('via_type' if group == 'vias' else 'layer', '')
                        counts[label] = counts.get(label, 0)+1
                    output['stats'][key] = counts
        tmp = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=target.parent, delete=False) as f:
                tmp = f.name
                json.dump(output, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        self.path = target
        self.saved_position = self.position

"""Mutation fuzzing for the four timeline importers (FCPXML, FCP 7 XML, CMX3600 EDL, OTIO).

``mutants(seed, kind, count, rng)`` yields mutated copies of a valid exported file: numeric
extremes, foreign or unknown encodings, truncation, deep nesting, deleted, duplicated and
swapped spans, JSON type swaps (OTIO) and line damage (EDL). ``run(...)`` imports each one and
returns the inputs that failed with anything the server would not answer as a client error.

The suite runs a small fixed-seed sample (tests/test_interchange_fuzz.py); for a long run::

    python3 tests/interchange_fuzz.py 3000
"""
import json
import random
import re
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

# The exceptions server.py answers with HTTP 400 on a POST route.
CLIENT_ERRORS = (ValueError, KeyError, TypeError)
EXTREMES = ['0', '-0', '-1', '1e-300', '-1e-300', '1e308', '1e309', '-1e308', 'nan', 'NaN', 'inf', '-inf',
            'Infinity', '99999999999999999999999', '2147483648', '-2147483649', '0.0000001', '1/0', '0/0', '-5/0',
            '1e-300/1', '1/1e-300', '1/99999999999999999999', '3.3', '', ' ', 'x', '1e', '0x10', '١٢']
ENCODINGS = ['nan', 'x', 'ebcdic', 'utf-16', 'utf-32', 'UTF-16LE', 'latin-1', 'cp1252', 'ascii', 'shift_jis',
             'utf-7', 'idna', 'rot13', 'base64', 'hex', 'zlib', 'punycode', 'unicode_escape', '']
NUMBER = re.compile(r'-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?:/\d+)?s?')
JSON_VALUES = [None, True, False, 0, -1, 1e-300, 1e308, -1e308, 2 ** 64, '', 'x', [], {}, [None], {'OTIO_SCHEMA': 'x'},
               {'OTIO_SCHEMA': 'RationalTime.1', 'rate': 1e-300, 'value': 1},
               {'OTIO_SCHEMA': 'RationalTime.1', 'rate': 24, 'value': 1e300}]


def _numbers(text, rng):
    spans = [m.span() for m in NUMBER.finditer(text)]
    if not spans:
        return text
    for start, end in sorted(rng.sample(spans, min(len(spans), rng.randint(1, 6))), reverse=True):
        text = text[:start] + rng.choice(EXTREMES) + text[end:]
    return text


def _encoding(text, rng):
    declaration = f'<?xml version="1.0" encoding="{rng.choice(ENCODINGS)}"?>'
    end = text.find('?>') if text.startswith('<?xml') else -1
    if end >= 0:
        return declaration + text[end + 2:]
    return declaration + text


def _nest(text, rng, kind):
    depth = rng.choice([70, 200, 2000, 20000])
    if kind == 'otio':
        return '[' * depth + text + ']' * depth if rng.random() < .5 else text.replace(
            '"children": [', '"children": [' + '{"OTIO_SCHEMA": "Stack.1", "children": [' * depth, 1)
    tag = rng.choice(['sequence', 'clipitem', 'spine', 'asset-clip', 'x'])
    marker = rng.choice(['<spine>', '<track>', '<sequence', '<library', '<'])
    position = text.find(marker)
    position = position if position >= 0 else len(text) // 2
    return text[:position] + f'<{tag}>' * depth + f'</{tag}>' * depth + text[position:]


def _spans(text, rng):
    if len(text) < 4:
        return text
    a, b = sorted(rng.sample(range(len(text)), 2))
    choice = rng.random()
    if choice < .3:
        return text[:a] + text[b:]
    if choice < .6:
        return text[:b] + text[a:b] * rng.randint(2, 6) + text[b:]
    c, d = sorted(rng.sample(range(len(text)), 2))
    return text[:a] + text[c:d] + text[b:]


def _bytes(text, rng):
    data = bytearray(text.encode('utf-8'))
    for _ in range(rng.randint(1, 24)):
        if data:
            data[rng.randrange(len(data))] = rng.randrange(256)
    return data.decode('utf-8', 'replace')


def _json(text, rng):
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        return text
    nodes = []

    def walk(node, depth=0):
        if depth > 40:
            return
        if isinstance(node, dict):
            for key in list(node):
                nodes.append((node, key))
                walk(node[key], depth + 1)
        elif isinstance(node, list):
            for index in range(len(node)):
                nodes.append((node, index))
                walk(node[index], depth + 1)
    walk(document)
    for parent, key in rng.sample(nodes, min(len(nodes), rng.randint(1, 8))):
        try:
            parent[key] = rng.choice(JSON_VALUES)
        except (IndexError, KeyError):
            continue
    return json.dumps(document)


def _lines(text, rng):
    lines = text.splitlines()
    if not lines:
        return text
    for _ in range(rng.randint(1, 6)):
        index = rng.randrange(len(lines))
        action = rng.random()
        if action < .25:
            lines[index] = ' '.join(rng.choice(EXTREMES) or '0' for _ in range(rng.randint(1, 9)))
        elif action < .5:
            fields = lines[index].split()
            if fields:
                fields[rng.randrange(len(fields))] = rng.choice(
                    ['99:99:99:99', '00:00:00:00', '-1:00:00:00', '23:59:59;29', '1e308', 'C', 'D', 'W001', 'K', 'B',
                     'AA/V', 'NONE', '999', '00:00:00:99', 'M2', '-1.0', '0.0', '9999999.0'])
                lines[index] = ' '.join(fields)
        elif action < .75:
            lines.insert(index, lines[index])
        else:
            lines[index] = rng.choice(['FCM: DROP FRAME', 'FCM: NON-DROP FRAME', 'TITLE: ' + 'x' * 5000,
                                       '* FROM CLIP NAME: ' + '\u0000' * 4, 'M2   AX       999.0    00:00:00:00',
                                       '001  AX       V     C        00:00:00:00 00:00:00:00 00:00:00:00 00:00:00:00'])
    return '\n'.join(lines)


def mutants(seed, kind, count, rng):
    strategies = [_numbers, _numbers, _spans, _bytes, lambda t, r: t[:r.randrange(len(t) + 1)],
                  lambda t, r: _nest(t, r, kind)]
    if kind in ('fcpxml', 'xmeml'):
        strategies += [_encoding, _encoding]
    if kind == 'otio':
        strategies += [_json, _json, _json]
    if kind == 'edl':
        strategies += [_lines, _lines, _lines]
    for _ in range(count):
        text = seed
        for _ in range(rng.choice([1, 1, 2, 3])):
            text = rng.choice(strategies)(text, rng)
        yield text


FILENAMES = {'fcpxml': 'fuzz.fcpxml', 'xmeml': 'fuzz.xml', 'edl': 'fuzz.edl', 'otio': 'fuzz.otio'}


def run(seeds, count, seed=0, slow=5.0):
    """Import ``count`` mutants of each ``seeds[kind]``. Returns (failures, slow, counts): failures
    are (kind, exception, text) for every non-client error; slow lists imports over ``slow`` s."""
    from interchange import api
    with warnings.catch_warnings():
        # a mutant declaring encoding="unicode_escape" makes that codec warn about its escapes
        warnings.simplefilter('ignore', DeprecationWarning)
        return _run(api, seeds, count, seed, slow)


def _run(api, seeds, count, seed, slow):
    rng = random.Random(seed)
    failures, slow_ones, counts = [], [], {}
    for kind, text in seeds.items():
        for mutant in mutants(text, kind, count, rng):
            options = {'fps': rng.choice(['23.976', '25', '29.97', '1e-300', 'nan', '0', '60']),
                       'width': rng.choice([None, 1920, '1e308', 'nan', 16])} if kind == 'edl' else {}
            started = time.monotonic()
            try:
                api.import_timeline(FILENAMES[kind], mutant, options)
                outcome = 'ok'
            except CLIENT_ERRORS as error:
                outcome = type(error).__name__
            except RecursionError as error:  # a RuntimeError: the server would answer 500
                failures.append((kind, error, mutant))
                outcome = 'RecursionError'
            except Exception as error:  # noqa: BLE001 - every other failure is what the fuzz looks for
                failures.append((kind, error, mutant))
                outcome = type(error).__name__
            took = time.monotonic() - started
            if took > slow:
                slow_ones.append((kind, took, mutant))
            counts[(kind, outcome)] = counts.get((kind, outcome), 0) + 1
    return failures, slow_ones, counts


def seeds_from_export():
    """The four formats exported from the synthetic fixture timeline. Its media paths name files
    that do not exist; the importers never open media (linking is a separate step)."""
    from interchange import api
    from interchange_fixtures import synthetic_project
    names = ['camera_a', 'camera_b', 'drop', 'plain', 'tone']
    paths = {name: Path('/fuzz/card') / f'{name}.mov' for name in names}
    result = api.export_timeline(synthetic_project(paths), {'formats': ['fcpxml', 'edl', 'otio', 'xmeml'],
                                                             'unknownTimecode': 'zero'})
    files = {f['name']: f['content'] for f in result['files']}
    pick = lambda suffix: next(text for name, text in files.items() if name.endswith(suffix))
    return {'fcpxml': pick('.fcpxml'), 'xmeml': pick('.xml'), 'edl': pick('-V1.edl'), 'otio': pick('.otio')}


if __name__ == '__main__':
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    failures, slow_ones, counts = run(seeds_from_export(), total, seed=int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    for (kind, outcome), number in sorted(counts.items()):
        print(f'{kind:7} {outcome:22} {number}')
    seen = {}
    for kind, error, text in failures:
        seen.setdefault((kind, type(error).__name__, str(error)[:80]), text)
    for (kind, name, message), text in seen.items():
        print(f'FAIL {kind} {name}: {message}\n  {text[:200]!r}')
    for kind, took, _ in slow_ones[:10]:
        print(f'SLOW {kind} {took:.1f}s')
    print(f'{len(failures)} failures, {len(slow_ones)} slow, {sum(counts.values())} imports')

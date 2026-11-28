"""A stand-in Electron binary: just a fuse wire behind Electron's sentinel, as @electron/fuses expects."""
SENTINEL = b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX'
# Electron 45's nine V1 fuses, as shipped: RunAsNode on, cookie encryption off, NODE_OPTIONS and
# --inspect on, the rest as Electron sets them.
SHIPPED = b'101111111'


def fake_electron(path, wire=SHIPPED):
    path.write_bytes(b'MZ' + b'\0' * 64 + SENTINEL + bytes([1, len(wire)]) + wire + b'\0' * 64)
    return path


def _wire(path):
    data = path.read_bytes()
    start = data.index(SENTINEL) + len(SENTINEL)
    return data[start + 2:start + 2 + data[start + 1]].decode()


fake_electron.wire = _wire

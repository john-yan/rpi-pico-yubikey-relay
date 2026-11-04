# cbor_min.py — minimal CBOR codec for CTAP2 messages (Auto-UV, plan.md §5.2).
#
# Supports exactly the types CTAP2 uses: unsigned/negative integers,
# byte strings, text strings, definite-length arrays and maps, and the
# simple values true/false/null. Tags are decoded transparently (CTAP2
# does not use them); indefinite lengths and floats raise ValueError.
#
# Any decode/encode failure must be caught by the caller and treated
# as "pass the message through unchanged" (fail-open, plan.md §7) — a
# codec error must never break the relay pipe.

MAJOR_UINT = 0
MAJOR_NEGINT = 1
MAJOR_BYTES = 2
MAJOR_TEXT = 3
MAJOR_ARRAY = 4
MAJOR_MAP = 5
MAJOR_TAG = 6
MAJOR_SIMPLE = 7

# Nesting cap: CTAP2 messages are shallow (<= 4 levels); anything deeper
# is hostile input (a 2-bytes-per-level bomb inside the 4 KiB reassembly
# window reaches ~2000 frames). Raise ValueError instead of risking stack
# exhaustion on the Pico.
MAX_DEPTH = 16


def _read_head(data, i):
    """Return (major, value, next_index) for the item header at data[i]."""
    b = data[i]
    major = b >> 5
    info = b & 0x1F
    i += 1
    if info < 24:
        return major, info, i
    if info == 24:
        return major, data[i], i + 1
    if info == 25:
        return major, (data[i] << 8) | data[i + 1], i + 2
    if info == 26:
        v = 0
        for k in range(4):
            v = (v << 8) | data[i + k]
        return major, v, i + 4
    if info == 27:
        v = 0
        for k in range(8):
            v = (v << 8) | data[i + k]
        return major, v, i + 8
    raise ValueError("indefinite/reserved additional info")


def _decode1(data, i, depth=0):
    if depth > MAX_DEPTH:
        raise ValueError("CBOR nesting too deep")
    major, val, i = _read_head(data, i)
    if major == MAJOR_UINT:
        return val, i
    if major == MAJOR_NEGINT:
        return -1 - val, i
    if major == MAJOR_BYTES:
        return bytes(data[i : i + val]), i + val
    if major == MAJOR_TEXT:
        return str(data[i : i + val], "utf-8"), i + val
    if major == MAJOR_ARRAY:
        out = []
        for _ in range(val):
            v, i = _decode1(data, i, depth + 1)
            out.append(v)
        return out, i
    if major == MAJOR_MAP:
        out = {}
        for _ in range(val):
            k, i = _decode1(data, i, depth + 1)
            v, i = _decode1(data, i, depth + 1)
            out[k] = v
        return out, i
    if major == MAJOR_TAG:
        v, i = _decode1(data, i, depth + 1)
        return v, i  # tags are not used by CTAP2; decode through
    if major == MAJOR_SIMPLE:
        if val == 20:
            return False, i
        if val == 21:
            return True, i
        if val == 22:
            return None, i
        raise ValueError("unsupported simple/float value")
    raise ValueError("bad major type")


def decode(data):
    """Decode a single definite-length CBOR item; trailing bytes raise."""
    try:
        v, i = _decode1(data, 0)
    except IndexError:
        raise ValueError("truncated CBOR")
    if i != len(data):
        raise ValueError("trailing bytes")
    return v


def _head(major, val):
    if val < 24:
        return bytes([(major << 5) | val])
    if val < 0x100:
        return bytes([(major << 5) | 24, val])
    if val < 0x10000:
        return bytes([(major << 5) | 25, (val >> 8) & 0xFF, val & 0xFF])
    if val < 0x100000000:
        return bytes(
            [
                (major << 5) | 26,
                (val >> 24) & 0xFF,
                (val >> 16) & 0xFF,
                (val >> 8) & 0xFF,
                val & 0xFF,
            ]
        )
    raise ValueError("value too large for CTAP2 use")


def encode(v):
    """Encode a Python value as canonical CTAP2 CBOR.

    Map entries are emitted sorted by encoded key, length first then
    bytewise (RFC 7049 canonical form, which CTAP2 mandates and
    python-fido2 enforces on responses). MicroPython dicts iterate in
    hash order, so explicit sorting is required, not optional.
    """
    if v is True:
        return b"\xf5"
    if v is False:
        return b"\xf4"
    if v is None:
        return b"\xf6"
    if isinstance(v, int):
        if v >= 0:
            return _head(MAJOR_UINT, v)
        return _head(MAJOR_NEGINT, -1 - v)
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v)
        return _head(MAJOR_BYTES, len(v)) + v
    if isinstance(v, str):
        e = v.encode("utf-8")
        return _head(MAJOR_TEXT, len(e)) + e
    if isinstance(v, (list, tuple)):
        out = _head(MAJOR_ARRAY, len(v))
        for x in v:
            out += encode(x)
        return out
    if isinstance(v, dict):
        entries = [(encode(k), encode(v[k])) for k in v]
        entries.sort(key=lambda e: (len(e[0]), e[0]))
        out = _head(MAJOR_MAP, len(entries))
        for ek, ev in entries:
            out += ek + ev
        return out
    raise ValueError("unsupported value type")

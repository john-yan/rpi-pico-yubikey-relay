# sha256.py — pure-Python SHA-256 for CircuitPython builds without
# hashlib.sha256 (the RP2040 CircuitPython build has a hashlib module but
# no sha256 in it).
#
# Verified byte-identical to CPython's hashlib on the test vectors in
# test/test_injector.py (empty, 'abc', 200-byte inputs). Throughput is
# ~5 blocks per ceremony HMAC - tens of milliseconds on the RP2040,
# paid once per injected request.

_K = (
    0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5,
    0x3956C25B, 0x59F111F1, 0x923F82A4, 0xAB1C5ED5,
    0xD807AA98, 0x12835B01, 0x243185BE, 0x550C7DC3,
    0x72BE5D74, 0x80DEB1FE, 0x9BDC06A7, 0xC19BF174,
    0xE49B69C1, 0xEFBE4786, 0x0FC19DC6, 0x240CA1CC,
    0x2DE92C6F, 0x4A7484AA, 0x5CB0A9DC, 0x76F988DA,
    0x983E5152, 0xA831C66D, 0xB00327C8, 0xBF597FC7,
    0xC6E00BF3, 0xD5A79147, 0x06CA6351, 0x14292967,
    0x27B70A85, 0x2E1B2138, 0x4D2C6DFC, 0x53380D13,
    0x650A7354, 0x766A0ABB, 0x81C2C92E, 0x92722C85,
    0xA2BFE8A1, 0xA81A664B, 0xC24B8B70, 0xC76C51A3,
    0xD192E819, 0xD6990624, 0xF40E3585, 0x106AA070,
    0x19A4C116, 0x1E376C08, 0x2748774C, 0x34B0BCB5,
    0x391C0CB3, 0x4ED8AA4A, 0x5B9CCA4F, 0x682E6FF3,
    0x748F82EE, 0x78A5636F, 0x84C87814, 0x8CC70208,
    0x90BEFFFA, 0xA4506CEB, 0xBEF9A3F7, 0xC67178F2,
)

_MASK = 0xFFFFFFFF


def _rotr(x, n):
    return ((x >> n) | (x << (32 - n))) & _MASK


def sha256(data) -> bytes:
    """SHA-256 digest of bytes/bytearray."""
    data = bytes(data)
    ml = len(data) * 8
    # padding: 0x80, zeros to 56 mod 64, 8-byte big-endian bit length
    pad = bytearray()
    pad.append(0x80)
    while (len(data) + len(pad)) % 64 != 56:
        pad.append(0)
    for i in range(7, -1, -1):
        pad.append((ml >> (8 * i)) & 0xFF)

    h = (
        0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A,
        0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19,
    )

    total = data + bytes(pad)
    for off in range(0, len(total), 64):
        block = total[off : off + 64]
        w = [
            (block[i] << 24) | (block[i + 1] << 16) | (block[i + 2] << 8)
            | block[i + 3]
            for i in range(0, 64, 4)
        ]
        for i in range(16, 64):
            s0 = _rotr(w[i - 15], 7) ^ _rotr(w[i - 15], 18) ^ (w[i - 15] >> 3)
            s1 = _rotr(w[i - 2], 17) ^ _rotr(w[i - 2], 19) ^ (w[i - 2] >> 10)
            w.append((w[i - 16] + s0 + w[i - 7] + s1) & _MASK)

        a, b, c, d, e, f, g, hh = h
        for i in range(64):
            S1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)
            ch = (e & f) ^ (~e & g)
            t1 = (hh + S1 + ch + _K[i] + w[i]) & _MASK
            S0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)
            maj = (a & b) ^ (a & c) ^ (b & c)
            t2 = (S0 + maj) & _MASK
            hh = g
            g = f
            f = e
            e = (d + t1) & _MASK
            d = c
            c = b
            b = a
            a = (t1 + t2) & _MASK

        h = (
            (h[0] + a) & _MASK, (h[1] + b) & _MASK,
            (h[2] + c) & _MASK, (h[3] + d) & _MASK,
            (h[4] + e) & _MASK, (h[5] + f) & _MASK,
            (h[6] + g) & _MASK, (h[7] + hh) & _MASK,
        )

    out = bytearray(32)
    for i in range(8):
        out[i * 4] = (h[i] >> 24) & 0xFF
        out[i * 4 + 1] = (h[i] >> 16) & 0xFF
        out[i * 4 + 2] = (h[i] >> 8) & 0xFF
        out[i * 4 + 3] = h[i] & 0xFF
    return bytes(out)

#!/usr/bin/env python3
"""Unit tests for the Auto-UV injector (host-side, no hardware).

Covers: HMAC-SHA-256 against CPython's hmac module, parameter placement
for makeCredential vs getAssertion (positional CTAP2 keys), uv-option
stripping, both policies, and the fail-open paths.
"""
import hashlib
import hmac as py_hmac
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "lib"))

import cbor_min  # noqa: E402
import injector  # noqa: E402
import sha256  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(f"{'ok  ' if ok else 'FAIL'} {name}")
    fails += not ok


# ---- sha256 direct vectors (cited in sha256.py's docstring) --------------
check(
    "sha256(empty)",
    sha256.sha256(b"").hex()
    == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
)
check(
    "sha256(abc)",
    sha256.sha256(b"abc").hex()
    == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
)
check("sha256(200-byte x)", sha256.sha256(b"x" * 200) == hashlib.sha256(b"x" * 200).digest())

# ---- cbor_min depth cap ---------------------------------------------------
try:
    cbor_min.decode(b"\x81" * 30 + b"\x01")
    check("cbor depth cap rejects deep nesting", False)
except ValueError:
    check("cbor depth cap rejects deep nesting", True)
expected = 1
for _ in range(10):
    expected = [expected]
check("cbor depth cap allows shallow nesting", cbor_min.decode(b"\x81" * 10 + b"\x01") == expected)


TOKEN = bytes(range(32))
CDH = bytes(range(32, 64))

# ---- HMAC correctness --------------------------------------------------
for i in range(5):
    key = os.urandom(32)
    msg = os.urandom(1 + i * 37)
    mine = injector.hmac_sha256(key, msg)
    ref = py_hmac.new(key, msg, hashlib.sha256).digest()
    check(f"hmac vs CPython #{i}", mine == ref and len(mine) == 32)
check(
    "hmac known vector",
    injector.hmac_sha256(b"\x00" * 32, b"\x01" * 32).hex()
    == py_hmac.new(b"\x00" * 32, b"\x01" * 32, hashlib.sha256).hexdigest(),
)

# ---- request shapes ------------------------------------------------------
def mc_request(options=None, cdh=CDH):
    params = {
        1: cdh,
        2: {"id": "probe.test", "name": "p"},
        3: {"id": b"u", "name": "u"},
        4: [{"type": "public-key", "alg": -7}],
    }
    if options is not None:
        params[7] = dict(options)
    return bytes([0x01]) + cbor_min.encode(params)


def ga_request(options=None, cdh=CDH, allow=None):
    params = {
        1: "probe.test",
        2: cdh,
    }
    if allow is not None:
        params[3] = [{"type": "public-key", "id": allow}]
    if options is not None:
        params[5] = dict(options)
    return bytes([0x02]) + cbor_min.encode(params)


# ---- injection, protocol 2 ----------------------------------------------
out, inj = injector.inject(mc_request({"uv": True}), TOKEN, 2, "follow")
p = cbor_min.decode(out[1:])
check("mc: injected", inj)
check("mc: param key 8 = full 32B hmac", p[8] == injector.hmac_sha256(TOKEN, CDH))
check("mc: proto key 9 = 2", p[9] == 2)
check("mc: uv stripped from options key 7", "uv" not in p.get(7, {}))
check("mc: rk preserved when present",
      cbor_min.decode(injector.inject(mc_request({"uv": True, "rk": True}), TOKEN, 2)[0][1:])[7] == {"rk": True})
check("mc: other fields intact", p[1] == CDH and p[4] == [{"type": "public-key", "alg": -7}])

out, inj = injector.inject(ga_request({"uv": True}), TOKEN, 2, "follow")
p = cbor_min.decode(out[1:])
check("ga: injected", inj)
check("ga: param key 6 = hmac", p[6] == injector.hmac_sha256(TOKEN, CDH))
check("ga: proto key 7 = 2", p[7] == 2)
check("ga: uv stripped", "uv" not in p.get(5, {}))

# protocol 1 truncation
out, inj = injector.inject(ga_request({"uv": True}), TOKEN, 1, "follow")
check("proto1: 16-byte param", cbor_min.decode(out[1:])[6] == injector.hmac_sha256(TOKEN, CDH)[:16])

# empty pinUvAuthParam (Chrome pre-flight) is REPLACED, not duplicated
pre = ga_request({"up": False})
params = cbor_min.decode(pre[1:])
params[6] = b""  # empty pre-flight param
pre = bytes([0x02]) + cbor_min.encode(params)
out, inj = injector.inject(pre, TOKEN, 2, "always")
p = cbor_min.decode(out[1:])
check("pre-flight param replaced (single entry)", p[6] == injector.hmac_sha256(TOKEN, CDH))

# ---- policies -------------------------------------------------------------
out, inj = injector.inject(ga_request(), TOKEN, 2, "follow")
check("follow: no uv option -> untouched", not inj and out == ga_request())
out, inj = injector.inject(ga_request({"uv": False}), TOKEN, 2, "follow")
check("follow: uv:false -> not injected but uv stripped",
      not inj and "uv" not in cbor_min.decode(out[1:]).get(5, {}))
out, inj = injector.inject(ga_request(), TOKEN, 2, "always")
check("always: injected without uv option", inj)
out, inj = injector.inject(mc_request({"rk": True}), TOKEN, 2, "always")
check("always: mc rk-only injected, rk kept",
      inj and cbor_min.decode(out[1:])[7] == {"rk": True})

# ---- tokenless / allowlist degradation -------------------------------------
out, inj = injector.inject(ga_request({"uv": True}), None, 2, "follow")
p = cbor_min.decode(out[1:])
check(
    "tokenless: uv stripped (graceful degradation), not injected",
    not inj and 5 not in p and "uv" not in p.get(5, {}) and p[1] == "probe.test",
)

out, inj = injector.inject(ga_request({"uv": True}), TOKEN, 2, "follow",
                          rp_allow=("probe.test",))
check("allowlisted rpId: injected", inj)
out, inj = injector.inject(ga_request({"uv": True}), TOKEN, 2, "follow",
                          rp_allow=("other.test",))
p = cbor_min.decode(out[1:])
check(
    "non-allowlisted rpId: uv stripped, not injected",
    not inj and 6 not in p and "uv" not in p.get(5, {}),
)
out, inj = injector.inject(ga_request({"uv": True}), TOKEN, 2, "follow", rp_allow=())
check("empty allowlist = allow all", inj)
out, inj = injector.inject(mc_request({"uv": True}), TOKEN, 2, "follow",
                          rp_allow=("probe.test",))
check("mc rp map id honored by allowlist", inj)

# ---- fail-open -------------------------------------------------------------
out, inj = injector.inject(ga_request({"uv": True}), None, 2)
p = cbor_min.decode(out[1:])
check(
    "fail-open: no token -> uv stripped, not injected",
    not inj and "uv" not in p.get(5, {}) and p[1] == "probe.test",
)
bad = bytes([0x02]) + b"\xff\xff\xff"  # unparseable CBOR
check("fail-open: garbage CBOR", injector.inject(bad, TOKEN, 2) == (bad, False))
check("fail-open: other command (GetInfo)",
      injector.inject(bytes([0x04]) + cbor_min.encode({}), TOKEN, 2)[1] is False)
no_cdh = bytes([0x02]) + cbor_min.encode({1: "probe.test", 5: {"uv": True}})
out, inj = injector.inject(no_cdh, TOKEN, 2, "follow")
p = cbor_min.decode(out[1:])
check(
    "fail-open: missing clientDataHash -> uv stripped, not injected",
    not inj and "uv" not in p.get(5, {}) and p[1] == "probe.test",
)
empty = injector.inject(b"", TOKEN, 2)
check("fail-open: empty message", empty == (b"", False))

print()
print("FAILURES:", fails)
sys.exit(1 if fails else 0)

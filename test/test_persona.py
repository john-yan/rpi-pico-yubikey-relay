#!/usr/bin/env python3
"""Unit tests for cbor_min + persona (host-side, no hardware).

Runs against the captured fixture test/fixtures_getinfo.hex (raw
authenticatorGetInfo response from the YubiKey 5 fw 5.4.3) and a set
of synthetic CBOR round-trips. Exit non-zero on any failure.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "lib"))

import cbor_min  # noqa: E402
import persona  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(f"{'ok  ' if ok else 'FAIL'} {name}")
    fails += not ok


# ---- CBOR round-trips ------------------------------------------------
cases = [
    0, 1, 23, 24, 255, 256, 65535, 65536, 1200,
    -1, -24, -25, -70000,
    True, False, None,
    b"", b"\x01\x02", b"\xff" * 300,
    "", "uv", "clientPin", "FIDO_2_1_PRE", "hmac-secret" * 10,
    [], [1, 2], ["U2F_V2", "FIDO_2_0"],
    {}, {1: "a"}, {4: {"uv": True, "rk": False, "clientPin": True}},
    {"int": 1, "neg": -7, "bytes": b"\x00" * 16, "arr": [{"alg": -257}]},
    # deeply-ish nested like real GetInfo
    {
        1: ["U2F_V2", "FIDO_2_0", "FIDO_2_1_PRE"],
        2: ["credProtect", "hmac-secret"],
        3: bytes(range(16)),
        4: {"rk": True, "up": True, "plat": False, "clientPin": True},
        5: 1200,
        6: [2, 1],
        7: 8,
        8: 32,
        9: [{"type": "public-key", "alg": -7}, {"type": "public-key", "alg": -257}],
        10: 0x05040300,
    },
]
for i, v in enumerate(cases):
    enc = cbor_min.encode(v)
    dec = cbor_min.decode(enc)
    check(f"round-trip #{i} ({type(v).__name__})", dec == v)

# decode rejects garbage / trailing bytes
for bad in [b"", b"\x1c", b"\x5f\x00", b"\x01\x02"]:
    try:
        cbor_min.decode(bad)
        check(f"decode(reject {bad!r})", False)
    except ValueError:
        check(f"decode(reject {bad!r})", True)

# ---- persona against the real fixture --------------------------------
raw = bytes.fromhex(open(os.path.join(HERE, "fixtures_getinfo.hex")).read())
info_before = cbor_min.decode(raw[1:])
check("fixture decodes", isinstance(info_before, dict))
check(
    "fixture has clientPin set",
    info_before.get(4, {}).get("clientPin") is True,
)
check("fixture has no uv", "uv" not in info_before.get(4, {}))

out = persona.rewrite_getinfo(raw)
info_after = cbor_min.decode(out[1:])
check("rewritten decodes", isinstance(info_after, dict))
check("clientPin removed", "clientPin" not in info_after.get(4, {}))
check("uv is true", info_after.get(4, {}).get("uv") is True)
check(
    "other fields preserved",
    info_after.get(1) == info_before.get(1)
    and info_after.get(3) == info_before.get(3)
    and info_after.get(5) == info_before.get(5)
    and info_after.get(6) == info_before.get(6),
)
check("status byte kept", out[0] == 0x00)
check("idempotent", persona.rewrite_getinfo(out) == out)

# Golden bytes: the canonical CTAP2 encoding of the persona-rewritten
# fixture, constructed from the fixture itself with only the options
# region replaced (options after rewrite: rk, up, uv, plat,
# credentialMgmtPreview - canonical length-first key order). Verified
# byte-for-byte against the Pico encoder's output.
GOLDEN = (
    "00ac0183665532465f5632684649444f5f325f306c4649444f5f325f315f50524502826b6372"
    "656450726f746563746b686d61632d7365637265740350ee882879721c491397753dfcce9707"
    "2a04a562726bf5627570f5627576f564706c6174f47563726564656e7469616c4d676d745072"
    "6576696577f5051904b00682020107080818800981637573620a82a263616c67266474797065"
    "6a7075626c69632d6b6579a263616c672764747970656a7075626c69632d6b65790d040e1a00"
    "050403"
)
check("canonical golden bytes", out.hex() == GOLDEN)

# ---- persona fail-open paths -----------------------------------------
check("error status untouched", persona.rewrite_getinfo(b"\x2e\x00") == b"\x2e\x00")
check("empty untouched", persona.rewrite_getinfo(b"") == b"")
garbage = b"\x00\xff\xff\xff\xff"
check("garbage CBOR untouched", persona.rewrite_getinfo(garbage) == garbage)
non_getinfo_shape = b"\x00\x01\x02"  # valid CBOR int, not a map
check("non-map untouched", persona.rewrite_getinfo(non_getinfo_shape) == non_getinfo_shape)
no_options = b"\x00\xa1\x01\x01"  # {1: 1}
check("no-options-map untouched", persona.rewrite_getinfo(no_options) == no_options)

print()
print("FAILURES:", fails)
sys.exit(1 if fails else 0)

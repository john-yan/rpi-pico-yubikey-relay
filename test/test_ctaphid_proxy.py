#!/usr/bin/env python3
"""Unit tests for the CTAPHID message-layer proxy (host-side, no hardware).

Covers the assembler/packetize contract that ALL CTAP2 0x90 traffic flows
through regardless of AUTOUV: canonical round-trips at every segmentation
boundary, and the defensive BYPASS paths (foreign channel, sequence loss,
oversized messages, non-CBOR INIT packets, restart mid-assembly).

These were originally ad-hoc session scripts; committing them because both
shipped flag-confusion regressions lived in exactly this territory
(adversarial review, 2025-11-04).
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "lib"))

import ctaphid_proxy as P  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(f"{'ok  ' if ok else 'FAIL'} {name}")
    fails += not ok


def pad(b):
    assert len(b) <= 64
    return b + b"\x00" * (64 - len(b))


def canon(cid, msg):
    """Canonical segmentation (what a real CTAPHID sender emits)."""
    pkts = [pad(struct.pack(">IBH", cid, 0x90, len(msg)) + msg[:57])]
    rest, seq = msg[57:], 0
    while rest:
        chunk, rest = rest[:59], rest[59:]
        pkts.append(pad(struct.pack(">IB", cid, seq) + chunk))
        seq += 1
    return pkts


# ---- round-trips at every segmentation boundary -------------------------
for size in (1, 4, 57, 58, 59, 60, 100, 116, 117, 204, 400, 1200, 1234):
    msg = bytes((i * 7 + 3) & 0xFF for i in range(size))
    cid = 0x11223344 + size
    asm = P.CtapAssembler()
    result = None
    for pkt in canon(cid, msg):
        r = asm.feed(pkt)
        if r not in (None, P.BYPASS):
            result = r
    ok = result == (cid, msg) and list(P.packetize(cid, msg)) == canon(cid, msg)
    check(f"round-trip + canonical packetize, {size}B", ok)

# single-packet message: re-emission is byte-identical to the input report
msg, cid = b"\x04", 0xAABBCCDD
asm = P.CtapAssembler()
r = asm.feed(canon(cid, msg)[0])
check("single-packet completes on INIT", r == (cid, msg))
check(
    "single-packet re-emission identical",
    list(P.packetize(cid, msg))[0] == canon(cid, msg)[0],
)

# ---- defensive BYPASS paths ----------------------------------------------
asm = P.CtapAssembler()
msg = bytes(range(150))
asm.feed(pad(struct.pack(">IBH", 0x11223344, 0x90, 150) + msg[:57]))
asm.feed(pad(struct.pack(">IB", 0x11223344, 0) + msg[57:116]))
check("INIT+CONT0 accumulate", asm.active)
r = asm.feed(pad(struct.pack(">IB", 0xAABBCCDD, 1) + b"z" * 59))
check("foreign-cid CONT bypasses and clears", r is P.BYPASS and not asm.active)
r = asm.feed(pad(struct.pack(">IB", 0x11223344, 1) + b"w" * 59))
check("CONT after clear bypasses", r is P.BYPASS)

r = asm.feed(pad(struct.pack(">IBH", 1, 0x90, 5000) + b"q" * 57))
check("oversized bcnt bypasses", r is P.BYPASS)
r = asm.feed(pad(struct.pack(">IBH", 0x11223344, 0x90, 0)))
check("zero bcnt bypasses", r is P.BYPASS)
r = asm.feed(pad(struct.pack(">IBH", 0x11223344, 0xBB, 1) + b"\x02"))
check("KEEPALIVE (non-CBOR INIT) bypasses", r is P.BYPASS)
r = asm.feed(pad(struct.pack(">IBH", 0x11223344, 0x91, 0)))
check("CANCEL bypasses", r is P.BYPASS)

# restart mid-assembly: a new INIT discards the stale partial
asm = P.CtapAssembler()
asm.feed(pad(struct.pack(">IBH", 7, 0x90, 150) + b"a" * 57))
r = asm.feed(pad(struct.pack(">IBH", 8, 0x90, 1) + b"\x04"))
check("restart mid-assembly works", r == (8, b"\x04"))

# largest in-window message (4096 B) assembles with seq up to ~68;
# the 0x7F mask in feed() is defensive - a seq > 127 cannot occur within
# MAX_REASSEMBLE, so no wrap test is possible through the public path.
asm = P.CtapAssembler()
big = bytes(4096)
asm.feed(pad(struct.pack(">IBH", 9, 0x90, len(big)) + big[:57]))
rest, seq, complete = big[57:], 0, None
while rest:
    chunk, rest = rest[:59], rest[59:]
    r = asm.feed(pad(struct.pack(">IB", 9, seq) + chunk))
    seq += 1
    if r not in (None, P.BYPASS):
        complete = r
check("max-window message assembles", complete == (9, big) and seq == 69)

print()
print("FAILURES:", fails)
sys.exit(1 if fails else 0)

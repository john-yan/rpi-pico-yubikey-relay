#!/usr/bin/env python3
"""Unit tests for the Pico-side token service (host-side, mocked CDC).

Covers the CDC line protocol, the private-channel broker state machine
(nonce-matched INIT capture, WAITING consume, timeouts with the correct
reply type), the state-independent private-cid blackhole (the leak class
that killed daemon acquisitions), token/heartbeat gating, and the
SET_TOKEN/GOODBYE lifecycle. The mocked port/sender make the whole
service testable on CPython without hardware.
"""
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "lib"))

import ctaphid_proxy  # noqa: E402
import token_service as TS  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(f"{'ok  ' if ok else 'FAIL'} {name}")
    fails += not ok


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class MockPort:
    def __init__(self):
        self.rx = b""
        self.tx = []

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, n):
        data, self.rx = self.rx[:n], self.rx[n:]
        return data

    def write(self, b):
        self.tx.append(bytes(b))

    def feed(self, obj):
        self.rx += json.dumps(obj).encode() + b"\n"

    def lines(self):
        out = [json.loads(l) for l in b"".join(self.tx).split(b"\n") if l]
        self.tx = []
        return out


def pad(b):
    return b + b"\x00" * (64 - len(b))


def make_svc(sticky=False):
    TS.time = FakeTime()  # monkeypatch the module's clock
    svc = TS.TokenService(sticky=sticky)
    svc.port = MockPort()
    svc.sender = lambda pkt: sent.append(pkt)
    return svc


sent = []  # packets captured by svc.sender


def init_response(nonce, cid, cmd=0x86):
    return pad(
        b"\xff\xff\xff\xff" + bytes([cmd, 0x00, 17]) + nonce
        + struct.pack(">I", cid) + b"\x02\x05\x04\x03\x05"
    )


# ---- channel allocation --------------------------------------------------
sent.clear()
svc = make_svc()
svc.port.feed({"c": "HELLO"})
svc.poll()
check("HELLO with no channel sends INIT downstream", len(sent) == 1 and sent[0][4] == 0x86)
nonce = bytes(sent[0][7:15])

# Chrome's INIT response (different nonce) must NOT be consumed
foreign = init_response(bytes(n ^ 0xFF for n in nonce), 0x1234)
check("foreign-nonce INIT response not consumed", svc.consume(foreign) is False)

# ours is captured and answered with WELCOME
ours = init_response(nonce, 0x0A0B0C0D)
check("own INIT response consumed", svc.consume(ours) is True)
w = svc.port.lines()
check("WELCOME carries epoch + no token", w == [{"c": "WELCOME", "e": 0, "t": False}])

# late duplicate of our INIT response (after allocation) is blackholed
check("late own INIT response blackholed", svc.consume(ours) is True)
check("blackhole produced no reply", svc.port.lines() == [])

# ---- broker exchange ------------------------------------------------------
sent.clear()
svc.port.feed({"c": "BROKER", "h": "0603a2..."[:0] or "0402"})  # any CTAP2 msg
svc.poll()
check(
    "BROKER writes packetized request downstream",
    len(sent) == 1 and sent[0][4] == 0x90 and sent[0][5:7] == b"\x00\x02",
)
resp = bytes([0x00, 0x01, 0x02])
for pkt in ctaphid_proxy.packetize(0x0A0B0C0D, resp):
    check(f"consume response pkt seq={pkt[4]}", svc.consume(pkt) is True)
b = svc.port.lines()
check("BROKERED returns the message hex", b == [{"c": "BROKERED", "h": resp.hex()}])

# keepalive on the private cid mid-exchange is consumed, exchange continues
svc.port.feed({"c": "BROKER", "h": "0402"})
svc.poll()
ka = pad(struct.pack(">IB", 0x0A0B0C0D, 0) + b"")
ka = pad(struct.pack(">I", 0x0A0B0C0D) + b"\xbb\x00\x01\x02")
check("keepalive on private cid consumed", svc.consume(ka) is True)
for pkt in ctaphid_proxy.packetize(0x0A0B0C0D, resp):
    svc.consume(pkt)
check("exchange completes after keepalive", svc.port.lines()[-1]["c"] == "BROKERED")

# CTAPHID error on the private cid ends the exchange with the code
svc.port.feed({"c": "BROKER", "h": "0402"})
svc.poll()
err = pad(struct.pack(">I", 0x0A0B0C0D) + b"\xbf\x00\x01\x05")
check("CTAPHID error consumed", svc.consume(err) is True)
check(
    "BROKERED carries ctaphid error",
    svc.port.lines() == [{"c": "BROKERED", "e": "ctaphid-05"}],
)

# ---- timeouts reply with the right message type ---------------------------
svc.port.feed({"c": "BROKER", "h": "0402"})
svc.poll()
TS.time.now += TS.BROKER_TIMEOUT + 0.1
svc.poll()
check(
    "broker timeout replies BROKERED e:timeout",
    svc.port.lines() == [{"c": "BROKERED", "e": "timeout"}],
)

sent.clear()
svc2 = make_svc()
svc2.port.feed({"c": "HELLO"})
svc2.poll()
TS.time.now += TS.BROKER_TIMEOUT + 0.1
svc2.poll()
check(
    "INIT timeout replies WELCOME e2:timeout (not a stale BROKERED)",
    svc2.port.lines() == [{"c": "WELCOME", "e": 0, "e2": "timeout"}],
)

# ---- state-independent blackhole (the leak-class regression) ---------------
# After a timed-out exchange the channel stays allocated; late private-cid
# strays must be consumed even though state is IDLE.
svc.state = TS.IDLE  # already timed out above; cid still 0x0A0B0C0D
stray = pad(struct.pack(">IB", 0x0A0B0C0D, 3) + b"junk")
check("late private-cid CONT blackholed in IDLE", svc.consume(stray) is True)
other = pad(struct.pack(">IB", 0x99999999, 3) + b"junk")
check("foreign cid still relayed in IDLE", svc.consume(other) is False)
check("blackhole silent (no replies)", svc.port.lines() == [])

# ---- SET_TOKEN / GOODBYE / gating -----------------------------------------
svc.port.feed({"c": "SET_TOKEN", "h": "33" * 32, "p": 2})
svc.poll()
check(
    "SET_TOKEN accepted",
    svc.port.lines() == [{"c": "ACK", "e": 0, "t": True}] and len(svc.token) == 32,
)
svc.port.feed({"c": "SET_TOKEN", "h": "33" * 5, "p": 2})  # bad length
svc.poll()
check("bad token length rejected", svc.port.lines()[-1]["t"] is False and svc.token is None)

svc.port.feed({"c": "SET_TOKEN", "h": "11" * 32, "p": 2})
svc.poll()
check("active() true with fresh contact", svc.active() is True)
TS.time.now += TS.HEARTBEAT_TIMEOUT + 0.1
check("active() false after heartbeat timeout", svc.active() is False)
svc.port.feed({"c": "PING"})
svc.poll()
check("PING refreshes contact", svc.active() is True)
svc.port.feed({"c": "GOODBYE"})
svc.poll()
check(
    "GOODBYE clears token + daemon flag",
    svc.token is None and svc.daemon_alive is False,
)

# ---- sticky mode (AUTOUV_STICKY) ------------------------------------------
# A cached token alone keeps the persona on; no daemon liveness required.
# GOODBYE and on_attach remain the kill paths.
st = make_svc(sticky=True)
st.port.feed({"c": "SET_TOKEN", "h": "44" * 32, "p": 2})
st.poll()
check("sticky: token alone keeps active() on", st.active() is True)
TS.time.now += 3600.0  # a year of no daemon contact
check("sticky: stale contact irrelevant", st.active() is True)
st.port.feed({"c": "GOODBYE"})
st.poll()
check("sticky: GOODBYE still clears (explicit kill switch)", st.active() is False)
st.port.feed({"c": "SET_TOKEN", "h": "55" * 32, "p": 2})
st.poll()
check("sticky: re-armed after GOODBYE", st.active() is True)
st.on_attach()
check("sticky: key re-attach clears (power-state bound)", st.active() is False)
nst = make_svc(sticky=False)
nst.port.feed({"c": "SET_TOKEN", "h": "66" * 32, "p": 2})
nst.poll()
nst.daemon_alive = False
check("non-sticky default unchanged: daemon liveness still required", nst.active() is False)

# ---- attach lifecycle -------------------------------------------------------
svc.port.feed({"c": "SET_TOKEN", "h": "22" * 32, "p": 2})
svc.poll()
svc.on_attach()
check(
    "on_attach bumps epoch, clears token and channel",
    svc.epoch == 1 and svc.token is None and svc.cid == 0 and svc.state == TS.IDLE,
)
check("post-attach blackhole inert (cid cleared)", svc.consume(stray) is False)

print()
print("FAILURES:", fails)
sys.exit(1 if fails else 0)

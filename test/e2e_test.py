#!/usr/bin/env python3
"""End-to-end relay integration test.

Single owner of /dev/ttyACM0: (re)deploys the given code file (default:
production src/code.py), soft-reloads the board, then runs host-side
CTAPHID INIT + PING checks (from test_ctaphid.py helpers) against the
relay's hidraw node and prints the relay's output interleaved. Exits
non-zero on any failure. For a packet-tracing run, `make deploy-debug`
first, then run this unchanged.
"""

import os
import subprocess
import sys
import threading
import time

import serial

from test_ctaphid import find_ctaphid_node, init_handshake, ping_roundtrip

CIRCUITPY = "/run/media/johnyan/CIRCUITPY"
# Repo root (parent of this test/ directory); the default deploy target is
# the production firmware at src/code.py, resolved independently of CWD.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE_LOCAL = (
    sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "src", "code.py")
)

# ---------------------------------------------------------------- listener
ser = serial.Serial("/dev/ttyACM0", 115200, timeout=0.2)
lines = []


def reader():
    while not stop.is_set():
        try:
            data = ser.read(4096)
        except serial.SerialException:
            time.sleep(0.5)  # port re-enumerated; try to keep up
            continue
        if data:
            text = data.decode("utf-8", "replace")
            lines.append(text)
            sys.stdout.write(text)
            sys.stdout.flush()


stop = threading.Event()
thread = threading.Thread(target=reader)
thread.start()


def say(msg):
    print(f"\n########## {msg} ##########")


try:
    # 1. deploy the relay code and force a soft reload
    say(f"deploying {CODE_LOCAL} and reloading")
    subprocess.run(["cp", CODE_LOCAL, f"{CIRCUITPY}/code.py"], check=True)
    # code.py may import from lib/ - keep the board's lib/ in sync too
    lib_src = os.path.join(ROOT, "src", "lib")
    lib_dst = os.path.join(CIRCUITPY, "lib")
    os.makedirs(lib_dst, exist_ok=True)
    for name in os.listdir(lib_src):
        if name.endswith(".py"):
            subprocess.run(["cp", os.path.join(lib_src, name),
                            os.path.join(lib_dst, name)], check=True)
    subprocess.run(["sync"], check=True)
    time.sleep(2)
    ser.write(b"\x04")  # Ctrl-D soft reload
    # Wait for the relay to attach: after a reload the downstream power
    # sequence sometimes needs a self-heal power-cycle (several seconds),
    # and testing during that window produces a spurious failure.
    deadline = time.time() + 30
    while time.time() < deadline:
        if any("attached" in chunk for chunk in lines):
            break
        time.sleep(0.2)
    else:
        say("WARNING: relay did not report 'attached' within 30s")

    # 2. CTAPHID INIT + PING through the relay (helpers from test_ctaphid)
    node, fd = find_ctaphid_node(strict_pico=True)
    if fd is None:
        say("ERROR: relay CTAPHID node not found")
        raise SystemExit(1)
    say(f"relay node: {node}")

    say("INIT handshake")
    try:
        cid, payload = init_handshake(fd)
    except RuntimeError as e:
        say(f"INIT FAILED: {e}")
        os.close(fd)
        raise SystemExit(1)
    say(f"channel {cid:#010x}, protocol {payload[12]}")

    # 100 bytes -> INIT + 1 CONT each way; the YubiKey 5.4.3 echo is
    # unreliable for larger PINGs even when connected directly.
    ping = (bytes(range(256)) * 2)[:100]
    say("PING round trip")
    try:
        echoed = ping_roundtrip(fd, cid, ping)
    except RuntimeError as e:
        say(f"PING FAILED: {e}")
        os.close(fd)
        raise SystemExit(1)
    if echoed == ping:
        say(f"PING OK: {len(echoed)}-byte multi-packet payload echoed intact")
    else:
        say(f"PING FAILED: got {len(echoed)} of {len(ping)} bytes")
        os.close(fd)
        raise SystemExit(1)
    os.close(fd)

    time.sleep(3)
finally:
    stop.set()
    thread.join()
    ser.close()

print("\n=== captured relay lines ===")
print("".join(lines).replace("\r", "\n"))

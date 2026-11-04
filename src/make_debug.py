#!/usr/bin/env python3
"""Regenerate code_debug.py from code.py.

code_debug.py is code.py plus two packet-tracing prints (F>YK / Y>PC).
It used to be maintained by hand and drifted from production; this script
is the single source of truth for how it is produced.

Usage: python3 src/make_debug.py [--check]   (run from the repo root)
  --check   verify code_debug.py is up to date; exit 1 if not, no write.

WARNING: the debug build logs the first 8 bytes of every CTAPHID packet
(channel ID, command, length, and payload fragments) to the serial
console. Do not leave it deployed on the relay in normal use.
"""

import os
import sys

SRC_DIR = os.path.dirname(os.path.abspath(__file__))

TRACE_HOST_TO_YK = '''                    ctap_write(ctap_out, pkt, timeout=100)
                    print("F>YK:", " ".join("{:02x}".format(b) for b in pkt[:8]))'''

TRACE_YK_TO_HOST = '''            try:
                ctap_send(pkt)
                print("Y>PC:", " ".join("{:02x}".format(b) for b in pkt[:8]))'''

ANCHOR_HOST_TO_YK = '''                    ctap_write(ctap_out, pkt, timeout=100)'''

ANCHOR_YK_TO_HOST = '''            try:
                ctap_send(pkt)'''


def build():
    src = open(os.path.join(SRC_DIR, "code.py")).read()
    for anchor in (ANCHOR_HOST_TO_YK, ANCHOR_YK_TO_HOST):
        if src.count(anchor) != 1:
            sys.exit(f"error: anchor not found exactly once in code.py:\n{anchor!r}")
    src = src.replace(ANCHOR_HOST_TO_YK, TRACE_HOST_TO_YK)
    src = src.replace(ANCHOR_YK_TO_HOST, TRACE_YK_TO_HOST)
    return src


def main():
    out = build()
    debug_path = os.path.join(SRC_DIR, "code_debug.py")
    if "--check" in sys.argv:
        current = open(debug_path).read() if os.path.exists(debug_path) else ""
        if current != out:
            print("code_debug.py is out of date with code.py")
            sys.exit(1)
        print("code_debug.py is up to date")
        return
    open(debug_path, "w").write(out)
    print(f"wrote {debug_path} (code.py + packet tracing)")


if __name__ == "__main__":
    main()

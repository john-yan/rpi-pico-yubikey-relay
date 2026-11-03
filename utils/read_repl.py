#!/usr/bin/env python3
"""Read CircuitPython REPL output from /dev/ttyACM0 for N seconds.

DISRUPTIVE: sends Ctrl-C (kills running code.py) then Ctrl-D (soft
reload) and captures the fresh boot output. To watch the relay
without disturbing it, use listen_repl.py instead.
"""
import sys
import time

import serial

duration = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

ser = serial.Serial("/dev/ttyACM0", 115200, timeout=0.5)
# Interrupt running code, then soft-reload (Ctrl-D) to rerun code.py
time.sleep(0.3)
ser.write(b"\x03")
time.sleep(0.3)
ser.write(b"\x04")
deadline = time.time() + duration
while time.time() < deadline:
    data = ser.read(4096)
    if data:
        sys.stdout.write(data.decode("utf-8", "replace"))
        sys.stdout.flush()
ser.close()

#!/usr/bin/env python3
"""Passively listen to the CircuitPython REPL on /dev/ttyACM0 (no Ctrl-C)."""
import sys
import time

import serial

duration = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0

ser = serial.Serial("/dev/ttyACM0", 115200, timeout=0.5)
deadline = time.time() + duration
while time.time() < deadline:
    data = ser.read(4096)
    if data:
        sys.stdout.write(data.decode("utf-8", "replace"))
        sys.stdout.flush()
ser.close()

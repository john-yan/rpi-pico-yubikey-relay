#!/usr/bin/env python3
"""Send a command to the CircuitPython REPL on /dev/ttyACM0 and show output."""
import sys
import time

import serial

cmd = sys.argv[1]
duration = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

ser = serial.Serial("/dev/ttyACM0", 115200, timeout=0.5)
time.sleep(0.3)
ser.write(b"\x03")  # interrupt running code -> REPL
time.sleep(0.5)
ser.write(b"\r\n")  # ensure we're at a fresh prompt
# Send the command character by character to avoid dropped bytes
for ch in cmd:
    ser.write(ch.encode())
    time.sleep(0.002)
ser.write(b"\r\n")

deadline = time.time() + duration
while time.time() < deadline:
    data = ser.read(4096)
    if data:
        sys.stdout.write(data.decode("utf-8", "replace"))
        sys.stdout.flush()
ser.close()

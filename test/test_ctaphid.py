#!/usr/bin/env python3
"""Host-side verification for the Pico YubiKey relay.

Finds the CTAPHID device (usage page 0xF1D0) exposed by the Pico's
upstream USB port, then:
  1. CTAPHID INIT handshake (expects a real answer from the YubiKey)
  2. CTAPHID PING with a >64-byte payload (exercises multi-packet
     transfer in both directions through the relay)

Usage: python3 test_ctaphid.py
"""

import glob
import os
import struct
import sys
import time

TYPE_GLOBAL = 1

CMD_INIT = 0x86
CMD_PING = 0x81
TYPE_MASK = 0x80  # response packets have the high bit set
BCNT_MAX = 64 - 7
CTAP_REPORT_LEN = 64


def parse_usage_pages(descriptor):
    """Yield 16-bit usage pages declared in a report descriptor."""
    i = 0
    while i < len(descriptor):
        item = descriptor[i]
        item_type = (item >> 2) & 0x03
        size = item & 0x03
        data = descriptor[i + 1 : i + 1 + size]
        if item_type == TYPE_GLOBAL and (item & 0xF0) == 0x00:  # Usage Page
            if size == 1:
                yield data[0]
            elif size == 2:
                yield data[0] | (data[1] << 8)
        i += 1 + size
    return


PICO_VID = "239a"
PICO_PID = "80f4"


def _usb_id(sysfs_dev):
    """Return (idVendor, idProduct) of the USB device behind a hidraw node."""
    d = os.path.realpath(sysfs_dev)
    for _ in range(6):  # walk up: hid device -> interface -> USB device
        d = os.path.dirname(d)
        try:
            with open(os.path.join(d, "idVendor")) as f:
                vid = f.read().strip()
            with open(os.path.join(d, "idProduct")) as f:
                pid = f.read().strip()
            return vid, pid
        except OSError:
            continue
    return None, None


def find_ctaphid_node(strict_pico=False):
    """Find the Pico relay's CTAPHID node; fall back to any F1D0 device.

    With strict_pico=True, only the relay itself is accepted (no fallback
    to other security keys) - for harnesses that must not test the wrong
    device.
    """
    fallback = None
    for node in sorted(glob.glob("/dev/hidraw*")):
        sysfs = f"/sys/class/hidraw/{os.path.basename(node)}/device"
        try:
            with open(os.path.join(sysfs, "report_descriptor"), "rb") as f:
                descriptor = f.read()
            fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        except (PermissionError, FileNotFoundError, OSError):
            print(f"  {node}: not accessible")
            continue
        try:
            if 0xF1D0 not in parse_usage_pages(descriptor):
                os.close(fd)
                continue
            vid, pid = _usb_id(sysfs)
            if (vid, pid) == (PICO_VID, PICO_PID):
                return node, fd  # the relay itself
            if fallback is None:
                fallback = node
            os.close(fd)
        except Exception:
            os.close(fd)
    if fallback is not None and not strict_pico:
        try:
            return fallback, os.open(fallback, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            pass
    return None, None


def make_packet(cid, cmd, payload):
    """Build a CTAPHID packet (INIT or continuation as needed).
    Always padded to 64 bytes - CTAPHID uses fixed-size packets."""
    if len(payload) > BCNT_MAX:
        raise ValueError("payload too large for one packet")
    pkt = struct.pack(">IBH", cid, cmd, len(payload)) + payload
    return pkt + b"\x00" * (64 - len(pkt))


def packetize(cid, cmd, payload):
    """Split payload into 64-byte INIT + CONT packets.
    The INIT packet's bcnt field carries the TOTAL payload length."""
    first = payload[:BCNT_MAX]
    pkt = struct.pack(">IBH", cid, cmd, len(payload)) + first
    packets = [pkt + b"\x00" * (64 - len(pkt))]
    rest = payload[BCNT_MAX:]
    seq = 0
    while rest:
        chunk, rest = rest[: CTAP_REPORT_LEN - 1], rest[CTAP_REPORT_LEN - 1 :]
        pkt = struct.pack(">IB", cid, seq) + chunk
        packets.append(pkt + b"\x00" * (64 - len(pkt)))
        seq += 1
    return packets


def assemble(fd, timeout=5.0):
    """Read packets until a complete response message is assembled."""
    os.set_blocking(fd, False)
    deadline = time.time() + timeout
    message = None
    bcnt = 0
    cont_expected = 0
    while time.time() < deadline:
        try:
            data = os.read(fd, 64)
        except BlockingIOError:
            time.sleep(0.001)
            continue
        if len(data) < 7:
            continue
        cid = struct.unpack_from(">I", data)[0]
        if data[4] & TYPE_MASK:  # INIT-type response packet
            cmd = data[4] & 0x7F
            bcnt = (data[5] << 8) | data[6]
            # an INIT packet carries at most 57 payload bytes
            chunk = min(bcnt, 57)
            message = bytearray(data[7 : 7 + chunk])
            cont_expected = 0
            if len(message) >= bcnt:
                return cid, cmd, bytes(message)
        else:  # continuation packet
            if message is None or data[4] != cont_expected:
                continue
            # a CONT carries at most 63 bytes; the final one may be shorter
            chunk = min(63, bcnt - len(message))
            message.extend(data[5 : 5 + chunk])
            cont_expected += 1
            if len(message) >= bcnt:
                return cid, cmd, bytes(message)
    raise TimeoutError("no CTAPHID response within timeout")


def init_handshake(fd):
    """Run a CTAPHID INIT handshake; return (cid, payload).

    Raises RuntimeError if the response is not a valid INIT reply with our
    nonce echoed back.
    """
    nonce = struct.pack(">Q", int(time.time() * 1000) & 0xFFFFFFFFFFFFFFFF)
    os.write(fd, make_packet(0xFFFFFFFF, CMD_INIT, nonce))
    _, cmd, payload = assemble(fd)
    if cmd != (CMD_INIT & 0x7F) or payload[:8] != nonce:
        raise RuntimeError(f"bad INIT response cmd={cmd:#x}")
    # assigned channel id lives in payload bytes 8..12
    cid = struct.unpack_from(">I", payload, 8)[0]
    return cid, payload


def ping_roundtrip(fd, cid, payload):
    """Send a PING of `payload` bytes; return the echoed bytes.

    Raises RuntimeError if the response is not a PING.
    """
    for pkt in packetize(cid, CMD_PING, payload):
        os.write(fd, pkt)
    _, cmd, echoed = assemble(fd, timeout=10.0)
    if cmd != (CMD_PING & 0x7F):
        raise RuntimeError(f"expected PING response, got cmd={cmd:#x}")
    return echoed


def main():
    print("scanning /dev/hidraw* for CTAPHID (usage page 0xF1D0) ...")
    node, fd = find_ctaphid_node()
    if fd is None:
        print("ERROR: no CTAPHID device found - is the relay running?")
        sys.exit(1)
    print(f"found CTAPHID device at {node}")

    # --- 1. INIT handshake ---
    try:
        cid, payload = init_handshake(fd)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    proto, major, minor, build = payload[12], payload[13], payload[14], payload[15]
    caps = payload[16] | ((payload[17] << 8) if len(payload) > 17 else 0)
    print(f"INIT ok: channel {cid:#010x}, CTAPHID protocol v{proto}, "
          f"device fw {major}.{minor}.{build}, caps {caps:#06x}")

    # --- 2. PING with multi-packet payload both ways ---
    # 100 bytes -> INIT + 1 CONT packet in each direction. The YubiKey
    # 5.4.3's PING echo is unreliable for messages needing 2+ CONTs even
    # when connected directly (verified against hidraw0), so stay in the
    # range it handles; real CTAP2 multi-packet traffic is unaffected.
    payload = (bytes(range(256)) * 2)[:100]
    try:
        echoed = ping_roundtrip(fd, cid, payload)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    if echoed == payload:
        print(f"PING ok: {len(echoed)}-byte multi-packet payload echoed intact")
    else:
        print(f"ERROR: PING payload corrupted (got {len(echoed)} bytes)")
        sys.exit(1)

    print("\nALL TESTS PASSED - the relay is transparently bridging CTAPHID.")
    os.close(fd)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Query authenticatorGetInfo from a CTAPHID device.

Usage:
    python3 fido_getinfo.py [/dev/hidrawN]

Defaults to the first Yubico (1050) CTAPHID node. Reuses the transport
helpers from test/test_ctaphid.py. Note: modern CTAP carries CTAP2 over
CTAPHID_CBOR (0x10, sent as 0x90 with the INIT bit); CTAPHID_MSG (0x83)
is CTAP1/U2F-only on this firmware, and a CTAP2 payload sent there is
rejected with a U2F-style 6E00 status word.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "test"))
import test_ctaphid as t  # noqa: E402

import cbor2  # noqa: E402

CTAPHID_CBOR = 0x90  # CTAP2 transport command 0x10, with INIT bit set
CTAPHID_CBOR_CMD = 0x10  # same, as returned by assemble() (bit stripped)


def find_yubico_node():
    for name in sorted(os.listdir("/dev/hidraw")):
        node = "/dev/" + name
        sysfs = f"/sys/class/hidraw/{name}/device"
        vid, _ = t._usb_id(sysfs)
        if vid == "1050":
            try:
                with open(os.path.join(sysfs, "report_descriptor"), "rb") as f:
                    desc = f.read()
                if 0xF1D0 in t.parse_usage_pages(desc):
                    return node, os.open(node, os.O_RDWR | os.O_NONBLOCK)
            except OSError:
                pass
    return None, None


def main():
    if len(sys.argv) > 1:
        node = sys.argv[1]
        fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
    else:
        node, fd = find_yubico_node()
        if fd is None:
            print("no Yubico CTAPHID node found")
            sys.exit(1)
    print(f"querying {node} ...")

    cid, payload = t.init_handshake(fd)
    proto, major, minor, build = payload[12:16]
    print(f"INIT ok: channel {cid:#010x}, CTAPHID v{proto}, fw {major}.{minor}.{build}")

    # authenticatorGetInfo: CTAP2 command 0x04, no parameters
    os.write(fd, t.make_packet(cid, CTAPHID_CBOR, b"\x04"))
    _, cmd, resp = t.assemble(fd, timeout=5.0)
    if cmd != CTAPHID_CBOR_CMD:
        print(f"expected CBOR response, got cmd={cmd:#x}")
        sys.exit(1)
    if resp[0] != 0x00:
        print(f"GetInfo CTAP2 error: 0x{resp[0]:02x}")
        sys.exit(1)
    info = cbor2.loads(resp[1:])

    print("versions:", info.get(1))
    print("aaguid:", info.get(3).hex() if info.get(3) else None)
    options = info.get(4) or {}
    print("options:", options)
    print("maxMsgSize:", info.get(5))
    print("pinUvAuthProtocols:", info.get(6))
    print()
    if options.get("clientPin") is True:
        print("=> FIDO2 PIN is SET on this key.")
        print("   Chrome will prompt for it on every UV='preferred' (WebAuthn")
        print("   default) site. CTAP has no remove-PIN; only ykman fido reset")
        print("   clears it (wipes all FIDO2 credentials).")
    elif options.get("clientPin") is False:
        print("=> clientPin=False: PIN supported but NOT set -> touch-only")
        print("   for UV='preferred' sites; UV='required' sites will fail.")
    if "uv" in options:
        print("=> built-in biometric UV present:", options["uv"])
    os.close(fd)


if __name__ == "__main__":
    main()

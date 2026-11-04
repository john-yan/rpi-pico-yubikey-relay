#!/usr/bin/env python3
"""Phase-0 spec verification probes (PIN-free) — plan.md §10.

All probes are read-only: no PIN attempts (getPinRetries does not decrement),
no credential creation, no state change on the YubiKey.

Uses python-fido2 as the reference client so request encodings are correct
by construction. Needs the `fido2` package importable and `cryptography`
(the distro python usually has it):
    PYTHONPATH=/path/to/fido2-1.2.0 python3 utils/spec_probes.py [/dev/hidrawN]

Probes (plan.md §10 numbering):
  P2a  getPinToken (legacy subcmd 0x05) recognized?
  P2b  getPinUvAuthTokenUsingPinWithPermissions (subcmd 0x09) recognized?
  P2c  CONTROL: unknown subcommand 0x7f -> expect INVALID_SUBCOMMAND
  P5a  getAssertion options {uv:true}, no pinUvAuthParam: uv option accepted?
  P5b  getAssertion bogus allowList, no options: baseline error
  P5c  getAssertion with EMPTY pinUvAuthParam (Chrome's allow-list pre-flight)
  P6a  discoverable getAssertion (no allowList), no pinUvAuthParam
  P6b  getPinRetries / getKeyAgreement (protocol 2) sanity
"""
import os
import sys
import threading

try:
    from fido2.hid import CtapHidDevice
    from fido2.ctap2 import Ctap2, ClientPin
    from fido2.ctap2.base import CtapError
except ImportError:
    sys.path.insert(0, "/tmp/fido2-1.2.0")  # session checkout fallback
    from fido2.hid import CtapHidDevice
    from fido2.ctap2 import Ctap2, ClientPin
    from fido2.ctap2.base import CtapError

PICO_VID, PICO_PID = 0x239A, 0x80F4


def find_device(node=None):
    if node:
        return CtapHidDevice(list_devices=None) if False else None
    for d in CtapHidDevice.list_devices():
        if d.descriptor.vid == PICO_VID and d.descriptor.pid == PICO_PID:
            return d
    return None


def err_name(e):
    try:
        return f"CTAP2 error {e.code.name} (0x{int(e.code):02x})"
    except Exception:  # noqa: BLE001
        return f"CTAP2 error {e}"


def try_call(fn, **kw):
    keepalives = []

    def on_keepalive(status):
        keepalives.append(status)

    ev = threading.Event()
    timer = threading.Timer(3.0, ev.set)
    timer.start()
    try:
        result = fn(event=ev, on_keepalive=on_keepalive, **kw)
        return f"OK: {result}", keepalives
    except CtapError as e:
        return err_name(e), keepalives
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}", keepalives
    finally:
        timer.cancel()


def main():
    node = sys.argv[1] if len(sys.argv) > 1 else None
    dev = None
    if node:
        os.system(f"true")
        # open a specific hidraw node via fido2's linux backend
        from fido2.hid.linux import list_descriptors
        for desc in list_descriptors():
            if str(desc.path) == node:
                from fido2.hid import open_connection
                from fido2.ctap2 import Ctap2 as _C
                dev = _C(open_connection(desc))
                break
    if dev is None:
        d = find_device()
        if d is None:
            print("relay CTAPHID device (239a:80f4) not found")
            sys.exit(1)
        dev = Ctap2(d)

    info = dev.info
    print(f"target: {dev.device.descriptor.path if hasattr(dev, 'device') else node}")
    print(f"versions={info.versions}")
    print(f"options={info.options}")
    print(f"pin_uv_protocols={info.pin_uv_protocols}")
    cp = ClientPin(dev)

    print("\n== P6b: getPinRetries (protocol 2) ==")
    r = cp.get_pin_retries()
    print(f"   pin_retries={r}")

    print("\n== P6b: getKeyAgreement (protocol 2) ==")
    ka, ss = cp._get_shared_secret()
    x = ka.get(-2, b"")
    print(f"   COSE key ok, x[:8]={bytes(x)[:8].hex()}..., shared_secret len={len(ss)}")

    print("\n== P2a: getPinToken legacy (0x05), no pinHashEnc ==")
    print("  ", try_call(dev.client_pin, pin_uv_protocol=2,
                         sub_cmd=ClientPin.CMD.GET_TOKEN_USING_PIN_LEGACY)[0])

    print("\n== P2b: getPinUvAuthTokenUsingPinWithPermissions (0x09), stripped ==")
    print("  ", try_call(dev.client_pin, pin_uv_protocol=2,
                         sub_cmd=ClientPin.CMD.GET_TOKEN_USING_PIN)[0])

    print("\n== P2c: CONTROL unknown subcommand 0x7f ==")
    print("  ", try_call(dev.client_pin, pin_uv_protocol=2, sub_cmd=0x7F)[0])

    chal = bytes(range(32))
    bogus = [{"type": "public-key", "id": bytes(range(32, 64))}]
    ga = dev.get_assertion

    print("\n== P5b: getAssertion bogus allowList, no options ==")
    res, ka_ = try_call(ga, rp_id="probe.test", client_data_hash=chal,
                        allow_list=bogus)
    print(f"   {res}\n   keepalive statuses seen: {ka_}")

    print("\n== P5a: getAssertion bogus allowList, options {uv:true} ==")
    res, ka_ = try_call(ga, rp_id="probe.test", client_data_hash=chal,
                        allow_list=bogus, options={"uv": True})
    print(f"   {res}\n   keepalive statuses seen: {ka_}")

    print("\n== P5c: getAssertion bogus allowList, EMPTY pinUvAuthParam (pre-flight) ==")
    res, ka_ = try_call(ga, rp_id="probe.test", client_data_hash=chal,
                        allow_list=bogus, pin_uv_param=b"", pin_uv_protocol=2)
    print(f"   {res}\n   keepalive statuses seen: {ka_}")

    print("\n== P6a: getAssertion discoverable (no allowList), no pinUvAuthParam ==")
    res, ka_ = try_call(ga, rp_id="probe.test", client_data_hash=chal)
    print(f"   {res}\n   keepalive statuses seen: {ka_}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Phase-4 acceptance probes: persona-shaped UV requests through the relay.

RUN WITH THE DAEMON UP (utils/autouv_daemon.py) so the persona is on and
a token is cached. These are the exact wire shapes Chrome produces for a
bio-persona key (options uv:true, no pinUvAuthParam) - before the
injector they failed with UNSUPPORTED_OPTION (phase-0 P5f).

  UV1  makeCredential options {uv:true}        -> expect UV=true
  UV2  makeCredential options {uv:true, rk}    -> expect UV=true (passkey shape)
  UV3  getAssertion  options {uv:true}         -> expect UV=true
  UV4  getAssertion  no options (discouraged)  -> expect UV=false (untouched)

Creates throwaway non-discoverable credentials for rpId probe.test
(nothing stored on the key). Touches are handled by the relay's
auto-touch.
"""
import os
import sys
import time

try:
    from fido2.hid import CtapHidDevice
    from fido2.ctap2 import Ctap2
    from fido2.ctap2.base import CtapError
except ImportError:
    sys.path.insert(0, "/tmp/fido2-1.2.0")
    from fido2.hid import CtapHidDevice
    from fido2.ctap2 import Ctap2
    from fido2.ctap2.base import CtapError

PICO_VID, PICO_PID = 0x239A, 0x80F4
RP = {"id": "probe.test", "name": "Phase-4 probe"}
USER = {"id": b"phase4probe", "name": "probe", "displayName": "Phase 4"}
ES256 = {"type": "public-key", "alg": -7}
CDH = bytes(range(32))


def find_ctap2():
    for d in CtapHidDevice.list_devices():
        if d.descriptor.vid == PICO_VID and d.descriptor.pid == PICO_PID:
            return Ctap2(d)
    return None


def uv_flag(x):
    try:
        return x.auth_data.flags.uv
    except AttributeError:
        return bool(x.auth_data.flags & 0x04)


def main():
    ctap = find_ctap2()
    if ctap is None:
        print("relay CTAPHID device not found")
        sys.exit(1)
    print("persona GetInfo options:", ctap.info.options)
    if ctap.info.options.get("uv") is not True:
        print("PERSONA IS OFF - start the daemon first "
              "(python3 utils/autouv_daemon.py)")
        sys.exit(1)

    print("\n>>> UV1: makeCredential {uv:true} (touch when the key blinks)")
    att = ctap.make_credential(CDH, RP, USER, [ES256], options={"uv": True})
    print(f"UV1: UV flag = {uv_flag(att)}  (expect True)")
    cred_id = att.auth_data.credential_data.credential_id

    print("\n>>> UV2: makeCredential {uv:true, rk:true} - passkey shape")
    att2 = ctap.make_credential(CDH, RP, USER, [ES256],
                                options={"uv": True, "rk": True})
    print(f"UV2: UV flag = {uv_flag(att2)}  (expect True)")

    print("\n>>> UV3: getAssertion {uv:true} (touch when the key blinks)")
    assertion = ctap.get_assertion(
        "probe.test", CDH,
        allow_list=[{"type": "public-key", "id": cred_id}],
        options={"uv": True},
    )
    print(f"UV3: UV flag = {uv_flag(assertion)}  (expect True)")

    print("\n>>> UV4: getAssertion no options - UV-discouraged shape (touch)")
    assertion = ctap.get_assertion(
        "probe.test", CDH,
        allow_list=[{"type": "public-key", "id": cred_id}],
    )
    print(f"UV4: UV flag = {uv_flag(assertion)}  (expect False - untouched)")

    print("\nALL PHASE-4 PROBES DONE")


if __name__ == "__main__":
    main()

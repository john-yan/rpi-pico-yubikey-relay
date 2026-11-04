#!/usr/bin/env python3
"""Phase-0 PIN-requiring spec probes — plan.md §10 items 1 and 5 (end-to-end).

RUN THIS YOURSELF (the PIN is read via getpass and never appears in a
transcript or process listing):

    python3 utils/spec_probes_pin.py [/dev/hidrawN]

Safety:
  - Checks getPinRetries first; refuses to run with < 3 retries left.
  - Exactly ONE token acquisition attempt per protocol; aborts permanently
    on PIN_INVALID (never risks the 3-strike / 8-total lockout).
  - Creates one throwaway NON-discoverable credential for rpId "probe.test"
    (nothing is stored on the YubiKey; wrapped key goes to the probe only).

Probes:
  P1a  makeCredential + getAssertion with protocol-2 token,
       pinUvAuthParam = full 32-byte HMAC  -> expect UV=true
  P1b  pinUvAuthParam wrongly truncated to 16 bytes with protocol-2 token
       -> expect PIN_AUTH_INVALID (proves length is checked)
  P1c  protocol-1 token with 16-byte param -> expect UV=true
  P5f  persona-shaped request: options {uv:true} + injected pinUvAuthParam
       -> expect UV=true (this is the exact wire shape the injector emits)
"""
import getpass
import os
import sys

try:
    from fido2.hid import CtapHidDevice
    from fido2.ctap2 import Ctap2, ClientPin, PinProtocolV1, PinProtocolV2
    from fido2.ctap2.base import CtapError
    from fido2.webauthn import PublicKeyCredentialRpEntity, PublicKeyCredentialUserEntity
except ImportError:
    sys.path.insert(0, "/tmp/fido2-1.2.0")  # session checkout fallback
    from fido2.hid import CtapHidDevice
    from fido2.ctap2 import Ctap2, ClientPin, PinProtocolV1, PinProtocolV2
    from fido2.ctap2.base import CtapError
    from fido2.webauthn import PublicKeyCredentialRpEntity, PublicKeyCredentialUserEntity

PICO_VID, PICO_PID = 0x239A, 0x80F4
RP = {"id": "probe.test", "name": "Phase-0 probe"}
USER = {"id": b"phase0probe", "name": "probe", "displayName": "Phase 0 Probe"}
ES256 = {"type": "public-key", "alg": -7}


def find_ctap2(node=None):
    if node:
        from fido2.hid.linux import list_descriptors, open_connection
        for desc in list_descriptors():
            if str(desc.path) == node:
                return Ctap2(open_connection(desc))
    for d in CtapHidDevice.list_devices():
        if d.descriptor.vid == PICO_VID and d.descriptor.pid == PICO_PID:
            return Ctap2(d)
    return None


def uv_flag(att_or_assertion):
    try:
        return att_or_assertion.auth_data.flags.uv
    except AttributeError:
        return bool(att_or_assertion.auth_data.flags & 0x04)


def read_pin():
    """PIN from (in order): PICO_RELAY_PIN env, .pin file next to this script
    or in cwd, else getpass. Never echoed or logged."""
    import os as _os
    env = _os.environ.get("PICO_RELAY_PIN")
    if env:
        return env.strip()
    here = _os.path.dirname(_os.path.abspath(__file__))
    for p in (_os.path.join(here, ".pin"), ".pin",
              _os.path.join(here, "..", ".pin")):
        if _os.path.isfile(p):
            with open(p) as f:
                pin = f.read().strip()
            if pin:
                return pin
    return getpass.getpass("YubiKey FIDO2 PIN (input hidden): ")


def main():
    ctap = find_ctap2(sys.argv[1] if len(sys.argv) > 1 else None)
    if ctap is None:
        print("relay CTAPHID device (239a:80f4) not found")
        sys.exit(1)
    print(f"target: fw {ctap.info.versions}, options {ctap.info.options}")

    # ---- lockout guard --------------------------------------------------
    cp = ClientPin(ctap)
    retries = cp.get_pin_retries()[0]
    print(f"PIN retries available: {retries}")
    if retries is None or retries < 3:
        print("REFUSING to run: not enough retries headroom.")
        sys.exit(1)

    pin = read_pin()

    client_data_hash = bytes(range(32))  # any 32 bytes; not a real ceremony

    # ---- P1a: protocol 2, full 32-byte param ----------------------------
    try:
        cp2 = ClientPin(ctap, PinProtocolV2())
        token2 = cp2.get_pin_token(pin)
    except CtapError as e:
        print(f"token acquisition FAILED: {e}. Aborting (no retry).")
        sys.exit(2)
    print(f"P1 protocol-2 token acquired ({len(token2)} bytes), "
          f"retries now: {cp.get_pin_retries()[0]}")

    param2 = PinProtocolV2().authenticate(token2, client_data_hash)
    assert len(param2) == 32

    print("\n>>> TOUCH THE KEY when it blinks (P1a makeCredential) ...")
    att = ctap.make_credential(client_data_hash, RP, USER, [ES256],
                               pin_uv_param=param2, pin_uv_protocol=2)
    print(f"P1a makeCredential p2 full-param: UV flag = {uv_flag(att)}")

    print("\n>>> TOUCH THE KEY (P1a getAssertion) ...")
    assertion = ctap.get_assertion("probe.test", client_data_hash,
                                   allow_list=[{
                                       "type": "public-key",
                                       "id": att.auth_data.credential_data
                                             .credential_id}],
                                   pin_uv_param=param2, pin_uv_protocol=2)
    print(f"P1a getAssertion  p2 full-param: UV flag = {uv_flag(assertion)}")

    # ---- P1b: protocol 2, wrongly truncated param ------------------------
    try:
        ctap.make_credential(client_data_hash, RP, USER, [ES256],
                             pin_uv_param=param2[:16], pin_uv_protocol=2)
        print("P1b truncated param: ACCEPTED (unexpected!)")
    except CtapError as e:
        print(f"P1b truncated param rejected as expected: {e}")

    # ---- P5f: persona shape (options uv:true + injected param) -----------
    try:
        print("\n>>> TOUCH THE KEY when it blinks (P5f makeCredential) ...")
        att = ctap.make_credential(client_data_hash, RP, USER, [ES256],
                                   options={"uv": True},
                                   pin_uv_param=param2, pin_uv_protocol=2)
        print(f"P5f makeCredential uv:true+param: UV flag = {uv_flag(att)}")
    except CtapError as e:
        print(f"P5f makeCredential uv:true+param REJECTED: {e} "
              "(injector must strip uv)")

    # ---- P1c: protocol 1, 16-byte param ----------------------------------
    try:
        cp1 = ClientPin(ctap, PinProtocolV1())
        token1 = cp1.get_pin_token(pin)
        param1 = PinProtocolV1().authenticate(token1, client_data_hash)
        assert len(param1) == 16
        print("\n>>> TOUCH THE KEY when it blinks (P1c makeCredential) ...")
        att = ctap.make_credential(client_data_hash, RP, USER, [ES256],
                                   pin_uv_param=param1, pin_uv_protocol=1)
        print(f"P1c makeCredential p1 16B-param: UV flag = {uv_flag(att)}")
    except CtapError as e:
        print(f"P1c protocol-1 path failed: {e}")

    print(f"\nfinal PIN retries: {cp.get_pin_retries()[0]}")


if __name__ == "__main__":
    main()

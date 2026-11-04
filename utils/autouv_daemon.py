#!/usr/bin/env python3
"""Auto-UV token daemon — plan.md §5.4, phase 3.

Holds the FIDO2 PIN on the host and performs the CTAP2 clientPIN PAKE
with the YubiKey THROUGH the relay's CDC-brokered private channel (the
daemon must not touch /dev/hidraw3 directly: Linux hidraw does not fan
out reads, so it would steal Chrome's packets mid-ceremony). Pushes the
resulting pinUvAuthToken (plaintext, protocol 2, legacy unscoped —
phase-0 finding) to the Pico, which gates the persona on it.

The Pico never sees the PIN. The token is power-cycle-scoped: the Pico
drops it when the key re-attaches and this daemon re-acquires (epoch
watch in the heartbeat loop).

Usage:
    python3 utils/autouv_daemon.py [--port /dev/ttyACM1] [--once] [-v]

    --once   acquire the token, push it, verify, exit (no heartbeat loop)

PIN source (first found): PICO_RELAY_PIN env, .pin next to this
script / in cwd / in the repo root, else getpass.

Lockout safety (plan.md §5.4): getPinRetries is checked before every
acquisition and the daemon refuses to run below 3. Exactly one token
attempt per acquisition; on PIN_INVALID it NEVER retries — it says so
loudly and exits 2. A bug here must not eat the 3-strike/8-total FIDO
lockout budget.

Exit codes: 0 = daemon stopped cleanly (GOODBYE sent), 1 = transport /
protocol failure, 2 = PIN rejected (auto-UV disabled, fix the PIN and
restart), 3 = lockout guard refused to run.
"""

import argparse
import getpass
import hashlib
import json
import os
import sys
import time

import serial  # pyserial

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "lib"))
import cbor_min  # noqa: E402

# CTAP2
CLIENT_PIN = 0x06
SUB_GET_RETRIES = 0x01
SUB_GET_KEY_AGREEMENT = 0x02
SUB_GET_TOKEN_LEGACY = 0x05
PIN_PROTOCOL = 2

ERR_PIN_INVALID = 0x31
ERR_PIN_AUTH_INVALID = 0x33

HEARTBEAT_S = 5.0


def read_pin(interactive_ok=True):
    env = os.environ.get("PICO_RELAY_PIN")
    if env:
        return env.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, ".pin"), ".pin", os.path.join(here, "..", ".pin")):
        if os.path.isfile(p):
            mode = os.stat(p).st_mode & 0o777
            if mode & 0o077:
                print(
                    f"WARNING: {p} is mode {oct(mode)} - group/other can read "
                    "your FIDO2 PIN. Fix with: chmod 600 {p}"
                )
            with open(p) as f:
                pin = f.read().strip()
            if pin:
                return pin
    if not interactive_ok:
        return None
    return getpass.getpass("YubiKey FIDO2 PIN (input hidden): ")


class Relay:
    """Line protocol with the Pico's token service over the CDC data port."""

    def __init__(self, port, verbose=False):
        self.ser = serial.Serial(port, 115200, timeout=0.2)
        self.ser.reset_input_buffer()
        self.verbose = verbose
        self.epoch = None

    def _send(self, obj):
        if self.verbose:
            safe = dict(obj)
            if safe.get("c") == "SET_TOKEN":
                # never log the token bytes (they are a live UV credential)
                safe["h"] = "<redacted %dB>" % (len(safe.get("h", "")) // 2)
            print("->", safe)
        self.ser.write(json.dumps(obj).encode("utf-8") + b"\n")

    def _readline(self, timeout):
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                buf += chunk
                if b"\n" in buf:
                    line = buf.split(b"\n", 1)[0].strip()
                    if self.verbose:
                        print("<-", line.decode("utf-8", "replace"))
                    return json.loads(line)
        raise TimeoutError("no reply from the relay token service")

    def cmd(self, obj, expect, timeout=4.0):
        self._send(obj)
        while True:
            reply = self._readline(timeout)
            if reply.get("c") == expect:
                if "e" in reply and reply["e"] is not None:
                    self.epoch = reply["e"]
                return reply
            # unsolicited lines (e.g. late BROKERED) - keep waiting

    def hello(self):
        r = self.cmd({"c": "HELLO"}, "WELCOME")
        if r.get("e2"):
            raise RuntimeError(f"relay refused HELLO: {r['e2']}")
        return r

    def broker(self, message: bytes) -> bytes:
        r = self.cmd({"c": "BROKER", "h": message.hex()}, "BROKERED", timeout=5.0)
        if r.get("e"):
            raise RuntimeError(f"broker exchange failed: {r['e']}")
        return bytes.fromhex(r["h"])

    def set_token(self, token: bytes):
        r = self.cmd(
            {"c": "SET_TOKEN", "h": token.hex(), "p": PIN_PROTOCOL}, "ACK"
        )
        if not r.get("t"):
            raise RuntimeError("relay did not accept the token")
        return r

    def ping(self):
        return self.cmd({"c": "PING"}, "ACK")

    def goodbye(self):
        try:
            self.cmd({"c": "GOODBYE"}, "ACK", timeout=1.5)
        except (TimeoutError, OSError):
            pass


# ---- CTAP2 clientPIN over the broker -------------------------------------


def ctap2(relay, message: bytes):
    """One brokered CTAP2 exchange; returns (status, parsed-cbor-or-None)."""
    resp = relay.broker(message)
    status = resp[0]
    data = cbor_min.decode(resp[1:]) if len(resp) > 1 else None
    return status, data


def cose_xy(cose):
    def field(k):
        v = cose[k]
        if isinstance(v, int):
            return v.to_bytes(32, "big")
        return bytes(v)

    return field(-2), field(-3)


def acquire_token(relay, pin):
    """The full clientPIN dance. Returns the 32-byte pinUvAuthToken."""
    # 1. lockout guard (clientPIN keys: 1=pinUvAuthProtocol, 2=subCommand,
    # 3=keyAgreement, 6=pinHashEnc - positional args() mapping in CTAP2)
    status, data = ctap2(
        relay,
        bytes([CLIENT_PIN]) + cbor_min.encode({1: PIN_PROTOCOL, 2: SUB_GET_RETRIES}),
    )
    if status != 0x00:
        raise RuntimeError(f"getPinRetries failed: 0x{status:02x}")
    retries = data.get(3)
    print(f"PIN retries available: {retries}")
    if retries is None or retries < 3:
        print("REFUSING to attempt: not enough lockout headroom (need >= 3).")
        sys.exit(3)

    # 2. key agreement
    status, data = ctap2(
        relay,
        bytes([CLIENT_PIN])
        + cbor_min.encode({1: PIN_PROTOCOL, 2: SUB_GET_KEY_AGREEMENT}),
    )
    if status != 0x00:
        raise RuntimeError(f"getKeyAgreement failed: 0x{status:02x}")
    peer_x, peer_y = cose_xy(data[1])

    # 3. ECDH + HKDF (pinUvAuthProtocol 2, mirroring python-fido2)
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    priv = ec.generate_private_key(ec.SECP256R1())
    peer = ec.EllipticCurvePublicNumbers(
        int.from_bytes(peer_x, "big"), int.from_bytes(peer_y, "big"), ec.SECP256R1()
    ).public_key()
    shared = priv.exchange(ec.ECDH(), peer)
    hmac_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"\x00" * 32,
        info=b"CTAP2 HMAC key",
    ).derive(shared)
    aes_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"\x00" * 32,
        info=b"CTAP2 AES key",
    ).derive(shared)

    nums = priv.public_key().public_numbers()
    our_cose = {
        1: 2, 3: -25, -1: 1,
        -2: nums.x.to_bytes(32, "big"),
        -3: nums.y.to_bytes(32, "big"),
    }

    # 4. getPinToken with the encrypted PIN hash
    pin_hash = hashlib.sha256(pin.encode()).digest()[:16]
    iv = os.urandom(16)
    enc = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    pin_hash_enc = iv + enc.update(pin_hash) + enc.finalize()

    status, data = ctap2(
        relay,
        bytes([CLIENT_PIN])
        + cbor_min.encode(
            {
                1: PIN_PROTOCOL,
                2: SUB_GET_TOKEN_LEGACY,
                3: our_cose,
                6: pin_hash_enc,
            }
        ),
    )
    if status == ERR_PIN_INVALID:
        print("\n*** PIN REJECTED (CTAP2_ERR_PIN_INVALID). Never retrying —")
        print("*** check the PIN in .pin and restart the daemon.")
        print("*** (One attempt consumed; lockout budget intact.)")
        sys.exit(2)
    if status != 0x00:
        raise RuntimeError(f"getPinToken failed: 0x{status:02x}")
    token_enc = bytes(data[2])

    dec = Cipher(
        algorithms.AES(aes_key), modes.CBC(token_enc[:16])
    ).decryptor()
    token = dec.update(token_enc[16:]) + dec.finalize()
    if len(token) != 32:
        raise RuntimeError(f"unexpected token length {len(token)}")
    return token


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default="/dev/ttyACM1")
    ap.add_argument("--once", action="store_true",
                    help="acquire and push the token, then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    # NOTE: no --pin flag on purpose - a PIN on the command line is
    # readable by every local user via /proc/*/cmdline (plan.md §5.4).
    # Use PICO_RELAY_PIN (environ is same-user-only), .pin, or getpass.
    pin = read_pin()
    relay = Relay(args.port, verbose=args.verbose)

    print(f"connecting to relay on {args.port} ...")
    w = relay.hello()
    print(f"connected: epoch {w['e']}, token present: {w['t']}")

    def acquire_and_push():
        token = acquire_token(relay, pin)
        relay.set_token(token)
        print(f"token acquired ({len(token)} bytes) and pushed - persona is ON")

    acquire_and_push()
    if args.once:
        relay.goodbye()
        print("exiting (--once); persona falls back off until the next run")
        return

    print("heartbeat loop started (Ctrl-C to stop -> persona off)")
    try:
        while True:
            time.sleep(HEARTBEAT_S)
            epoch_before = relay.epoch
            r = relay.ping()
            if r.get("e") != epoch_before or not r.get("t"):
                print("key re-attached or token lost - re-acquiring")
                relay.hello()  # re-allocate the private channel if needed
                acquire_and_push()
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        relay.goodbye()
        print("GOODBYE sent - persona off; Chrome PIN prompts are back")


if __name__ == "__main__":
    main()

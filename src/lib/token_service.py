# token_service.py — Auto-UV phase 3 (plan.md §5.4): the Pico side of the
# token service.
#
# A host script (utils/autouv.py) holds the FIDO2 PIN and runs the
# CTAP2 clientPIN PAKE with the YubiKey over a PRIVATE CTAPHID channel
# that this service brokers over the CDC data port. The PIN never reaches
# the Pico; only the resulting pinUvAuthToken does — and that is scoped to
# the key's power cycle (cleared on re-attach) and to daemon liveness
# (heartbeat timeout), which is exactly what gates the persona.
#
# CDC line protocol (newline-delimited JSON, ttyACM1):
#   daemon -> pico: {"c":"HELLO"}                      allocate/reuse channel
#                     {"c":"BROKER","h":"<hex>"}        CTAP2 msg -> key
#                     {"c":"SET_TOKEN","h":"<hex>","p":2}
#                     {"c":"GOODBYE"}
#                     {"c":"PING"}                      heartbeat
#   pico -> daemon: {"c":"WELCOME","e":epoch,"t":bool}
#                     {"c":"BROKERED","h":"<hex>"}      key's CTAP2 response
#                     {"c":"BROKERED","e":"<reason>"}   failed exchange
#                     {"c":"ACK","e":epoch,"t":bool}    to SET_TOKEN/PING/...
#
# Broker mechanics: the private channel is allocated by sending a CTAPHID
# INIT (unique nonce) downstream and capturing the matching INIT response
# before it would be relayed upstream. While an exchange is in flight, all
# downstream traffic on the private cid is consumed here — never forwarded
# to the PC, never touching the touch-detection state.

import json
import time

import ctaphid_proxy

HEARTBEAT_TIMEOUT = 15.0  # s without daemon traffic -> persona off
BROKER_TIMEOUT = 2.0  # s for one brokered exchange with the key
MAX_BUF = 512  # CDC line buffer garbage guard

# broker states
IDLE = 0
INIT_SENT = 1
WAITING = 2


class TokenService:
    def __init__(self, sticky=False):
        self.port = None  # usb_cdc.data (bound by code.py; None = no data CDC)
        self.sender = None  # callback: 64-byte packet -> downstream EP
        self.buf = b""
        self.epoch = 0  # downstream attach generation
        self.token = None  # plaintext pinUvAuthToken (bytes) or None
        self.token_proto = 0
        self.daemon_alive = False
        self.last_contact = 0.0
        # Sticky mode (config AUTOUV_STICKY): a cached token alone keeps
        # the persona on - the daemon may exit after pushing it and the
        # token's exposure window is the key's power state instead of the
        # daemon's lifetime.
        self.sticky = sticky
        # broker exchange state
        self.state = IDLE
        self.deadline = 0.0
        self.cid = 0  # private CTAPHID channel (0 = none)
        self.nonce = b""
        self.asm = ctaphid_proxy.CtapAssembler()

    # ---- lifecycle ------------------------------------------------------

    def on_attach(self):
        """Downstream key (re)attached: tokens died with the power cycle."""
        self.epoch += 1
        self.token = None
        self.cid = 0  # the private channel died with the key too
        self.state = IDLE

    def active(self):
        """Persona gate.

        Sticky mode: a cached token alone keeps the persona on; the token
        still dies with the key's power state (on_attach clears it on
        re-attach/power-cycle) and GOODBYE is the explicit kill switch.
        Non-sticky (original semantics): additionally require a daemon
        that heartbeated within HEARTBEAT_TIMEOUT.
        """
        if self.token is None:
            return False
        if self.sticky:
            return True
        return (
            self.daemon_alive
            and time.monotonic() - self.last_contact < HEARTBEAT_TIMEOUT
        )

    def waiting(self):
        return self.state != IDLE

    # ---- main-loop entry points ------------------------------------------

    def poll(self):
        """Non-blocking: drain the CDC port, handle commands, check timeouts."""
        now = time.monotonic()
        if self.state != IDLE and now > self.deadline:
            timed_out_init = self.state == INIT_SENT
            self.state = IDLE
            if timed_out_init:
                # Channel-INIT allocation timed out: reply WELCOME-type so the
                # daemon's hello() fails cleanly instead of discarding a
                # stale BROKERED line that a later cmd() could mis-consume.
                self._reply({"c": "WELCOME", "e": self.epoch, "e2": "timeout"})
            else:
                self._reply({"c": "BROKERED", "e": "timeout"})
        port = self.port
        if port is None:
            return
        try:
            n = port.in_waiting
            if not n:
                return
            data = port.read(n)
        except OSError:
            return
        if not data:
            return
        self.daemon_alive = True
        self.last_contact = now
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self._handle(line)
            if len(self.buf) > MAX_BUF:
                self.buf = b""
                return

    def consume(self, pkt):
        """Offer a downstream (key -> relay) packet; True if consumed.

        Called by the main loop for EVERY 64-byte packet from the key,
        before any relay handling: broker traffic must never reach the PC
        or the touch state machine. The private-cid filter is NOT gated on
        the broker state - a timed-out or lost exchange drops state to IDLE
        while the channel lives on, and late strays leaking into the
        verbatim upstream path block send_report (2 s per packet with no
        host reader) and tear the session down (the bug class fixed after
        the phase-4 refactor).
        """
        if self.sender is None:
            return False
        cid = (pkt[0] << 24) | (pkt[1] << 16) | (pkt[2] << 8) | pkt[3]
        if self.state == INIT_SENT:
            if cid != 0xFFFFFFFF or pkt[4] != 0x86:
                return False  # not an INIT response at all
            if bytes(pkt[7:15]) != self.nonce:
                return False  # the PC's (Chrome's) INIT response, not ours
            self.cid = (
                (pkt[15] << 24) | (pkt[16] << 16) | (pkt[17] << 8) | pkt[18]
            )
            self.state = IDLE
            self._reply(
                {"c": "WELCOME", "e": self.epoch, "t": self.token is not None}
            )
            return True
        if self.state == WAITING:
            # everything on our private channel is ours
            if cid != self.cid:
                return False
            if pkt[4] == 0xBB:  # keepalive on our channel: ignore, keep waiting
                return True
            if pkt[4] == 0xBF:  # CTAPHID error: end the exchange with it
                self.state = IDLE
                self._reply({"c": "BROKERED", "e": "ctaphid-{:02x}".format(pkt[7])})
                return True
            r = self.asm.feed(pkt)
            if r is None:
                return True  # response still accumulating
            if r is ctaphid_proxy.BYPASS:
                self.state = IDLE
                self._reply({"c": "BROKERED", "e": "bypass"})
                return True
            _, msg = r
            self.state = IDLE
            self._reply({"c": "BROKERED", "h": msg.hex()})
            return True
        # IDLE: blackhole anything that still belongs to us - late strays
        # of a timed-out or corrupted exchange (same leak class as above),
        # and a late INIT response for an allocation that already timed out
        # (it is on the broadcast cid and matches our nonce, so it cannot
        # be the PC's).
        if self.cid and cid == self.cid:
            return True
        if (
            self.nonce
            and cid == 0xFFFFFFFF
            and pkt[4] == 0x86
            and bytes(pkt[7:15]) == self.nonce
        ):
            return True
        return False

    # ---- CDC command handling --------------------------------------------

    def _handle(self, line):
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
            cmd = msg.get("c")
        except (ValueError, AttributeError, TypeError):
            return
        if cmd == "HELLO":
            if self.cid and self.state == IDLE:
                self._reply(
                    {"c": "WELCOME", "e": self.epoch, "t": self.token is not None}
                )
            else:
                self._start_channel()
        elif cmd == "BROKER":
            self._broker(msg.get("h", ""))
        elif cmd == "SET_TOKEN":
            try:
                token = bytes.fromhex(msg.get("h", ""))
                if len(token) not in (16, 32):
                    raise ValueError("bad token length")
                self.token = token
                self.token_proto = int(msg.get("p", 2))
            except (ValueError, TypeError):
                self.token = None
            self._reply(
                {"c": "ACK", "e": self.epoch, "t": self.token is not None}
            )
        elif cmd == "GOODBYE":
            self.token = None
            self.daemon_alive = False
            self._reply({"c": "ACK", "e": self.epoch, "t": False})
        elif cmd == "PING":
            self._reply(
                {"c": "ACK", "e": self.epoch, "t": self.token is not None}
            )

    def _start_channel(self):
        if self.sender is None:
            self._reply({"c": "WELCOME", "e": self.epoch, "e2": "no-key"})
            return
        t = int(time.monotonic() * 1000)
        self.nonce = bytes(
            (
                0xA5,
                self.epoch & 0xFF,
                (t >> 24) & 0xFF,
                (t >> 16) & 0xFF,
                (t >> 8) & 0xFF,
                t & 0xFF,
                0x5A,
                (t ^ self.epoch) & 0xFF,
            )
        )
        pkt = bytearray(64)
        pkt[0] = 0xFF
        pkt[1] = 0xFF
        pkt[2] = 0xFF
        pkt[3] = 0xFF
        pkt[4] = 0x86  # CTAPHID INIT
        pkt[5] = 0
        pkt[6] = 8
        for i in range(8):
            pkt[7 + i] = self.nonce[i]
        self.sender(pkt)
        self.state = INIT_SENT
        self.deadline = time.monotonic() + BROKER_TIMEOUT

    def _broker(self, h):
        if self.state != IDLE or not self.cid:
            self._reply({"c": "BROKERED", "e": "no-channel"})
            return
        try:
            msg = bytes.fromhex(h)
        except (ValueError, TypeError):
            self._reply({"c": "BROKERED", "e": "bad-hex"})
            return
        if not msg or len(msg) > 1000:
            self._reply({"c": "BROKERED", "e": "bad-size"})
            return
        for pkt in ctaphid_proxy.packetize(self.cid, msg):
            self.sender(pkt)
        self.asm = ctaphid_proxy.CtapAssembler()
        self.state = WAITING
        self.deadline = time.monotonic() + BROKER_TIMEOUT

    def _reply(self, obj):
        port = self.port
        if port is None:
            return
        try:
            port.write(bytes(json.dumps(obj), "utf-8") + b"\n")
        except OSError:
            pass

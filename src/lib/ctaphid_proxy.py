# ctaphid_proxy.py — CTAPHID message-layer proxy (plan.md §5.1, Phase 1).
#
# Reassembles CTAP2/CBOR messages (CTAPHID wire command 0x90) in each
# direction so complete messages can be inspected or rewritten before
# being re-packetized (the persona and injector hooks of plan.md §5.2/§5.3
# install in later phases). With identity hooks the proxy is byte-for-byte
# equivalent to the raw pipe for all well-formed traffic: single-packet
# messages re-emit the identical 64-byte report, multi-packet messages
# re-emit the canonical segmentation.
#
# All other CTAPHID traffic (INIT, PING, MSG/U2F, WINK, KEEPALIVE, ERROR,
# CANCEL) is NOT handled here - the caller forwards those verbatim.
#
# Limitation (accepted, plan.md §5.1): one in-flight CBOR message per
# direction. CTAPHID forbids interleaving messages on a channel and CTAP2
# clients (Chrome, python-fido2) issue CBOR requests sequentially, so a
# new INIT-type 0x90 simply restarts the accumulator (a stale partial is
# discarded) and stray continuation packets fall back to verbatim
# forwarding.

CTAP_REPORT_LEN = 64
CMD_CBOR = 0x90  # CTAPHID CBOR command on the wire (0x10 | TYPE_INIT)
INIT_DATA_MAX = 57  # payload bytes in an INIT-type packet (64 - 7)
CONT_DATA_MAX = 59  # payload bytes in a continuation packet (64 - 5)
MAX_REASSEMBLE = 4096  # beyond this, stay a raw pipe for that message


class _Bypass:
    def __repr__(self):
        return "BYPASS"


BYPASS = _Bypass()  # sentinel: packet not consumed - forward verbatim


class CtapAssembler:
    """Reassembles one in-flight CTAPHID CBOR message.

    feed(pkt) -> None            accumulating (packet consumed)
                -> (cid, bytes)  message complete
                -> BYPASS        packet not consumed: forward verbatim

    An INIT-type 0x90 packet (re)starts a message. Continuation packets
    are consumed only while they match the accumulated channel id and
    sequence; anything unexpected clears the accumulator and returns
    BYPASS so the caller relays the stream untouched.
    """

    def __init__(self):
        self.cid = 0
        self.bcnt = 0
        self.seq = 0
        self.buf = None  # None = idle (no message in progress)

    @property
    def active(self):
        return self.buf is not None

    def feed(self, pkt):
        if pkt[4] & 0x80:  # INIT-type packet: (re)start the message
            if pkt[4] != CMD_CBOR:
                # KEEPALIVE/ERROR/CANCEL/... never start a CBOR message
                return BYPASS
            self.cid = (pkt[0] << 24) | (pkt[1] << 16) | (pkt[2] << 8) | pkt[3]
            self.bcnt = (pkt[5] << 8) | pkt[6]
            if self.bcnt == 0 or self.bcnt > MAX_REASSEMBLE:
                self.buf = None  # unbufferable: stay a raw pipe
                return BYPASS
            n = self.bcnt if self.bcnt < INIT_DATA_MAX else INIT_DATA_MAX
            self.buf = bytearray(pkt[7 : 7 + n])
            self.seq = 0
            if len(self.buf) >= self.bcnt:
                return self._finish()
            return None
        # continuation packet
        if self.buf is None:
            return BYPASS
        cid = (pkt[0] << 24) | (pkt[1] << 16) | (pkt[2] << 8) | pkt[3]
        if cid != self.cid or pkt[4] != self.seq:
            # foreign channel or lost sequence: give up, forward verbatim
            self.buf = None
            return BYPASS
        take = self.bcnt - len(self.buf)
        if take > CONT_DATA_MAX:
            take = CONT_DATA_MAX
        self.buf.extend(pkt[5 : 5 + take])
        self.seq = (self.seq + 1) & 0x7F
        if len(self.buf) >= self.bcnt:
            return self._finish()
        return None

    def _finish(self):
        msg = bytes(self.buf)
        cid = self.cid
        self.buf = None
        return cid, msg


def packetize(cid, message):
    """Yield 64-byte CTAPHID CBOR packets for a message (canonical split).

    Mirrors the segmentation every canonical CTAPHID sender produces:
    one INIT-type packet carrying the first 57 payload bytes, then CONT
    packets of 59 bytes, all zero-padded to the fixed report length.
    """
    first = message[:INIT_DATA_MAX]
    pkt = bytearray(CTAP_REPORT_LEN)
    pkt[0] = cid >> 24
    pkt[1] = (cid >> 16) & 0xFF
    pkt[2] = (cid >> 8) & 0xFF
    pkt[3] = cid & 0xFF
    pkt[4] = CMD_CBOR
    pkt[5] = len(message) >> 8
    pkt[6] = len(message) & 0xFF
    pkt[7 : 7 + len(first)] = first
    yield pkt
    rest = message[INIT_DATA_MAX:]
    seq = 0
    while rest:
        chunk = rest[:CONT_DATA_MAX]
        pkt = bytearray(CTAP_REPORT_LEN)
        pkt[0] = cid >> 24
        pkt[1] = (cid >> 16) & 0xFF
        pkt[2] = (cid >> 8) & 0xFF
        pkt[3] = cid & 0xFF
        pkt[4] = seq
        pkt[5 : 5 + len(chunk)] = chunk
        yield pkt
        rest = rest[CONT_DATA_MAX:]
        seq = (seq + 1) & 0x7F

# code.py — YubiKey USB relay for Raspberry Pi Pico (CircuitPython).
#
# Architecture (see AGENTS.md):
#   Upstream Agent   : native USB port (device set chosen in boot.py)
#   Downstream Agent : PIO USB host on GP16 (D+) / GP17 (D-) to the YubiKey
#   Orchestrator     : this loop, routing traffic between the two
#
# This loop relays whichever functions exist on BOTH sides:
#   OTP / Keyboard : YubiKey keyboard IN EP  ->  PC keyboard device
#                    (only in boot.py MODE="keyboard")
#   FIDO2 / CTAPHID: PC OUT reports -> YubiKey OUT EP,
#                    YubiKey IN EP  -> PC CTAPHID IN reports
#                    (transparent pipe: channel IDs stay end-to-end
#                    between the host and the YubiKey)
#   CCID           : ignored
#
# Timing notes:
#   - CircuitPython HID endpoints use bInterval=8ms, so the PC cannot hand
#     us OUT reports faster than every 8ms; the poll loop stays under that.
#   - YubiKey IN data is held by the device until polled (no loss).
#   - read()/write() timeout=0 means "wait forever"; the orchestrator below
#     picks explicit timeouts from the CTAPHID request/response phase.

import array
import time

import board
import digitalio
import usb_cdc
import usb_host
import usb.core
import usb_hid

import adafruit_usb_host_descriptors
import ctaphid_proxy
import injector
import persona
import token_service

DIR_IN = 0x80
CLASS_HID = 0x03
SUBCLASS_BOOT = 0x01
KBD_REPORT_LEN = 8
CTAP_REPORT_LEN = 64
CMD_CBOR = ctaphid_proxy.CMD_CBOR  # CTAPHID wire command for CTAP2/CBOR
ATTACH_TIMEOUT = 5.0  # seconds before power-cycling the downstream device
AUTO_TOUCH_DELAY = 3.0  # seconds of touch-wait before simulating the finger
AUTO_TOUCH_HOLD = 1.0  # seconds to hold the simulated touch

# Downstream device allowlist: only Yubico security keys are relayed.
# The upstream identity is always the Pico regardless of what answers
# downstream, so this is the only gate on what gets to act as the user's
# authenticator. Extend the tuple to trust other vendors' keys.
ALLOWED_VIDS = (0x1050,)

# ---------------------------------------------------------------- power

# GP5 drives the dual-transistor (NPN + PN2907A) high-side switch feeding
# 5V to the USB breakout. High = YubiKey powered, low/hi-Z = unpowered.
# At raw boot and during BOOTSEL/flashing the pin floats, so the YubiKey is
# unpowered until this code deliberately powers it - which sidesteps the
# CircuitPython cold-boot bug (#8886: a device attached around host-stack
# init is never enumerated) because we only energize the device AFTER the
# host port is initialized.
power = digitalio.DigitalInOut(board.GP5)
power.direction = digitalio.Direction.OUTPUT

# GP15 drives the YubiKey's touch input: low = touch active, high = none.
# Held high from boot (and the key is unpowered until GP5 enables it), so
# the key never sees a phantom touch while the firmware boots.
sim_touch = digitalio.DigitalInOut(board.GP15)
sim_touch.direction = digitalio.Direction.OUTPUT
sim_touch.value = True


def yubikey_power(on):
    power.value = on


# Boot sequence: force a clean downstream state, then init the host port,
# then attach the device. The power-off also clears a device left wedged
# by a firmware reset (on 10.3.1 such a device needs a physical replug
# without this switch).
yubikey_power(False)
time.sleep(1.0)
usb_host.Port(board.GP16, board.GP17)
time.sleep(0.1)
yubikey_power(True)

# Onboard LED status: slow blink = waiting for YubiKey (or power-cycling
# it), fast blink = YubiKey waiting for a user touch, solid = relaying.
led = digitalio.DigitalInOut(board.LED)
led.direction = digitalio.Direction.OUTPUT

# ---------------------------------------------------------------- upstream

# Keyboard device reports usage_page 0x01 (Generic Desktop), usage 0x06.
kbd_upstream = None
ctap_upstream = None
for _d in usb_hid.devices:
    if _d.usage_page == 0x01 and _d.usage == 0x06:
        kbd_upstream = _d
    elif _d.usage_page == 0xF1D0:
        ctap_upstream = _d

if kbd_upstream is None and ctap_upstream is None:
    raise RuntimeError("boot.py must enable a Keyboard or CTAPHID device")

print("upstream:", "CTAPHID" if ctap_upstream else "keyboard", "ready")


# -------------------------------------------------------------- downstream

def parse_interfaces(dev):
    """Return [(class, subclass, [(ep_addr, ep_size)])] for a device."""
    config = adafruit_usb_host_descriptors.get_configuration_descriptor(dev, 0)
    interfaces = []
    current = None
    i = 0
    while i < len(config):
        dlen = config[i]
        dtype = config[i + 1]
        if dtype == adafruit_usb_host_descriptors.DESC_INTERFACE:
            current = (config[i + 5], config[i + 6], [])
            interfaces.append(current)
        elif (
            dtype == adafruit_usb_host_descriptors.DESC_ENDPOINT
            and current is not None
        ):
            addr = config[i + 2]
            size = config[i + 4] | (config[i + 5] << 8)
            current[2].append((addr, size))
        i += dlen
    return interfaces


def scan_relay_endpoints(dev):
    """Find the relay-able endpoints on an attached device.

    Returns (kbd_ep, ctap_in_ep, ctap_out_ep); entries are None when the
    function is missing or has no upstream counterpart. HID interfaces
    only - CCID (smartcard) is ignored by design.
    """
    kbd_ep = None
    ctap_in = None
    ctap_out = None
    for cls, sub, eps in parse_interfaces(dev):
        if cls != CLASS_HID:
            continue
        ins = [a for a, s in eps if a & DIR_IN]
        outs = [a for a, s in eps if not (a & DIR_IN)]
        if sub == SUBCLASS_BOOT and ins and kbd_ep is None:
            kbd_ep = ins[0]  # OTP keyboard, 8-byte reports
        elif ins and outs and ctap_in is None:
            ctap_in = ins[0]  # CTAPHID pair, 64-byte reports
            ctap_out = outs[0]
    # Only relay functions that exist on both sides.
    if kbd_upstream is None:
        kbd_ep = None
    if ctap_upstream is None:
        ctap_in = None
        ctap_out = None
    return kbd_ep, ctap_in, ctap_out


def wait_for_yubikey():
    """Block until a relay-able device is attached; return its endpoints.

    Returns (dev, kbd_ep, ctap_in_ep, ctap_out_ep); endpoint entries are
    None when that function is missing or has no upstream counterpart.

    Self-healing: if nothing enumerates within ATTACH_TIMEOUT, power-cycle
    the downstream device and try again, forever.
    """
    print("waiting for YubiKey on GP16/GP17 ...")
    blink = False
    deadline = time.monotonic() + ATTACH_TIMEOUT
    while True:
        led.value = blink
        blink = not blink
        for dev in usb.core.find(find_all=True):
            if dev.idVendor not in ALLOWED_VIDS:
                continue  # not a trusted security key
            try:
                kbd_ep, ctap_in, ctap_out = scan_relay_endpoints(dev)
                if kbd_ep is None and ctap_in is None:
                    continue  # nothing relay-able on this device
                try:
                    dev.set_configuration()
                except usb.core.USBError:
                    pass  # already configured (e.g. after code.py reload)
                try:
                    name = dev.product or "device"
                except Exception:
                    name = "device"  # string fetch can fail on a flaky bus
                print(
                    "attached: {} kbd EP {:04x}, ctap {:04x}->{:04x}".format(
                        name,
                        kbd_ep or 0,
                        ctap_out or 0,
                        ctap_in or 0,
                    )
                )
                return dev, kbd_ep, ctap_in, ctap_out
            except Exception as e:
                print("enumerate error:", e)
        if time.monotonic() > deadline:
            # Nothing enumerated: power-cycle the YubiKey and retry.
            print("no device - power-cycling breakout 5V")
            yubikey_power(False)
            time.sleep(1.0)
            yubikey_power(True)
            deadline = time.monotonic() + ATTACH_TIMEOUT
        else:
            time.sleep(0.5)


# -------------------------------------------------------------- orchestrator

kbd_buf = array.array("B", [0] * KBD_REPORT_LEN)
ctap_buf = array.array("B", [0] * CTAP_REPORT_LEN)

# Timing model (all derived from CTAPHID's request-response structure):
# - The PC's HID endpoints use bInterval=8ms and CircuitPython's usb_hid
#   buffers only the LATEST OUT report, so while the host is streaming a
#   multi-packet request the loop must stay well under 8ms (short reads).
# - Once the request is complete, the host goes quiet and the YubiKey
#   answers within a few ms. A short read would abort the in-flight
#   transfer right as the data lands (corrupting the stream), so the
#   read window opens to 50ms until the response arrives or times out.
# - IN data is held by the YubiKey until polled: expired reads cost
#   latency, never a packet.
IDLE_MS = 2
RESP_MS = 50
INIT_DATA_MAX = 57   # payload bytes in an INIT-type packet (64 - 7 header)
CONT_DATA_MAX = 59   # payload bytes in a continuation packet (64 - 5 header)


def conts_expected(bcnt):
    """CONT packets a side must still send for a bcnt-byte message."""
    rest = bcnt - INIT_DATA_MAX
    if rest <= 0:
        return 0
    return (rest + CONT_DATA_MAX - 1) // CONT_DATA_MAX


# ------------------------------------------------- CTAP2 message hooks
# The message-layer proxy (plan.md §5.1) reassembles CTAPHID CBOR (0x90)
# messages in both directions and hands complete messages to these hooks
# before re-packetizing.
#
# Auto-UV master switch (plan.md). Phase 2: the GetInfo persona is
# always on when True. Phase 3 will gate this on the daemon holding a
# live pinUvAuthToken instead (safe fallback: persona off -> normal
# Chrome PIN prompts).
#
# PERSONA EFFECT (plan.md §5.2): GetInfo responses are rewritten so the
# host sees a biometric key - clientPin removed, uv:true added. Chrome
# then never shows a PIN dialog and sends options {uv:true} with no
# pinUvAuthParam; until the phase-4 injector exists, the real key
# rejects that option (UNSUPPORTED_OPTION) on UV-required / discoverable
# flows, while UV-preferred flows work touch-only (UV=false).
AUTOUV = True

# Injector policy (plan.md §5.3): "follow" injects pinUvAuthParam only when
# the request carries options.uv:true (Chrome's UV-required / discoverable
# shape); "always" injects on every makeCredential/getAssertion.
UV_POLICY = "follow"

# Injector RP-ID allowlist (plan.md §6, review finding): when non-empty,
# pinUvAuthParam injection only happens for these RP IDs - everything else
# degrades to touch-only (UV=false). The token itself cannot be scoped on
# this firmware (fw 5.4.3 rejects scoped tokens, phase-0 P2b), so this is the
# only rpId-level restriction available. Empty tuple = inject for any RP.
UV_RPIDS = ()

# Phase-3 token service (plan.md §5.4): the persona is gated on a live
# pinUvAuthToken pushed by the host daemon over the CDC data port — the
# PIN itself never leaves the host. Daemon absent/expired -> persona off
# -> normal Chrome PIN prompts (safe fallback).
token_svc = token_service.TokenService()

_pending_req = {}  # cid -> CTAP2 command byte of the outstanding request

# Rolling diagnostics (plan.md §7 fault forensics): raw packet headers are
# captured to a bounded in-memory ring at ~zero timing cost, and dumped to
# the console on anomaly signatures (orphaned CONT packets, CTAPHID errors,
# fault-handler entry). Prints per packet would perturb the very races
# being hunted, so the ring stores bytes and formats only on dump.
_ring = []
_RING_MAX = 48


def _ring_log(tag, b4):
    if len(_ring) >= _RING_MAX:
        _ring.pop(0)
    _ring.append((tag, bytes(b4)))


def _ring_dump(reason):
    print("DIAG:", reason)
    for tag, b in _ring:
        print(
            "  ",
            tag,
            " ".join("{:02x}".format(x) for x in b),
        )
    _ring.clear()


def _cid_of(pkt):
    return (pkt[0] << 24) | (pkt[1] << 16) | (pkt[2] << 8) | pkt[3]


def cbor_request_hook(cid, message):
    """PC -> YubiKey CTAP2 message (command byte + CBOR, e.g. 0x04=GetInfo)."""
    if AUTOUV and message:
        if len(_pending_req) > 32:
            _pending_req.clear()  # CTAPHID channels are never closed; cap growth
        _pending_req[cid] = message[0]
        if message[0] in (
            injector.CTAP2_MAKE_CREDENTIAL,
            injector.CTAP2_GET_ASSERTION,
        ):
            # mc/ga always run through the injector while AUTOUV is on:
            # with a live token it splices pinUvAuthParam (genuine UV);
            # without one it still strips options.uv so persona-shaped
            # requests degrade to touch-only instead of the key rejecting
            # them with UNSUPPORTED_OPTION (graceful daemon-outage path).
            new_msg, injected = injector.inject(
                message,
                token_svc.token,
                token_svc.token_proto,
                UV_POLICY,
                UV_RPIDS,
            )
            if injected:
                _ring_log("ij", message[:4])
                return new_msg
            if new_msg is not message:
                return new_msg  # uv stripped, no injection
    return message


def cbor_response_hook(cid, message):
    """YubiKey -> PC CTAP2 message (GetInfo, assertions, errors)."""
    if AUTOUV:
        # Always pop, even when the token gate is off mid-ceremony, so
        # stale entries never accumulate to the 32-entry wipe.
        cmd = _pending_req.pop(cid, None)
        if token_svc.active():
            if cmd == persona.CTAP2_GET_INFO:
                return persona.rewrite_getinfo(message)
            if (
                cmd in (injector.CTAP2_MAKE_CREDENTIAL, injector.CTAP2_GET_ASSERTION)
                and message
                and message[0] == 0x33
            ):
                # PIN_AUTH_INVALID: our cached token went stale (e.g. PIN
                # changed under us). Drop it; the daemon re-acquires on its
                # next heartbeat (<=5s) and the persona falls back off until
                # then. The affected ceremony fails visibly at the RP.
                token_svc.token = None
                print("token rejected (PIN_AUTH_INVALID) - dropped")
    return message


# Consecutive fault teardowns across attach epochs; reset only when a
# session proves stable, so an enumerable-but-wedged device gets power-
# cycled after three rapid faults instead of looping forever.
teardowns = 0

while True:
    dev, kbd_ep, ctap_in, ctap_out = wait_for_yubikey()
    led.value = True  # solid: relay active
    attached_at = time.monotonic()
    token_svc.on_attach()  # tokens died with the key's power cycle

    try:
        # The daemon rides the CDC data port (ttyACM1, enabled in boot.py);
        # stays None when the port is not enabled.
        # Hoisted bound methods: cuts per-iteration overhead on the RP2040.
        # CTAPHID entries stay None in keyboard mode (boot.py MODE="keyboard").
        get_report = (
            ctap_upstream.get_last_received_report
            if ctap_upstream is not None
            else None
        )
        ctap_send = (
            ctap_upstream.send_report if ctap_upstream is not None else None
        )
        ctap_read = dev.read
        ctap_write = dev.write

        # Single funnel points for every CTAPHID packet sent downstream or
        # upstream (verbatim relays and proxy re-emissions alike); the
        # tracing build (make_debug.py) instruments exactly these two.
        def send_downstream(pkt):
            _ring_log("F>", pkt[4:8])
            # A transient NAK/stall on the PIO bus can make dev.write raise
            # an empty-message USBError (observed on proxy re-emission
            # bursts); tearing the session down for it kills in-flight
            # ceremonies. Retry with backoff instead - only a genuinely
            # dead bus exhausts the attempts and re-raises to the outer
            # fault handler.
            for attempt in range(3):
                last_op[0] = "write_down"
                try:
                    ctap_write(ctap_out, pkt, timeout=100)
                    print("F>YK:", " ".join("{:02x}".format(b) for b in pkt[:8]))
                    return
                except usb.core.USBError:
                    _ring_log("F!", pkt[4:8])  # retried downstream write
                    if attempt == 2:
                        raise
                    time.sleep(0.01)

        def send_upstream(pkt):
            _ring_log("Y>", pkt[4:8])
            last_op[0] = "send_up"
            try:
                ctap_send(pkt)
                print("Y>PC:", " ".join("{:02x}".format(b) for b in pkt[:8]))
            except OSError:
                # One retry: the native USB stack can transiently refuse a
                # back-to-back send_report on the re-emission burst.
                # Ring-tagged ("Y!") so a retried-and-possibly-lost packet
                # stays visible in failure forensics.
                _ring_log("Y!", pkt[4:8])
                time.sleep(0.005)
                last_op[0] = "send_up_retry"
                ctap_send(pkt)

        # CTAP2/CBOR message-layer state, one in-flight message per
        # direction (see ctaphid_proxy.py for the limitation rationale).
        req_asm = ctaphid_proxy.CtapAssembler()   # PC -> YubiKey
        resp_asm = ctaphid_proxy.CtapAssembler()  # YubiKey -> PC

        # Broker writes go through the same hardened downstream funnel.
        token_svc.sender = send_downstream
        token_svc.port = usb_cdc.data

        # Fault attribution: the outer handler reports which call raised
        # (str(e) is often empty for usb.core.USBError/OSError).
        last_op = ["init"]

        pending_conts = 0  # CONT packets the host still owes for this message
        resp_pending = 0  # CONT packets the YubiKey still owes for its response
        touch_pending = False  # KEEPALIVE reports the key awaits a finger
        touch_phase = 0  # auto-touch: 0 idle, 1 countdown, 2 touching, 3 done
        touch_start = 0.0  # monotonic time of the first waiting KEEPALIVE
        read_ms = IDLE_MS
        read_errors = 0  # consecutive spurious USBErrors on downstream reads
        drop_stray_conts = False  # swallowing CONTs of a lost response

        while True:
            # 0) token service: daemon commands, broker timeouts, heartbeats
            token_svc.poll()

            # 1) PC -> YubiKey : drain ALL pending CTAPHID OUT reports.
            if get_report is not None:
                while True:
                    report = get_report()
                    if report is None:
                        break
                    cmd_byte = report[4]
                    _ring_log(">", report[4:8])
                    if not cmd_byte & 0x80 and not req_asm.active and not pending_conts:
                        # Orphaned host CONT: the PC is streaming a request
                        # continuation with no assembly and no verbatim
                        # message pending - an INIT was lost (only-latest OUT
                        # buffer). The key would then wait forever on a
                        # truncated request.
                        _ring_dump("orphan request CONT")
                    handled = False
                    if cmd_byte == CMD_CBOR or (
                        req_asm.active and not cmd_byte & 0x80
                    ):
                        # CTAP2/CBOR: message-layer proxy. The reassembled
                        # message passes through cbor_request_hook (identity
                        # in Phase 1) and is re-packetized canonically.
                        r = req_asm.feed(report)
                        if r is None:
                            # host still streaming CONTs: drain priority
                            read_ms = IDLE_MS
                            handled = True
                        elif r is not ctaphid_proxy.BYPASS:
                            cid, msg = r
                            msg = cbor_request_hook(cid, msg)
                            for pkt in ctaphid_proxy.packetize(cid, msg):
                                send_downstream(pkt)
                            # request complete (or single-packet): reply due
                            read_ms = RESP_MS
                            handled = True
                    if not handled:
                        # Any other command (or a proxy bypass): verbatim.
                        last_op[0] = "write_down_verbatim"
                        send_downstream(report)
                        if cmd_byte & 0x80:  # INIT-type packet: bcnt in header
                            pending_conts = conts_expected(
                                (report[5] << 8) | report[6]
                            )
                        else:  # continuation packet
                            if pending_conts > 0:
                                pending_conts -= 1
                        if pending_conts == 0:
                            # request complete (or single-packet): expect a
                            # reply
                            read_ms = RESP_MS
                        else:
                            # host still streaming CONTs: drain priority
                            read_ms = IDLE_MS

            # 2) YubiKey -> PC : CTAPHID IN reports (forward verbatim).
            if ctap_in is not None:
                # A short read timeout aborts the in-flight transfer, and if
                # that lands exactly as data arrives, TinyUSB reports
                # ABORTED instead of a clean timeout - a bare USBError even
                # though the device is fine. Retry a few times before
                # tearing down; only persistent errors mean a real fault.
                try:
                    last_op[0] = "read"
                    n = ctap_read(
                        ctap_in, ctap_buf, timeout=read_ms, raise_on_timeout=False
                    )
                except usb.core.USBError:
                    read_errors += 1
                    if read_errors > 5:
                        raise  # genuine fault: re-enumerate (and self-heal)
                    continue
                read_errors = 0
                # Interrupt IN packets are atomic: only ever forward full
                # 64-byte reports, never a short read padded with stale bytes.
                if n == CTAP_REPORT_LEN:
                    print("YK>>:", " ".join("{:02x}".format(ctap_buf[i]) for i in range(8)))
                    cmd_byte = ctap_buf[4]
                    _ring_log("<", ctap_buf[4:8])
                    # Broker/private-channel traffic first: consumed packets
                    # never reach the PC, the orphan detectors, or the touch
                    # state machine (a broker response INIT must not clear a
                    # concurrent Chrome touch-wait).
                    brokered = token_svc.consume(ctap_buf)
                    if brokered:
                        read_ms = RESP_MS if token_svc.waiting() else IDLE_MS
                    if (
                        not brokered
                        and drop_stray_conts
                        and not cmd_byte & 0x80
                    ):
                        # Swallowing stray CONTs of a response whose INIT or
                        # sibling CONT was lost on the downstream read race:
                        # the message is unrecoverable, the client already got
                        # a clean CTAPHID ERROR (INVALID_SEQ). Any INIT-type
                        # packet ends the swallow and resumes normal relaying.
                        _ring_log("XX", ctap_buf[4:8])
                        brokered = True  # consumed: skip all relay handling
                    elif not brokered and drop_stray_conts:
                        drop_stray_conts = False  # next message begins
                    if not brokered and (
                        not cmd_byte & 0x80
                        and not resp_asm.active
                        and not resp_pending
                    ):
                        # Orphaned CONT: the key is streaming a continuation
                        # with no response assembly in progress and no
                        # verbatim message pending either - a downstream
                        # packet was lost (read-abort race on the PIO bus).
                        # The message is unrecoverable: tell the client with
                        # a clean CTAPHID ERROR (INVALID_SEQ) it can retry
                        # from, and swallow the remaining CONTs, instead of
                        # leaking them (which surfaces as a confusing
                        # INVALID_COMMAND at the client).
                        _ring_dump("lost response packet - INVALID_SEQ sent")
                        _pending_req.pop(_cid_of(ctap_buf), None)
                        drop_stray_conts = True
                        err = bytearray(CTAP_REPORT_LEN)
                        err[0] = ctap_buf[0]
                        err[1] = ctap_buf[1]
                        err[2] = ctap_buf[2]
                        err[3] = ctap_buf[3]
                        err[4] = 0xBF  # CTAPHID ERROR
                        err[5] = 0
                        err[6] = 1
                        err[7] = 0x04  # INVALID_SEQ
                        send_upstream(err)
                        brokered = True  # this CONT is part of the wreckage
                    if not brokered and cmd_byte == 0xBF:
                        _ring_dump("CTAPHID error from key")
                        _pending_req.pop(_cid_of(ctap_buf), None)
                    handled = False
                    if not brokered and (
                        cmd_byte == CMD_CBOR
                        or (resp_asm.active and not cmd_byte & 0x80)
                    ):
                        # CTAP2/CBOR: message-layer proxy. The reassembled
                        # response passes through cbor_response_hook
                        # (identity in Phase 1) before going upstream.
                        was_active = resp_asm.active
                        r = resp_asm.feed(ctap_buf)
                        if r is None:
                            # response CONTs still due: stay open
                            read_ms = RESP_MS
                            handled = True
                        elif r is not ctaphid_proxy.BYPASS:
                            cid, msg = r
                            msg = cbor_response_hook(cid, msg)
                            for pkt in ctaphid_proxy.packetize(cid, msg):
                                send_upstream(pkt)
                            read_ms = IDLE_MS  # transaction complete
                            handled = True
                        elif was_active and not cmd_byte & 0x80:
                            # BYPASS on a CONT that belonged to an active
                            # assembly: a sibling CONT was lost mid-message.
                            # Same recovery as the orphan path above.
                            _ring_dump("lost mid-response CONT - INVALID_SEQ sent")
                            _pending_req.pop(_cid_of(ctap_buf), None)
                            drop_stray_conts = True
                            err = bytearray(CTAP_REPORT_LEN)
                            err[0] = ctap_buf[0]
                            err[1] = ctap_buf[1]
                            err[2] = ctap_buf[2]
                            err[3] = ctap_buf[3]
                            err[4] = 0xBF
                            err[5] = 0
                            err[6] = 1
                            err[7] = 0x04
                            send_upstream(err)
                            handled = True
                    if not brokered and not handled:
                        # Any other command (or a proxy bypass): verbatim.
                        # Broker packets are excluded TWICE (here and in the
                        # dispatch above): leaking one into send_upstream
                        # with no hidraw reader blocks the main loop 2s per
                        # packet in send_report, then tears the session down
                        # (the phase-3/4 refactor briefly regressed this and
                        # killed daemon acquisitions with no reader active).
                        last_op[0] = "send_up_verbatim"
                        send_upstream(ctap_buf)
                        if cmd_byte & 0x80:  # response INIT packet
                            resp_pending = conts_expected(
                                (ctap_buf[5] << 8) | ctap_buf[6]
                            )
                        else:  # response continuation packet
                            if resp_pending > 0:
                                resp_pending -= 1
                        # stay open only while response CONTs are still due;
                        # once the transaction completes, return to drain
                        # priority so the next request is never starved.
                        read_ms = RESP_MS if resp_pending else IDLE_MS
                    # Touch detection (observation only, no protocol
                    # change): a KEEPALIVE (cmd 0x3B -> 0xBB on the wire)
                    # with status 0x02 means the key is waiting for user
                    # presence; any other response packet means the
                    # ceremony was answered (or cancelled). The first
                    # status-2 KEEPALIVE arms the auto-touch timer; only
                    # a response clears it, so a stray "processing"
                    # keepalive cannot restart the countdown.
                    # MUST run for relay-path packets regardless of proxy
                    # dispatch: CBOR responses set handled=True while being
                    # reassembled, and a response INIT that fails to clear
                    # touch_pending here leaves it stuck - back-to-back
                    # ceremonies on one channel then never re-arm (phase-2
                    # regression caught by the phase-4 UV probes; luci masked
                    # it because every plugin run starts with a CTAPHID INIT
                    # that cleared it). Only broker traffic is excluded.
                    if not brokered:
                        if cmd_byte == 0xBB:
                            # Arm only when no hold is in flight
                            # (touch_phase == 0): arming during the phase-2
                            # hold would overwrite it and leave GP15 stuck
                            # low (no fresh touch edge for the next ceremony
                            # -> ACTION_TIMEOUT). Pre-existing race, surfaced
                            # by unattended back-to-back ceremonies; fixed
                            # during the Phase-1 proxy work.
                            if (
                                ctap_buf[7] == 2
                                and not touch_pending
                                and touch_phase == 0
                            ):
                                touch_pending = True
                                touch_phase = 1
                                touch_start = time.monotonic()
                        elif cmd_byte & 0x80:
                            touch_pending = False
                else:
                    read_ms = IDLE_MS  # window expired - back to drain priority

            # 3) YubiKey -> PC : OTP keyboard reports (keyboard mode only).
            if kbd_ep is not None:
                n = dev.read(kbd_ep, kbd_buf, timeout=10, raise_on_timeout=False)
                if n:
                    for i in range(n, KBD_REPORT_LEN):
                        kbd_buf[i] = 0
                    kbd_upstream.send_report(kbd_buf)

            # Auto-touch: once the key has waited AUTO_TOUCH_DELAY seconds,
            # simulate the finger for AUTO_TOUCH_HOLD seconds. One shot per
            # ceremony; a physical touch still works at any time. The hold
            # always runs its full course, even when the ceremony completes
            # early (the response clears touch_pending, but GP15 stays low
            # until AUTO_TOUCH_HOLD has elapsed).
            if touch_phase == 2:
                if (
                    time.monotonic() - touch_start
                    >= AUTO_TOUCH_DELAY + AUTO_TOUCH_HOLD
                ):
                    sim_touch.value = True  # finger off
                    touch_phase = 0
            elif touch_pending:
                if (
                    touch_phase == 1
                    and time.monotonic() - touch_start >= AUTO_TOUCH_DELAY
                ):
                    sim_touch.value = False  # finger on
                    touch_phase = 2
            else:
                touch_phase = 0  # ceremony over: re-arm for the next one

            # LED: solid = relaying, fast blink (~8 Hz) = waiting for touch.
            # Wall-clock based, so the poll loop is never blocked.
            led.value = (not touch_pending) or bool(int(time.monotonic() * 8) & 1)

    except (usb.core.USBError, OSError) as e:
        # Bus error or disconnect: tear down and re-enumerate (fault tolerance).
        # A device that still enumerates but keeps failing would otherwise
        # re-attach instantly and loop; power-cycle it after repeated faults.
        if time.monotonic() - attached_at > 5.0:
            teardowns = 0  # the session was stable for a while - not a wedge
        teardowns += 1
        try:
            import sys

            sys.print_exception(e)  # full traceback (str(e) is often empty)
        except Exception:
            pass
        _ring_dump("fault at " + last_op[0])
        print("relay error at", last_op[0], ":", e, "- re-enumerating")
        if teardowns >= 3:
            print("repeated faults - power-cycling YubiKey")
            yubikey_power(False)
            time.sleep(1.0)
            yubikey_power(True)
            teardowns = 0

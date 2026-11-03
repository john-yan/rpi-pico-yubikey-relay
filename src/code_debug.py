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
import usb_host
import usb.core
import usb_hid

import adafruit_usb_host_descriptors

DIR_IN = 0x80
CLASS_HID = 0x03
SUBCLASS_BOOT = 0x01
KBD_REPORT_LEN = 8
CTAP_REPORT_LEN = 64
ATTACH_TIMEOUT = 5.0  # seconds before power-cycling the downstream device

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
# it), solid = relay attached and running.
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


# Consecutive fault teardowns across attach epochs; reset only when a
# session proves stable, so an enumerable-but-wedged device gets power-
# cycled after three rapid faults instead of looping forever.
teardowns = 0

while True:
    dev, kbd_ep, ctap_in, ctap_out = wait_for_yubikey()
    led.value = True  # solid: relay active
    attached_at = time.monotonic()

    try:
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

        pending_conts = 0  # CONT packets the host still owes for this message
        resp_pending = 0  # CONT packets the YubiKey still owes for its response
        read_ms = IDLE_MS
        read_errors = 0  # consecutive spurious USBErrors on downstream reads

        while True:
            # 1) PC -> YubiKey : drain ALL pending CTAPHID OUT reports.
            if get_report is not None:
                while True:
                    report = get_report()
                    if report is None:
                        break
                    ctap_write(ctap_out, report, timeout=100)
                    print("F>YK:", " ".join("{:02x}".format(b) for b in report[:8]))
                    cmd_byte = report[4]
                    if cmd_byte & 0x80:  # INIT-type packet: header carries bcnt
                        pending_conts = conts_expected(
                            (report[5] << 8) | report[6]
                        )
                    else:  # continuation packet
                        if pending_conts > 0:
                            pending_conts -= 1
                    if pending_conts == 0:
                        # request complete (or single-packet): expect a reply
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
                    print("Y>PC:", " ".join("{:02x}".format(ctap_buf[i]) for i in range(8)))
                    ctap_send(ctap_buf)
                    cmd_byte = ctap_buf[4]
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
                else:
                    read_ms = IDLE_MS  # window expired - back to drain priority

            # 3) YubiKey -> PC : OTP keyboard reports (keyboard mode only).
            if kbd_ep is not None:
                n = dev.read(kbd_ep, kbd_buf, timeout=10, raise_on_timeout=False)
                if n:
                    for i in range(n, KBD_REPORT_LEN):
                        kbd_buf[i] = 0
                    kbd_upstream.send_report(kbd_buf)

    except (usb.core.USBError, OSError) as e:
        # Bus error or disconnect: tear down and re-enumerate (fault tolerance).
        # A device that still enumerates but keeps failing would otherwise
        # re-attach instantly and loop; power-cycle it after repeated faults.
        if time.monotonic() - attached_at > 5.0:
            teardowns = 0  # the session was stable for a while - not a wedge
        teardowns += 1
        print("relay error:", e, "- re-enumerating")
        if teardowns >= 3:
            print("repeated faults - power-cycling YubiKey")
            yubikey_power(False)
            time.sleep(1.0)
            yubikey_power(True)
            teardowns = 0

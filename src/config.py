# config.py — relay configuration.
#
# Every knob the relay respects lives here; code.py is logic only. Edit
# this file and run `make deploy` (or edit the deployed copy on the
# CIRCUITPY drive directly — code.py auto-reloads on save).
#
# Groups:
#   1. Pin assignment (Pico)       — physical wiring
#   2. USB / CTAPHID protocol facts — structural, do not change
#   3. Relay timing                 — tunable, see the timing model notes
#   4. Downstream allowlist          — security-relevant
#   5. Auto-UV feature               — security-relevant, see plan.md §6
#   6. Debug tracing                 — do not leave on

import board

import ctaphid_proxy

# ------------------------------------------------- 1. pin assignment (Pico)

# Physical wiring (AGENTS.md topology). The USB data lines must stay
# consecutive GPIO with D+ = D- + 1 (PIO USB host requirement).

# Dual-transistor (NPN + PN2907A) high-side switch feeding 5V to the USB
# breakout. High = YubiKey powered, low/hi-Z = unpowered.
PIN_POWER = board.GP5

# Drives the YubiKey's touch input: low = touch active, high = none.
PIN_SIM_TOUCH = board.GP15

# PIO USB host data lines to the breakout.
PIN_USB_DP = board.GP16
PIN_USB_DM = board.GP17

# Onboard status LED.
PIN_LED = board.LED

# ------------------------------------------------- 2. protocol facts

DIR_IN = 0x80            # USB endpoint direction mask (IN = device->host)
CLASS_HID = 0x03         # USB interface class: HID
SUBCLASS_BOOT = 0x01     # HID boot (keyboard) subclass
KBD_REPORT_LEN = 8       # keyboard HID report size
CTAP_REPORT_LEN = 64     # CTAPHID report size (fixed by the spec)
CMD_CBOR = ctaphid_proxy.CMD_CBOR  # CTAPHID wire command for CTAP2/CBOR

# Payload bytes per CTAPHID packet (derived from the 64-byte report):
INIT_DATA_MAX = 57       # INIT-type packet (64 - 7 header)
CONT_DATA_MAX = 59       # continuation packet (64 - 5 header)

# ------------------------------------------------- 3. relay timing

# Seconds without a downstream device before power-cycling it.
ATTACH_TIMEOUT = 5.0

# Auto-touch (simulated finger on GP15): seconds of touch-wait before
# answering the key, and how long to hold the contact.
AUTO_TOUCH_DELAY = 1.0
AUTO_TOUCH_HOLD = 1.0

# Poll-loop read windows (ms). Timing model:
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

# Diagnostic ring depth: raw packet headers kept for fault forensics
# (plan.md §7). Prints per packet would perturb the races being hunted,
# so the ring stores bytes and formats only on dump.
RING_MAX = 48

# ------------------------------------------------- 4. downstream allowlist

# Only Yubico security keys are relayed. The upstream identity is always
# the Pico regardless of what answers downstream, so this is the only
# gate on what gets to act as the user's authenticator. Extend the tuple
# to trust other vendors' keys.
ALLOWED_VIDS = (0x1050,)

# ------------------------------------------------- 5. Auto-UV feature

# Master switch (plan.md). When True the GetInfo persona and the
# pinUvAuthParam injector hooks are active — but only while the host
# daemon holds a live pinUvAuthToken (token_svc gating). Daemon absent
# or expired -> persona off -> normal Chrome PIN prompts (safe
# fallback); persona-shaped requests still degrade gracefully to
# touch-only.
AUTOUV = True

# Injector policy (plan.md §5.3): "follow" injects pinUvAuthParam only
# when the request carries options.uv:true (Chrome's UV-required /
# discoverable shape); "always" injects on every
# makeCredential/getAssertion.
UV_POLICY = "follow"

# Injector RP-ID allowlist (plan.md §6, review finding): when non-empty,
# pinUvAuthParam injection only happens for these RP IDs - everything
# else degrades to touch-only (UV=false). The token itself cannot be
# scoped on this firmware (fw 5.4.3 rejects scoped tokens, phase-0 P2b),
# so this is the only rpId-level restriction available. Empty tuple =
# inject for any RP.
UV_RPIDS = ()

# ------------------------------------------------- 6. debug tracing

# Packet tracing: set True (or run `make deploy-debug`) to log the first
# 8 bytes of every CTAPHID packet - channel ID, command, length and
# payload fragments - to the serial console. Do not leave enabled in
# normal use.
IS_DEBUG = False

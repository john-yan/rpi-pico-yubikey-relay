# Pico USB Relay for YubiKey

A Raspberry Pi Pico (CircuitPython) that sits between your PC and a YubiKey,
relaying FIDO2/WebAuthn traffic (and optionally OTP keystrokes) transparently.

```
[ PC ] ──Pico's native micro-USB── [ Raspberry Pi Pico ] ──GP16/GP17 + VBUS── [ YubiKey ]
         (presents CTAPHID             (relay orchestrator,                   (physical
          HID device to the PC)         this code.py)                          token)
```

## Files

| File | Purpose |
|---|---|
| `src/boot.py` | Defines the PC-facing USB device (CTAPHID or Keyboard). **Deploy to CIRCUITPY.** |
| `src/code.py` | The relay orchestrator. **Deploy to CIRCUITPY.** |
| `src/code_debug.py` | Generated: code.py + packet tracing. Regenerate with `python3 src/make_debug.py`. **Warning:** logs the first 8 bytes of every CTAPHID packet to the serial console — don't leave it deployed |
| `src/make_debug.py` | Generator for code_debug.py (`--check` verifies it is current) |
| `src/lib/adafruit_usb_host_descriptors.py` | Descriptor parsing helper. **Deploy to CIRCUITPY/lib.** |
| `test/test_ctaphid.py` | Host-side verification: CTAPHID INIT + multi-packet PING through the relay |
| `test/e2e_test.py` | Deploys code (default: production `src/code.py`), reloads the board, runs CTAPHID INIT/PING checks (helpers imported from `test_ctaphid.py`), captures the relay log |
| `utils/read_repl.py` / `utils/send_repl.py` / `utils/listen_repl.py` | Serial console helpers for /dev/ttyACM0 |
| `70-pico-yubikey-relay.rules` | udev rule so browsers/tests can access the relay's hidraw node |
Firmware images (`circuitpython-pico-*.uf2`) are kept locally next to the
repo as rollback copies but are git-ignored — fetch them from
[circuitpython.org](https://circuitpython.org/board/raspberry_pi_pico/)
if you need them.

## Deployment

1. Flash CircuitPython **10.3.1** for Raspberry Pi Pico (BOOTSEL mode, copy
   the .uf2 — a copy is saved here as `circuitpython-pico-10.3.1.uf2`).
   **Do not use 11.0.0-alpha.2**: it fixes the cold-boot attach issue
   ([circuitpython#8886]) but was observed to wedge the whole native USB
   stack (board vanishes from the bus) after YubiKey unplug/replug cycles.
   Revisit when 11.x reaches beta/stable.
2. Copy the sources to the CIRCUITPY drive:

   ```bash
   cp src/boot.py src/code.py /run/media/johnyan/CIRCUITPY/
   cp src/lib/adafruit_usb_host_descriptors.py /run/media/johnyan/CIRCUITPY/lib/
   ```
3. **Hard-reset the Pico** (replug or `microcontroller.reset()`), then plug
   the YubiKey in once the LED is blinking (10.3.1 misses a device attached
   around host init; hot-plug after boot is reliable).
4. Install the udev rule (one time, for browser access):

   ```bash
   sudo cp 70-pico-yubikey-relay.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```

5. Verify: `python3 test/test_ctaphid.py` → should print INIT + PING ok.

To redeploy code changes: `cp src/code.py /run/media/johnyan/CIRCUITPY/ && sync`
(auto-reload restarts it) or use `python3 test/e2e_test.py` (optionally
`python3 test/e2e_test.py src/code_debug.py` for a traced run).

[circuitpython#8886]: https://github.com/adafruit/circuitpython/issues/8886

## Hardware cautions (learned the hard way)

- **Plug the Pico directly into a PC root port, not a hub.** A flaky hub
  caused enumeration timeouts and may have contributed to a firmware crash
  that left the board unreachable until a cold power cycle.
- **Downstream 5V is switched by GP5** through a dual-transistor high-side
  switch (NPN driver + PN2907A). The pin floats at boot and during flashing,
  so the YubiKey is unpowered until code.py enables it after the USB host
  port is initialized - this sidesteps the CircuitPython cold-boot bug
  (#8886) and lets the firmware power-cycle the device to self-heal.
- The breakout's VBUS line (Pico pin 40) **back-powers the board** through
  the VSYS diode when the switch is on. To hard-reset the Pico itself,
  unplug the PC side - or drive GP5 low / disconnect the breakout first -
  otherwise BOOTSEL is ignored (sampled only at power-on).

## Operating modes (boot.py)

CircuitPython multiplexes **all** `usb_hid` devices onto a single USB HID
interface, and CTAPHID requires unnumbered fixed 64-byte reports. Adding the
standard Keyboard (which uses report ID 1) makes hosts mis-parse every CTAPHID
packet (Chrome rejects report-ID-0 writes outright), so the two relay functions
are mutually exclusive:

- `MODE = "ctaphid"` (default) — FIDO2 / WebAuthn relay
- `MODE = "keyboard"` — OTP keystroke relay only (no BIOS boot-protocol
  support: CircuitPython requires a boot keyboard on interface #0, which
  conflicts with CDC/MSC and puts the board in safe mode)

A true composite Keyboard + CTAPHID device needs a second HID interface, which
CircuitPython cannot express; that would require C-level firmware (e.g. a
TinyUSB-based build like the Feather RP2040 USB Host's Arduino offering).

## How the relay works (code.py)

- **Downstream**: PIO USB host on GP16 (D+) / GP17 (D-) enumerates the YubiKey,
  finds the CTAPHID HID interface (64-byte IN/OUT endpoints) and the boot
  keyboard interface, ignores CCID. Replug and bus errors re-enumerate safely.
- **Upstream**: the CTAPHID device from boot.py; `get_last_received_report()`
  yields each host OUT report exactly once; `send_report()` pushes IN reports.
- **Orchestrator**: a CTAPHID-aware state machine.
  - The host's HID endpoints use bInterval=8ms and CircuitPython buffers only
    the *latest* OUT report, so while the host streams a multi-packet request
    the loop keeps reads at 2 ms (drain priority).
  - The INIT packet's `bcnt` tells how many CONT packets follow; once the
    request is complete the read window opens to 50 ms so the YubiKey's reply
    (1-3 ms) lands inside the wait — expiring a read with data in flight can
    corrupt the stream, and short reads would abort exactly then.
  - Response `bcnt` is parsed symmetrically; when the transaction completes the
    loop drops back to drain priority so the next request is never starved.
- CTAPHID channel IDs stay end-to-end between the host and the YubiKey — the
  relay is a transparent pipe and keeps no protocol state beyond pacing.

## Known limitations

- **Keyboard (OTP) and CTAPHID cannot run simultaneously** (see modes above).
- **Onboard LED status**: slow blink = waiting for the YubiKey or power-
  cycling it, solid = relay attached and running.
- A short read timeout can abort a transfer exactly as data lands, surfacing
  as a bare USBError; the loop retries these and only tears down after six
  consecutive errors (a real device fault). After the teardown it
  power-cycles the YubiKey if nothing re-enumerates within 5 s, and also
  after three rapid fault teardowns even if the device still enumerates.
- The YubiKey 5.4.3's PING echo is unreliable for payloads needing 2+ CONT
  packets even when connected directly (verified against the bare device);
  real CTAP2 traffic is unaffected. The test suite uses a 100-byte PING.
- Host writes shorter than 64 bytes are silently dropped by CircuitPython
  (`out_report_lengths` mismatch). Real CTAPHID stacks always write 64.
- **Downstream allowlist**: only Yubico devices (VID 0x1050) are relayed
  (`ALLOWED_VIDS` in code.py). The PC always sees the Pico's identity
  upstream, so host-side policies cannot distinguish the downstream
  device — this allowlist is that gate. Extend the tuple for other keys.
- **udev rule breadth**: the rule matches every stock-CircuitPython Pico
  (239a:80f4), not just this relay; it grants hidraw access to the seated
  user only.
- **Keyboard mode is a keystroke-injection bridge by design**: whatever
  the downstream (allowlisted) device reports is typed verbatim into the
  host. Treat the breakout connection as keyboard input.

## Verified

Automated (`python3 test/test_ctaphid.py`, re-runnable):

- CTAPHID INIT handshake through the relay (nonce echoed, protocol v2).
- 100-byte multi-packet PING echoed byte-intact through the relay, both
  directions (3/3 consecutive standalone runs on both 10.3.1 and
  11.0.0-alpha.2).

Manual (observed once by hand, no automated test):

- Cold-boot reliability: after `microcontroller.reset()` with the YubiKey
  attached, it re-enumerates with no replug (GP5 power sequencing; the same
  scenario deterministically required a replug before the switch existed).
- Real WebAuthn: Chrome `git credential-luci reauth` — device probe, 251-byte
  multi-packet GET_ASSERTION, KEEPALIVE touch-prompt stream, 229-byte signed
  assertion relayed back intact.
- Behavior identical to the YubiKey connected directly (side-by-side
  comparison at multiple payload sizes).
- LED status behavior; power-cycle self-heal after device removal.

Not tested at all: keyboard mode (OTP scancode relay) beyond startup.

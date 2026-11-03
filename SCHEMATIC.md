# Connection Schematic

Wiring for the YubiKey USB relay: a Raspberry Pi Pico bridging a PC and a
YubiKey, with firmware-controlled 5 V power to the downstream device.

## System overview

```
┌────────────┐  micro-USB  ┌────────────────────────────┐   GP16/GP17    ┌──────────────┐
│     PC     │────────────►│      Raspberry Pi Pico     │══ D+ / D− ════►│ USB breakout │
│ (browser / │             │                            │   (data)       │    board     │
│ git-luci)  │             │  native USB device port:   │                └──────┬───────┘
└────────────┘             │  presents CTAPHID to the   │                USB-A │ VBUS, GND
                           │  PC (see src/boot.py)      │   GP5 ◄────── switch │ (below)
                           │                            │   (power)           ▼
                           │  PIO USB host port:        │                ┌──────────┐
                           │    GP16 = D+               │                │ YubiKey  │
                           │    GP17 = D−               │                └──────────┘
                           └────────────────────────────┘
```

Data flows: PC ⇄ Pico over the Pico's native micro-USB (CTAPHID, 64-byte
reports); Pico ⇄ YubiKey over GPIO bit-banged USB (PIO host). The YubiKey's
5 V is switchable by firmware via GP5.

## Pico pin assignment

| Pico pin | Phys. | Connects to | Notes |
|---|---|---|---|
| GP16 | 21 | breakout **D+** | PIO USB host D+ |
| GP17 | 22 | breakout **D−** | PIO USB host D− (must be D+ pin + 1) |
| GND | 38 (any GND) | breakout **GND** | common ground |
| VBUS | 40 | Q2 emitter, R2 | unswitched 5 V from the PC's USB port |
| GP5 | 7 | R1 → Q1 base | power-switch control (high = YubiKey on) |
| GP25 | onboard LED | — | status only: blink = waiting, solid = relaying |

## Power switch (GP5 → YubiKey 5 V)

A dual-transistor high-side switch: Q1 (any small NPN) level-shifts the
3.3 V GPIO to drive Q2 (PN2907A PNP), which switches the 5 V line.

```
        +5V VBUS (Pico pin 40, fed by the PC's USB port)
          │
          ├─────────────────────┐
          │                     │
          │                   [R2 10k]   (pull-up: Q2 OFF when GP5 floats)
          │                     │
          │                     ├─── B
          │                  ┌──┴────────┐
          └─────────────────►│ E         │
              (to Q2 E)      │    Q2     │  Q2: PN2907A (PNP)
                             │         C─┼──────────►  breakout VBUS
                             └───────────┘             (switched +5V → YubiKey)
                                   ▲
                                   │ (this node is Q2's base: R2 pulls it
                                   │  to +5V; R3 + Q1 pull it low to switch
                                   │  the YubiKey on)
                                   │
                              [R3 1k]
                                   │
                                   │ C
                              ┌────┴─────┐
              GP5 ──[R1 1k]───┤ B   Q1   │  Q1: any small NPN
                              │ (2N3904/ │
                              │  2N2222) │
                              │       E ─┼──►  GND
                              └──────────┘
```

Net list (the drawing above, as a list):

| From | Through | To |
|---|---|---|
| Pico VBUS (pin 40) | — | Q2 **emitter** |
| Pico VBUS (pin 40) | R2 (10 kΩ) | Q2 **base** |
| Q2 **base** | R3 (1 kΩ) | Q1 **collector** |
| Q1 **emitter** | — | GND |
| Pico GP5 (pin 7) | R1 (1 kΩ) | Q1 **base** |
| Q2 **collector** | — | breakout **VBUS** (YubiKey 5 V) |

TO-92 pinouts (flat face toward you, legs down): both the PN2907A and the
2N3904/2N2222 are **1 = E, 2 = B, 3 = C** — verify against your part's
datasheet, marking variants exist.

## Operating logic

| GP5 state | Q1 | Q2 | YubiKey power |
|---|---|---|---|
| high (3.3 V) | on | on | **on** (~4.7 V after Q2's saturation drop) |
| low (0 V) | off | off | off |
| floating (boot, BOOTSEL, safe mode) | off | off | off |

The floating-off row is the design's key property: the YubiKey is unpowered
until `code.py` drives GP5 high *after* the USB host port is initialized,
which sidesteps [circuitpython#8886] (device attached around host init is
never enumerated) and lets the firmware power-cycle the device to self-heal.

## Cautions

- **Pin 40 back-powers the Pico**: VBUS (pin 40) feeds VSYS through the
  on-board Schottky diode. While the switch is on, the breakout's 5 V keeps
  the Pico alive even if the micro-USB is unplugged — a hard reset then
  requires disconnecting the breakout (or driving GP5 low) so the chip
  actually loses power.
- **Keep the Pico on a PC root port, not a hub**: a flaky hub caused
  enumeration timeouts and a firmware crash during bring-up.
- The data lines (GP16/GP17) carry the YubiKey's USB traffic — keep the
  breakout wiring short; the PIO host is a bit-banged full-speed USB port.
- Before first power-up, verify with a multimeter: ~5 V at the breakout
  VBUS with GP5 high (or floating during boot: must be ~0 V), 0 V with GP5
  low.

[circuitpython#8886]: https://github.com/adafruit/circuitpython/issues/8886

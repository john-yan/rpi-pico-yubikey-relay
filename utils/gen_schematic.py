#!/usr/bin/env python3
"""Generate connection-schematic.svg — the wiring sheet for the Pico USB relay.

Style is EasyEDA Std's default ("Original") theme, taken from the colour
swatches in EasyEDA's View > Theme > Colour Setting dialog:

    symbol graphics + pins   #8B0000   wire             #008800
    pin names / numbers      #0000FF   junction         #FF0000
    reference / value        #000080   net label        #0000FF
    sheet + title block      #8B0000   net flag (5V/GND) #000000

EasyEDA conventions beyond colour: a pin dot at every connector/module pin,
pin numbers above the pin, pin names inside the body, `+5V` as a T-bar flag,
`GND` as a three-bar earth, and unfilled symbol bodies.

The drawing is a static SVG built from plain string templates — no third
party packages, no KiCad/EasyEDA install required:

    python3 utils/gen_schematic.py        # rewrites connection-schematic.svg
    rsvg-convert -w 1584 connection-schematic.svg -o /tmp/preview.png

Coordinates are in SVG user units on an A3 landscape sheet (1584x1120), with
the drawing grid on 10-unit steps. Nets and pin numbers follow src/config.py
and SCHEMATIC.md; U1's pin numbers are the Pico's physical header pins.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "connection-schematic.svg"

W, H = 1584, 1120                       # A3 landscape, ~3.8 units/mm

# ---- palette ---------------------------------------------------------------
SYM, PIN, WIRE, FIELD = "#8B0000", "#0000FF", "#008800", "#000080"
NOTE, FLAG, JUNCTION = "#0000FF", "#000000", "#FF0000"
PAPER, BODY = "#FFFFFF", "none"
F = "DejaVu Sans, Helvetica, Arial, sans-serif"

o = []
a = o.append

# ---- document --------------------------------------------------------------
a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
a('<title>Pico YubiKey USB Relay - schematic</title>')
a(f'<marker id="bjt" markerWidth="13" markerHeight="11" refX="11" refY="5.5" orient="auto" markerUnits="userSpaceOnUse">'
  f'<path d="M0,0 L11,5.5 L0,11 z" fill="{SYM}"/></marker>')
a(f'<rect x="0" y="0" width="{W}" height="{H}" fill="{PAPER}"/>')

# ---- sheet frame (zones 1-8 / A-F) -----------------------------------------
a(f'<g fill="none" stroke="{SYM}" stroke-width="1.6">')
a(f'<rect x="18" y="18" width="{W-36}" height="{H-36}"/>')
a(f'<rect x="36" y="36" width="{W-72}" height="{H-72}"/>')
a('</g>')
cols, rows = 8, 6
cw, rh = (W - 72) / cols, (H - 72) / rows
a(f'<g stroke="{SYM}" stroke-width="1.2" fill="none">')
for i in range(1, cols):
    x = 36 + i * cw
    a(f'<path d="M{x:.1f},18 V36 M{x:.1f},{H-36} V{H-18}"/>')
for i in range(1, rows):
    y = 36 + i * rh
    a(f'<path d="M18,{y:.1f} H36 M{W-36},{y:.1f} H{W-18}"/>')
a('</g>')
a(f'<g font-family="{F}" font-size="13" fill="{SYM}" text-anchor="middle">')
for i in range(cols):
    x = 36 + (i + 0.5) * cw
    a(f'<text x="{x:.1f}" y="32">{i+1}</text><text x="{x:.1f}" y="{H-22}">{i+1}</text>')
for i in range(rows):
    y = 36 + (i + 0.5) * rh + 4
    a(f'<text x="27" y="{y:.1f}">{"ABCDEF"[i]}</text><text x="{W-27}" y="{y:.1f}">{"ABCDEF"[i]}</text>')
a('</g>')

# ---- title block -----------------------------------------------------------
tx, ty = 1088, 908
tw = W - 36 - tx
a(f'<g stroke="{SYM}" stroke-width="1.6" fill="none">')
a(f'<rect x="{tx}" y="{ty}" width="{tw}" height="{H-36-ty}"/>')
for y in (950, 984, 1018, 1052):
    a(f'<path d="M{tx},{y} H{tx+tw}"/>')
for x1 in (1230, 1420):
    a(f'<path d="M{x1},950 V1018"/>')
a('</g>')
a(f'<g font-family="{F}" fill="{SYM}">')
a(f'<text x="{tx+12}" y="930" font-size="11">Title</text>')
a(f'<text x="{tx+62}" y="936" font-size="19" font-weight="bold">Pico YubiKey USB Relay</text>')
a(f'<text x="{tx+12}" y="972" font-size="11">Size</text><text x="{tx+62}" y="972" font-size="12">A3</text>')
a(f'<text x="1242" y="972" font-size="11">Date</text><text x="1292" y="972" font-size="12">2025-11-04</text>')
a(f'<text x="1432" y="972" font-size="11">Rev</text><text x="1478" y="972" font-size="12">1.0</text>')
a(f'<text x="{tx+12}" y="1006" font-size="11">EDA</text><text x="{tx+62}" y="1006" font-size="12">EasyEDA Std</text>')
a(f'<text x="1432" y="1006" font-size="11">Id</text><text x="1478" y="1006" font-size="12">1/1</text>')
a(f'<text x="{tx+12}" y="1040" font-size="11">File</text>')
a(f'<text x="{tx+62}" y="1040" font-size="11">connection-schematic.svg</text>')
a(f'<text x="{tx+12}" y="1072" font-size="11">Note</text>')
a(f'<text x="{tx+62}" y="1072" font-size="10.5">U1 pin numbers = Pico physical pins</text>')
a('</g>')


# ---- primitives ------------------------------------------------------------
def wire(*pts, w=2, color=WIRE):
    a(f'<path d="M{" L".join(f"{x},{y}" for x, y in pts)}" fill="none" '
      f'stroke="{color}" stroke-width="{w}"/>')


def pindot(x, y):
    a(f'<circle cx="{x}" cy="{y}" r="2.8" fill="{SYM}"/>')


def junction(x, y):
    a(f'<circle cx="{x}" cy="{y}" r="4" fill="{JUNCTION}"/>')


def gnd(x, y, side="right"):
    """Earth flag; connection point at (x, y), label left or right."""
    a(f'<g stroke="{FLAG}" stroke-width="2.2" fill="none">')
    a(f'<path d="M{x},{y} V{y+12}"/>')
    a(f'<path d="M{x-17},{y+12} H{x+17}"/>')
    a(f'<path d="M{x-11},{y+19} H{x+11}"/>')
    a(f'<path d="M{x-4},{y+26} H{x+4}"/>')
    a('</g>')
    if side == "right":
        a(f'<text x="{x+20}" y="{y+22}" font-family="{F}" font-size="11" fill="{FLAG}">GND</text>')
    else:
        a(f'<text x="{x-20}" y="{y+22}" font-family="{F}" font-size="11" fill="{FLAG}" '
          f'text-anchor="end">GND</text>')


def p5v(x, y):
    """T-bar supply flag; connection point at (x, y), stem upwards."""
    a(f'<g stroke="{FLAG}" stroke-width="2.2" fill="none">')
    a(f'<path d="M{x},{y} V{y-18}"/>')
    a(f'<path d="M{x-11},{y-18} H{x+11}"/>')
    a('</g>')
    a(f'<text x="{x}" y="{y-26}" font-family="{F}" font-size="12" fill="{FLAG}" '
      f'text-anchor="middle">+5V</text>')


def res_v(x, y1, y2, ref, val):
    a(f'<rect x="{x-8}" y="{y1+22}" width="16" height="{y2-y1-44}" fill="{BODY}" '
      f'stroke="{SYM}" stroke-width="2.2"/>')
    a(f'<path d="M{x},{y1} V{y1+22} M{x},{y2-22} V{y2}" stroke="{SYM}" stroke-width="2.2" fill="none"/>')
    a(f'<text x="{x+16}" y="{(y1+y2)//2+2}" font-family="{F}" font-size="12" fill="{FIELD}">{ref}</text>')
    a(f'<text x="{x+16}" y="{(y1+y2)//2+18}" font-family="{F}" font-size="12" fill="{FIELD}">{val}</text>')


def res_h(x1, x2, y, ref, val):
    a(f'<rect x="{x1+22}" y="{y-8}" width="{x2-x1-44}" height="16" fill="{BODY}" '
      f'stroke="{SYM}" stroke-width="2.2"/>')
    a(f'<path d="M{x1},{y} H{x1+22} M{x2-22},{y} H{x2}" stroke="{SYM}" stroke-width="2.2" fill="none"/>')
    a(f'<text x="{x1+8}" y="{y-16}" font-family="{F}" font-size="12" fill="{FIELD}">{ref}</text>')
    a(f'<text x="{x1+46}" y="{y-16}" font-family="{F}" font-size="12" fill="{FIELD}">{val}</text>')


def bjt(cx, cy, kind, ref, val):
    """BJT centred at (cx, cy): base pin left, C/E pins 66 units up and down."""
    a(f'<circle cx="{cx}" cy="{cy}" r="26" fill="none" stroke="{SYM}" stroke-width="2.2"/>')
    a(f'<path d="M{cx-14},{cy-16} V{cy+16}" stroke="{SYM}" stroke-width="4.5" fill="none"/>')
    a(f'<g stroke="{SYM}" stroke-width="2.2" fill="none">')
    a(f'<path d="M{cx-54},{cy} H{cx-14}"/>')
    a(f'<path d="M{cx-14},{cy-8} L{cx+16},{cy-26} M{cx+16},{cy-26} V{cy-66}"/>')
    a(f'<path d="M{cx-14},{cy+8} L{cx+16},{cy+26} M{cx+16},{cy+26} V{cy+66}"/>')
    a('</g>')
    if kind == 'pnp':      # arrow on the upper lead, pointing at the base bar
        a(f'<path d="M{cx+16},{cy-26} L{cx-8},{cy-12}" stroke="{SYM}" stroke-width="2.2" '
          f'fill="none" marker-end="url(#bjt)"/>')
    else:                  # arrow on the lower lead, pointing away
        a(f'<path d="M{cx-8},{cy+12} L{cx+16},{cy+26}" stroke="{SYM}" stroke-width="2.2" '
          f'fill="none" marker-end="url(#bjt)"/>')
    a(f'<text x="{cx+40}" y="{cy-4}" font-family="{F}" font-size="12" fill="{FIELD}">{ref}</text>')
    a(f'<text x="{cx+40}" y="{cy+12}" font-family="{F}" font-size="12" fill="{FIELD}">{val}</text>')


def cap_v(x, ytop, ref, val):
    """Capacitor hanging downwards from (x, ytop); pin 2 ends 50 units below."""
    a(f'<g stroke="{SYM}" stroke-width="2.2" fill="none">')
    a(f'<path d="M{x},{ytop} V{ytop+18}"/><path d="M{x-18},{ytop+18} H{x+18}"/>')
    a(f'<path d="M{x-18},{ytop+28} H{x+18}"/><path d="M{x},{ytop+28} V{ytop+50}"/>')
    a('</g>')
    a(f'<text x="{x+26}" y="{ytop+22}" font-family="{F}" font-size="12" fill="{FIELD}">{ref}</text>')
    a(f'<text x="{x+26}" y="{ytop+38}" font-family="{F}" font-size="12" fill="{FIELD}">{val}</text>')


def note(x, y, s, size=10.5, anchor="start"):
    a(f'<text x="{x}" y="{y}" font-family="{F}" font-size="{size}" fill="{NOTE}" '
      f'text-anchor="{anchor}">{s}</text>')


def label(x, y, s, anchor="start"):
    """Local net label (EasyEDA draws these in net-label blue)."""
    a(f'<text x="{x}" y="{y}" font-family="{F}" font-size="12.5" fill="{NOTE}" '
      f'text-anchor="{anchor}">{s}</text>')


def conn(x, y, w, h, ref):
    a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{BODY}" stroke="{SYM}" stroke-width="2.2"/>')
    a(f'<text x="{x}" y="{y-12}" font-family="{F}" font-size="12" fill="{FIELD}">{ref}</text>')


# ---- U1 : Raspberry Pi Pico ------------------------------------------------
UX1, UX2, UY1, UY2 = 120, 340, 300, 740
a(f'<rect x="{UX1}" y="{UY1}" width="{UX2-UX1}" height="{UY2-UY1}" fill="{BODY}" '
  f'stroke="{SYM}" stroke-width="2.2"/>')
a(f'<text x="{UX1}" y="{UY1-12}" font-family="{F}" font-size="13" fill="{FIELD}">U1</text>')
a(f'<text x="{UX1}" y="{UY2+20}" font-family="{F}" font-size="13" fill="{FIELD}">Raspberry Pi Pico</text>')
note(140, UY1 + 30, "upstream: native micro-USB \u2192 PC")

for num, name, y in [("40", "VBUS", 380), ("38", "GND", 440), ("21", "GP16", 500),
                     ("22", "GP17", 560), ("7", "GP5", 620), ("20", "GP15", 680)]:
    a(f'<path d="M{UX2},{y} H{UX2+40}" stroke="{SYM}" stroke-width="2.2"/>')
    pindot(UX2 + 40, y)
    a(f'<text x="{UX2-6}" y="{y+4}" font-family="{F}" font-size="12" fill="{PIN}" '
      f'text-anchor="end">{name}</text>')
    a(f'<text x="{UX2+20}" y="{y-6}" font-family="{F}" font-size="10" fill="{PIN}" '
      f'text-anchor="middle">{num}</text>')

# ---- J2 : downstream USB (breakout -> YubiKey) -----------------------------
JX1, JY1 = 1060, 410
conn(JX1, JY1, 100, 240, "J2")
a(f'<text x="{JX1}" y="{JY1+262}" font-family="{F}" font-size="12" fill="{FIELD}">Conn_01x04</text>')
note(JX1, JY1 + 280, "USB breakout \u2192 YubiKey")

for num, name, y in [("1", "VBUS", 440), ("2", "D+", 500), ("3", "D\u2212", 560), ("4", "GND", 620)]:
    a(f'<path d="M{JX1-40},{y} H{JX1}" stroke="{SYM}" stroke-width="2.2"/>')
    pindot(JX1 - 40, y)
    a(f'<text x="{JX1+6}" y="{y+4}" font-family="{F}" font-size="12" fill="{PIN}">{name}</text>')
    a(f'<text x="{JX1-20}" y="{y-6}" font-family="{F}" font-size="10" fill="{PIN}" '
      f'text-anchor="middle">{num}</text>')

# ---- U1 supply, ground and USB data ---------------------------------------
wire((380, 380), (440, 380), (440, 340));  p5v(440, 340)
gnd(380, 440)
wire((380, 500), (455, 500));              label(461, 494, "USB_DP")
wire((380, 560), (455, 560));              label(461, 554, "USB_DM")
wire((1020, 500), (960, 500));             label(990, 494, "USB_DP", "middle")
wire((1020, 560), (960, 560));             label(990, 554, "USB_DM", "middle")
wire((1020, 620), (1020, 650));            gnd(1020, 650, "left")

# ---- GP5 power switch (Q1 NPN -> R3 -> Q2 PNP high side) -------------------
p5v(836, 200); wire((836, 200), (836, 274)); junction(836, 210)
wire((836, 210), (756, 210), (756, 240))
res_v(756, 240, 340, "R2", "10k")
junction(756, 340); wire((756, 340), (766, 340))
bjt(820, 340, 'pnp', "Q2", "PN2907A")
wire((836, 406), (836, 440), (1020, 440))
res_v(756, 340, 440, "R3", "1k")
wire((756, 440), (756, 494), (836, 494))
bjt(820, 560, 'npn', "Q1", "2N3904")
res_h(666, 766, 560, "R1", "1k")
wire((666, 560), (620, 560), (620, 620), (380, 620))
wire((836, 626), (836, 632)); gnd(836, 632)

# ---- GP15 auto-touch: relay module K1 --------------------------------------
wire((380, 680), (480, 680), (480, 870), (560, 870))

BL, BT, BR, BB = 600, 750, 900, 960
a(f'<rect x="{BL}" y="{BT}" width="{BR-BL}" height="{BB-BT}" fill="{BODY}" '
  f'stroke="{SYM}" stroke-width="2.2"/>')
a(f'<text x="{BL}" y="{BT-12}" font-family="{F}" font-size="12" fill="{FIELD}">K1</text>')
a(f'<text x="{BL}" y="{BB+22}" font-family="{F}" font-size="12" fill="{FIELD}">relay module</text>')
note(BL, BB + 40, "IN active-low")

for num, name, y in [("1", "VCC", 790), ("2", "GND", 830), ("3", "IN", 870)]:
    a(f'<path d="M{BL-40},{y} H{BL}" stroke="{SYM}" stroke-width="2.2"/>')
    pindot(BL - 40, y)
    a(f'<text x="{BL+8}" y="{y+4}" font-family="{F}" font-size="12" fill="{PIN}">{name}</text>')
    a(f'<text x="{BL-20}" y="{y-6}" font-family="{F}" font-size="10" fill="{PIN}" '
      f'text-anchor="middle">{num}</text>')

for num, name, y in [("4", "COM", 810), ("5", "NO", 890)]:
    a(f'<path d="M{BR},{y} H{BR+40}" stroke="{SYM}" stroke-width="2.2"/>')
    pindot(BR + 40, y)
    a(f'<text x="{BR-8}" y="{y-8}" font-family="{F}" font-size="12" fill="{PIN}" '
      f'text-anchor="end">{name}</text>')
    a(f'<text x="{BR+20}" y="{y-6}" font-family="{F}" font-size="10" fill="{PIN}" '
      f'text-anchor="middle">{num}</text>')

# coil with winding diagonal, then the N.O. contact and the mechanical link
a(f'<rect x="700" y="800" width="32" height="80" fill="none" stroke="{SYM}" stroke-width="2.2"/>')
a(f'<path d="M700,880 L732,800" stroke="{SYM}" stroke-width="2.2" fill="none"/>')
a(f'<path d="M{BR},810 H846" stroke="{SYM}" stroke-width="2.2" fill="none"/>')
a(f'<circle cx="840" cy="812" r="3.5" fill="none" stroke="{SYM}" stroke-width="2.2"/>')
a(f'<path d="M840,890 H{BR}" stroke="{SYM}" stroke-width="2.2" fill="none"/>')
a(f'<circle cx="840" cy="890" r="3.5" fill="{SYM}"/>')
a(f'<path d="M840,890 L826,824" stroke="{SYM}" stroke-width="2.4" fill="none"/>')
a(f'<path d="M732,845 H818" stroke="{SYM}" stroke-width="1.6" fill="none" stroke-dasharray="6 5"/>')

# module supply
p5v(560, 790)
gnd(560, 830, "left")

# COM -> touch pad connector
a(f'<path d="M940,810 H980" stroke="{SYM}" stroke-width="2.2"/>')
pindot(940, 810)
a(f'<rect x="980" y="795" width="40" height="30" fill="{BODY}" stroke="{SYM}" stroke-width="2.2"/>')
a(f'<text x="965" y="803" font-family="{F}" font-size="10" fill="{PIN}" text-anchor="middle">1</text>')
a(f'<text x="980" y="783" font-family="{F}" font-size="12" fill="{FIELD}">J4</text>')
a(f'<text x="980" y="845" font-family="{F}" font-size="12" fill="{FIELD}">Conn_01x01</text>')
note(980, 863, "YubiKey touch pad")

# NO -> C1 -> GND
wire((940, 890), (1000, 890))
cap_v(1000, 890, "C1", "10uF")
gnd(1000, 940)

a('</svg>')

svg = "\n".join(o) + "\n"
OUT.write_text(svg, encoding="utf-8")
print(f"wrote {OUT.relative_to(ROOT)} ({len(svg)} bytes)")

# boot.py — Upstream Agent (PC-facing) device definition.
#
# Presents the Pico's native USB port to the PC as a CTAPHID device
# (FIDO Alliance usage page 0xF1D0, unnumbered 64-byte IN/OUT reports),
# the same shape as a real YubiKey's FIDO2 interface.
#
# IMPORTANT LIMITATION: CircuitPython multiplexes ALL usb_hid devices onto
# ONE USB HID interface with one composite report descriptor. CTAPHID
# requires unnumbered fixed 64-byte reports; adding the standard Keyboard
# (which uses report ID 1) would make the host treat the first byte of every
# CTAPHID packet as a report ID - Chrome refuses such writes outright
# ("Invalid output report ID"). So the keyboard (OTP) relay and the CTAPHID
# (FIDO2) relay are mutually exclusive here. Set MODE accordingly:
#
#   "ctaphid"  - FIDO2 / WebAuthn relay (default)
#   "keyboard" - OTP keystroke relay only
#
# usb_hid.enable() must run in boot.py, before USB starts. Changes take
# effect after a hard reset (power cycle or microcontroller.reset()).

import usb_hid

MODE = "ctaphid"

# CTAPHID report descriptor per the FIDO U2F HID spec:
# 64-byte input report + 64-byte output report, no report IDs.
CTAPHID_REPORT_DESCRIPTOR = bytes(
    [
        0x06, 0xD0, 0xF1,  # Usage Page (FIDO Alliance 0xF1D0)
        0x09, 0x01,        # Usage (U2F Authenticator HID)
        0xA1, 0x01,        # Collection (Application)
        0x09, 0x20,        #   Usage (Input Report Data)
        0x15, 0x00,        #   Logical Minimum (0)
        0x26, 0xFF, 0x00,  #   Logical Maximum (255)
        0x75, 0x08,        #   Report Size (8 bits)
        0x95, 0x40,        #   Report Count (64)
        0x81, 0x02,        #   Input (Data, Variable, Absolute)
        0x09, 0x21,        #   Usage (Output Report Data)
        0x15, 0x00,        #   Logical Minimum (0)
        0x26, 0xFF, 0x00,  #   Logical Maximum (255)
        0x75, 0x08,        #   Report Size (8 bits)
        0x95, 0x40,        #   Report Count (64)
        0x91, 0x02,        #   Output (Data, Variable, Absolute)
        0xC0,              # End Collection
    ]
)

if MODE == "ctaphid":
    ctaphid = usb_hid.Device(
        report_descriptor=CTAPHID_REPORT_DESCRIPTOR,
        usage_page=0xF1D0,
        usage=0x01,
        report_ids=(0,),  # descriptor specifies no report IDs
        in_report_lengths=(64,),
        out_report_lengths=(64,),
    )
    usb_hid.enable([ctaphid])

elif MODE == "keyboard":
    # No boot_device=1 here: CircuitPython requires a boot keyboard to be
    # USB interface #0, but CDC/MSC take the lower interface numbers, and
    # insisting on it puts the board in safe mode ("Boot device must be
    # first"). The keyboard still works normally in any OS.
    usb_hid.enable([usb_hid.Device.KEYBOARD])

else:
    raise ValueError("MODE must be 'ctaphid' or 'keyboard'")

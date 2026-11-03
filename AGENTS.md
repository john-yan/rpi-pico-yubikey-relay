# System Agents Architecture

This document defines the high-level operational actors (agents) within the Pi Pico USB Relay system. It outlines their primary roles, responsibilities, and how they interact to seamlessly bridge a composite USB peripheral (like a YubiKey) to a host PC.

## Physical Connection Topology

```
[ USB Device ]
      │ (USB-A Cable)
      ▼
[ USB Breakout Board ]
      │ (GPIO Data Lines + 5V VBUS)
      ▼
[ Raspberry Pi Pico ] ─── Native Micro-USB ───► [ PC ]
  (PIO USB Host)           (Native USB Device)
```

### Pinout

| USB Breakout Pin | Pico Pin | Description |
|---|---|---|
| VBUS (5V) | VBUS (Pin 40) | Taps the 5V incoming from the PC micro-USB port to power the downstream device. |
| GND | GND (e.g., Pin 38) | Common ground reference. |
| D+ (Data Plus) | GPIO 16 (or any GPIO pair) | PIO USB Host DP line. |
| D- (Data Minus) | GPIO 17 (must be consecutive, GPIO_DP + 1) | PIO USB Host DM line. |

## 1. Upstream Agent (Host-Facing Emulator)
**Role:** Acts as the virtual USB peripheral communicating directly with the PC or target operating system.
*   **Descriptor Management:** Registers and broadcasts the system's identity to the PC by presenting a composite USB device profile (e.g., simultaneous Standard HID Keyboard and FIDO2 CTAPHID).
*   **Ingress Handling:** Captures incoming `OUT` requests from the PC (such as WebAuthn challenges or status queries from a browser) and queues them for internal routing.
*   **Egress Handling:** Transmits outbound `IN` reports (such as keystrokes or signed cryptographic responses) to the PC exactly as the original peripheral would.
*   **Lifecycle Management:** Manages the USB connection state with the PC, handling enumeration, suspend/resume states, and endpoint configuration.

## 2. Downstream Agent (Peripheral-Facing Host)
**Role:** Acts as the virtual PC/Host communicating directly with the physical USB device attached to the breakout board.
*   **Hardware Abstraction:** Utilizes Programmable I/O (PIO) to emulate physical USB bus signaling, bit-stuffing, and clock synchronization on standard GPIO pins.
*   **Device Enumeration:** Detects when a device is plugged in, resets the bus, assigns a USB address, and reads the peripheral's configuration descriptors.
*   **Endpoint Discovery:** Dynamically parses the connected device's interfaces to locate required operational endpoints (e.g., identifying the 8-byte Keyboard IN endpoint vs. the 64-byte FIDO2 IN/OUT endpoints) while safely ignoring unsupported interfaces (like CCID/Smart Card).
*   **Polling Execution:** Continuously polls the peripheral's interrupt endpoints for new data and pushes pending data to the peripheral's output endpoints.

## 3. Relay Orchestrator (The Middleware)
**Role:** The central router and synchronization engine that bridges the Upstream and Downstream Agents.
*   **Traffic Multiplexing:** Identifies the type of data being handled (OTP keystroke vs. FIDO2 payload) and routes it between the correct internal buffers.
*   **Timing & Concurrency:** Manages the strict execution loop to ensure that blocking operations on one interface (like waiting for a keystroke) do not cause timeouts on time-sensitive protocols (like a browser's FIDO2 handshake).
*   **State Translation:** Ensures that packet sizes and logical states map correctly between the physical peripheral and the virtual device presented to the PC.
*   **Fault Tolerance:** Catches bus errors, unexpected disconnects, or pipeline stalls, safely tearing down and re-initializing the connections without requiring a hard reboot of the microcontroller.

## 4. Human Actor (Physical Authorization)
**Role:** The external participant required to complete secure transaction loops.
*   **User Presence Validation:** Physically touches the capacitive sensor on the downstream hardware to authorize cryptographic signatures. The system must natively account for the unpredictable timing of this human action by maintaining active, non-blocking polling loops until the physical interaction occurs.

## Primary Data Flows

*   **Unidirectional Flow (OTP / Keyboard):** 
    Peripheral generates keystroke -> Downstream Agent reads `IN` endpoint -> Orchestrator buffers data -> Upstream Agent pushes `IN` report to PC.
*   **Bidirectional Flow (FIDO2 / WebAuthn):** 
    PC initiates challenge -> Upstream Agent receives `OUT` report -> Orchestrator routes challenge -> Downstream Agent writes to peripheral -> Human Actor touches device -> Downstream Agent reads `IN` report -> Orchestrator routes response -> Upstream Agent pushes signed response to PC.
# plan.md — Auto-UV: PIN auto-entry in the Pico YubiKey relay

**Status: DESIGN (not implemented). Feature-flagged; default OFF preserves the
current transparent-pipe behavior.**

## 0. Goal

Chrome currently shows a FIDO2 PIN dialog on every WebAuthn ceremony because
both keys report `clientPin: true` and sites use the WebAuthn default
`userVerification: "preferred"`. Goal: the user touches the key, never types
a PIN in Chrome, and relying parties still receive **genuine, key-signed
`UV=true` assertions**. The PIN is entered once, by a host-side daemon, and
re-used via CTAP2 tokens.

Non-goals: changing luci/git flows (they must keep working unchanged),
supporting macOS/Windows Chrome, credential management UI in Chrome
(phase 6, optional).

## 1. Measured facts this design builds on

From `utils/fido_getinfo.py` runs and CTAP 2.3 spec (fetched, quoted in the
session log):

- Key: YubiKey 5 fw 5.4.3, `FIDO_2_1_PRE`, `pinUvAuthProtocols [2,1]`,
  `clientPin: true`, `rk: true`, `credentialMgmtPreview: true`,
  `maxMsgSize 1200`.
- CTAP2 rides CTAPHID command `0x90` (`CBOR`/`0x10`); `0x83` (`MSG`) is
  CTAP1/U2F-only on this firmware (CTAP2 payload via `0x83` → U2F-style
  `6E00`).
- **`pinUvAuthParam = authenticate(pinUvAuthToken, clientDataHash)`** for
  `makeCredential` and `getAssertion` — HMAC over the 32-byte client data
  hash only (CTAP 2.3 §6.5.4/§6.5.7), not the whole request. This is what
  makes injection cheap on the Pico.
- `authenticate` = HMAC-SHA-256; protocol 1 truncates to 16 bytes, protocol 2
  returns the full 32 bytes.
- Token permissions: `mc=0x01` (rpId required), `ga=0x02` (rpId required),
  `cm=0x04` (rpId optional).
- No "remove PIN" exists in CTAP; the only no-PIN alternatives are factory
  reset or this design.

## 2. Core idea: the biometric persona

Chrome's PIN dialog appears because *Chrome* decides to run the clientPIN
protocol when `GetInfo` says `clientPin: true` and UV is wanted. We cannot
"answer" that dialog from the device side. Instead the relay rewrites
`GetInfo` so Chrome believes it is talking to a **fingerprint key**
(YubiKey-Bio-like):

| GetInfo option | real key | persona (upstream) |
|---|---|---|
| `clientPin` | `true` | **removed** |
| `uv` | absent | **`true`** |
| everything else | — | passthrough |

Consequences:

- Chrome never shows a PIN dialog (biometric keys have no PIN prompt).
- `UV=preferred` sites: Chrome requests UV, expects the device to do it
  internally.
- `UV=required` sites: proceed (persona claims UV available) — **unlike a
  reset key, these keep working**.
- `UV=discouraged` sites (luci-style): unchanged, touch-only, no injection.
- The touch prompt remains (persona keys require a gesture too).

The relay then makes UV *actually happen* downstream by injecting
`pinUvAuthParam` into Chrome's requests, computed from a real
`pinUvAuthToken` obtained from the key with the real PIN. The UV flag in the
final assertion is set by the YubiKey itself after verifying our HMAC —
**nothing is forged at the assertion level**.

## 3. Architecture — where the crypto lives

The PAKE (ECDH P-256 + HKDF + AES-256-CBC + HMAC) is too heavy for
CircuitPython (no ECC support; pure-Python ECDH would cost seconds and
complexity). **Split it:**

```
                native USB (hidraw3)                PIO USB host
  Chrome ─────────────────────────────► Pico relay ─────────────► YubiKey 5
   ▲                                     │  ▲
   │ CTAPHID 0x90, GetInfo spoofed,      │  │ private CTAPHID channel
   │ pinUvAuthParam injected             │  │ (brokered raw bytes)
   │                                     ▼  │
   │                            CDC serial │
   └──────────── token service ────────────┘
             utils/autouv_daemon.py  (host, CPython)

  autouv_daemon.py  — holds the PIN (from user's keyring/config, 0600),
                      runs the full clientPIN PAKE through the Pico's
                      brokered private channel, derives pinUvAuthToken,
                      pushes {token, protocol, permissions, rpId} to the
                      Pico over CDC.
```

Why the daemon talks to the key *through the Pico* (CDC brokered) instead of
opening `/dev/hidraw3` directly: Linux hidraw does not fan out reads — a
daemon reading hidraw3 would steal Chrome's response packets mid-ceremony.
The CDC-brokered private channel is invisible to Chrome and deterministic.

Security property of the split: **the PIN never touches the Pico flash.**
The Pico holds only short-lived, permission-scoped tokens (invalidated on
key power cycle or PIN change). PIN compromise surface = the host, same as
any password manager.

### Component map (per AGENTS.md roles)

| AGENTS.md role | New component |
|---|---|
| Upstream Agent | `persona`: GetInfo rewriter (`uv:true`, drop `clientPin`) |
| Downstream Agent | `broker`: private-channel raw relay daemon↔key |
| Relay Orchestrator | `injector`: CTAP2 splice engine + token cache + policy |

## 4. Wire-level happy path

```
Chrome                 Pico                        YubiKey        daemon
  │  GetInfo            │                             │              │
  │────────────────────►│─ ─ ─ passthrough ─ ─ ─ ─ ─►│              │
  │                     │◄─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│              │
  │                     │ rewrite: -clientPin, +uv:true             │
  │◄────────────────────│ (persona ON only while daemon holds       │
  │                     │  a live token; else untouched)            │
  │                     │                                             │
  │  makeCredential     │                             │   ACQUIRE(rpId)  (once per
  │  (no pinUvAuthParam,│                             │◄───────────────────  rpId, cached)
  │   maybe options     │                             │  clientPIN dance:
  │   uv:true)          │                             │  getKeyAgreement,
  │────────────────────►│ cache miss → BROKER ───────►│  getPinToken/PAT w/ PIN
  │                     │◄───────────────────────────│              │
  │                     │  SET_TOKEN(token,proto,     │              │
  │                     │  perms,rpId) over CDC       │              │
  │                     │ append to request CBOR:                    │
  │                     │   pinUvAuthProtocol(0x07),                 │
  │                     │   pinUvAuthParam=HMAC(token,               │
  │                     │     clientDataHash)                        │
  │                     │─────────────────────────────►│             │
  │                     │        (user touches key; KEEPALIVEs       │
  │                     │         passed through upstream)           │
  │◄────────────────────│◄─ ─ assertion, UV=true, signed ─ ─ ─ ─ ─ ─ │
```

Chrome sees: biometric key → touch → UV=true. RP sees a genuine assertion.

## 5. Component specs

### 5.1 CTAPHID proxy layer (`src/lib/ctaphid_proxy.py`)

Refactor of the current raw pipe in `code.py`:

- Keep raw 64-byte relay for: INIT/PING/WINK/ERROR/KEEPALIVE and all CTAP1
  (`0x83`) traffic — zero behavior change, luci/U2F unaffected.
- For upstream `0x90` (CBOR) messages: **reassemble** the full message
  (INIT + CONT packets), hand the reassembled payload to the persona and
  injector hooks, then re-packetize downstream. Same in the reverse
  direction (GetInfo response rewrite needs reassembly of key→Chrome).
- Channel model: Chrome's CIDs pass through untouched (allocated by the key
  via INIT passthrough, as today). The relay's own private channel gets its
  own CID from a relay-initiated INIT; all traffic on that CID is routed to
  the broker, never upstream.
- Buffer budget: 2 × `maxMsgSize` (1200) + framing — trivial for RP2040.

### 5.2 Persona (`GetInfo` rewriter, `src/lib/persona.py`)

- Parse the reassembled GetInfo response CBOR; edit the `options` sub-map
  (key `0x04`): delete `clientPin`, add `uv: true`. Re-encode only the
  options map; splice it back into the response bytes.
- Minimal CBOR codec (`src/lib/cbor_min.py`): decode/encode maps, text
  keys, bools, ints, byte strings — only what these flows need (~150 lines,
  no general CBOR).
- Persona is **gated**: only active when the daemon has registered a live
  token. Daemon absent/dead → GetInfo passes through untouched → normal
  Chrome PIN prompts (safe fallback, see §7).

### 5.3 Injector (`src/lib/injector.py`)

For each reassembled upstream `0x90` message with CTAP2 command
`makeCredential (0x01)` or `getAssertion (0x02)`:

1. Extract `clientDataHash` (`0x01`… `0x04` field, byte string), and
   `rp/rpId` for the token permission scope.
2. Policy `UV_POLICY` (config in `code.py`):
   - `follow` (default): inject only when Chrome's `options` map contains
     `uv: true` (i.e. Chrome asked for UV). `UV=discouraged` stays
     UV=false — luci behavior preserved bit-for-bit.
   - `always`: inject on every mc/ga (fallback if Chrome turns out not to
     set the `uv` option for persona keys — benign failure of `follow`
     is UV=false on preferred sites, which RPs accept anyway).
3. Look up token for (permission, rpId); on miss, request acquisition from
   the daemon (§5.4) and wait (bounded, ~1 s).
4. Append `pinUvAuthProtocol` and `pinUvAuthParam` to the request map,
   re-encode, forward downstream.
5. **Strip `uv` from the `options` map** (confirmed required: fw 5.4
   returns `CTAP2_ERR_UNSUPPORTED_OPTION` for `uv:true` — phase-0 probe P5a),
   and **replace** any existing (empty, pre-flight) `pinUvAuthParam` /
   `pinUvAuthProtocol` entries rather than appending (P5c).
6. Error mapping: if the key returns `CTAP2_ERR_PIN_AUTH_INVALID`
   (`0x33`), invalidate the cached token, re-acquire once, retry once.
   `CTAP2_ERR_PIN_AUTH_BLOCKED` (`0x34`) → surface, fail-open.

Fail-open rule: if no token can be obtained, forward the request
unmodified (UV=preferred sites still work touch-only; UV=required sites
fail at the RP — visible, not silent) and blink the LED / log via CDC.

### 5.4 Token service (daemon `utils/autouv_daemon.py` + Pico side)

CDC serial protocol (newline-framed JSON or compact binary; JSON is fine at
this rate):

| Daemon → Pico | Pico → Daemon |
|---|---|
| `HELLO` | `WELCOME {persona: on/off}` |
| `BROKER {hex bytes}` — raw CTAPHID frames for the private channel | `BROKERED {hex bytes}` — raw responses from the key |
| — | `NEED_TOKEN {perm, rpId}` — injector cache miss |
| `SET_TOKEN {token_hex, protocol, perm, rpId}` | `ACK` |
| `HEARTBEAT` (every 5 s) | `ACK` |
| `GOODBYE` | — |

Daemon logic (host, CPython; `python-fido2` already vendored in this
session at `/tmp/fido2-1.2.0` — pin it into `.venv` properly):

- PIN source: `--pin` via libsecret/gnome-keyring if available, else
  `~/.config/pico-relay/pin` (0600, warn loudly). Never a CLI arg in
  long-running processes (visible in `ps`).
- Acquisition (through brokered channel):
  `INIT` → `getKeyAgreement` → `getPinUvAuthTokenUsingPinWithPermissions`
  (protocol 2; fall back to CTAP2.0 `getPinToken` if the `FIDO_2_1_PRE`
  firmware rejects the 2.1 subcommand — verify, see §9).
- **Lockout guard (critical):** before every attempt, `getPinRetries`; if
  < 3, refuse and notify. On `CTAP2_ERR_PIN_INVALID` (`0x31`): **never
  retry**, drop to persona-off, desktop notification "wrong PIN — auto-UV
  disabled". YubiKey locks after 3 consecutive misses (power-cycle unlock)
  and 8 total (reset required); a bug here can brick the FIDO app.
- Token lifecycle: invalidate on key detach/re-attach (Pico signals
  `KEY_ATTACHED`), on `GOODBYE`/heartbeat timeout, on `PIN_AUTH_INVALID`
  from the injector.
- systemd user unit (`utils/autouv-daemon.service`) for autostart.

### 5.5 Token cache on the Pico

- Small fixed table: 8 × {perm, rpId-hash, token[32], protocol}.
- Cleared on downstream key re-attach (tokens die at power cycle) and on
  daemon `GOODBYE`.

## 6. Security assessment (honest)

What we give up / accept:

1. **The PIN lives on the host** (daemon config/keyring) — same trust level
   as a password manager or the SSH agent. An attacker with the user's
   desktop session already owns the accounts anyway.
2. **The Pico never stores the PIN** — only power-cycle-scoped,
   permission-scoped tokens. Stealing the YubiKey alone still does *not*
   yield UV (better than baking the PIN into the relay flash, which was
   rejected).
3. **RP-visible UV remains truthful at the protocol level**: the YubiKey
   itself verifies the HMAC and sets the flag. What changes is *who typed
   the PIN and when* (once, at provisioning) — equivalent semantics to a
   remembered password, but RPs that treat UV as "fresh proof of the
   PIN-holder" see it as such per ceremony. This is the deliberate
   convenience-for-assurance trade the user has chosen.
4. **Relay compromise**: malicious Pico code could use live tokens within
   their rpId/permission scope while powered. Bounded by token invalidation
   at power cycle; the Pico never gains PIN knowledge.
5. **Downgrade is graceful**: unplug the daemon → persona off → Chrome
   prompts for the PIN again as today. No data is destroyed by disabling.

## 7. Failure modes

| Event | Behavior |
|---|---|
| Daemon not running | Persona off; raw passthrough; Chrome PIN prompts (today's behavior) |
| Daemon dies mid-ceremony | Heartbeat timeout → persona off; in-flight ceremony fails-open (UV=false where possible) |
| Wrong stored PIN | `getPinRetries` gate + single attempt + no-retry rule → auto-UV off, notification, **no lockout risk** |
| Token invalid (key replugged) | Cache cleared; re-acquire on next ceremony |
| `PIN_AUTH_INVALID` from key | One re-acquire + one retry, then fail-open + log |
| CBOR parse error in injector | Log, forward unmodified (fail-open), never brick the pipe |
| 3 rapid faults | Existing watchdog re-init loop in `code.py` extended to reset token cache/persona |

## 8. Phased implementation (each phase independently testable)

- **Phase 1 — Proxy refactor. DONE (2025-11-04).** CTAPHID reassembly/
  re-packetization with hooks, behavior-identical. Verified: make test,
  make e2e, GetInfo and full CTAP2 ceremonies (UV=true) through the proxy,
  10/10 unattended luci reauth. Hardening added during verification:
  send_downstream retry (transient PIO NAK/stall was tearing down sessions
  — the user's original ~75% luci failure), send_upstream retry, auto-touch
  arm race fix, fault attribution, diagnostic ring buffer.
- **Phase 2 — Persona. DONE (2025-11-04).** cbor_min (canonical CTAP2
  CBOR — map keys length-first sorted; MicroPython dicts iterate in hash
  order, so explicit sorting is mandatory; python-fido2 rejects
  non-canonical responses) + persona.py (GetInfo rewrite, fail-open) +
  AUTOUV flag and request-tracking hooks in code.py. Verified on-device:
  GetInfo through the relay reports uv:true with clientPin removed;
  python-fido2 1.2.0 and 2.0.0 clients accept the rewritten response;
  luci reauth 43/43 through the persona. Chrome dry-run pending (user).
  Known-open: two early occurrences of a dropped response-INIT re-emission
  (client saw orphaned CONTs -> INVALID_COMMAND), not reproduced in 43
  subsequent runs; the diagnostic ring + orphan-CONT detectors stay
  deployed to capture it if it recurs. Expected until phase 4: UV-required
  and discoverable/passkey flows fail (the key rejects options uv:true —
  UNSUPPORTED_OPTION); UV-preferred flows work touch-only with UV=false.
- **Phase 3 — Token service.** Daemon + broker + PIN dance + lockout guard.
  Verify: daemon logs token acquisition; Pico caches; wrong-PIN path
  disables cleanly with retries counter intact (`getPinRetries` before/
  after).
- **Phase 3 — Token service. DONE (2025-11-04).** boot.py enables a CDC
  data port (ttyACM1); src/lib/token_service.py implements the Pico side
  (private-channel allocation via nonce-matched CTAPHID INIT, brokered
  CTAP2 exchanges with their own assembler, token cache, heartbeat/
  epoch gating); utils/autouv_daemon.py holds the PIN on the host and
  runs the full clientPIN PAKE (protocol 2, hand-rolled against the
  python-fido2 reference) through the broker. Persona is now gated on a
  live token + fresh daemon heartbeat (fallback: off -> Chrome PIN
  prompts). Verified: token acquired through the relay (retries 8,
  32-byte protocol-2 token), persona on/off transitions both ways,
  wrong-PIN aborts after exactly one attempt with exit 2 (retries
  8->7, correct PIN restores), luci reauth through persona+daemon,
  make test/e2e. NOTE (clientPIN CBOR keys are positional in CTAP2:
  1=pinUvAuthProtocol, 2=subCommand, 3=keyAgreement, 6=pinHashEnc —
  getting these wrong yields CBOR_UNEXPECTED_TYPE 0x11). The rare
  dropped-response-INIT failure (~5% of luci runs, see phase-2 note)
  remains open; send-retry events are now ring-tagged ("Y!"/"F!") so
  the next capture will show whether the lost INIT hit the retry path.
- **Phase 4 — Injector. DONE (2025-11-04).** src/lib/sha256.py (pure-
  Python SHA-256 - the RP2040 CircuitPython build's hashlib has no
  sha256; verified byte-identical to CPython across padding
  boundaries, ~250 ms per ceremony HMAC) + src/lib/injector.py (uv
  stripped, pinUvAuthParam = HMAC(token, clientDataHash) spliced,
  positional per-command CTAP2 keys, follow/always policy, fail-open)
  wired into cbor_request_hook; PIN_AUTH_INVALID responses drop the
  cached token. Verified on-device (daemon + persona + injector):
  makeCredential {uv:true} -> UV=true; passkey shape {uv:true,rk:true}
  -> UV=true; getAssertion {uv:true} -> UV=true; discouraged shape
  untouched (UV=false); luci 5/5 + 10/10; make test.
  Two regressions found and fixed during phase-4 verification:
  (a) the phase-2 re-indent had scoped touch detection inside the
  proxy dispatch, so CBOR response INITs never cleared touch_pending
  (back-to-back ceremonies on one channel lost auto-touch; luci
  masked it via its per-run CTAPHID INIT) - now keyed on
  broker-consumed; (b) the same refactor leaked broker packets into
  the verbatim upstream path - with no hidraw reader, send_report
  blocked 2 s per packet and tore the session down, killing daemon
  acquisitions (user-reported; fixed by excluding broker traffic
  from the verbatim path explicitly).
  Lost-packet recovery deployed for the open ~5-10% failure: orphaned
  or mid-message-lost response CONTs now produce a clean CTAPHID
  ERROR (INVALID_SEQ) to the client plus silent swallowing of the
  strays, instead of leaked CONTs surfacing as INVALID_COMMAND; the
  event ring-dumps ("lost response packet") for forensics.
- **Phase 5 — Hardening.** systemd unit, keyring, LED patterns, REPL logs,
  fault-recovery integration.
- **Phase 6 (optional) — credMgmt.** Intercept Chrome's
  `getPinUvAuthTokenUsingUvWithPermissions` clientPIN subcommand and answer
  with a daemon `cm`-permission token, enabling Chrome's passkey management
  UI against the persona.

## 9. Test matrix

| Case | Expectation |
|---|---|
| `make test`, `make e2e` | pass unchanged (raw paths intact) |
| Chrome + webauthn.io (UV preferred) | touch only, no PIN dialog, assertion UV=true |
| UV=required demo RP | succeeds, UV=true |
| luci / `git credential-luci reauth` | unchanged (CTAP1 passthrough / UV-discouraged, UV=false) |
| Passkey create + restore (rk) | works; token `mc`/`ga` per rpId |
| Daemon stopped mid-session | next ceremony falls back to Chrome PIN prompt |
| Wrong PIN in daemon | auto-UV off, PIN retry counter unchanged |
| YubiKey re-plugged | token cache cleared, next ceremony re-acquires |
| Second key on direct port | none — test with only the relay key attached |

## 10. Phase-0 spec verification — RESULTS (probes in `utils/spec_probes.py`, PIN-free, and `utils/spec_probes_pin.py`, user-run)

Resolved 2025-11-03 against the fw 5.4.3 YubiKey 5 behind the relay, the
CTAP 2.3 spec, python-fido2 1.2.0 source, and Chromium `device/fido` sources:

| # | Question | Result | Design impact |
|---|---|---|---|
| 1 | `pinUvAuthParam` scope & length | `authenticate(pinUvAuthToken, clientDataHash)`; protocol 2 → 32 B, protocol 1 → 16 B (spec + `fido2/client.py:523,759,890`, `pin.py` `PinProtocolV1/V2.authenticate`). **Empirical: 32 B param under protocol 2 → `UV=true` on both makeCredential and getAssertion, through the relay** | Injector stores protocol alongside token; sends the 32 B param python-fido2 produces |
| 2 | Scoped 2.1 token subcommand | `getPinUvAuthTokenUsingPinWithPermissions` (0x09) → `INVALID_COMMAND`, same as unknown-subcommand control (0x7f). Legacy `getPinToken` (0x05) → `MISSING_PARAMETER` (recognized). Consistent with GetInfo lacking `pinUvAuthToken` | **Daemon uses legacy unscoped getPinToken; Pico token cache = single entry** |
| 3 | Protocol-2 token response encoding | Encrypted under the ECDH shared secret (`pin.py: get_pin_token` decrypts `resp[PIN_UV_TOKEN]`); P-2 KDF = HKDF-SHA256 (salt=32×0, info `CTAP2 HMAC key`/`CTAP2 AES key`), AES-256-CBC random-IV | Daemon mirrors python-fido2 exactly |
| 4 | Chrome's UV request shape for bio persona | Chromium `PINUVDispositionForGetAssertion/MakeCredential`: persona (`uv:true`, no `clientPin`, no `pinUvAuthToken`) → `kNoTokenInternalUV` = *"send the request as-is with uv=true"*; encoder writes `options {uv:true}` iff resolved `kRequired` (RP-required, discoverable allow-list — `SpecializeRequestForAuthenticator` forces `kRequired` for empty allow-lists — or `alwaysUv`) | **`UV_POLICY=follow` (inject iff `options.uv==true`) matches Chromium's wire behavior; `always` kept as fallback** |
| 5 | Key accepts `options.uv:true`? | **NO** — `getAssertion` with `options {uv:true}`, no param → `UNSUPPORTED_OPTION (0x2B)`, rejected before touch; **and with a valid injected param present → still `UNSUPPORTED_OPTION` (empirical)** | **Injector MUST strip `uv` from options before forwarding — mandatory, no exceptions** |
| 6 | Discoverable flows without token | Key proceeds to touch-wait (CTAP2.0-style), no upfront `PUAT_REQUIRED`; empty `pinUvAuthParam` (Chrome pre-flight) accepted at parse level | Injector must **replace** an existing empty `pinUvAuthParam`, not append; still injects for UV on discoverable flows |
| — | Lockout headroom | `getPinRetries = 8`, key agreement (protocol 2) works through the relay | Guard design validated |

Empirical PIN-requiring probes (`utils/spec_probes_pin.py`, run 2025-11-03,
PIN from untracked `.pin`, retry counter unchanged at 8 throughout):

- **P1a**: protocol-2 token + full 32-byte `pinUvAuthParam` → makeCredential
  **UV=true** and getAssertion **UV=true** (through the relay).
- **P1b**: 16-byte truncated param under protocol 2 was *accepted* (firmware
  leniency — likely first-16-byte comparison; the CTAP2.0-era protocol-2
  definition used 16 B). No design impact: injector sends the canonical
  32 B.
- **P1c**: protocol-1 token + 16-byte param → UV=true. Both protocols viable;
  daemon standardizes on protocol 2.
- **P5f**: persona-shaped request (`options {uv:true}` + valid injected
  param) → `UNSUPPORTED_OPTION` even with a valid param → **stripping `uv`
  is mandatory**, as §5.3 already requires.

**Phase-0 is complete. Every §10 item is resolved; the design in §2–§5 is
validated end-to-end at the CTAP2 level: a client that (a) believes the key
has internal UV and sends `options {uv:true}` with no token, (b) has the
`uv` option stripped and a real 32-byte `pinUvAuthParam` injected, gets a
genuine UV=true assertion from the key through the relay.**

## 11. Rollout & rollback

- All new behavior behind `AUTOUV = False` default in `code.py`.
- Daemon is additive (`utils/`), no existing file's raw-relay logic changes
  when the flag is off.
- Rollback = flash previous `code.py` (git revert); the YubiKey itself is
  never modified (no reset, no PIN change, no new credentials).

## 12. Out of scope

- `hmac-secret` / PRF extension flows requiring tokens (rare; fail-open).
- Windows/macOS Chrome personas.
- Storing the PIN on the Pico (rejected — see §3/§6).
- Auto-typing into Chrome's dialog on the host (rejected — Wayland-
  fragile; the persona makes it unnecessary).

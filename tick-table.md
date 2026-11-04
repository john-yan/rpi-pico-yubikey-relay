# tick-table.md — Design plan: declarative tick table for the relay loop

**Status: IMPLEMENTED (2026-11-05, both migration steps).** This upgrades the hand-rolled cyclic
executive in `src/code.py` (`run_session`) into a declarative, fixed-priority
tick table. It formalizes what the loop already does — it does **not**
introduce asyncio, threads, preemption, or cadence scheduling (see
"Rejected alternatives").

Companion docs: `plan.md` (Auto-UV feature design), README (architecture),
the state-machine inventory (link / read-window / auto-touch / loss-recovery
in `code.py`; broker + assembler in `src/lib/`).

## 1. Motivation

The session loop is a fixed-order sequence of concerns whose priority
structure exists only as step-number comments (`# 0) token service...`).
Costs of that implicitness:

- **Ordering is load-bearing but invisible.** The dispatch order encodes
  real-time constraints (8 ms bInterval drain deadline, broker timeouts,
  read-window semantics). Nothing in the code *says* "this order is a
  contract"; a future edit can silently reprioritize the loop.
- **The deferred review finding (broker-drain starvation)** is precisely a
  priority question; with an explicit table it becomes a one-line,
  reviewable decision instead of archaeology in a 200-line loop body.
- **Fault attribution** currently bottoms out at funnel-level tags
  (`read`, `write_down`, ...); a fault in a tick's own body (not in a
  funnel) reports a stale tag.
- New periodic concerns (status reporting, richer LED patterns) currently
  require editing the loop body in the "right" place.

## 2. Goals / non-goals

**Goals**

1. One declarative table, session-scoped, that names every periodic task
   and fixes their dispatch order (= priority).
2. The loop body becomes trivial; each task is a named function.
3. Zero behavior change: same order, same timing, same fault semantics.
4. Zero per-iteration heap allocation (no task objects, no closures
   created in the loop).
5. Tick-granularity fault attribution for free.

**Non-goals (deliberate)**

- No concurrency of any kind (single thread, cooperative, no `asyncio`,
  no `_thread` — argued in the 2026-11-04 design discussion; the known
  dropped-INIT race and the GC/latency budget both rule it out).
- No cadence fields (`every_n`, deadlines). All tasks run every
  iteration today; a cadence extension can be added later without
  changing the table shape (append a field, default 1).
- No splitting of the downstream packet pipeline into "tasks" — it stays
  ONE task (see §4, invariant I3).
- No dynamic task add/remove at runtime.

## 3. Current loop → task inventory

Mapping of today's `run_session` inner loop (in dispatch order):

| # | Tick | Current code | Blocking? | Why this position |
|---|------|--------------|-----------|-------------------|
| 0 | `broker` | `token_svc.poll()` | no | CDC control plane: broker FSM deadlines (HELLO/INIT/BROKER timeouts) and daemon commands; cheap when idle, so running it first minimizes daemon-visible jitter |
| 1 | `upstream` | `if get_report is not None: while ...` drain of PC OUT reports | no (drains what's buffered) | **Hard deadline**: `usb_hid` buffers only the LATEST OUT report (bInterval 8 ms); during a multi-packet request burst the loop must get back here before the host's next report or packets are lost |
| 2 | `downstream` | `if ctap_in is not None:` `ctap_read(timeout=read_ms)` + the whole per-packet dispatch | yes, 2–50 ms (read window) | The only variable-timeout read; runs after the drain so a full request burst is already forwarded downstream. Window state (`read_ms`) is written by ticks 1 and 2 |
| 3 | `keyboard` | `if kbd_ep is not None: dev.read(kbd_ep, ..., timeout=10)` | yes, 10 ms (keyboard mode only) | Only active in `boot.py MODE="keyboard"`, where the CTAPHID tick is inert — the modes are mutually exclusive, so the blocking read is acceptable |
| 4 | `touch` | `touch.poll()` | no | 1 s-scale deadlines (AUTO_TOUCH_DELAY/HOLD); per-iteration is far finer than needed |
| 5 | `led` | `led.value = ...` | no | Cosmetic; wall-clock blink |

**Cross-tick coupling that a naive scheduler would break (the reason the
pipeline stays monolithic):**

- The broker task is split-brain by nature: its *control plane* is tick 0
  (CDC line drain, timeouts), but its *data plane* lives inside tick 2 —
  broker response packets arrive on the downstream read and are consumed
  by `token_svc.consume()` before orphan detection sees them.
- The read-window state machine (`win_drain()` / `win_expect()`) is
  written by ticks 1 and 2 and read by tick 2's `ctap_read` timeout.
- Touch observation (`touch.arm()` / `touch.answer()`) happens inside
  tick 2's dispatch and must see relay-path packets even when the proxy
  claims them (the phase-2 regression).

## 4. Design

### 4.1 Task contract

A tick is a session-scoped closure, signature `def tick_x():`, taking no
arguments and returning nothing. Faults propagate: a tick does NOT catch
its exceptions — `usb.core.USBError` / `OSError` escape the dispatch loop,
through `run_session`, to the link lifecycle's `recover_from_fault`
(unchanged semantics).

### 4.2 The table and dispatch loop

Built once per session (after the funnels/window/recovery helpers exist),
iterated every loop pass:

```python
        # Fixed-priority tick table: dispatch order IS priority. The
        # ordering invariants are I1-I5 in tick-table.md - reordering
        # is a behavior change and needs the full verification matrix.
        TICKS = (
            ("broker",     tick_broker),
            ("upstream",   tick_upstream),
            ("downstream", tick_downstream),
            ("keyboard",   tick_keyboard),
            ("touch",      tick_touch),
            ("led",        tick_led),
        )

        while True:
            for name, tick in TICKS:
                last_op[0] = name   # tick-granularity fault attribution
                tick()
```

`last_op[0] = name` is overwritten by the finer funnel-level tags
(`read`, `write_down`, `send_up`, ...) as soon as a tick enters a funnel,
so fault reports keep today's granularity and gain tick names for faults
outside funnels.

### 4.3 Ordering invariants (the contract the table encodes)

- **I1** — `broker` before `upstream`/`downstream`: control-plane
  deadlines are soft but cheap; no hard constraint, kept first as-is.
- **I2** — `upstream` strictly before `downstream`: the 8 ms drain
  deadline must not queue behind a 50 ms response window.
- **I3** — the entire downstream per-packet dispatch stays inside the
  `downstream` tick: one packet, one pass, ordered stages (broker
  consume → swallow check → orphan detection → assembler → verbatim →
  touch observation). These are pipeline stages, not tasks.
- **I4** — `touch`/`led` last: no deadline anywhere near tick scale.
- **I5** — `keyboard` may only block because it is mutually exclusive
  with `downstream` (boot.py mode); if the modes are ever unified, its
  10 ms blocking read must become window-driven first.

### 4.4 Shared state and mutation policy

Session state shared across ticks (`read_ms`, `recover`, `pending_conts`,
`resp_pending`, `read_errors`) stays in `run_session` scope; tick bodies
mutate it via `nonlocal` — the pattern already established by
`win_drain()` / `win_expect()` / `recover_lost_response()`. Objects
(`touch`, `req_asm`, `resp_asm`, `token_svc`, buffers) are mutated in
place, no `nonlocal` needed.

*A `Session` state object was considered and rejected for this pass:
bigger diff, no behavioral gain, and the `nonlocal` transition functions
already concentrate the mutations (see §7).*

### 4.5 Allocation and timing budget

- The table is a tuple of pre-bound closures built once per session;
  the dispatch loop allocates nothing (tuple iteration is
  index-based in MicroPython, no iterator objects on the hot path —
  verify with a heap-size probe during validation).
- Added per-iteration cost: 6 function calls + 6 `last_op` writes ≈
  0.1–0.3 ms on the RP2040 — negligible against the 2 ms drain window
  and the 8 ms bInterval budget.

## 5. Target code shape (sketch)

```python
    def run_session(dev, kbd_ep, ctap_in, ctap_out):
        """LINK_RELAY state body (see link lifecycle map)."""
        ...  # funnels, assemblers, window/recovery transitions,
             # session counters — unchanged from today ...

        def tick_broker():
            # 0) daemon commands, broker timeouts, heartbeats
            token_svc.poll()

        def tick_upstream():
            # 1) drain ALL pending CTAPHID OUT reports (I2 deadline)
            if get_report is not None:
                ... today's step-1 body verbatim ...

        def tick_downstream():
            # 2) downstream read + the monolithic packet pipeline (I3)
            nonlocal read_errors, recover, resp_pending
            if ctap_in is not None:
                ... today's step-2 body verbatim ...

        def tick_keyboard():
            # 3) OTP keyboard reports (keyboard mode only, I5)
            if kbd_ep is not None:
                ... today's step-3 body verbatim ...

        def tick_touch():
            # 4) auto-touch actuation (TouchFSM)
            touch.poll()

        def tick_led():
            # 5) status LED (wall-clock blink)
            led.value = (not touch.pending) or bool(int(time.monotonic() * 8) & 1)

        TICKS = (...)   # §4.2
        while True:
            for name, tick in TICKS:
                last_op[0] = name
                tick()
```

## 6. Migration plan (two steps, each independently verified)

**Step 1 — extraction (no table yet).** Move each step body into its
`tick_*` function; the loop calls them in the same order inline:

```python
        while True:
            tick_broker(); tick_upstream(); tick_downstream()
            tick_keyboard(); tick_touch(); tick_led()
```

Pure function extraction; the diff is indentation + `nonlocal`
declarations. Full hardware matrix (below) before proceeding.

**Step 2 — the table.** Introduce `TICKS` + the dispatch loop +
`last_op[0] = name`. Diff is ~15 lines. Full hardware matrix again.

**Verification matrix (after each step):**

| Check | Covers |
|---|---|
| `make unit` | lib modules untouched (sanity) |
| `make test` | INIT + multi-packet PING through the new loop |
| `make e2e` | deploy/reload/verify pipeline + console capture |
| daemon `--once` | broker control plane (tick 0) + token acquisition |
| `spec_probes_uv.py` | touch FSM end-to-end, injector, response windows |
| luci reauth ×5 | multi-packet ceremonies, back-to-back sessions, drain priority |
| `make deploy-debug` + `make test` | tracing through tick closures |
| heap probe (REPL `gc.mem_free()` before/after a ceremony) | no hot-path allocation regression |
| manual: unplug YubiKey mid-ceremony | fault propagation to `recover_from_fault` (tick exceptions must tear the session down, exactly as today) |

Effort estimate: step 1 ≈ 1–2 h careful edits, step 2 ≈ 30 min,
verification ≈ 30 min per step.

## 7. Risks & mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| `nonlocal` mistakes in extracted ticks (stale local shadows session var) | high impact, compile-visible | `py_compile` catches unassigned-nonlocal only partially; grep audit that every cross-tick mutated name appears in exactly one `nonlocal` statement; the window/recovery mutations already go through the existing transition functions |
| Accidental reorder during extraction | high | Step 1 keeps literal call order in one line; step 2 table is copied from it; diff review |
| Timing regression in the burst path | medium | luci ×5 + the heap/timing probes in the matrix; the known 5–10 % dropped-INIT bug makes any timing change suspect — if its rate changes, treat as a regression signal |
| Session-scope churn (closures rebuilt per attach) | negligible | unchanged from today |

## 8. Rejected alternatives

- **asyncio / threads:** cooperative tasks can't preempt the 250 ms HMAC
  anyway; `ctap_read` is a blocking C call so the hot read would degrade
  to `timeout=0` polling, worsening the exact read-abort race that is
  the open bug; heap churn in a deliberately allocation-free loop.
- **`lib/sched.py` (host-testable scheduler module):** the dispatch loop
  is 4 lines; extracting it adds an import + indirection for no test
  value — the tasks themselves are board-only regardless.
- **`Session` state object:** cleaner than `nonlocal`, but a bigger diff
  in the most fragile code for zero behavioral gain; revisit only if a
  third or fourth cross-tick mutable appears.
- **Cadence/deadline fields:** YAGNI; every task is every-iteration and
  the two slow tasks (`touch`, `led`) are sub-microsecond when idle.

## 9. Rollback

Both steps land as one commit each (or one combined commit); rollback is
`git revert`. No data, protocol, or deployment-shape changes; the board
just gets the previous `code.py`.

## 10. Decisions (resolved 2026-11-05)

1. **`touch` and `led` stay separate ticks.** Both are trivial and both
   run last; merging them into a `ui` tick saves nothing measurable, and
   separate names preserve tick-granularity fault attribution.
2. **`tick_keyboard` keeps its 10 ms blocking read.** It is acceptable
   precisely because keyboard and CTAPHID modes are mutually exclusive
   (I5). It becomes window-driven only if the modes are ever unified —
   and that unification must fix the blocking read as a prerequisite,
   not a follow-up.
3. **The table is session-scoped.** Built once per attach epoch in
   `run_session`, closing over session state directly. A module-scope
   table would need a context parameter threading the same state —
   indirection with no gain. Revisit only together with the `Session`
   state-object question (§7), which stays rejected for now.

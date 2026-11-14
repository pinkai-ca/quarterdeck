# Quarterdeck design: Tests

A page of [the design](../DESIGN.md).

## 12. Tests

The suite separates pure logic, native-format contracts, synthetic integration and opt-in live proof. Recorded fixtures establish what an adapter can read; a live check establishes that the installed program can complete its public path. Neither substitutes for the other. The [test tool](working.md#running-tests) owns bounded execution, source isolation, result evidence and cleanup.

### Layers

| Layer | Scope |
| --- | --- |
| Unit | Ship validation/editing, immutable messages, inboxes, attribution, accounting arithmetic and rendered operational facts. |
| Contract | Adapter records, screens, dialogs, controls and folding, with fixtures for each proven version. Every known interruption has its captured form and each control has its confirming state. |
| Integration | Real private tmux with Claude/Codex-shaped fake programs, ordinary services and recorded delivery/lifecycle outcomes. Other programs rely on their native contracts and installed checks. |
| Live drill | Explicitly requested real-program throwaway seats, delivery matrix and verifiable filesystem/command/post tasks. It spends tokens; it is never implied by a synthetic pass. |

Repository checks also guard runtime-content exclusion and ignored runtimes, law carriers and existing law IDs. File length is guidance: split by concept, without compressing code to satisfy a count.

### Drill evidence

The synthetic drill sends every seat to every other, crashes/recovers the service mid-delivery, then explicitly starts fresh and repeats. Human-session traffic uses its watch and the guarded waking line. Its generated drydock leaves automatic after-order fresh unset so it cannot race the drill's own transitions.

Crash delivery requires the public fresh receipt, one delivered attempt and one native acceptance. A timed dequeue/input echo for one enqueue counts once by original acceptance time; different enqueue times/direct acceptances count separately. Stale, untimed and merely queued evidence proves no delivery. Synthetic submission counts independently detect identical same-timestamp enqueues; the live drill has no such counter.

Fresh proves an open order in the new conversation's first input. It may be carried without prior old-conversation acceptance when unattempted or its old attempt is terminal. Blind remains unconfirmed, not a receipt. Active typing/confirmation settles first under lifecycle coordination. Busy readiness may retry before starting; a begun fresh never repeats. Output distinguishes delivery-first from carriage, and prints failures before passes to preserve them in clipped logs.

### Private tmux lifetime

Tests and fake drills allocate fresh mode-0700 namespaces outside recursively removed data trees. Every command names its own `-S` endpoint. Existing TMUX_TMPDIR supplies a transport root; otherwise standalone allocation uses project `work/`. Suite sessions use the verified ignored short project `.testruns/s` anchor, or the tool's per-run `.testruns/r`. Socket budgeting includes native nested/replacement names, filesystem bytes and a twelve-byte margin below the 107-byte limit; irreducibly long roots refuse.

The creator writes the mode-0600 regular namespace lock/header. Bound observers open only that validated inode without following links or blocking on special files, use `-N` to prevent native server creation and obtain no cleanup rights from a socket variable. Known query/buffer/key queues share one nonblocking lease; lifecycle/options/mixed/unknown queues require exclusive dispatch. Unknown generic commands refuse. Exclusive waiters mark the namespace folder with flock so new shared readers cannot starve them. The exclusive acquisition budget is two seconds with no retry; close uses at most two plus its bounded shutdown observation.

Private client commands have a ten-second answer bound. An unanswered server raises uncertainty, ends only the caller's own client and retains server/socket ownership. It never proves a missing pane or dead server. Creator close can succeed later when it responds. Waiters revalidate directory/lock identity after acquisition.

Creators retain separate directory, server birth nonce/pid/start and socket-descriptor evidence. Cleanup verifies the same server in-server, then needs ended-process and refused-connection evidence before unlinking that same endpoint under lock. Gone/ESRCH or Z/X/x processes count ended; other read errors hold. Only the exact owned lock and empty containers are removed, never recursive namespace sweeps. Unknown peers/replacements remain with reasons. Same-UID mutation that ignores the protocol is outside its POSIX guarantee.

Creator servers use exit-empty off to survive gaps between sessions. Only the creator ends them; a native exit-empty shutdown still needs proved-dead endpoint cleanup before restart. Uncatchable death can leave a held namespace, never authority to adopt it. `--keep` preserves server/data, prints its quoted endpoint/session command and restores caller environment. Bound observers cannot reclaim it. A held shutdown retains drill data too.

Hosted CI creates a mode-0700 short `$RUNNER_TEMP/qs.XXXXXXXX` only for pytest. Descriptor/identity checks and empty-only removal preserve peers and original nonzero test status; cleanup failure after success fails the step. It does not change standalone drill lifetime.

### Installed checks

`qd check PROGRAM` proves selected executable launch, door readiness, a deliberately long folding paste and exact receipt, task/file/report, record join and ended turn. Optional model selection requires native identity confirmation. The ordinary proven marker records that bounded public proof, not every adapter feature. [Harnesses](harnesses.md#versions-checks-and-controls) describes version and model evidence.

`qd check PROGRAM --minimal` instead selects one short write-line-and-report task through the same door, receipt and ended-record checks. It omits the folding stress payload and separate human wake probe. Output names minimal mode; outcomes live under `runtime/minimal-checks/`, without writing the ordinary full-check proven marker. Scheduling is a separate operation.

Short-pane geometry, retained unread-paste screens and other runtime delivery diagnostics belong to the [door](door.md), rather than test acceptance claims. Native behavior outside measured fixtures stays unmeasured even when invented geometry frames exercise a diagnostic.

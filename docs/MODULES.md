# Modules

The modules that grew beside the parts, in `src/quarterdeck/` and in its `harness/` folder, each with what it holds. The module of each part itself, such as `ship.py`, `seat.py`, `door.py`, `post.py`, `watch.py` and `ledger.py`, and the adapter of each harness are named in [section 13 of the design](#13-repository-layout), which lays out the whole repository. [CONTRIBUTING.md](../CONTRIBUTING.md) says how to change them.

## The framework

| Module | What it holds |
| --- | --- |
| `check.py` | `qd check`, the live proof of an installed program |
| `compaction_alert.py` | an idle seat that is ready for a fresh start by hand: one report to the systems seats for each such reading, and a named alarm while no systems seat is up |
| `cost_evidence.py` | a turn's cost evidence, kept beside the Ledger's turns |
| `credential.py` | the one link a seat shares with its pool, reseeded and relinked on the tick |
| `doctor.py` | `qd doctor`, the installation checked and handed to a seat to fix |
| `endpoint.py` | endpoint pools: what a server serves, the model a seat takes from it, the key beside it |
| `fakeharness.py` | the entry point installed as `qd-fakeharness`, for the simulator under `harness/` |
| `headlines.py` | operational status-line formatting; provider allowances belong in detailed readings |
| `input_receipt.py` | the one rule that matches a delivery to the record's evidence of its input |
| `install.py` | `qd sync`: the framework at one commit installed as the ship's own copy, which every `qd` then hands off to |
| `ledger_reading.py` | the Ledger's rows and usage windows as a reading takes them, and their tables |
| `machine.py` | a seat's process tree, sampled for the Ledger |
| `output.py` | one shape for every reading, rendered for a person or serialized as JSON |
| `paste_refusal.py` | proved owned-unsubmitted paste cleanup, and refusal evidence scoped by payload, version and geometry |
| `pending.py`, `delivery_record.py` | durable paste evidence and historical receipts; the service schedules each message |
| `pricing.py` | a pool's stated rates, and the price saved beside a launch or a model choice |
| `provenance.py` | which launch a record belongs to |
| `providers.py` | normalizes declared companies and builds public identity metadata |
| `quota.py` | quota walls and the usage refresh |
| `record_owner.py` | which program a record belongs to |
| `refusal.py` | a provider-refused crew seat started fresh once, the refused message withheld and its sender told |
| `sandbox.py` | the bubblewrap jail around a sandboxed seat's program |
| `servicectl.py` | the service's heartbeat, lock, start, stop and requests |
| `tmux.py` | the only module that runs tmux |
| `turn_failure.py` | a turn that ended in a recorded error: the door's hold, its alarm, the receipts it unsettles and a human seat's operational metadata |
| `usage_cache.py` | the latest real reading of each usage window, and its last attempt |

## Under `harness/`

| Module | What it holds |
| --- | --- |
| `antigravity_quota.py` | the Antigravity record's quota facts |
| `antigravity_record.py` | the Antigravity record decoded from its snapshot |
| `antigravity_wire.py` | the Antigravity record's wire format |
| `capabilities.py` | what each adapter declares of its harness's efforts and defaults, the checks of a seat's `model` and `effort` against that, and the words for a program's own default |
| `costs.py` | an endpoint program's step costs: what its record states, what stated rates prove, and what stays unpriced |
| `failures.py` | a recorded error's bounded first words |
| `home.py` | building a seat's home: a pool's credential brought into it, a key pool's environment, and the ship's settings layered over a program's own |
| `json_record.py` | JSON-lines records |
| `limits.py` | usage windows and walls |
| `opencode_record.py` | the OpenCode database read session by session: the latest root session is the seat's, and a task's child sessions are accounted on their own |
| `simulator.py` | the fake program the drill runs |
| `sqlite_snapshot.py` | a SQLite record read from a snapshot of its bytes and its log, shared by the Antigravity and OpenCode readers |
| `synthetic_control.py` | the rendezvous a test holds the fake program at |
| `synthetic_record.py` | the fake program's records, in Claude Code's or Codex's shape |
| `types.py` | the values the adapters and the framework pass between them: inputs, tokens, turns and their recorded failures, state, keys, controls, screens, boxes, credentials, launches |

## 13. Repository layout

```
quarterdeck/                       the project folder: the framework repository
  LICENSE  README.md  pyproject.toml  .gitignore (ships/*, !ships/drydock/, !ships/default/, !ships/forum/, !ships/cascade/, !ships/shuttle/, work/, .testruns/)
  AGENTS.md       for an agent that arrives to set Quarterdeck up or to use it; CLAUDE.md is one line that imports it
  CONTRIBUTING.md for whoever works on Quarterdeck itself, person or agent
  src/quarterdeck/
    cli.py         the qd command
    setup.py       office initialization and guided crew creation/reruns, with replaceable machine and live-operation seams
    raise_ship.py  exact template proposals, person-shell approval records in the office runtime, and crew creation/launch
    setup_answers.py   one flags/file/terminal answer model, edits and explicit acceptance
    setup_proposal.py  read-only setup proposals and their shared text/JSON summary
    ship.py        ship.toml -> Ship: crew/office kind, sessions, seats, roles, pools, optional groups and cascade; the line editor; the fence
    providers.py   declared companies, input normalization and public identities
    program_use.py   record-first current program activity and launch-bound historical last use
    check_outcome.py   bounded safe check outcomes, atomic publication and shared operational alarms
    tmux.py        the only module that runs tmux
    tmux_socket.py   private endpoint ownership and namespace locking; no native command execution
    seat.py        seat identity, lifecycle and controls: up, down, restart, fresh, stop, set
    lifecycle.py   shared seat-transition claim for lifecycle and ordinary delivery
    door.py        the one typing path: clear, type, check, submit, confirm; three speeds; the wall it reads
    pending.py delivery_record.py   durable paste evidence and historical receipts
    pending_lifecycle.py   service-only null-identity retirement and exact old-launch record links
    pane_obstruction.py   one bounded short-pane reason marker and its named journal, with writer-owned expiry
    receipt_refresh.py   service-owned framework-revision receipt-refresh progress
    human_wake.py  one fixed human-seat wake per watch episode, using Door and the existing Proof/receipt
    recovery.py    one reserved native continuation and unresolved-mode lifecycle hold, using the existing Door/Proof
    compaction.py  the threshold, handoff, readiness and transition journal for fresh starts after compaction
    compaction_alert.py   once-per-reading reports to systems for idle manual-fresh readiness, and named unrouted alarms
    idle.py        the ordinary live idle proof shared by relaunch, failover and selection
    move_hold.py   service-written operational move facts bound to launch and watch generation
    inbox_watch.py   current own-watch registration and live generation proof for the seat-aware idle exception
    person_quiet.py    a human seat's person quiet time from its record, and its one wake per readiness
    input_receipt.py   the one rule that matches a delivery to the record's evidence of its input
    paste_refusal.py   owned unsubmitted-paste cleanup and scoped negative evidence
    turn_failure.py    a turn ended in a recorded error: the door's hold, its alarm, and the receipts it unsettles
    refusal.py     a provider-refused crew seat started fresh once, the refused message withheld and its sender told
    service.py     the ship's one long-running process: inboxes, deliveries, attempts, deferred controls, the heartbeat,
                   the machine sample; the one writer of the Ledger
    servicectl.py  the service's lock, heartbeat, start, stop and requests
    request_queue.py   one queued-request owner: visible holds, terminal cancellation/supersession and writer coordination
    shutdown.py    service-local SIGTERM deferral, runtime owner/boundary facts and bounded stop annotation
    control_confirmation.py   pure value choice and bounded consumed-control continuation in the one request owner
    model_mismatch.py   record-first canonical model differences, fixed systems report intents and named fallback alarms
    presentation.py   disposable status text rendering and freshness checks; the console bootstrap's optional fast path,
                   never lifecycle, receipt or usage evidence
    reader_stamp.py   explicit per-harness reader dependencies and attribution-engine files, hashed from their bytes for accounting certificates
    harness/       base.py types.py home.py claude.py codex.py antigravity.py opencode.py pi.py grok.py fake.py; json_record.py limits.py sqlite_snapshot.py costs.py
                   grok_record.py grok_screen.py grok_usage.py grok_workspace.py grok_notice.py   native ACP updates, fullscreen composer, program-owned billing, exact native workspace trust and information-only project inventory
                   opencode_record.py antigravity_record.py antigravity_wire.py antigravity_quota.py; simulator.py synthetic_record.py synthetic_control.py,
                   the fake program the drill runs, its records in Claude Code's or Codex's shape, and the rendezvous a test holds it at
                   each fake launch acknowledges its own startup token after its first prompt; only a live synthetic
                   launch with that acknowledgment can finish the survival observation early; real programs retain it
    harness/capabilities.py   immutable effort/default metadata, pure declaration admission, override conflicts and shared default labels
    harness/codex_usage.py   Codex usage windows from the newest eligible snapshot in current launch records
    harness/claude_statusline.py   packaged default Claude footer: session identity and generic ledger allowance bars, runtime-only background cache
    harness/claude_limits.py  native Claude model allowances, family scope, reset-period inference and safe shape diagnostics
    harness/antigravity_models.py  explicit Google model/group relations and native identity equalities; unmeasured facts remain unknown
    harness/claude_watch.py  structured watch-output consumption for attribution, never delivery receipts
    harness/claude_background.py   measured root Bash background-task evidence, separate from turn accounting
    harness/compactions.py   the complete native compaction count and work snapshot from the current owned record
    harness/closed_record.py   read-only payload absence in an exact closed launch's owned record, using its saved reader and input diagnostics
    harness/check_evidence.py   bounded private check-only tool call/results from an owned pinned record; no delivery authority
    harness/thinking.py opencode_thinking.py pi_thinking.py   native recovery source evidence and proved off/restore controls;
                   program configuration, request/body and selection knowledge stays in adapters
    endpoint.py    endpoint pools: what a server serves, the model a seat takes from it, the key beside it
    sandbox.py     the bubblewrap jail around a sandboxed seat's program
    post.py        messages: store, inbox, the typed form, the watch
    structure_outcome.py shared ended-turn association, in-memory service-pass grace and read-only derived waits
    group_post.py  declared fanout, fixed intents and member outcomes, current dead/held-limit observation, terminal notices and derived group holds
    thread_context.py   open-root amendment selection, context-only own questions and exact whole-input consumed-bundle matching
    post_threads.py     incoming/open/sent-awaiting thread indexes and read-only qd thread rendering
    carriage.py         canonical carriage metadata, private carried-folder ownership and final exec byte limits
    carriage_plan.py    bounded first-message/index pages, full self-handoff files and atomic publication
    cascade.py          declared three-seat ring, runtime-only per-question journal and notes, immutable service relay intents and final notice
    cascade_runtime.py  checked runtime file observations, once-only notes appends and derived cascade holds
    watch.py       reading a seat: activity and door; the status line
    output.py      one shape for every reading: rendered for a person, or serialized as JSON for a caller
    ledger.py      turns, tokens, tool calls, machine load, by pool, seat and order; opened read-only by every reading
    accounting_health.py   bounded accounting-failure streaks and process-bound disposable operational status evidence
    ledger_reading.py   the Ledger's rows and usage windows as a reading takes them, and their tables
    order_attribution.py   explicit reply threads and timestamped whole-turn attribution rules
    attribution_cache.py   service replay and compatible side verdicts, invalidated by older turn writes
    pricing.py cost_evidence.py   a pool's stated rates and the price saved beside a launch; a turn's cost evidence,
                   kept beside the Ledger's turns
    provenance.py record_owner.py quota.py usage_cache.py   which launch and which program a record belongs to; quota walls,
                   the usage refresh, and the latest real reading of each usage window
    headlines.py   operational status-line text, with category counts when alert names exceed the budget
    credential.py  the one link (or kept copy) a seat shares with its pool, reseeded and relinked on the tick
    auth_failure.py recorded sign-in failure readings and bounded resumes after renewed pool files
    machine.py     a seat's process tree, sampled for the Ledger
    standing.py    standing orders: laws, the ship's orders, cards, the brief
    drill.py       the all-hands drill
    check.py       qd check: an installed program proven live on a throwaway seat
    doctor.py      qd doctor: the installation checked, and the failures handed to a seat
    install.py     sync: live runtime/venv, one cp -a backup, proof, journaled recovery and back
    copy_state.py  standard-library-only declaration mode and read-only copy/proof selection
    resources.py   target resource declaration, bounded historical copy-list reader and explicit share synchronization
    fakeharness.py the entry point installed as qd-fakeharness, for the simulator under harness/
  standing-orders/ laws.md  roles/  harnesses/        the laws, and the generic cards a ship starts from
  ships/
    drydock/       ship.toml  orders.md    three fake seats, one in each kind of session: the tests and the drill run here
    default/       ship.toml  orders.md    the smallest crew on real programs, a desk, a captain and an engineer; copy it for a ship of your own
    forum/         ship.toml  orders.md  roles/lead.md  roles/reviewer.md; one lead and four independent reviewers
    cascade/       ship.toml orders.md roles/ README.md    three providers in a service-relayed ring and one orchestrator; runtime notes only
    shuttle/       ship.toml orders.md roles/ README.md mission-record.md    captain, engineer and first officer; reviewed and signed missions
    office/        ship.toml orders.md roles/desk.md README.md    template for the separate project-top office
    <yours>/       a nested repository, ignored above: ship.toml  orders.md  roles/  harnesses/  homes/  runtime/
    */runtime/     a nested repository with no commits: the fence around a ship's state
  work/            worktrees for work on the framework itself, ignored; the checkout above is what a running ship uses
  .testruns/       ignored: the test tool's registrations, run scratch, results and reports, and the native test sockets
  tests/           unit/ contract/ fixtures/ integration/; testrun.py and testrunner/, the test tool (standard library only)
    shared_install.py  worker-local session wheel/install fixtures; immutable seeds and writable relocated copies

                   CI layout: testrunner/run.py gitops.py environment.py scratch.py clean.py;
                   evidence_plugin.py evidence.py report.py measure session layout and outcomes;
                   sessions.py servers.py accept the trusted CI plugin's explicit run context;
                   usersite.py carries the resolved tool base through CI nested runs
    unit/test_ci_lane_options.py  ordinary CI/fast/whole command and declared disk-policy cases
    integration/ci_layout_ordinary_probe.py  explicitly selected ordinary real-mode Git/temp/environment observations
    integration/ci_layout_probe.py test_testrun_ci_layout_gates.py test_testrun_ci_layout_corrections.py
    integration/test_testrun_tests_origin.py; unit/test_testrun_ci_layout.py test_testrun_ci_layout_gates.py
                   supplied frozen CI verification modules; source/test bytes tracked separately
    unit/test_fast_lane_selection.py  ordinary pure lane, worker-default, first-line and report checks
    unit/test_fast_lane_slots.py  ordinary private-process atomic slot acquisition and return checks
    authentication_shapes.py   invented Claude authentication-error envelopes, using measured structural keys only
    integration/waiting.py   an attached terminal client retires its verified owned command group and wrapper,
                   bounded by explicit patience, without waiting for other clients; attach failures keep the whole output
    unit/test_attached_client_retirement.py   real-process cases for prompt retirement, independent clients and owned descendants
    unit/test_structure_outcome.py integration/test_structure_outcome.py   ordinary synthetic ended-turn/grace/recovery association and private fake pi service passes
    unit/test_shuttle_template.py integration/test_shuttle.py   template/resource roundtrip and scripted fake repository missions; no native review-quality claim
  docs/DESIGN.md  docs/design/  docs/ANTIGRAVITY.md
```

Python 3.12 or later. No runtime dependencies beyond the standard library, none optional either. `pytest` for tests, run by the repository's own test tool `tests/testrun.py` ([section 15](design/working.md#15-working-on-the-framework-from-a-ship)), which the framework never imports.

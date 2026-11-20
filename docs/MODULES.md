# Modules

The modules that grew beside the parts, in `src/quarterdeck/` and in its `harness/` folder, each with what it holds. The module of each part itself, such as `ship.py`, `seat.py`, `door.py`, `post.py`, `watch.py` and `ledger.py`, and the adapter of each harness are named in the [repository layout](#repository-layout) below. [CONTRIBUTING.md](../CONTRIBUTING.md) says how to change them.

## The framework

| Module | What it holds |
| --- | --- |
| `status_profile.py` | `qd profile-status`: one full status collection of the service's own profiled at a systems or engineering seat's request, one at a time, written to `runtime/status-profiles/` as code locations (folders included), call counts, times and callers only, with the collecting thread's own CPU and a note that other threads appear, bounded in size and number |
| `status_snapshot.py` | service-produced JSON status, bounded by the reading’s age and owner |
| `check.py` | `qd check`, the live proof of an installed program; scoped report filing through the shared post door when no service is alive |
| `check_capacity.py` | a nightly check's one-pass reservation of an existing shared test slot, held through its cleanup |
| `compaction_alert.py` | an idle seat that is ready for a fresh start by hand: one report to the systems seats for each such reading, and a named alarm while no systems seat is up |
| `cost_evidence.py` | a turn's cost evidence, kept beside the Ledger's turns |
| `credential.py` | the one link a seat shares with its pool, reseeded and relinked on the tick, never from a jailed seat, a seat on an endpoint pool, or a launch naming a pool not declared for the seat. Metadata-only review holds and their audited release |
| `daily_live.py` | the daily live test: a minimal check of each program the day's readings do not show was used, and one report to the desk |
| `doctor.py` | `qd doctor`, the installation checked and handed to a seat to fix |
| `endpoint.py` | endpoint pools: what a server serves, the model a seat takes from it, the key beside it |
| `fakeharness.py` | the entry point installed as `qd-fakeharness`, for the simulator under `harness/` |
| `headlines.py` | priority and display-column budgeting for live, cached and annotated status lines |
| `input_receipt.py` | the one rule that matches a delivery to the record's evidence of its input |
| `install.py` | `qd sync`: the framework at one commit installed as the ship's own copy, which every `qd` then hands off to |
| `launch_opening.py` | a deferred opening bound to one published launch, its startup proof and one ordinary Door delivery; the service advances it without polling and held openings are never resent |
| `ledger_recovery.py` | preserves malformed accounting databases, starts fresh replay and keeps named recovery alarms until acknowledged |
| `ledger_reading.py` | the Ledger's rows and usage windows as a reading takes them, and their tables |
| `lock_holders.py` | who holds a file's exclusive flock, read from the kernel's lock table and, where the table shows no row, confirmed from the holders' own descriptors; never takes the lock. The one reader for the service's lock and the installer's |
| `machine.py` | a seat's process tree, sampled for the Ledger |
| `output.py` | one shape for every reading, rendered for a person or serialized as JSON |
| `closed_pipe.py` | a completed command keeps its own status when a standard output pipe closes; only the broken stream is retired |
| `paste_refusal.py` | proved owned-unsubmitted paste cleanup, and refusal evidence scoped by payload, version and geometry |
| `pending.py`, `delivery_record.py` | saved paste evidence and historical receipts; the service schedules each message |
| `post_notices.py` | derived settlement of exact informational notices; never a read receipt |
| `post_view.py` | a jailed seat's view of the post: copies of what it sent or received, a refused message only as its notice, its own inbox, the shorts taken; kept by the service, bound in the post's place |
| `pr_screen.py` | a pull request from outside screened by a program before any seat reads it: fetched under made-up names, searched, read by its hand (or two hands, one on each model) with a planted check whose posts are held, judged by the answer's shape, and the desk or the First Officer told without quoting it |
| `pr_screen_patterns.py` | the screen's one list of patterns: known forms of an instruction to a reader and of a harmful command |
| `pricing.py` | a pool's stated rates, and the price saved beside a launch or a model choice |
| `hand_door.py` | a hand's one order at a time at the door: its order in hand, read as its fresh start reads it; what does not belong to it waits, unread and named on the status line, for the report and the fresh start that carries the next |
| `hand_transcripts.py` | accounted, closed hand transcripts retained outside the seat's write allowance |
| `provenance.py` | which launch a record belongs to |
| `providers.py` | normalizes declared companies and builds public identity metadata |
| `quota.py` | quota walls and the usage refresh |
| `record_location.py` | logical record names that survive retirement without rewriting their ledger keys |
| `record_follow.py` | the status line's failure scans read each seat's JSON-lines record from where they last stopped |
| `record_owner.py` | which program a record belongs to |
| `refusal.py` | a provider-refused crew seat started fresh once, the refused message withheld and its sender, a seat of this ship or of the fleet, told by a notice built from the store, never from the journal's words |
| `sandbox.py` | the bubblewrap jail around a sandboxed seat's program |
| `servicectl.py` | the service's heartbeat, lock, start (in a systemd scope of its own, from a declared environment), stop and requests |
| `service_processes.py` | re-read a service's private process scope after its owner has released the lock |
| `ship_locks.py` | the ship's lock files, all under runtime/locks where every jail hides them, and the one bounded wait for each |
| `tmux.py` | the only module that runs tmux |
| `trusted_cache.py` | what the service derives from a seat's trusted files, kept while those files stand as they were read: a seat's launch, and its record's owner |
| `trusted_files.py` | where the files the service writes for a seat and trusts are kept: `runtime/trusted/<seat>`, which no jail can see, and their names |
| `turn_failure.py` | a turn that ended in a recorded error: the door's hold, its alarm, the receipts it unsettles and a human seat's operational metadata |
| `output_limit.py` | a seat whose last turn stopped at its output limit with an order open: its status line and its one alarm to the systems seats, cleared when a new turn starts or the order closes |
| `usage_cache.py` | the latest real reading of each usage window, and its last attempt |
| `usage_forecast.py` | bounded weekly usage history, three-hour spending rates, exhaustion forecasts and once-per-period systems notifications |
| `record_growth.py` | owned-pointer size sampling and once-per-record forecasts of an approaching read bound |

## Under `harness/`

| Module | What it holds |
| --- | --- |
| `antigravity_quota.py` | the Antigravity record's quota facts |
| `antigravity_record.py` | the Antigravity record decoded from its snapshot |
| `antigravity_wire.py` | the Antigravity record's wire format |
| `capabilities.py` | what each adapter declares of its harness's efforts and defaults, the checks of a seat's `model` and `effort` against that, and the words for a program's own default |
| `composer_frames.py` | a program's own composer frame, as its adapter's screen reader names it, so the systems seat's guard never takes it for a dialog's frame and reads the question above it |
| `costs.py` | an endpoint program's step costs: what its record states, what stated rates prove, and what stays unpriced |
| `failures.py` | a recorded error's bounded first words |
| `held_reading.py` | until when a record's unknown turn state holds only for a time, as its reader says, so that the accounting pass reads the record again once it has passed; the service's alone, in no reader's stamp set |
| `home.py` | building a seat's home: a pool's credential brought into it, a key pool's environment, and the ship's settings layered over a program's own |
| `json_record.py` | JSON-lines records |
| `remote_session.py` | which seat a commit of the ship folder came from, by the session trailer its program writes and the session that seat's home records; the service's alone, in no reader's stamp set |
| `record_file.py` | the one opener of a seat's program record: no link at its name or on the way from the seat folder, no fifo, socket or device, only this user's own regular file with one name; one read at most a bound (1 GiB for streamed Claude, 128 MiB for ordinary JSON lines, 512 MiB for SQLite with its log), refused past it; the status line's word on a record near or past its bound |
| `limits.py` | usage windows and walls |
| `muse.py` | Muse's native launch and adapter interfaces, measured at 1.4.20; [Muse contract facts](MUSE.md) |
| `muse_home.py` | Muse's native HOME/XDG layout kept to its owner alone (0700, set through descriptors), contributor-off settings seed and same-home startup proof |
| `muse_program.py` | the selected Muse installation's versioned executable, without running its updating launcher |
| `muse_record.py` | Muse's typed session records, root turn boundaries and exact accepted/materialized input links |
| `muse_screen.py` | Muse's measured styled composer and activity; unmeasured input forms stay unsupported |
| `muse_usage.py` | Muse's native allowance decoded without a provider request |
| `opencode_record.py` | the OpenCode database read session by session: the latest root session is the seat's, and a task's child sessions are accounted on their own |
| `resume_target.py` | launch-only conversation identity from an already joined record, including bounded identity reads of oversized records |
| `simulator.py` | the fake program the drill runs |
| `sqlite_snapshot.py` | a SQLite record read from a snapshot of its bytes and its log, shared by the Antigravity and OpenCode readers |
| `synthetic_control.py` | the rendezvous a test holds the fake program at |
| `synthetic_record.py` | the fake program's records, in Claude Code's or Codex's shape |
| `types.py` | the values the adapters and the framework pass between them: inputs, tokens, turns and their recorded failures, state, keys, controls, screens, boxes, credentials, launches |

## Repository layout

The project folder is the framework's repository.

| Path | What it holds |
| --- | --- |
| `LICENSE` | the MIT licence |
| `README.md` | what Quarterdeck is, its quick start and how to use it |
| `pyproject.toml` | the package: no dependencies, the `qd` and `qd-fakeharness` commands, and the files installed beside the code |
| `.gitignore` | the ignore rules: every ship folder but the templates, each ship's `runtime/`, the live office, `work/` and `.testruns/` |
| `AGENTS.md` | for an agent that arrives to set Quarterdeck up or to use it; `CLAUDE.md` is one line that imports `AGENTS.md` |
| `CONTRIBUTING.md` | for whoever works on Quarterdeck itself, person or agent |
| `src/quarterdeck/` | the framework: the modules in the tables above and below |
| `standing-orders/` | the laws in `laws.md`, and the generic cards in `roles/` and `harnesses/` that a ship starts from |
| `ships/` | the template ships, and each ship of your own |
| `work/` | ignored: worktrees for work on the framework itself |
| `.testruns/` | ignored: the test tool's runs, their results and reports; the suite's sockets are kept outside the project, in a private folder under `/tmp` named from the user's id and the project's path |
| `tests/` | the tests, and the repository's own test tool |
| `docs/` | these pages: the design in `DESIGN.md` and `design/`, and the pages beside the design, such as `ANTIGRAVITY.md` |

The framework needs Python and the Python standard library alone. `pyproject.toml` states the Python version the framework requires and declares no dependency; the `dev` extra in `pyproject.toml` holds `pytest` and `pytest-xdist` for the tests. The repository's own test tool, `tests/testrun.py`, runs the tests, and the framework never imports the tool. [Working on the framework from a ship](design/working.md#running-tests) shows the tool's commands.

### Under `src/quarterdeck/`

The modules that the framework table does not list, the module of each part among them.

| Module | What it holds |
| --- | --- |
| `accounting_health.py` | the Ledger's health as the service reports it: a named cause, such as a busy database or full storage, and disposable status evidence |
| `accounting_slices.py` | the Ledger's catch-up in slices: a low-priority child process reads the records and plans the rows, and the service commits the plan in bounded steps |
| `accounting_gate.py` | private, tick-bounded input evidence lets a completed accounting job wait for a change or a due hold, with a ten-minute backstop and bounded class/count start diagnostics |
| `accounting_scope.py` | per-input dirty observations and proven record owners select whole-seat jobs below unchanged global barriers, without postponing the full backstop |
| `acts.py` | `qd press`, `qd type` and `qd set-aside`: a systems seat's answer to what the door cannot settle (a screen it does not know, or one named delivery journal that is provably stuck, moved unchanged into the runtime's evidence), through the door's own path, recorded before anything is sent or moved, and never into a session the human owns |
| `alarms.py` | the alarms' saved readings, their reports to the systems seats, or to the desk where no systems seat is up or an alarm stays untaken, each rebuilt by the service from checked fields, and the reply that takes an alarm |
| `attribution_cache.py` | the attribution the service writes beside the turn rows, which any turn write invalidates |
| `auth_failure.py` | recorded sign-in failures: the next delivery first, then bounded resumes once the pool's sign-in is renewed |
| `carriage.py` | what a launch carries, and the byte checks on the final command |
| `carriage_plan.py` | the thread index pages, bounded by `carry_bytes`, and a fresh seat's whole handoff to itself |
| `cascade.py` | a ring of seats that the service relays between, with one run and one notes file per question |
| `cascade_runtime.py` | a cascade's runtime files, read and appended with checks, and the holds derived from those files |
| `check_outcome.py` | a check's bounded outcome, kept beside the proven marks |
| `cli.py` | the `qd` command: one verb per act, a proof line after every act, every reading as JSON |
| `compaction.py` | fresh starts after compaction: the threshold, the handoff, readiness and the transition journal |
| `control_confirmation.py` | a consumed control's continuation, kept by the request owner |
| `copy_state.py` | which installed copy runs a ship, read-only, for the CLI and the status path |
| `door.py` | the door: the one typing path, which clears, types, checks, submits and confirms at each speed; the wall the door reads |
| `drill.py` | the Drill: the all-hands exercise that proves the ship works |
| `durable.py` | a file replaced whole without forcing the write to storage; three fixed records keep a forced write: a delivery's first attempt, a refusal's quarantine and a compaction's transition; in a seat's folder, through `seat_files` |
| `endpoint_relay.py` | a jailed seat's one way out: the seat's model endpoint, relayed over a Unix socket into a jail that has no network of its own |
| `fleet.py` | the fleet: an index of the project's declarations, never another configuration |
| `fleet_post.py` | posts between ships: offered by the sender and fetched by the receiver, each writer in its own runtime |
| `forward_post.py` | recorded forwarding routes from an old address; the original posts stay as they are |
| `fresh_handoff.py` | durable crew order, report and idle-break proof for a new conversation; an immediate start on a chosen pool only when the current pool is walled, and the same handoff staged on that pool when the current pool is open |
| `group_post.py` | declared groups: a fixed fanout to the members, and one terminal notice |
| `human_wake.py` | the one fixed waking line for a human seat, through the door, once per watch |
| `idle.py` | the one live idle proof that relaunch, failover and selection share |
| `inbox_watch.py` | a seat's own inbox watch: its registration and its live generation |
| `jail_end.py` | a seat's sandbox ends whole: its tree and orphans at the stop, a hold where they do not end, and the sweep of a stray sandbox |
| `jail_environment.py` | what an environment may hold, named once: the jail's allowlist, the unjailed panes' and the service's declared lists, the pattern of names that look secret, and the launcher that starts each jail from bwrap's `--clearenv` |
| `ledger.py` | the Ledger: turns, tokens, tool calls and machine use, by pool, seat and order; the service writes the Ledger, and every reading opens the Ledger read-only |
| `lifecycle.py` | the seat-transition claim that lifecycle acts and ordinary delivery share: an explicit act waits a bounded time for it, a delivery never waits |
| `model_mismatch.py` | differences between a seat's recorded model and the ship file, and one fixed notice to the systems seats for each |
| `move_hold.py` | expiring idle observations and the service's decision stage at a wall, kept for display only |
| `order_attribution.py` | the order a turn is charged to, from reply threads and timestamped evidence, never from prose |
| `pane_obstruction.py` | one small marker that names why the door held a paste, such as a pane too short |
| `pending_lifecycle.py` | journal retirement by the journal's writer, and the last exact join of a retiring launch |
| `person_quiet.py` | a human seat's quiet time, from the seat's record, and one wake per readiness |
| `post.py` | Post: orders, reports, questions, answers and acknowledgments as immutable files; the store, the inbox, the attempts and the watch |
| `post_dir.py` | every path under `runtime/post` opened folder by folder, so that a link is refused by name and never followed |
| `post_message.py` | a message's immutable identity, its JSON and the envelope the message is typed in |
| `post_reads.py` | the bodies a seat has read whole through `qd thread`: claimed as `thread-reads.json` in its own writable folder only after the thread's complete output, then taken by an unjailed qd (the service's pass, a launch) into a receipt in its trusted folder, dated no later than the take; a receipt settles a read notice without changing any attempt's verdict |
| `post_retirement.py` | finished delivery evidence moved, unchanged, out of the service's working folders into batches that every reader still resolves |
| `retirement_gate.py` | a completed retirement sweep may wait; its certificate never permits a move |
| `post_threads.py` | the thread indexes, and a thread read whole for `qd thread` |
| `operational_notices.py` | read-only status lines from ship-local tools, with bounded data, expiry and writer identity checks |
| `presentation.py` | published operational text, independent service liveness, stale age and bounded fallback to current facts; never lifecycle, receipt or usage evidence |
| `procstat.py` | the states `/proc` gives a process that has exited, each read as ended |
| `program_use.py` | each program's current activity, and its last use bound to saved launch evidence |
| `push_reminder.py` | after a push of `main`, seen on the local tracking ref alone, one order twenty quiet minutes later to the seat that `push_reminder` under `[ship]` names, where one is declared, to read the hosted runs of the pushed tips |
| `raise_ship.py` | a template ship, declared after the person approves its exact proposal |
| `raise_notice.py` | every raise seen: the order that told the office's desk of a crew qd raise started, kept beside the raise's records; the status line names the raise while that order is open, and the desk's report on it clears it |
| `receipt_refresh.py` | the service's progress through one closed-record receipt pass per framework revision |
| `recovery.py` | one native continuation after a proved thinking exhaustion, through the door's journal |
| `relaunch_intent.py` | a relaunch the service makes as one section a stop waits out: its intent written and a line logged before the down, a line after the up, and an intent a killed service left finished once by the next service, or said on the status line |
| `request_queue.py` | one owner for queued seat requests, their visible waits and their answers |
| `resources.py` | the framework's resource files, for packaging and `qd sync` |
| `seat.py` | the Seat: one program in one tmux pane under one role, and its lifecycle: up, down, restart, fresh and a move to another pool; its launch record read whole, and the pools it may name |
| `seat_files.py` | writes into a seat's folder, its home included, that never follow a link its jail could have planted: each folder on the way opened without following, and made so, a temporary renamed over the name, a link refused on an append, and a read that feeds a write reading a link as nothing and refused past 4 MiB |
| `seat_home.py` | a seat's home kept to its owner alone: made 0700, and at every launch proved an owned real directory and mended to 0700 through its own descriptor, an absent one refused where the launch expects it built |
| `seat_migration.py` | the move, once per seat: the files the service once kept in a seat's own folder taken into its trusted folder only where live state confirms them, the rest set aside unchanged in the evidence |
| `seat_command.py` | the exact `qd` command a seat is told to run, shell quoting included, shared by the brief and the record readers |
| `server_recovery.py` | a ship brought back when its own tmux server has gone: only when the server is proved gone, at most twice in an hour, and through `qd up`'s own path |
| `service.py` | the service: the ship's one long-running process, not a seat, which renders inboxes, delivers, writes attempts, answers requests, the quick ones in short bounded turns between its other duties, re-briefs seats, writes the heartbeat and alone writes the Ledger |
| `service_inbox.py` | skips a completed inbox pass only while its file inputs and bounded received tail remain unchanged; recent timestamps and failed passes certify nothing |
| `service_status.py` | cheap change hints for status publication; the ship file sets unchanged refresh, thirty seconds by default; never delivery evidence |
| `service_timing.py` | one numeric summary per minute: pass count, duty totals/maxima and resting time |
| `service_wait.py` | directory arrival notifications during service rest and drill settlement, with bounded fallback checks |
| `setup.py` | guided setup, which asks Git for a usable identity before it writes anything, with one replaceable boundary for every machine, account and live operation |
| `setup_answers.py` | one answer resolver for setup's conversation, flags, file and summary edits |
| `setup_proposal.py` | read-only setup discovery, and the one renderer of a proposed ship |
| `ship.py` | the Ship: `ship.toml` read and checked, with its kind, sessions, pools, seats, groups and cascade; the line editor that `qd set` writes with; the runtime's fence |
| `shutdown.py` | the service's stop at a durable delivery boundary, held through a relaunch section (`relaunch_intent.py`), and read-only diagnosis of unresolved startup delivery evidence |
| `stop_barrier.py` | a service stop's process identities, retained until every exit is confirmed |
| `barrier_settle.py` | `qd service --settle-barrier`: an unreadable stop barrier moved aside unchanged, only when no service, scope member or QD_SHIP carrier of the ship runs |
| `delivery_boundary.py` | `qd service --settle-delivery`: a delivery pointer the next service cannot resolve, moved aside unchanged once the old owner is proved stopped; an in-flight journal it names is held uncertain, a wake by its episode, and a cancelled or delivered wake is not held; the mark is forced to disk before the move |
| `standing.py` | standing orders: the laws, the ship's orders, the cards and the brief every seat reads |
| `brief_change.py` | a change to a seat's brief wakes no seat: the brief last told each seat, and the head of changed lines its next message carries, bounded; the editor of a commit not told of its own edit |
| `structure_outcome.py` | read-only proof of an ended turn, and one completed service pass of report grace |
| `sync_all.py` | `qd sync --all`: every ship of the project synced in turn through the single-ship installer, the default ship last, stopping at the first failure |
| `thread_context.py` | open-thread context, with incoming carriage kept apart from outgoing explanation |
| `tmux_socket.py` | creator evidence and serialization for the ship's private tmux endpoints |
| `unconfirmed_alert.py` | the alarm for a seat whose earlier launch left a delivery unconfirmed, raised through the common alarms |
| `watch.py` | the Watch: a seat's activity and door, and the status line |
| `watch_broker.py` | a jailed seat's inbox watch, kept by the service on the seat's behalf, so that nothing under `runtime/post` is writable inside the jail |
| `write_bound.py` | the write-bound alarm: a jailed seat whose writable folders pass their bound, walked by descriptors in a thread of the service's own, has its jail killed and is held down until a person's word for it |

### Under `src/quarterdeck/harness/`

The adapters, and the harness modules that the `harness/` table does not list.

| Module | What it holds |
| --- | --- |
| `base.py` | the `Harness` interface: everything an adapter knows about one program |
| `antigravity.py`, `claude.py`, `codex.py`, `grok.py`, `opencode.py`, `pi.py` | the adapters for Antigravity, Claude Code, Codex, Grok, OpenCode and pi |
| `fake.py` | the fake harness adapter, the reference implementation of the interface; the fake program writes back the launch's startup token once the program has drawn a first screen, which ends a live launch's survival wait early, while a real program's launch keeps the whole wait |
| `antigravity_models.py` | exact Antigravity scope facts; a model's name alone establishes no quota membership |
| `check_evidence.py` | bounded tool evidence from a check's own pinned record, never a receipt |
| `claude_background.py` | Claude Code's root Bash background tasks, apart from turn accounting |
| `claude_children.py` | Claude Code 2.1.325's subagent records joined to their root: a child's own background commands and its Agent call's lifetime |
| `claude_held_box.py` | Claude's exact-edge word wraps in the Door's held composer comparison |
| `claude_limits.py` | Claude's native allowance fields, model families and safe shape diagnostics |
| `claude_extra_usage.py` | timestamped refusal metadata, status and doctor wording, and durable private Desk notices |
| `claude_statusline.py` | Claude Code's default footer, run as a standalone standard-library script |
| `claude_record.py` | guarded streaming Claude JSON lines, bounded retained native evidence and raw prefix proof |
| `claude_watch.py` | recorded watch output that a tool consumed, for attribution only, never a receipt |
| `closed_record.py` | whether a payload is absent from a closed launch's record |
| `codex_usage.py` | Codex usage windows, from one snapshot dated by its record event |
| `compactions.py` | a complete snapshot of the current record and its work frontier, for fresh readiness |
| `grok_models.py` | Grok's model selection diagnostics and its cached model list, read only |
| `grok_notice.py` | an information-only inventory of the hooks, MCP and LSP that a Grok workspace declares |
| `grok_record.py` | Grok's native ACP update stream, read through the shared JSON-lines walk |
| `grok_screen.py` | Grok's fullscreen composer, measured with and without color |
| `grok_usage.py` | Grok's own ACP billing reader, with no provider HTTP client and no token handling |
| `grok_workspace.py` | Grok's native folder trust for the whole repository |
| `reader_stamp.py` | reader identities derived from files, independent of where Quarterdeck is installed |
| `reading_scope.py` | one reading's own cache: a program's record that has not changed is read once within that reading, and nothing is kept beyond it |
| `thinking.py`, `opencode_thinking.py`, `pi_thinking.py` | the proved controls that turn a model's thinking off and back on in OpenCode and pi, and the values and checks the controls share |

### Under `ships/`

Each template holds a `ship.toml` and an `orders.md`.

| Folder | What it holds |
| --- | --- |
| `drydock/` | fake seats, one in each kind of session; the tests and the drill run here |
| `default/` | the smallest crew on real programs: a desk, a captain and an engineer; copy `default/` for a ship of your own |
| `forum/` | a lead and independent reviewers, with `roles/lead.md` and `roles/reviewer.md` |
| `cascade/` | a ring of seats on different providers that the service relays between, and an orchestrator, with `roles/` and a `README.md`; the cascade's notes stay in the runtime, never committed |
| `shuttle/` | a captain, an engineer and a first officer for reviewed and signed missions, with `roles/`, a `README.md` and `mission-record.md` |
| `office/` | the template for the project's separate office, with `roles/desk.md` and a `README.md` |
| `<yours>/` | a ship of your own, a nested repository that the project ignores: `ship.toml`, `orders.md`, `roles/`, `harnesses/`, `homes/` and `runtime/` |
| `*/runtime/` | a nested repository with no commits: the fence around a ship's state |

### Under `tests/`

| Path | What it holds |
| --- | --- |
| `unit/`, `contract/`, `integration/` | the tests |
| `fixtures/` | each program's native forms, ship files and the other fixed inputs the tests read |
| `testrun.py`, `testrunner/` | the repository's own test tool, standard library only |
| `testrunner/run.py` | one run of a clean committed target, in fresh scratch the run owns |
| `testrunner/gitops.py` | Git as the authority on what is tested |
| `testrunner/environment.py` | the run's environment: the caller's own, less what changes where the target is imported from or how Python, pip, pytest and git start |
| `testrunner/scratch.py` | a run's working storage; durable evidence stays in `.testruns/results` |
| `testrunner/clean.py` | a change's registration, a run's retirement and recovery, `clean`, `keep` and `release` |
| `testrunner/evidence_plugin.py`, `testrunner/evidence.py`, `testrunner/report.py` | the events and session layout a run records, what the events prove, and the result and report a run leaves |
| `testrunner/sessions.py`, `testrunner/servers.py` | the records of a run's session containers and private tmux servers, and the servers' bounded shutdown |
| `testrunner/usersite.py` | the ordinary interpreter's user site, made visible to the run's environment, nested CI runs included |
| `testrunner/jail.py` | `run --jail`: every step that runs the target's code runs inside bubblewrap, a jail built by listing what goes in |
| `testrunner/owned.py` | measuring and removing a tree the tool owns, never following a link, never crossing a filesystem, and refusing what is not this user's |
| `testrunner/procs.py` | which processes belong to a run: this user's, in the run's own process group or using its folder; one that cannot be inspected is counted as such |
| `testrunner/socket_paths.py`, `testrunner/socket_roots.py` | the length a test socket's path may have, and the short private folder under `/tmp` that holds a project's test sockets, named from the user's id and a hash of the project's path, with its sweep once the project is gone |
| `shared_install.py` | worker-local, immutable wheel and install fixtures; a test that changes an install clones its own |
| `authentication_shapes.py` | a measured Claude API-error envelope, with invented text, paths and identities |
| `integration/waiting.py` | `wait_for`, a condition polled until its deadline, and `attached_client`, which owns a terminal client, keeps the client's whole output and retires the client's own command group |
| `unit/test_attached_client_retirement.py` | real-process cases of retiring an attached client |
| `unit/test_ci_lane_options.py` | CI selection, command and declared disk-layout cases |
| `unit/test_fast_lane_selection.py`, `unit/test_fast_lane_slots.py` | lane selection and its presentation, and scheduling on private slot files |
| `integration/ci_layout_probe.py`, `integration/ci_layout_ordinary_probe.py` | real-checkout probes that only the CI-layout mode selects |
| `integration/test_testrun_ci_layout_gates.py`, `integration/test_testrun_ci_layout_corrections.py`, `integration/test_testrun_tests_origin.py`, `unit/test_testrun_ci_layout.py`, `unit/test_testrun_ci_layout_gates.py` | the gates and corrections of the test tool's CI layout, and where a run's `tests` come from |
| `unit/test_structure_outcome.py`, `integration/test_structure_outcome.py` | ended-turn outcomes, with the fake program and invented pi records, through real doors and service passes |
| `unit/test_shuttle_template.py`, `integration/test_shuttle.py` | the shuttle declaration and its installed resources, and scripted fake missions that prove transport and artifacts, not an agent's judgment |

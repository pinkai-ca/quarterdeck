# Quarterdeck design: Harness adapters

A page of [the design](../DESIGN.md).

## 5. Harness adapters

An adapter owns every fact about one terminal program: launch, home, credentials, records, screen, controls, usage and dialogs. Code outside `harness/` uses that interface rather than naming programs. The base implements common record joining, state/turn reading and declared control lookup. The adapter's card teaches its program; fixtures capture its supported native forms. Unknown evidence stays unknown and grants no action.

### Interface

`Harness` in `harness/base.py` groups these responsibilities:

| Members | Contract |
| --- | --- |
| `name`, `program`, `home_variable` | Registry identity, selected executable and native home variable. |
| `build_home`, `launch`, `owns_launch` | Build a private home from ship files and credential links; produce argv/environment and recognize the saved launch. |
| `preflight`, `workspace_notice` | Read-only readiness and optional workspace information. Preflight performs no native/provider call or write; omitted context never implies the operator's home or cwd. Notices prove no activation or permission. |
| `records`, `first_timestamp`, `session_id`, `reader` | Find records, establish native launch time and resume identity, read state and ended turns. Joining uses the first post-launch record; ambiguity is reported, never guessed. |
| `inputs`, `record_time_resolution` | Accepted/queued/consumed input evidence for the shared exact receipt matcher. Positive timestamp resolution floors only its comparison boundary, never the saved attempt. |
| `recorded_paste`, `paste_ignored` | Only measured native transformations of a typed payload; defaults preserve it. |
| `attribution_inputs` | Additional structured watch-consumption evidence for accounting, never a delivery receipt. Complete post envelopes must still match the immutable store. |
| `screen`, `styled_screen`, `box`, `box_columns` | Composer, work state, dialogs and layout. Styled capture carries current pane identity. Unknown work/layout stays unknown; a boxless dialog-free startup frame waits within `start_seconds` (default 60). |
| `queued_input`, `queued_waiting` | Prove a newly visible native queue after a matched paste and its continued wait. Visibility never replaces an exact record receipt. |
| `paste_clear_rules`, `paste_clear_keys`, `keys` | Versioned cleanup descriptors and proved submit/interrupt gestures. No descriptor means no automatic cleanup. |
| `controls`, `interruptions`, `answer_seconds` | Supported launch/session controls, resulting-state confirmation and recognized dialogs; default answer observation bound is five seconds. |
| `queues_while_working`, `opening_prompt_turn` | Whether work accepts queued input and whether an opening brief starts a native turn. |
| `human_wake_box_margin`, `waking_inapplicability` | Explicit waking-row layout proof or named inapplicability; default unknown never authorizes a wake. |
| `check_tool_evidence` | Check-only bounded reading of the temporary seat's last eight call/result pairs, at most 32 KiB; no receipt/activity authority. |
| `native_provider`, `accepts` | Company identity and pool compatibility. `EndpointHarness` supplies endpoint home/readiness behavior. |
| `credential_file`, `freshness`, grant/renewal readers | Credential location, generation and known fixed/self-renewing/unknown grant facts, read only through declared native fields. |
| Usage readers, `headline_windows`, `shared_usage_key` | Subscription windows, cadence, shared readings and optional background/private-home work. Missing capability leaves usage unknown; key pools use spend. |
| `model_scoped_usage`, `usage_scope` | True/false/unknown model applicability. Running source settings are record-first; destinations use their resolved choices. |
| `quota_wall`, `screen_walls` | Only measured screen quota evidence and resets. |
| `remote_control`, `wrap_slack`, `drill_settings` | Remote access, layout margins and synthetic-only settings. |

JSON-lines adapters supply `take`/`end` on `JsonLinesReader`, optionally `identifies` for a different native envelope. Grok identifies ACP method/update markers without rewriting rows and retains physical line numbers. A database holding all sessions gets a data directory per launch, keeping one-record identity exact.

### Homes, trust and permissions

Adapters pass ship-owned files and launch overrides without editing the operator's configuration, instructions or repository. Operator HOME material stays outside seat homes. Frontier programs trust the declared working folder as their maker intends; program-specific restrictions require the ship's authorization. Generated prompt suggestions and self-update are disabled where supported. A program is selected through PATH or the declaration, without pinning an executable path/hash.

Programs run without native approval prompts. A seat requests a jail with `sandbox = true`; endpoint seats default to jailed unless explicitly false. `sandbox.py` builds one recorded bubblewrap launch: read-only root, private capped tmp, empty operator home, required program/interpreter/PATH folders bound back read-only, writable workspace and own seat home, read-only ship with other seats hidden, and writable post access. The ship's tmux socket is hidden and `QD_TMUX_SOCKET` absent. Jailed readings explain their limited visibility.

The program's home is the seat home. The empty-home mount is depth ordered after ancestors and before allowed descendants. A workspace equal to home or any bind-back that exposes home or an ancestor refuses by path. A jailed OpenCode hand's finder starts in its own runtime work folder; explicitly named workspace paths and `QD_WORKSPACE` remain usable. Resume requires proof of the saved working folder; otherwise restart refuses before closing the old program. Fresh start retains the old record and carries open work.

### Versions, checks and controls

Adapters apply their newest known facts to later versions as best effort; an unproven version alone does not hold launch. Status separates running, installed and proven versions. `qd check <harness>` uses a throwaway seat to prove launch, readiness, full folded paste verification, exact receipt, a task/file and closing report, join and ended record. Fixtures separately cover record shapes, usage, controls and dialogs. A changed unrecognized form withholds rather than guesses.

Optional check `--model ID` affects only that seat. Requested identity must be confirmed record-first, with startup screen fallback and an owned-record model at completion. An explicit contradictory screen model prevents success; an absent screen model does not negate a confirming record. Proof names requested, launched and recorded models.

`version(home=None)` normally runs the selected program's version command. Only explicit `private_version_home` adapters receive an empty temporary home under an existing fenced runtime; no context creates a runtime just to inspect a version and no executed call retries without context. Grok uses `env -i`, controlled native ENV, selected PATH and that home for HOME/GROK_HOME. Context-free Grok executes nothing and parses only a recognized selected binary basename; otherwise version is unknown.

Launch controls apply when starting. In-session controls leave their visible command in the native log and avoid a restart. Launch-only changes require a stated restart. Confirmation needs resulting native state, never command echo. A pending control owns its dialog continuation, durable once claim and original thirty-second deadline. Only its requested-value-matched option, proved focus and unchanged command binding allow an answer. Uncertain gestures are observed or expire, never repeated. A person's independent `/model` dialog remains obstructed.

Claude Code 2.1.317's model answer is Enter on the highlighted, exact-display Yes option, at the measured 120×40 styled frame with hidden composer/cursor. Expected display comes from the fresh exact home catalog, without picker overrides. Confirmation requires the linked caveat, exact command and successful result after the saved complete-row byte boundary in the same unchanged-prefix record. Display alone, old results, unsupported geometry/style or missing command linkage cannot confirm it. The synthetic adapter uses its own linked command/result form; an initially unjoined synthetic control may observe a joined result but grants no dialog key.

### Selections and explicit defaults

`harness/capabilities.py` owns pure effort/default metadata. Capabilities distinguish levels, none, unknown and synthetic unrestricted text; complete vocabulary and exact-version measurement are separate. Pi's complete vocabulary is off/minimal/low/medium/high/xhigh, measured on 0.74.2; OpenCode has no seat effort control. Other CLI vocabularies remain unmeasured until established. Catalog/model support cannot be inferred from a level.

Pure ship loading rejects concrete effort on a no-effort harness or outside a universally complete vocabulary. Doctor, launch and running controls reuse their appropriate installed/running version observation. An unmeasured version passes with an explicit unchecked note; missing evidence is never invented. Cascade validates each effective choice. `qd controls --harness NAME` exposes the same metadata without ship lookup, tmux or native probes; it conflicts with an explicit seat.

Independent `model_mode`/`effort_mode` values are named or program-default. Named requires its string; explicit default requires it absent. Omitted modes retain legacy strings, including literal `default`. String pool choices inherit modes; inline choices own/reset theirs. Endpoint programs require a model; effort-default is allowed even when no effort control exists.

Explicit defaults omit native flags/generated selectors. Known conflicting template/settings/profile/environment selectors refuse by scope; unclassified declaration overrides refuse rather than being silently removed. Adapter baselines and unrelated model options remain. A marker neither proves account support nor resets a resumed conversation. Text labels it `program's default (not checked)` and keeps real observations separate. Mode changes need relaunch; named-to-default changes requiring deletion refuse through set with an explicit-edit remedy. Adopt saves actual observations with named modes.

### Measured receipt and screen behavior

The [door](door.md) owns transport and waking policy. Layout recognizes only supported forms. Tmux soft wraps are joined before parsing; padding, hard newlines and punctuation remain significant. Grok 1.0.77 uses styled rounded composer/footer/hint frames; the exact faint model-argument hint may be discarded only in its measured picker context. Unknown styles or hint tuples stay unknown. Indexed colors 16–255 decode as xterm cube/grey; system colors stay unknown.

Claude Code 2.1.317 expands pasted tabs to four spaces and escapes an opening `<pasted_content` wrapper tag. Its measured hidden-format-character sanitizer is applied symmetrically, preserving native context exceptions. These projections require every version-bearing row since the attempt to name that version; missing/other versions grant no projection. No near match is accepted.

Grok 1.0.77's `user_message_chunk` stores the entire text even when the composer folds it; resume appends the same shape to its original updates file. Timestamp resolution is one second. Join time instead comes from the UUIDv7 session folder, only when native rows agree on identity; there is no row-time fallback.

During foreground work Enter queues follow-ups in memory. At 112/256 columns a matched full paste followed by an exact new numbered preview and count increase proves screen queueing. The submitted journal stays open. A readable same-payload queue waits beyond 600 seconds; on dequeue a second record check precedes an age verdict, with grace strictly until five seconds after the preceding recorded completion while a recognized composer is working. Unknown queue evidence retains its hold. Send-now remains unset. Queued prompts reach records as separate exact new turns after completion, including across tool boundaries; a long-running Grok seat is kept fed by reading its own inbox.

Grok's four-line fold count uses LF delimiters, discounting one final terminator; extra terminal blank rows stay significant. Its 80-column ordinary text area wraps at 70 cells. A scrolling long-line viewport proves no full draft. Whitespace-only text indistinguishable from empty refuses. For waking, padding-only text counts as empty only with a stable identity-bracketed visible cursor at the adapter's empty cell. Known limit: Home/Ctrl-A after spaces can produce that same screen/cursor. No folded cleanup is inferred.

Pi's optional provider footer is supported on 0.79.0; its earlier measured box/working/wrap/fold forms remain. It launches with no-approve, no-context-files and no-extensions. Codex native item-completion metadata never makes an opaque exec wrapper's detached-child inventory complete. Unknown tool completeness still holds lifecycle operations.

### Grok trust, privacy and model facts

Grok grants its declared workspace's repository root, or cwd outside a repository, native trust through `--trust`. The guard accepts exactly that grant in `trusted_folders.toml`; unknown roots, other grants and malformed state refuse intact. Workspace contents do not refuse launch. Project instructions, rules, skills, hooks, MCP and LSP remain trusted; compatibility Claude/Cursor imports and extra rule directories stay disabled. Native managed/requirements files in home or `/etc/grok` refuse intact because native startup does not provide the claimed enforcement. Memory settings, GROK_MEMORY and no-memory disable native memory.

`workspace_notice` inventories bounded hook/MCP/LSP declarations, escaping paths/names and omitting command, URL, header, environment and token values. Unknown or indirect sources remain unknown. LSP standard-file paths are reported without claiming a serialization or activation. Optional saved `notice` is information, not identity/accounting/permissions. A reader that cannot accept that launch field requires bringing the seat down before rollback.

The measured first-run training question is answered only by the door, in a nonhuman idle empty composer with SGR mouse reporting: one recorded press/release on the unique exact Opt out text, with outcome saved in optional `launch.json.answered`. Unknown forms need a person; opt in is never pressed. A human session answers its own question. Templates/settings cannot override privacy.

Only cached `coding_data_retention_opt_out = true` permits a framework banner seed. `quarterdeck-privacy-seed.json` is written before the matching timestamped config acknowledgement. Every rebuild rechecks the flag; withdrawal removes the owned key before the seed. Native acknowledgements without a seed survive. Malformed/linked/mismatched seeds hold. Idle seeded-composer proof requires both the matching seed/config and the launch notice's seed evidence, on the measured version. A notice alone proves nothing. No account preference is changed remotely. Billing homes use the same rule. Older readers rejecting answered/notice fields need identity-checked down before downgrade.

Model identity is adapter-owned: Codex folds case only, pi/OpenCode use literal IDs, Antigravity uses its measured mapping. Grok's listed launch id is grok-4.7; billing grok-4.7-build is not a launch alias. Grok running identity comes from native input/config metadata, never billing keys or ambiguous display labels. Visible startup refusal rows accompany declared/observed selections without rewriting them. Doctor's measured 1.0.77 cache check requires matching id/model/key fields; missing/unknown cache shapes pass with uncertainty, while absence from a complete list fails. These local reads spend no turn. [Grok contract facts](../GROK.md) inventories further native limits.

For relaunch admission only, Claude Code 2.1.317 can set aside a background Bash task whose known start predates this launch and whose sole later evidence is the measured single-shell resume-stopped notice. Other versions, delegated/current tasks, unknown starts or other later evidence remain unknown. This launch-scoped view does not change accounting, status, controls or ordinary moves.

### Thinking exhaustion

Automatic recovery is restricted to proved nonhuman Qwen3.8-27B configurations on OpenCode 1.19.5 or pi 0.74.2, with exact launch/configuration evidence. Version prefixes, unknown plugins/budgets or missing compatible native off mode grant no recovery. Endpoint `output` is a positive response allowance below context, default at most 8192 and one quarter of context with a 1024 floor; server limits remain independent.

Eligibility binds one terminal reasoning-only length-capped generation exactly exhausting its effective allowance, with known input/work lineage, empty queues/tools/children, certain receipt and positively idle empty composer. Unknown or superseding work refuses. The service durably reserves one continuation report, then one door disables thinking, verifies off, submits/confirms the bound continuation and restores the original mode after work ends. The report closes no order; completed tools are not reissued.

Journal phases claimed/disabling/off/submitted/restoring/done precede their gestures. Owner, source, current leaf and settings are rechecked at every boundary. Uncertain controls never replay; active/unknown recovery holds delivery, controls and automatic lifecycle changes. Native marked compaction can authorize waiting only, never completion/restoration or another allowance. Restoration needs exact receipt and idle empty tools/queue/box; intervening settings are preserved. Failed recovery grants no second allowance.

Optional launch recovery hashes and Proof phase/source/input evidence are runtime evidence, not configuration. Missing evidence refuses eligibility; malformed evidence holds visibly. Ownership changes retain holds and once claims without reading a human conversation. Identity-checked down preserves journals. Before downgrade, finish restoration and bring eligible launches down with matching code; never delete unresolved journals to force progress.

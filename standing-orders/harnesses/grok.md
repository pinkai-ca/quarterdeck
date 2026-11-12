# Program: Grok Build

Your program is Grok Build, from xAI. The subscription pool names provider `xai`; your seat names harness `grok`.

Quarterdeck supplies a seat-owned `GROK_HOME`, a private `HOME` inside it, the brief through `--rules`, and a link to the pool's `auth.json`. XDG folders stay inside the private `HOME`; the fixed configuration overlay does not adopt an operator file. The service reconciles native credential refreshes with the pool and sibling seats. Leave sign-in and settings to that machinery. Self-update and prompt suggestions are configured off. Nonhuman sessions request normal tools approved and the native `ask_user_question` tool removed; ask through `qd post question`.

Folder trust is never granted and trust prompts are never answered automatically. An unexpected nonempty `trusted_folders.toml` holds launch and stays intact for inspection. The ship template and seat settings cannot supply `paths.extra_rule_dirs`, whose imports are independent of folder trust. Every `requirements.toml` or `managed_config.toml` under `/etc/grok` or `GROK_HOME` is refused and retained. Quarterdeck does not write a requirements file or depend on its survival. Managed configuration fetching is configured off.

A Grok seat cannot work at the framework root or in a repository with `AGENTS.md` or `CLAUDE.md`. It works in a folder holding only its material. A clean subfolder inside such a Git repository still inherits root sources and is refused. Doctor and launch report every found source from the Git root through the working directory; an unreadable or ambiguous scope is unknown and refused. A positively identified non-repository uses only its working directory.

The complete project-source inventory is checked at every directory in that scope:

| Kind | Refused paths |
| --- | --- |
| Named instructions | `Agents.md`, `Claude.md`, `CLAUDE.md`, `CLAUDE.local.md`, `AGENT.md`, `AGENTS.md`, `.claude/CLAUDE.md`, `.claude/CLAUDE.local.md` |
| Rules | `.grok/rules`, `.claude/rules`, `.cursor/rules` |
| MCP and plugins | `.mcp.json`, `.grok/plugins`; `.grok/config.toml` when it contains `[mcp_servers]` or `[plugins]` |
| Hooks | `.grok/hooks`, `.claude/settings.json`, `.claude/settings.local.json`, `.cursor/hooks.json` |
| Workflows | `.grok/workflows` |
| Skills and commands | `.grok/skills`, `.grok/commands`, `.agents/skills`, `.agents/commands`, `.claude/skills`, `.claude/commands`, `.cursor/skills` |
| Agents, personas and roles | `.grok/agents`, `.grok/personas`, `.grok/roles` |
| Native project LSP | `.grok/lsp.json` |

An ordinary `.grok/config.toml` without MCP or plugin sections is not refused solely by filename. No import marker or native MCP-policy persistence is assumed.

Grok also consults `/etc/claude-code/managed-settings.json` as advisory policy: it can only tighten MCP restrictions. This file is not refused or edited by Quarterdeck.

Controlled launch, billing and version environments carry `GROK_TELEMETRY_ENABLED=0`, `GROK_TELEMETRY_TRACE_UPLOAD=0`, `DISABLE_TELEMETRY=1`, `GROK_STORAGE_MODE=local` and `GROK_DISABLE_EXPERIMENTS=1`. Native configuration sets `features.telemetry=false`, `telemetry.trace_upload=false` and `relay.enabled=false`. The 1.0.77 agent option's local-storage value is `local`; no `--storage-mode` flag is added to the interactive command. These settings are not a general claim about every session/history/trace path. Independent native isolation validation is separate from synthetic configuration checks.

The program can install native bundled skills, agents, personas and roles, its signed settings cache and its models cache in `GROK_HOME`. These are native bundled material, not operator imports. The 1.0.77 bundle includes 22 skills, including `resume-claude`, `resume-codex` and `resume-cursor`, which can access the other programs' sessions. The adapter configures the documented `skills.ignore` prefix for `GROK_HOME/bundled/skills` and names those three in `skills.disabled`; effective suppression is not claimed before validation. It also sets `features.subagent_bundle=false` and `GROK_SUBAGENT_BUNDLE=0`; that documented switch affects only the daemon route, and the shell route ignores it. A separate documented switch disabling bundled shell agents, personas or roles has not been established; their absence is not claimed.

Startup can fetch the public `xai-org` plugin marketplace repository and offer its catalog. A supported seat setting that suppresses this built-in public fetch and offering has not been established. Guide 09 describes marketplace policy pins and `plugin_auto_update=false` for pinned session-start plugin updates; those do not establish suppression of the default public marketplace in this seat configuration.

Memory is configured off with `memory.enabled=false`, `memory_v2.enabled=false`, `memory_v2.batch_dream_enabled=false`, `GROK_MEMORY=0` and `--no-memory`. Background capture/Dream scenarios outside the measured launch remain **unproved until online validation** of those scenarios. The batch-Dream switch alone does not disable every kind of Dream. Synthetic home-output checks establish configuration only.

The record is `sessions/<encoded working directory>/<session>/updates.jsonl` in your home. Quarterdeck reads native input messages and explicit turn completions, and accounts the tokens and tool lifetimes the record reports. A class the record does not measure stays unknown. It never treats transcript text as a delivery receipt.

Native 1.0.77 update timestamps have one-second resolution. The adapter declares that resolution to the canonical receipt matcher, which floors only its local comparison time; it never changes the saved attempt time, payload or record. The complete unique-message payload in the launch-owned record remains required, and an input in an earlier second remains stale. An uncertain submission is never resent.

Billing is read through Grok's own agent protocol in a separate private home, without opening a conversation. The weekly percentage and reset come from that response. An absent percentage is unknown. No five-hour window is inferred. The subscription tier is a live billing fact, separate from the sign-in file's age.

Native model and effort launch flags are `--model` and `--reasoning-effort`; their session forms are `/model` and `/effort`. Quarterdeck confirms a setting from resulting native state, not the command's echo.

The 1.0.77 fullscreen composer has a rounded border, an `❯` prompt and a footer naming the active model, effort and permission mode. Ordinary short and multiline drafts, word wrapping and Ctrl+C clearing of an unsent draft were measured. The model picker and Commands modal are recognized separately; the picker contains a draft, so its known cancellation key does not authorize discarding that draft automatically. Ctrl+C dismisses the picker; Escape closes Commands. A control must wait for the resulting composer state before another command is typed.

Screen captures request native SGR in the same atomic pane read as identity. The adapter returns ordinary text. Native bright input, command color and muted argument hints differ; monochrome mode uses faint hints. Only the exact model argument hint in its measured picker is excluded from draft text. An identical bright literal stays content, and unknown styling or layout stays unknown. This does not establish arbitrary terminal themes or indexed color palettes.

Every painted input row is retained. Native unsent drafts with trailing newlines painted corresponding blank rows; interior and leading blank lines remained visible. For an unfolded payload ending in newlines, the verifier first requires exactly that many painted terminal blank rows, then uses the unchanged shared matcher for the remaining text and wrapping. It never silently removes meaningful rows. A bare newline and whitespace-only draft painted like an empty composer; exact whitespace-only content is not claimed. Measured native hint tuples distinguish idle, responding and foreground-tool displays. At 80 columns, the working draft shows `Enter:queue`, `Alt+Enter:newline`, `Shift+Tab:mode` and `Ctrl+c:cancel`, omitting `Ctrl+x:shortcuts`. Unmeasured hints remain unknown. A known composer or an unmeasured default field is never proof that the record has ended or the queue is empty.

At an 80-column pane, ordinary text wraps at 70 cells, with one additional cursor cell inside the composer. Native multiline pastes fold to `[Pasted: N lines]` at four counted lines. The count excludes exactly one final LF terminator, while including leading and additional trailing blank lines. Two or three lines plus a final LF stay ordinary; three lines plus two final LFs fold as four. Color and monochrome forms were measured. The count chip is compared against a pattern derived from the whole intended payload as the existing visual submission contract; a wrong count fails. This visual comparison never confirms delivery: only the exact whole input in the native record does that. No automatic owned-paste-clear rule clears folded input. A 2,000-character single-line draft instead showed a clipped scrolling viewport with a scrollbar. Its visible tail cannot establish the whole paste and is refused. Whitespace-only payloads also lack an exact visual proof. Unmeasured control characters and line-ending layouts are not inferred from these LF facts.

Guide 03 documents external editing through `/edit-prompt`, but excludes pasted/file/image chips so attachments are not flattened. The adapter uses the measured visual fold contract and the canonical record receipt; it does not invent an expansion key or an alternate delivery route.

The measured 1.0.77 mid-turn input behavior is queueing: Enter submits once, a `Queued 1` card appears, and the exact input is written to the record when the next turn consumes it. Queue screens can show `Enter:send now` with either `Ctrl+b:send to bg` or `Ctrl+;:queue`; only those measured hint tuples are recognized. The visible card never supplies a delivered receipt. `C-c` cancellation was measured during the follow-up's own foreground-tool turn after every receipt resolved and positive empty-composer/queue-clearance evidence was retained. Controls remain held while any delivery is unresolved. The extra immediate send-now action is absent and unproved; its hint alone does not establish behavior.

Normal restart resumed the same owned record with no new carried input, retaining both inputs and ended turns. A resume with open work to carry can create another input and is not an input-free operation. No explicit `-s` session-id launch option is used or claimed.

Join time comes only from the session folder's UUIDv7 millisecond creation time, with every readable native row carrying exactly that session identity. Non-v7, missing, conflicting, malformed or incomplete identity evidence supplies no join time. There is no whole-second row-time fallback or join-time flooring. Ordinary input timestamps and the receipt-only declared resolution remain separate.

Native usage names are `inputTokens`, `cachedReadTokens`, `cacheCreationTokens` and `outputTokens`; measured zero-creation input totals include cached reads, which are subtracted once. Missing/invalid classes and nonzero cache-creation semantics remain unknown. A single `modelUsage` key supplies the reported model; mixed models stay unknown. Native `numTurns`/`modelCalls` are not subagent counts, and `costUsdTicks` is not normalized without established units. Row-based durations have one-second precision. A begun tool invocation is counted once even when cancellation has no terminal tool update; its unstated duration and outcome remain unknown. Later explicit terminal evidence can refine that same invocation.

1.0.77 ordinary native screen, queue, completed/cancelled record, exact receipt, state-control and resume facts have been measured. New source adaptations still require affected checks; the frozen full suite and installed `qd check grok` remain separate acceptance steps. See [the contract inventory](../../docs/GROK.md) for precise limits and gaps.

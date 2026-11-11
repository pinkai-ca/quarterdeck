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

Controlled launch, billing and version environments carry `GROK_TELEMETRY_ENABLED=0`, `GROK_TELEMETRY_TRACE_UPLOAD=0`, `DISABLE_TELEMETRY=1`, `GROK_STORAGE_MODE=local` and `GROK_DISABLE_EXPERIMENTS=1`. Native configuration sets `features.telemetry=false`, `telemetry.trace_upload=false` and `relay.enabled=false`. The 1.0.77 agent option's local-storage value is `local`; no `--storage-mode` flag is added to the interactive command. Actual session/history/trace state remains unproved until native validation.

The program can install native bundled skills, agents, personas and roles, its signed settings cache and its models cache in `GROK_HOME`. These are native bundled material, not operator imports. The 1.0.77 bundle includes 22 skills, including `resume-claude`, `resume-codex` and `resume-cursor`, which can access the other programs' sessions. The adapter configures the documented `skills.ignore` prefix for `GROK_HOME/bundled/skills` and names those three in `skills.disabled`; effective suppression is not claimed before validation. It also sets `features.subagent_bundle=false` and `GROK_SUBAGENT_BUNDLE=0`; that documented switch affects only the daemon route, and the shell route ignores it. A separate documented switch disabling bundled shell agents, personas or roles has not been established; their absence is not claimed.

Memory is configured off with `memory.enabled=false`, `memory_v2.enabled=false`, `memory_v2.batch_dream_enabled=false`, `GROK_MEMORY=0` and `--no-memory`. Actual memory/background capture/Dream being off remains **unproved until online validation**. The batch-Dream switch alone does not disable every kind of Dream. Synthetic home-output checks establish configuration only.

The record is `sessions/<encoded working directory>/<session>/updates.jsonl` in your home. Quarterdeck reads native input messages and explicit turn completions, and accounts the tokens and tool lifetimes the record reports. A class the record does not measure stays unknown. It never treats transcript text as a delivery receipt.

Billing is read through Grok's own agent protocol in a separate private home, without opening a conversation. The weekly percentage and reset come from that response. An absent percentage is unknown. No five-hour window is inferred. The subscription tier is a live billing fact, separate from the sign-in file's age.

Native model and effort launch flags are `--model` and `--reasoning-effort`; their session forms are `/model` and `/effort`. Quarterdeck confirms a setting from resulting native state, not the command's echo.

Version facts are being measured on 1.0.77. Live check, screen, queue and isolation acceptance must complete before this adapter is declared proven.

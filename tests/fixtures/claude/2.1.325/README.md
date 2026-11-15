# Claude Code 2.1.325 record boundaries

Captured from a throwaway home on the declared secondary Claude subscription, using a private tmux server and the installed native 2.1.325 executable. No personal or office conversation was opened. The crew's program was not updated.

The four cumulative turn fixtures were produced by these short prompts:

- `Reply OK.`
- `Run true with Bash, then reply OK.`
- `Try Bash false once, then stop.` with `Bash(false)` denied in the throwaway settings.
- `Run sleep 30.` followed by Escape after the tool call was recorded. The native tool had already returned an error before the interrupt marker; this fixture proves interruption of the turn, not cancellation of an executing shell command.

Only fields needed by the record reader remain. Dates and message/tool identities are synthetic. Assistant prose, tool arguments/results, account identities, native paths, usage counters and API request bodies were removed. The native `[Request interrupted by user]` marker remains because it is the terminal evidence being tested. These fixtures do not preserve parent-history topology for compaction accounting.

`activity-metadata.jsonl` keeps one type tag for each non-input metadata kind observed. The expanded capture exercised Remote Control, compaction and resume, a local slash skill, queued input, a harmless local MCP ping tool, a hook that wrote its own marker, and an Opus 5.5/max turn with extended thinking. `bridge-session` and `cost-state` appeared in that expanded capture and are already recognized by the reader. All captured root kinds were already recognized at the base of this change; this is not evidence that an unidentified row kind from another conversation has been fixed.

`queued-prompt.jsonl` preserves the observed `queue-operation` enqueue of `Reply QUEUED.`, its dequeue without a content field, and the later native user echo and completed turn. An identity-free dequeue alone does not prove acceptance of any particular text.

A separate throwaway framework seat on the same pinned executable completed a short order, proved known idle through the ordinary idle reader, and accepted a fresh start. The live capture and its private evidence stay outside the repository.

`artifact-bookkeeping.jsonl` is entirely synthetic, built from the permitted key shapes of `frame-link` (both the saved artifact count and publication-link form), `artifact-comment-monitor`, and `artifact-autoreact-ledger`. The explicit classification is turn-neutral bookkeeping: none of these rows carries an input, a call, or a result. All identifiers, URLs, paths, titles and account values in that fixture are made up. The reader recognizes exactly these additional kind names; every other unmeasured kind remains unknown. Tests interleave the count between `atis-latch` and `bridge-session`, and the publication rows between an assistant/API row and the subsequent result, then after the completed turn.

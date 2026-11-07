# Delivery evidence and waiting

Each message gets its own paste/check/submit. Previously withheld items go after
new due items, so an item that cannot be transported cannot contaminate their
payloads. A pre-paste refusal leaves the door available for the next item. An
unverified paste, foreign draft, unknown dialog or uncertain submission holds
the door; there is no safe promise of unconditional progress through that state.
An unverified paste is never cleared automatically.

Terminal soft wraps are joined using tmux's `capture-pane -J` wrap flags before
any adapter parses the box. Application-rendered rows are compared with the
expected transport and the adapter's text-area width. A full row may break at
any character. A word/hyphen break is accepted only when its next piece cannot
fit. Interior spaces, hard newlines and punctuation are not collapsed or
removed. A plain string with no layout evidence must match exactly. Unknown
control/grapheme layouts fail closed. Adapters still support their established
folded-paste counters during the initial, empty-box paste verification.

The ordinary Codex 0.166.0 composer reserves two prefix cells and one right
margin, as defined in its [versioned layout source](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/tui/src/bottom_pane/chat_composer/composer_layout.rs).
Other ordinary adapters have a two-cell prompt/continuation prefix. Scrubbed
screen fixtures without terminal width are extraction evidence; tests of their
application wrapping must supply a text-area bound explicitly. Partial or
unrecognized layouts do not establish matching text.

Before typing, the service persists the exact transport, attempt files, original
attempt time, pane, PID, launch, reader and owned record. It persists the paste
and submit phases before each side effect, and the verified display before
submission. Recovery never repeats a paste or submit whose outcome is uncertain.
A same-launch submitted payload still showing its exact verified display is
**waiting to be taken**, without a human-obstruction alert. An empty composer is
**waiting for record confirmation**, never delivered. Edited, partial, foreign,
folded/ambiguous or unavailable pending displays require a human; so do identity
changes and crashes during an uncertain phase. Old-launch uncertainty remains a
ship-wide notice, but cannot own a different launch's empty, recognized door.
If lifecycle explicitly carries that message in a later launch, its `launch`
attempt supersedes the old hold; the old submission is still not called delivered.
A launch change alone never supplies that receipt.

The intervention bound is **600 seconds from the original attempt**, inclusive
at the boundary. It does not move on polls, retries or service restart. This is
an intervention policy consistent with the existing ten-minute stalled-work
threshold, not evidence that a six-minute queue delay is normal. Expiry appears
in every ship session's status and alerts the desk; it never triggers a resend.
Inspect the composer and owned record before explicitly resolving uncertainty.
An explicit resend may retire the old uncertainty only with an empty, recognized
composer; it cannot clear a draft and does not declare the prior attempt delivered.

Only an exact matching owned-record input proves delivered. The new journal
checks Codex user-item timestamps, including inputs consumed during a running
turn. Record-queued is distinct from delivered. Missing/unreadable records and
disappeared composers retain uncertainty. Legacy attempts lack the new payload
proof and cannot claim a composer as their own merely by text similarity.

The investigated long delay was repeated pre-submission verification failure,
not established program queuing: line wrapping after a hyphen became an extra
space during the old comparison, causing paste/check/clear retries. Once a
submission succeeded, record confirmation followed in the same second. Exact
messages, timestamps, attempt history and remaining scheduling uncertainty are
kept only in ignored runtime evidence, never fixtures.

Timestamped receipts also cover Claude's wrapped enqueue/remove entries and
Antigravity's individual USER_INPUT steps, including multiple inputs in one
execution. Stale or untimed history never confirms a new attempt. Antigravity
uses the existing source-read-only in-memory SQLite/WAL snapshot.

Malformed journals are human-visible unknowns and never reconstructed or resent.
They hold that seat's door; intact receipts for other seats still reconcile.

# Run a cascade

Copy this whole template to a ship folder of its own. From an installed ship copy it is at
`runtime/venv/share/quarterdeck/ships/cascade`; in a checkout it is `ships/cascade`.
The desk edits the copied ship file to choose its three providers, models and credential
pools. All three pools are native subscription examples: Claude, Codex and Grok, with Claude
as orchestrator. The credential directories are editable examples. Before `qd up`, the desk
must point the Grok pool at its native subscription directory containing `auth.json` and
replace `REPLACE_WITH_AVAILABLE_SUBSCRIPTION_MODEL` with a model available on that subscription.
This template claims no API model or live model availability. No compiler or `qd raise` is
needed. All seats run in the crew session.

```
qd --ship /path/to/my-cascade up
qd --ship /path/to/my-cascade post question --to orchestrator "How should we plan a garden?"
```

The orchestrator starts that question with `qd cascade --re <question-id>`. An operator may
also run `qd --ship /path/to/my-cascade cascade --re <question-id>` to start it explicitly.
Use the full or short question identity printed by `qd post`; retries recover the same run.
`--efforts low,high` selects fewer declared laps in the named order.

The start command prints the exact per-question path. Follow that file, for example:

```
tail -f /path/to/my-cascade/runtime/ring/<full-question-id>/responses.md
```

It grows by one bounded note per seat and lap, with requested and actual/unknown effort.
The start command creates the file before it returns, so `tail` can open it immediately.
Each question has its own file. Nothing is written into an external workspace. Ordinary
reports, journals and notes remain in the ship runtime and are never committed.

Watch three seats take turns; the orchestrator receives no intermediate reports. At the
end it gets one service notice and gives the question's sender one conclusion. A held limit
with no eligible destination, a failure tied to the current order, or a positively
dead owned pane ends the run early with its location named. Missing panes and unreadable
evidence are unknown; a failed older turn cannot stop a new lap. Late replies
cannot restart it. An interrupted child order stays open until its addressee reports.
On a recognized refusal of the current step, the final notice names the seat/lap, notes
path and report identities for a person to open, distinguishing last completed from any
stopped step's filed report. A known exclusion stops before report acceptance or relay,
leaving a filed report immutable without appending its note. It withholds the full answer
and refused input. The orchestrator concludes from completed notes only, names the stop,
and never automatically opens or retypes the referenced full reports. Other stops retain
the last full answer. Unreadable run evidence is held and shown in operational status;
it does not become a successful completion.

Effort changes use the program's existing confirmed in-session control. Where that control
is unavailable, unconfirmed or interrupted, the lap keeps the current effort and says so in
the notes and final service notice; the orchestrator includes these limitations in its answer.
No restart is invented to make an effort change appear successful. Fake drydock validation
uses three fake seats; the real template's provider/effort behavior still needs a separately
released live proof on installed programs before a demonstration.

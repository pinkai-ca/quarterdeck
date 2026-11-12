# Ship's orders: cascade

One question runs at a time. The orchestrator starts the ring with `qd cascade --re <question>`.
The service sends each whole report to the next seat, in declared seat and effort-lap order.
Ring seats reply only to their current order, which came from `ship`; they never send an
intermediate report to the orchestrator. The service alone writes each bounded note to
`runtime/ring/<question>/responses.md`. Seats do not write that file.

Keep your conversation between laps. Finish the current report before taking a new order.
Each report preserves the original question and a complete answer, so the next seat can
improve it without reading another seat's files. Do not change another seat or start a ship.

An unproven effort control leaves the current effort in place; its actual or unknown effort
is stated in the notes. A held limit with no eligible destination, a positively dead owned
pane, or a recorded failure associated with the current order ends the ring early. A missing
pane is unknown; a failed older turn cannot end a new lap.
An interrupted order remains open under ordinary post rules until its addressee reports;
the service never forges that report or resumes an ended cascade from a late reply.

The orchestrator receives one final service notice and answers the original question once.
Ordinary completion and other stops include the last full answer. On a recognized provider
refusal of the current step, the notice withholds both the full answer and refused input;
it names the seat/lap, notes path and report identities for a person to open, distinguishing
the last completed report from a report filed by the stopped step. Known exclusion wins
before report acceptance: a stopped step's filed report stays immutable, without a note
append, accepted-step marker or relay to the next seat. Unknown exclusion evidence holds.
The orchestrator then concludes from completed notes only and never automatically reads or
retypes those referenced full reports. Only its answer closes the original question.

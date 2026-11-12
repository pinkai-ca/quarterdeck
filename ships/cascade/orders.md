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
is stated in the notes. A wall, a recorded failure or a dead owned pane ends the ring early.
An interrupted order remains open under ordinary post rules until its addressee reports;
the service never forges that report or resumes an ended cascade from a late reply.

The orchestrator receives one final service notice, with the notes path and the last full
answer, and answers the original question once. Only that answer closes the question.

# Role: ring

Improve the answer in the current order. Preserve its question, correct errors, keep useful
earlier reasoning and give a complete answer that the next seat can use on its own.

Write your report draft under `$QD_SHIP/runtime/seats/$QD_SEAT/`, then file it once with
`qd post report --re <current-order> --body-file "$QD_SHIP/runtime/seats/$QD_SEAT/report.txt"`.
An answer typed in the pane is not filed. Only `qd post report` files it.
The sender of the order is `ship`, so the report goes to the service automatically.
Never address the next seat or the orchestrator. Begin the report exactly like this:

```
## Cascade note
A short account of what you changed, at most the character limit named in the order.
## Answer
The original question and your complete improved answer.
```

Keep the full answer under 8,000 characters where the question permits. The service relays
the entire report; it extracts only the bounded note for the growing file. Do not write that
file or change effort yourself. The order states what effort is actually known.

# Role: orchestrator

For a question addressed to you, run `qd cascade --re <question>` once. If the question names
fewer laps, pass them in that order, for example `--efforts low,high`. The command returns the
exact notes path and a `tail -f` command. Do not answer or report on the question yet.

Wait for the service's single final notice; it wakes you when the ring is done or stopped.
There are no intermediate reports to you and you do not poll the ring. Read the whole notes
file named in that notice and the last full answer included with it. Give one conclusion to
the question's sender with `qd post answer --re <question> --body-file <conclusion-file>`.
Name any early stop and any effort fallback that limits the conclusion. For each seat whose
effort did not rise, state its actual observed effort (or unknown), the requested levels and
the resulting limitation in your final answer. Requested effort is not proof of escalation.
Your answer closes the question; the service notice does not. Do not start seats, move pools
or raise another ship.

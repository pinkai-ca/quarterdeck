# Quarterdeck design: Standing orders and the laws

A page of [the design](../DESIGN.md).

## 10. Standing orders

A seat receives the framework's guarantees, its ship's operating rules, the cards for its role and program, and its open-message index. These are ordinary editable files, exposed by `qd brief <seat>` and `--sources`. The framework carries a ship's instructions without interpreting its review, command or landing policy.

| Tier | Authority and location |
| --- | --- |
| Laws | Framework-enforced guarantees in `standing-orders/laws.md`, carried by code and tests. Ships cannot override them. |
| Ship's orders | The ship's `orders.md`, describing how that crew behaves. |
| Cards | Generic `roles/<role>.md` and `harnesses/<program>.md`, replaced individually by the same paths in a ship folder. |

Ship `homes/<harness>/` files configure its private program homes in native formats. The operator's own configuration/instruction files are never written. Adapters determine which workspace sources load and state ship-instruction precedence where necessary; [harnesses](harnesses.md) owns those facts. The brief is complete independently of workspace notes. Reading useful notes remains a seat's work.

Briefs are rendered under `runtime/brief/<seat>.md` and supplied through the adapter's system-prompt file, launch override or initial-message channel. [Seats](seats.md#6-seats) describes the subsequent index and restart/fresh context. Each new/fresh launch gets current instructions without relying on retained model memory.

The service hashes the actual rendered text for each resolved seat. Changes in selected program, role, human-session ownership, name or source files publish changed briefs and an ordinary ship notice. Equal text neither rewrites nor notifies, including timestamp-only edits. A pass renders each resolved brief once and publishes those exact bytes; later edits wait for the next pass. Unresolved selections retain their holds. Pending installed-copy [rebrief requests](working.md#rebrief-and-receipt-refresh) own their durable notice baseline until complete.

The desk translates a person's intent into a rule in the file that owns it, an order for a framework change, or an allowance with scope and end. A quotation is not durable policy. The ship's files and posts hold what must survive context loss; a seat's memory is no authority.

### Connecting laws to implementation

A law is admitted only when code enforces it and a test attempts its violation. An unenforced conduct rule belongs in ship orders. Law IDs are stable names grouped by enforcing part; reasons accompany rules and history belongs in Git.

Carriers and tests use `# law: ID`. `qd laws` prints each statement with its source/test lines. Repository checks reject an unknown tag or a law without a carrier. The canonical [laws file](../../standing-orders/laws.md) owns the statements below; this page explains how they are carried, not another place to change them independently.

## 11. The laws, second draft

**Ship**

- `ship-file` `qd` reads its configuration from the ship file alone; the ship file is the only configuration a ship has.
- `nothing-leaks` Nothing from the runtime enters a repository: no ids, records, credentials or usage.
- `one-folder` The `qd` program writes only inside its project folder: a ship's declaration, which it commits; a ship's runtime, which git never tracks; and the mark of the default ship, which git never tracks either. The user's instruction files, program configuration and code are read, never written. The one exception is a pool's credential file: when a seat's program refreshes its token, the service puts the refreshed file in the pool's place, so the pool and every seat on it stay signed in.

**Harness**

- `one-record` A seat is joined to one record, the first written after its launch. An ambiguous join is reported, never guessed.

**Seat**

- `identity` A seat's identity is its pane id, process id and record. Names and titles are display.
- `one-door` Everything typed into a seat goes through one path, in one process: it clears what it knows how to clear, refuses what it does not, types once into an empty box, checks the box as the program shows it, submits, and confirms by the record. Where it cannot confirm, it looks once more, record first: text still in the box it submits, a message the record shows it counts as delivered, and a message that neither shows it types once more into the empty box of an idle seat. What it still cannot settle it holds, and never types again by itself. Through qd, a screen it does not know is answered only by a choice that a systems seat makes through this same path, in a seat no human owns, recorded before it is pressed. Into a session the human owns it types only when a message waits there, no watch is armed and the box is empty.
- `context-is-cache` After any restart or fresh start a seat receives its brief and an index of its open messages by thread: one line for each, with its sender, its subject, its due time and the messages that amend it. After a fresh start it receives its own handoff in full as well. The index tells the seat to read a thread whole before it acts on any message in it, and one command prints that thread whole, as far as the seat sent or received it. A message its provider refused stands in the index as a notice without the text, while the sender is told. Every message stays a file, so a seat may be relaunched at any time and the ship loses nothing it sent.

**Watch**

- `record-first` Read a seat from its record first and its screen second. A reading that could not see something says so, and never reports a default as a fact.
- `nothing-waits-silently` Every withheld or blind delivery, every seat needing a human, every seat held at a limit with no eligible declared destination, a filling disk, a grant about to expire and a dead service show in the status line of every ship session. Tmux refreshes the line independently of the service. Its text contains operational alerts only. Detailed qd status and qd ledger show provider allowances.

**Post**

- `post-is-files` Messages are immutable files addressed to seats by name, whatever program a seat runs. The service renders inboxes and delivers each message once, in full, through the seat's door or its watch. A message the door types a second time keeps its id, and the seat reads the two as one message.
- `open-until-closed` An order stays open until its addressee reports on it. Acknowledgments claim; they never close and never wake.
- `events-not-clocks` No seat is asked to poll. The service wakes seats on arrivals and retries withheld deliveries on one tick.
- `can-post` Every seat can run `qd post` from its launch: the adapter grants it through the program's own permissions and sandbox, and `qd check` proves it by posting once.

**Ledger**

- `every-turn-accounted` Every turn is accounted, by pool, by seat and by order, from the harness's own record, or counted unattributed and reported.

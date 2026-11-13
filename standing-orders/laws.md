# The laws

What the framework itself guarantees, enforced in code and tests. A rule nothing enforces is an order, not a law. Each law is tagged where the code carries it (`# law: <id>`) and where a test tries to break it; `qd laws` lists both.

## Ship

- `ship-file` `qd` reads its configuration from the ship file alone; the ship file is the only configuration a ship has.
- `nothing-leaks` Nothing from the runtime enters a repository: no ids, records, credentials or usage.
- `one-folder` The `qd` program writes only inside its project folder: a ship's declaration, which it commits; a ship's runtime, which git never tracks; and the mark of the default ship, which git never tracks either. The user's instruction files, program configuration and code are read, never written. The one exception is a pool's credential file: when a seat's program refreshes its token, the service puts the refreshed file in the pool's place, so the pool and every seat on it stay signed in.

## Harness

- `one-record` A seat is joined to one record, the first written after its launch. An ambiguous join is reported, never guessed.

## Seat

- `identity` A seat's identity is its pane id, process id and record. Names and titles are display.
- `one-door` Everything typed into a seat goes through one path, in one process: it clears what it knows how to clear, refuses what it does not, types once into an empty box, checks the box as the program shows it, submits, and confirms by the record. Where it cannot confirm, it looks once more, record first: text still in the box it submits, a message the record shows it counts as delivered, and a message that neither shows it types once more into the empty box of an idle seat. What it still cannot settle it holds, and never types again by itself. Through qd, a screen it does not know is answered only by a choice that a systems seat makes through this same path, in a seat no human owns, recorded before it is pressed. Into a session the human owns it types only when a message waits there, no watch is armed and the box is empty.
- `context-is-cache` After any restart or fresh start a seat receives its brief and every open message in full, except a message its provider refused, which it receives as a notice without the text while the sender is told; so a seat may be relaunched at any time and the ship loses nothing it sent. Where the open messages are too long for a seat's first message, that message gives a short account of each and names a file that holds them in full, which the seat is told to read whole before it acts.

## Watch

- `record-first` Read a seat from its record first and its screen second. A reading that could not see something says so, and never reports a default as a fact.
- `nothing-waits-silently` Every withheld or blind delivery, every seat needing a human, every seat held at a limit with no eligible declared destination, a filling disk, a grant about to expire and a dead service show in the status line of every ship session. Tmux refreshes the line independently of the service. Its text contains operational alerts only. Detailed qd status and qd ledger show provider allowances.

## Post

- `post-is-files` Messages are immutable files addressed to seats by name, whatever program a seat runs. The service renders inboxes and delivers each message once, in full, through the seat's door or its watch. A message the door types a second time keeps its id, and the seat reads the two as one message.
- `open-until-closed` An order stays open until its addressee reports on it. Acknowledgments claim; they never close and never wake.
- `events-not-clocks` No seat is asked to poll. The service wakes seats on arrivals and retries withheld deliveries on one tick.
- `can-post` Every seat can run `qd post` from its launch: the adapter grants it through the program's own permissions and sandbox, and `qd check` proves it by posting once.

## Ledger

- `every-turn-accounted` Every turn is accounted, by pool, by seat and by order, from the harness's own record, or counted unattributed and reported.

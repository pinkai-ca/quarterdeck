# Quarterdeck design: Standing orders and the laws

A page of [the design](../DESIGN.md).

## 10. Standing orders

Three tiers of document, each in its own file, each growing at its own pace, none allowed to become another:

- **Laws**, `laws.md`: what the framework itself guarantees, enforced in code and tests. One page, few by construction. They change rarely.
- **Ship's orders**, the ship's own `orders.md`: how this crew behaves. The ship changes them at will. The framework carries them to every seat and does not read them.
- **Cards**, `roles/<role>.md` and `harnesses/<name>.md`: facts a seat needs about its role (accountable for, may change, call when, never) and about its own program. They change with the crew and with program versions. The generic role cards are captain, desk, engineering, worker and hand; the hand is a worker on a small model served locally, told how to work an order step by step, to verify before it reports, and to ask rather than guess.

A seat's boot read is the laws, its ship's orders, its role card, its harness card and its inbox. Target: under 3,000 words, against about 21,000 today. `qd brief <seat>` prints exactly what that seat reads at boot, so a human can read what the agent read.

**Ours, and exposed.** The brief is Quarterdeck's own instruction set, independent of whatever `CLAUDE.md` or `AGENTS.md` a user keeps, and every word of it comes from a file the user can edit. The framework's `standing-orders/` folder holds `laws.md` and the generic cards. A ship's folder holds its `ship.toml` and `orders.md`, and may hold any card under the same path, `roles/<role>.md` or `harnesses/<name>.md`, which then replaces the framework's for that ship, and `homes/<harness>/`, the program's own settings files as this ship wants them, in the program's own format. To change what every seat reads, edit `orders.md`; what one role reads, its role card; what one program is told about itself, its harness card. The laws are the framework's, and the one part a ship does not override. Nothing is hidden in code: `qd brief <seat> --sources` prints which file each part of the brief came from, and names any instruction file a program will still load from elsewhere, for the programs that have no switch to stop it. Changing them is editing a file: the Desk, carrying the operator's word, edits `orders.md` or a card as it would any other file, and who may do so is a matter for the ship's orders, not the framework. An edit is an event like any other: the service re-renders the briefs it touches and tells each affected seat with an ordinary message from `ship`, `qd > ship's orders changed · read brief/scott.md`, and a seat coming up or starting fresh reads the new brief by itself.

The service's brief stamp hashes the text produced by the ordinary brief renderer for each resolved seat. It therefore follows ship name, operator and folder, seat name, role and display name, selected program and its human-tool policy, session ownership, and the laws, orders and selected cards, including fallback text. Moving a seat between crew and human sessions, or changing its session's human flag, refreshes the file on the next service pass without a card edit. Unchanged text does not rewrite the file or post a notice, including card timestamp changes and edits to unrelated ship settings. An unresolved selection keeps its existing hold and cannot acknowledge a complete pass. Each stamp pass reads and renders each resolved brief once in memory and reuses that exact text for publication; edits arriving later remain visible on the next pass. A seat resolved only after the stamp also waits for that next pass. This adds no timer or store. Pending upgrade rebrief requests retain publication ownership and their durable notice baseline.

**Each ship, its own orders.** A ship carries the orders and the cards for its own crew: a ship of three reads three cards about its three roles, and a ship of twelve reads its own. The framework's `standing-orders/` folder holds the generic cards a ship starts from and keeps or replaces card by card. The default ship's orders say how its crew lands code: the captain reviews a change once, reproducing what it doubts, and the desk lands it when the review is clear and the suite is green.

**The operator's word becomes the ship's.** The operator speaks briefly, often from a phone, and may be wrong in a detail; a quotation of what they said is not a rule. The desk asks until the intent, its scope and its implications are clear, then writes the rule in its own words, clean and unambiguous, in the file that owns it: the ship's orders for how the crew behaves, a role card for one role, the ship file for one seat. A change to the framework goes to the captain as an order, and a dispensation for the matter at hand is an order that names its scope and its end. Nothing durable lives in a seat's memory or context, and a seat's home runs its program with its own memory off, so the ship's files are the only place a rule can be and the only place anyone need look for one.

**How the brief reaches a seat, and nothing else does.** Every program loads instruction files of its own. Claude Code reads every `CLAUDE.md` from the working directory up to the root and combines them, root-most first, with the one in its home; checked on 7 November 2026 with three nested folders, all three were loaded. Codex reads `AGENTS.md` the same way, and the others have their equivalents. A ship's seats use the ship's instructions and nothing else. Claude Code is launched with `--setting-sources user`, which the same probe showed loads no instruction file from the working directory or its parents, and its home is ours; Codex is launched with its own switch for the same, pinned in the spike; where a program has no such switch, the brief states the precedence in one sentence: where these orders and a file the program loaded disagree about the ship, the orders win. The brief tells a seat where the workspace's own notes live, and reading them is the seat's work like reading any other file. Quarterdeck never writes a user's instruction files and never relies on them: the brief is complete on its own, and the fake harness, which loads no instruction files at all, is fully briefed by it. The brief is rendered to `brief/<seat>.md` in the runtime and reaches the seat by the channel its adapter declares: a file appended to the program's system prompt where the program takes one on its command line, which costs no turn; a configuration override where that is the program's way; or as the first message otherwise. In every case the seat's open messages follow, under a header saying the seat was briefed and with what: `qd > briefed as scott, engineering · 2 orders open`. The consequence worth wanting: ship doctrine that lives in a person's instruction files today, because there was nowhere else to put it, moves into the ship's orders, and those files go back to being about the person and the machine.

What keeps the laws few is not a number but a test of admission: a law exists only if the framework enforces it in code and tests. A rule nothing enforces is an order, not a law. A framework of a few thousand lines holds only so many such guarantees, so the page stays short by construction. If it ever needed a second page, the framework would have grown, and that is the thing to look at, not the page. The other habits: a law's statement is one or two sentences, its reason one sentence beside it, its history in git; laws have ids, not numbers, so retiring one never renumbers the rest; and the laws are grouped by the part that enforces them, so a reader who knows the parts knows where each law lives.

The connection to code is a comment and a command. Wherever code carries a law, the line is tagged with the law's id:

```python
# src/quarterdeck/door.py
def deliver(seat, text):
    clear_known(seat)                                # law: one-door
    paste(seat, text)
    shown = screen(seat).box
    if not matches(harness(seat).box(text), shown):  # law: one-door
        return Withheld(seat, text, shown)
    submit(seat)
    return confirm(seat, text)                       # law: one-door
```

The same tag goes on the tests that prove it, and the test tagged with a law tries to break it and fails to. `qd laws` finds every tag and prints each law with the lines that carry it:

```
one-door   Everything typed into a seat goes through one path, in one process: it clears what it ...
           src/quarterdeck/door.py:158, 176, 190   tests/integration/test_door.py:40
```

A test fails when a law has no carrier, or a tag names a law that does not exist. Reading the law, you can jump to the code that enforces it. Reading the code, you know why a check is there.

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

Retired from the first draft: "stalled means it moved and then stopped" is the Watch's definition of stalled; "walls are normal" is advice on conduct and belongs in a ship's orders; "the framework carries no law about code" is the scope statement in [section 1](../DESIGN.md#1-what-it-is), and nothing in code enforces it, so it is not a law. Restated after the review of 6 November: `ship-file` promised a runtime derived from the file and a file never edited by hand, neither of which code can enforce; `context-is-cache` promised what agents put in their posts; `events-not-clocks` forbade the clocks the Watch runs and the ticks the service needs, since the standard library has no file watcher; `every-turn-accounted` denied the unattributed turns [section 7](post.md#7-post) admits. Two added: `nothing-waits-silently` and `can-post`. Declined: a wall law, because a wall is a fact to route around and the status line then showed it; a human-first law, because `one-door` already never types into a person's text. Renamed on 7 November: `exact-join` became `one-record`, because the word exact read as a demand for exact program versions, and versions are best effort by design; the law was always about the record.

The flagship ship's orders begin: the Admiral's word reaches the bridge through the Desk; the Captain turns it into orders; no change reaches the ship's code until independent agents have reviewed it, it has passed its tests, and the First Officer has signed it.

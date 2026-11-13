# The laws

Quarterdeck runs AI coding agents as one crew, called a ship. The fourteen laws are what Quarterdeck itself guarantees: each law is enforced in code, and has a test that tries to break that law. A rule that nothing enforces is a standing order, not a law.

| Id | In short |
| --- | --- |
| `ship-file` | The ship file is the only configuration a ship has. |
| `nothing-leaks` | Nothing from the runtime enters a repository. |
| `one-folder` | `qd` writes only inside its project folder. |
| `one-record` | A seat is joined to one record; an ambiguous join is reported, never guessed. |
| `identity` | A seat's identity is its pane id, process id and record. |
| `one-door` | Everything typed into a seat goes through one path, and is confirmed by the record. |
| `context-is-cache` | A seat may be relaunched at any time and the ship loses nothing it sent. |
| `record-first` | A seat is read from its record first and its screen second. |
| `nothing-waits-silently` | Anything withheld, walled or waiting on a human shows in every status line. |
| `post-is-files` | Messages are immutable files addressed to seats by name. |
| `open-until-closed` | An order stays open until its addressee reports on it. |
| `events-not-clocks` | No seat is asked to poll. |
| `can-post` | Every seat can post from its launch, and `qd check` proves it. |
| `every-turn-accounted` | Every turn is accounted, by pool, by seat and by order. |

A seat is one agent. [The parts of Quarterdeck](PARTS.md) explains the other words, and [standing-orders/laws.md](../standing-orders/laws.md) states each law in full.

## A law from statement to test

The lines of code and test that carry a law are marked `# law: <id>`. Following `one-door`:

**1. The full statement.** "It" is the one path; the box is the input box of the agent's program. In a session the human owns, such as `Office`, the agent keeps a watch for messages.

> Everything typed into a seat goes through one path, in one process: it clears what it knows how to clear, refuses what it does not, types once into an empty box, checks the box as the program shows it, submits, and confirms by the record. Into a session the human owns it types only when a message waits there, no watch is armed and the box is empty. Never resend what you cannot confirm.

**2. The code.** `Door.deliver`, in [src/quarterdeck/door.py](../src/quarterdeck/door.py), types a message into an agent's pane:

```python
return self.confirm(text)  # law: one-door
```

A message counts as delivered only when the record, the program's log of the conversation, shows the message.

**3. The test.** The foreign-draft test, in [tests/integration/test_door.py](../tests/integration/test_door.py), puts someone else's text in the box, then tries to deliver a message:

```python
assert delivery.result == "withheld" and "text in the box" in delivery.detail  # law: one-door
```

The test also checks that the text stays untouched and nothing is submitted.

**4. Every marked line.** From the folder you cloned at [Quick start](../README.md#quick-start):

```sh
grep -rn 'law: one-door' src tests
qd laws  # every law with the lines of code and test that carry it
```

Any law's id works in place of `one-door`. [Design decisions](DECISIONS.md#everything-typed-goes-through-one-door) gives the reasoning.

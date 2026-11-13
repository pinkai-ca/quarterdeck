# The parts of Quarterdeck

Quarterdeck, run as `qd`, runs AI coding agents from different companies as one crew.

- **Ship.** One crew of agents, with the crew's orders and records. One file, `ship.toml`, names the tmux sessions, the seats in each, and every seat's role, harness, pool, model and persona (display name).
- **Seat.** One agent: one harness running in one tmux pane under one role, such as captain. Each seat has a home folder of its own, built from the ship's files and nothing of yours.
- **Harness.** The terminal coding program a seat runs, such as Claude Code or Codex.
- **Provider.** The company whose models or plans a pool uses, such as Anthropic or OpenAI.
- **Pool.** One way of paying a provider, and what a harness runs on: a subscription, an API key, or an endpoint (a model served at a URL, local or remote, with no quota and no sign-in of its own).
- **Post.** A ship's messages: orders, reports, questions, answers and acknowledgments, each a file that never changes. Quarterdeck types each message in full into the addressed agent's pane, except in `Office`, the session you work in, where the agent waits for messages itself.
- **Watch.** Quarterdeck's reading of what an agent is doing, from the harness's record of the conversation first and the pane's screen second.
- **Ledger.** The account of every turn by pool, by seat and by order: the turn's tokens and tool calls, and what the turn took of your machine in processor time, memory and disk.
- **Orders.** An order is a message asking an agent for work. Standing orders are the file `orders.md`: how the crew works, such as who reviews a change. Quarterdeck hands every agent the standing orders and enforces none.
- **Laws.** Fourteen guarantees Quarterdeck does enforce, each in code, each tried by a test, such as `open-until-closed`: an order stays open until its addressee reports on it. `qd laws` lists all fourteen.

[README](../README.md), [Ships](SHIPS.md), [Harnesses](HARNESSES.md), [The laws](LAWS.md), [Design decisions](DECISIONS.md).

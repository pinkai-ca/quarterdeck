# Harnesses

A harness is the terminal coding program an agent runs in. Quarterdeck runs each harness exactly as installed, in the harness's normal terminal interface.

| Harness | [Runs on](#what-a-harness-runs-on) | Proven live by `qd check` | In daily use |
| --- | --- | --- | --- |
| Claude Code (Anthropic), `claude` | an Anthropic subscription or API key | yes, on a subscription | yes |
| Codex (OpenAI), `codex` | an OpenAI subscription or API key | yes, on a subscription | yes |
| Grok Build (xAI), `grok` | an xAI subscription; an API key, not yet proven live | yes, on a subscription: [details](GROK.md) | not run |
| Antigravity (Google), `antigravity` | Antigravity's own sign-in | yes, on Gemini 3.8 Flash | not run |
| OpenCode, `opencode`, and pi, `pi` | a local model, OpenRouter with an API key, or any other endpoint | yes, on a local vLLM server and on OpenRouter | yes, on local models |

- **`claude`** and the like: the name to write in `qd check`, and as `harness` in the ship's file.
- **Proven live by `qd check`**: `qd check` started the installed harness and took the harness through [a short task](#what-a-live-check-proves).
- **In daily use**: agents work in the harness every day, in the crew that builds Quarterdeck.
- Muse (Meta) is being added, and is not in the Quarterdeck code.

## On your own machine

With a ship ([Quick start](../README.md#quick-start)):

```sh
qd doctor        # each installed harness's version, and whether your ship has proven that version
qd check claude  # prove one harness live on your ship, here Claude Code; spends a few turns
```

An unproven version of a harness still runs, on the newest facts Quarterdeck knows about the harness.

## What a harness runs on

A harness runs on a pool, one way of paying:

- **A subscription** you are signed in to, used only inside the harness of the company that sells the subscription.
- **An API key.** Claude Code, Codex and Grok Build each take their own company's key; Antigravity takes none. Every key lives in one file of yours, `~/.config/quarterdeck/keys.toml`, one line each, such as `anthropic = "<your key>"`, and a pool names it: `api_key = "keys:anthropic"`. A key beside an `endpoint` travels only over https, or over http to the machine's own loopback, and an endpoint's address holds no credentials, query or fragment. [What has been proved on a key](#what-has-been-proved-on-an-api-key) is short.
- **An endpoint**: a model served at an OpenAI-compatible URL, local or remote, such as a local model, or OpenRouter with an API key. The open harnesses, OpenCode and pi, take compatible endpoints, with an optional key from the same file.

## In the ship's file

[A ship of your own](SHIPS.md#a-project-ship-of-your-own) shows how to write the ship's file, `ship.toml`, where an agent is a seat. [The parts of Quarterdeck](PARTS.md) explains the other words.

- A pool's `provider` is the company's identifier, such as `anthropic`, `openai`, `google` or `xai`, and for an endpoint, whoever operates the endpoint, whichever company made the model.
- A seat's `pool` is one pool's name, or an ordered list, whose entries may name another harness and model. When a subscription is spent, the agent moves on to the next entry, and stays there while the agent runs. The agent's next launch takes the first entry that has room. `qd set <seat> pool=<choice>` moves a running agent by hand, and for an agent that is down chooses the pool of the agent's next launch. `qd up <seat> --pool <choice>` chooses the pool for one launch.

## Special cases

- **A local model or other endpoint.** The agent runs in a bubblewrap jail unless the ship's file says otherwise: your home folder hidden, the agent's working folder writable, and `qd post`, the agents' message command, working.
- **Antigravity** cannot keep state in another folder, so the agent runs in the Antigravity state you use yourself. [The Antigravity record](ANTIGRAVITY.md) says how Quarterdeck tells the agent's conversation from yours.
- **Antigravity's survey about itself**, which Antigravity shows after some turns in place of the input box, gets no rating. Quarterdeck skips the survey by Antigravity's own choice for that, on every ship; `qd watch` shows the press. For an agent in your own session Quarterdeck presses nothing.
- **Grok Build** trusts the whole repository that holds the agent's working folder, as xAI's trusted folders do, so that repository's instructions, hooks, MCP servers and LSP processes load or start. [The Grok Build contract](GROK.md) says what is measured and what is not.
- **A brand-new Grok Build home** opens on xAI's question about training data. Quarterdeck answers by opting out, on every ship, and never opts in; `qd watch` shows the answer. For an agent in your own session, such as your desk, Quarterdeck presses nothing: answer the question there yourself.

## What a live check proves

Pi 0.79.0 needs five pane rows for its empty box; an unfolded payload needs four more rows than its wrapped text (including a final newline's cursor row), and over five text rows also need enough pane height for pi's 30% editor viewport; large pastes fold to one row.

Each harness proven live launched, took a long paste that the harness's own record confirmed, wrote a named file, posted a report with `qd post`, and had the turn read by Quarterdeck.

- Every check ran on the ship that builds Quarterdeck: one ship, one person, that person's own hardware.
- Each harness is proven as one agent's harness in a crew. A whole crew on one harness alone has been run once, on Claude Code with an Anthropic key, as the next section says. On any other harness it should work by design, and has not been run yet.

## What has been proved on an API key

Live, with a real key, on 21 November 2026:

- **Claude Code 2.1.317** on an Anthropic API key: one live call, on `claude-haiku-5-5`.
- **OpenCode 1.19.5 and pi 0.79.0** on an Anthropic key, through Anthropic's OpenAI-compatible address: one live call each, on `claude-haiku-5-5`, each in its jail.
- **A whole crew on one provider**, the default setup: desk, captain and engineer all on Claude Code on an Anthropic key, raised with `qd setup`, carried one small change from its order to its landing on `claude-sonnet-5-5`. On `claude-haiku-5-5` the desk did the work itself and never ordered the crew.

No other provider's key is proven live yet.

## For maintainers

A ship's file in the older spelling, a company's own harness named as a pool's `provider`, is still read. `qd doctor` names the exact edits, and [Design](design/ship-file.md#4-the-ship-file) gives the order to make the edits in.

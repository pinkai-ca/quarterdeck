# Harnesses

A harness is the terminal coding program an agent runs in. Quarterdeck runs each harness exactly as installed, in the harness's normal terminal interface.

| Harness | [Runs on](#what-a-harness-runs-on) | Proven live by `qd check` | In daily use |
| --- | --- | --- | --- |
| Claude Code (Anthropic), `claude` | an Anthropic subscription or API key | yes, on a subscription | yes |
| Codex (OpenAI), `codex` | an OpenAI subscription or API key | yes, on a subscription | yes |
| Grok Build (xAI), `grok` | an xAI subscription or API key | yes, on a subscription: [details](GROK.md) | not run |
| Antigravity (Google), `antigravity` | Antigravity's own sign-in | yes, on Gemini 3.8 Flash | not run |
| OpenCode, `opencode`, and pi, `pi` | a local model, OpenRouter with an API key, or any other endpoint | yes, on a local vLLM server and on OpenRouter | yes, on local models |

- **`claude`** and the like: the name to write in `qd check`, and as `harness` in the ship's file.
- **Proven live by `qd check`**: `qd check` started the installed harness and took the harness through [a short task](#what-a-live-check-proves).
- **In daily use**: agents work in the harness every day, in the crew that builds Quarterdeck.
- Muse (Meta) is being added, and is not in the Quarterdeck code.

## On your own machine

The [Quick start](../README.md#quick-start) lists the tested environment and shows how to set up a ship. Once your ship is set up, you can check the harnesses installed on your machine.

```sh
qd doctor        # each installed harness's version, and whether your ship has proven that version
qd check claude  # prove one harness live on your ship, here Claude Code; spends a few turns
```

An unproven version of a harness still runs, on the newest facts Quarterdeck knows about the harness.

To select a specific Claude Code executable for a ship, copy the release into its `runtime/programs/` folder and declare its path and SHA-256 in [`[programs.claude]`](design/ship-file.md#program-executables). `qd doctor` shows the executable and `qd check claude` proves that copy. When changing releases, copy the next executable to a new path, update the path and digest together, and check it. Run `qd restart <seat>` when the seat is ready to restart. It uses the new executable and resumes the saved conversation.

For Claude Code 2.1.331, Quarterdeck checks named effort against the program's canonical CLI vocabulary of `low`, `medium`, `high`, `xhigh` and `max`. Claude Code governs which levels are available for each model.

On Claude Code 2.1.331, a live `qd set` model or named effort request waits for the matching native command result in that seat's record. A model cache warning can receive one Enter when the owned pane shows the full, unwrapped panel for the requested model with the measured true-colour focus on Yes. The typed model command drew this panel at 120×50, 273×65 and 319×78. A live run at 319×78 sent one confirmation Enter and confirmed the linked result naming Sonnet 5.5. Direct effort changes were confirmed by the native result. An effort confirmation dialog holds for a person.

## What a harness runs on

A harness runs on a pool, one way of paying:

- **A subscription** you are signed in to, used only inside the harness of the company that sells the subscription.
- **An API key.** Claude Code, Codex and Grok Build each take their own company's key; Antigravity takes none. Every key lives in one file of yours, `~/.config/quarterdeck/keys.toml`, one line each, such as `anthropic = "<your key>"`, and a pool names it: `api_key = "keys:anthropic"`. A key beside an `endpoint` travels only over https, or over http to the machine's own loopback, and an endpoint's address holds no credentials, query or fragment. See [what has been proved on an API key](#what-has-been-proved-on-an-api-key).
- **An endpoint**: a model served at an OpenAI-compatible URL, local or remote, such as a local model, or OpenRouter with an API key. The open harnesses, OpenCode and pi, take compatible endpoints, with an optional key from the same file.
  At `https://api.openai.com/v1`, OpenCode and pi use the Responses API by default, with function tools and the pool's output allowance for model turns. OpenCode defaults title generation to the selected model, and pi loads a bundled output-limit hook with extension discovery off. Both clients still select a model from the endpoint's `/models` listing. The default for compatible endpoints other than OpenAI and the Google route below is Chat Completions.

  For OpenCode and pi, declare `provider = "google"` and `endpoint = "https://generativelanguage.googleapis.com/v1beta/openai"` with your key reference. That address is used for model discovery and `qd doctor`. An explicitly requested bare model ID can match a `models/<id>` listing entry. Both programs use the matching metadata and send the bare ID to Google's native Gemini API at `https://generativelanguage.googleapis.com/v1beta` for inference.

## In the ship's file

[A ship of your own](SHIPS.md#a-project-ship-of-your-own) shows how to write the ship's file, `ship.toml`, where an agent is a seat. [The parts of Quarterdeck](PARTS.md) explains the other words.

- A pool's `provider` is the company's identifier, such as `anthropic`, `openai`, `google` or `xai`, and for an endpoint, whoever operates the endpoint, whichever company made the model.
- A seat's `pool` is one pool's name, or an ordered list, whose entries may name another harness and model. `qd set <seat> pool=<choice>` moves a running agent by hand, and for an agent that is down chooses the pool of the agent's next launch. `qd up <seat> --pool <choice>` chooses the pool for one launch.

When a provider refuses work at its limit or reports 100% usage, Quarterdeck tries eligible pools after the current one in list order and wraps to the beginning if needed. It moves the seat at the next idle break. A running seat stays on its subscription while it has room, even if an earlier subscription resets. It can return to an earlier subscription the next time it reaches a limit or when it relaunches.

On each automatic launch, Quarterdeck checks pools in list order and chooses the first with known room. If it reaches a usable saved pool whose allowance is unknown, it can choose that pool even if a later one has known room.

A key selected automatically gives way at idle to the first earlier subscription known to have room. An explicit key choice applies for that launch while the key can pay. A same-provider return keeps the conversation when Quarterdeck can verify that the program will resume it. A crew seat returning across providers writes a new handoff on the key. The return starts a new conversation with those notes after the report, an idle break and a fresh check that the chosen subscription is still the first earlier one known to have room. See the [automatic failover tests](#automatic-failover-tests).

A key pool's `budget` uses the costs Quarterdeck can price from completed turns. In the key tests, Quarterdeck priced Claude Code turns on Anthropic, read stated costs from Grok Build 1.0.77 on xAI, and priced OpenCode turns on the native Gemini API. When the record gives no usable cost, Quarterdeck can apply the pool's declared rates to measured token counts. For supported Anthropic models and routes it can also use the bundled Anthropic rates. The ledger shows `no cost stated` when it has no priced spend. Where only part of the spend is priced, it marks the total with a `+`. Unpriced usage can still incur charges. Pools that refer to the same key share their measured spend. The ledger tracks spend by pool name, so reusing a name for a different key can carry past spend into the current budget period.

For a key or endpoint pool, put a `price` table under the pool's name in the ship file. All four rates are required and must be finite nonnegative numbers in USD per million tokens. These example values illustrate the format.

```toml
[pools.example-api.price]
currency = "USD"
unit = "million_tokens"
input = 2
output = 4
cache_read = 1
cache_write = 3
```

## Special cases

- **A local model or other endpoint.** The agent runs in a bubblewrap jail unless the ship's file says otherwise: your home folder hidden, the agent's working folder writable, and `qd post`, the agents' message command, working.
- **Antigravity** cannot keep state in another folder, so the agent runs in the Antigravity state you use yourself. [The Antigravity record](ANTIGRAVITY.md) says how Quarterdeck tells the agent's conversation from yours.
- **Antigravity's survey about itself**, which Antigravity shows after some turns in place of the input box, gets no rating. Quarterdeck skips the survey by Antigravity's own choice for that, on every ship; `qd watch` shows the press. For an agent in your own session Quarterdeck presses nothing.
- **Grok Build** trusts the whole repository that holds the agent's working folder, as xAI's trusted folders do, so that repository's instructions, hooks, MCP servers and LSP processes load or start. [The Grok Build contract](GROK.md) says what is measured and what is not.
- **A brand-new Grok Build home** opens on xAI's question about training data. Quarterdeck answers by opting out, on every ship, and never opts in; `qd watch` shows the answer. For an agent in your own session, such as your desk, Quarterdeck presses nothing: answer the question there yourself.

## What a live check proves

Pi 0.79.0 needs five pane rows for its empty box; an unfolded payload needs four more rows than its wrapped text (including a final newline's cursor row), and over five text rows also need enough pane height for pi's 30% editor viewport; large pastes fold to one row.
At 80 columns, the measured pi 0.79.0 xAI footer can clip the final letter of
`thinking off` after a turn. When both editor rules also span 80 columns, the
reader reads the complete model field and leaves effort unset.

Each harness proven live launched, took a long paste that the harness's own record confirmed, wrote a named file, posted a report with `qd post`, and had the turn read by Quarterdeck.

Muse checks wait for this launch's contributor disclosure when its measured startup screen is still drawing. The check saves the frame used for its verdict, when available, in the owner-only `runtime/check/<check-seat>/startup/` directory. `styled.ansi` contains the original capture and `plain.txt` contains the same frame with styling removed.

- Every check ran on the ship that builds Quarterdeck: one ship, one person, that person's own hardware.
- Each harness is proven as one agent's harness in a crew. The [single-provider crew tests](#single-provider-crew-tests) also exercised the whole default crew.

## What has been proved on an API key

Live, with a real key, on 21 November 2026:

- **Claude Code 2.1.317** on an Anthropic API key: one live call, on `claude-haiku-5-5`.
- **OpenCode 1.19.5 and pi 0.79.0** on an Anthropic key, through Anthropic's OpenAI-compatible address: one live call each, on `claude-haiku-5-5`, each in its jail.
- **Codex 0.166.0** on an OpenAI API key made one live call on `gpt-6-luna`.
- **OpenCode 1.19.5 and pi 0.79.0** on an OpenAI API key through `https://api.openai.com/v1` each made one live call on `gpt-6-luna` in its jail.

In a compatibility test on 24 November 2026, **OpenCode 1.19.5** made one live call in its jail on `gemini-3.5-flash-lite` through Google's OpenAI-compatible endpoint. **pi 0.79.0** completed one live turn with a Google API key through the same endpoint, `https://generativelanguage.googleapis.com/v1beta/openai`. The pi retry used both `supportsDeveloperRole=false` and `supportsStore=false`. The cause of the earlier HTTP 400 remains undetermined because the retry tested both flags together.

On 24 November 2026, **Grok Build 1.0.77, OpenCode 1.19.5 and pi 0.79.0** each made one live call on an xAI API key.

On 24 November 2026, **Claude Code 2.1.331** on Z.ai's Messages address and **OpenCode 1.19.5 and pi 0.79.0** on its Chat Completions address each completed one live tool turn on a synthetic task with `glm-5.3` and a Z.ai Standard key. Quarterdeck has no supported Z.ai route for Codex. Its current key route uses native OpenAI login and requires the Responses wire protocol.

On 25 November 2026, **pi 0.79.0 and OpenCode 1.19.5** completed one live read-tool turn each on `gemini-3.5-flash-lite` using a Google API key and the native Gemini API. Each returned Gemini's thought signature with the tool result and answered. The earlier tool-turn attempts on Google's compatibility route failed.

### Manual pool moves

On 21 November 2026, a live Codex seat switched from an OpenAI API key to a Codex subscription and back using `qd down` and `qd up` with a pool choice. It resumed the same conversation and remembered the earlier task after each switch. The test of `qd set` to move to or from a key used a stand-in program.

### Automatic failover tests

The 25 November 2026 live tests used Claude Code 2.1.331 with disposable seats and synthetic prompts.

- **Anthropic subscription to Anthropic key and back.** A key pool can declare a different model. The provider served the turn that produced a 100% usage reading on Fable 5.2. At the next idle moment, Quarterdeck moved the seat automatically to an Anthropic key on Sonnet 5.5. The seat resumed the same conversation and recalled a word from before the move. After a restart on the key, the seat completed one turn on that key before the return. After the subscription reset, the seat returned automatically and recalled the word again in the same conversation. Claude Code can serve a different model from the one requested at launch. In this test it served Opus 5.5 on return, when the launch requested the subscription's declared model, Fable 5.2. Quarterdeck showed Opus 5.5 as the served model. The key spend was $0.16.
- **Anthropic subscription to Z.ai key.** The automatic move to a Z.ai key on `glm-5.3` started a new conversation and carried the handoff. The return failed in this live test, with retries about once a second. Stand-in tests verified that the outbound move can still start immediately with the handoff on file and that the return waits for a new handoff written on the key. Live verification is pending. A person's own seat stays on the key and reports that the desk is needed.

### Single-provider crew tests

The default crew raised with `qd setup` carried one small change from its order to its landing in each of two runs on 21 November 2026. In one run the desk, captain and engineer all used Claude Code on an Anthropic key with `claude-sonnet-5-5`. In the other they all used Codex on a Codex subscription with `gpt-6.2-sol`.

On 25 November 2026, two default crews raised with `qd setup --answers` each carried a small change from order to landing.

- **xAI API key.** The crew used Grok Build 1.0.77 on `grok-4.20-0309-non-reasoning`. In two earlier attempts the crew could not be reached: first the desk could not find the captain's address, then its orders could not be delivered to the Grok seats; in the second, the desk made and landed the change itself, against its instruction. Both causes have since been fixed. In the successful rerun, the crew proceeded while one folded report was held for delivery. The issue that held the report has since been fixed.
- **Google API key.** The crew used pi 0.79.0 and OpenCode 1.19.5 on `gemini-3.5-flash-lite`. While waiting for the crew, the desk polled within one turn and attempted to merge. It reached the call cap shortly after the landing report.
- **Claude Code with `claude-haiku-5-5` in the 21 November test.** The desk did the work itself and never ordered the crew.

## For maintainers

A ship's file in the older spelling, a company's own harness named as a pool's `provider`, is still read. `qd doctor` names the exact edits, and [Design](design/ship-file.md#4-the-ship-file) gives the order to make the edits in.

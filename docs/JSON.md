# Reading as JSON

Any command that shows you something, such as `qd status` or `qd ledger`, can give it as JSON instead of text. Put `--json` before or after the command name.

```sh
qd --json status
qd ledger --by seat --json
```

Each command prints one JSON document and a newline: no table, and no Markdown around it.

## For a person

The text readings say the same things, so you will want JSON only to feed something else: a script, a dashboard, or `jq`.

```sh
qd --json status | jq -r '.seats[] | "\(.name)  \(.activity.state)"'  # each agent, working or idle
```

What each reading holds:

| Command | The document holds |
| --- | --- |
| `status`, or `watch` without a seat | every seat's reading, the open orders, and the status facts |
| `status --public /absolute/path/to/post-privacy.toml` with `--json` | the dashboard status from the service, with the privacy policy applied |
| `status --line` | the facts alone, with the counts of what waits and the operational warnings |
| `watch <seat>` | that seat's reading, the full state of its record, and its screen as parsed |
| `ledger` | the stored usage windows and how fresh each is, the declared pools, the spend on key pools, usage attempts, and the walls now standing |
| `ledger --by seat`, `--by order`, `--by pool`, and `ledger --tools` | rows with named fields, each with its counts of what was unmeasured, untimed or unjudged |
| `doctor`, without `--fix` | each check with its name, a true or false result, and its detail |
| `laws` | each law's id, part and text, and the paths and line numbers that carry it |
| `brief`, also with `--sources` | the brief's text, its source files and its word count |
| `controls` | what each adapter can control, and the ship's controls for relaunching |
| `controls --harness <name>` | without a ship: what that harness's adapter can control, the efforts the harness takes and the versions they were measured on, and the model and effort to suggest |
| `inbox` | the open and the received messages, with the reports that closed them |
| `inbox --wait` | one object, with `seat` and a `messages` array; then it exits |

`qd --json status` adds a `reading` object with `mode`, `at` and `label`.
A service reading younger than 60 seconds says `served: service reading at <time>`.
A stopped service, an older or incomplete publication, or an unreadable publication
uses the independent reading and says `cold: independent reading at <time>`.
All existing feed fields keep their names and values. Plain text status and watch
continue to collect independently.

Each JSON status call marks demand under the runtime's private status folder. While no
clients are attached, the service makes its ordinary periodic whole readings only
for five minutes after the latest call. The minute-by-minute site feed keeps that
demand alive; five quiet minutes stop headless collection. A call that cannot write
the private status stamp falls back cold. The stamp never renews a reading's own time.

## Dashboard status

Run `qd --json status --public /absolute/path/to/post-privacy.toml` with the
dashboard's post privacy file. The TOML file must define exactly two top-level
arrays of names.

```toml
office_seats = ["assistant"]
private_ships = ["private-review"]
```

Names must be Quarterdeck address components. Matching is case-insensitive, with
duplicate names refused within each array. An explicitly empty array is valid.
A private ship matches either its fleet folder or its declared name.

A successful document has exactly these fields.

| Field | Value |
| --- | --- |
| `service` | Service health as a string |
| `observed_at` | The service snapshot's observation time, in ISO 8601 UTC |
| `open_orders` | The number of open orders |
| `seats` | Rows with `name`, `activity` (the state string), `harness`, `model` (string or null), `pool`, and, where allowed, `order` |
| `programs` | Aggregate rows with `harness`, `working_now` (boolean or null), and `last_turn_ended` (ISO 8601 string or null) |
| `withheld_titles` | A sorted list of canonical order IDs in threads involving the ship's Security seats, or null when the whole graph cannot be verified |

The `order` field is omitted for office seats and every seat on a private ship.
On other rows it holds the canonical order ID or null. A claimed question or an
invalid current reference appears as null. Orders whose ID names an office sender
or a private originating ship also appear as null. Program rows reflect the
service's aggregate program observation. If program history is
unavailable, `last_turn_ended` is null and `working_now` still reflects the
independent activity reading.

The service follows every reply link in its observed message graph, including
replies whose senders or recipients are private. The privacy policy filters
withheld IDs by their encoded senders before output. An empty list means the
service confirmed that there are no eligible IDs to withhold. On null, omit
all order titles and keep the other public figures available. A post generation
change makes the list null until the service publishes another whole observation.

Each call with a valid policy marks the usual demand and reads the service's
separate dashboard snapshot. A refresh leaves the preceding snapshot in place,
and the usual freshness and service identity checks determine whether it can
still be served. A successful refresh replaces it in one step. A missing, invalid,
stale, or incompatible snapshot or an unavailable policy exits nonzero with
`public status unavailable` on standard error and empty standard output. The
dashboard should hold its previous publication until a reading succeeds.

Detailed status and the ledger show each provider's allowance as it was last stored. A command that changes something keeps its proof line as text; that includes `watch --save-screen` and `doctor --fix`.

## For an agent

What you can rely on:

- **One command, one document.** A single JSON document, then a newline. `inbox --wait` prints one batch and exits: it is not JSON Lines, and not a stream.
- **Types.** Numbers, booleans, `null` and collections stay what they are. Times are ISO 8601. Paths are strings.
- **Unknown is `null`, never a default.** The model observed, the model declared, the harness and its version are separate fields, and one that was not observed is `null`. What is unknown about tokens and prices in the text is unknown here too.
- **Absence is said.** `watch <seat>` has separate fields for a record that is absent and for one that could not be read. A ledger with no book has `available: false` and a `reason`; an empty book is available, with empty collections.
- **Freshness is said.** Stored usage keeps its original pair of reading and reset, and its latest error. `current`, `complete`, `reason` and `age_seconds` describe how fresh it was at `observed_at`.
- **Who is who.** `provider_id` and `provider_name`, the harness and its `program`, and the paying pool are separate. Status, watch, doctor and the ledger carry the declared `pools` and `harnesses`; in the ledger a pool's company is `pools[].provider_id`, not `pools[].provider`. A seat's reading names the company and the executable `program`. Provider ids inside a harness's own record, and the harness stored at a launch, keep the meaning their protocol gives them.
- **The status line is the same.** Its facts, and what it chooses to show, are those of the text.
- **Reading changes nothing.** The ledger's readings do not write the book, and do not ask a provider for usage.
- **A receipt follows the output.** `inbox --wait` serializes, prints and flushes the whole document before it records the messages as received. If the output fails, the attempts stay unresolved: a message that was printed is not thereby marked received.

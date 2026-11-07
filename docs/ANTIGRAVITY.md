# Antigravity record contract (1.5.2)

The canonical source is the seat-owned conversation SQLite database, including its
committed WAL. Only filenames are inspected during joining. An ambiguous join never
opens candidates. Pinned records are retained in the seat's owned-record list across
fresh starts, including missing files so accounting can report missing coverage.
No other real conversations are scanned or decoded.

The reader opens source files as bytes, checks database and WAL fingerprints before
and after copying to memory, validates WAL header and frame checksums, and applies
only frames through a commit boundary. It ignores incomplete transaction tails and
old-salt frames after a WAL restart. Invalid checksums in the current generation,
unsupported tables and unstable snapshots raise RecordError. SQLite deserializes the
result in memory and queries every table from that one image. It never opens the
source with SQLite, creates sidecars, or stages private bytes in a temporary folder.
An active rollback journal is unsupported. The fingerprint includes presence, inode,
size, mtime, device and ctime for both DB and WAL; failed reads are not cached.

## Descriptor evidence

These are schema facts from embedded protobuf descriptors in the installed 1.5.2
program, not copied conversation content. Offsets identify that build only.

| Descriptor | Offset | Fields used |
|---|---|---|
| exa.cortex_pb.CortexStepStatus | 0x67f6dd0 | UNSPECIFIED=0, PENDING=1, RUNNING=2, DONE=3, INVALID=4, CLEARED=5, CANCELED=6, ERROR=7, GENERATING=8, WAITING=9, QUEUED=11, INTERRUPTED=12; 10 reserved |
| exa.cortex_pb.CortexStepType | embedded enum | USER_INPUT=14, PLANNER_RESPONSE=15, ERROR_MESSAGE=17, CHECKPOINT=23, GENERIC=132 |
| exa.cortex_pb.ExecutorMetadata | 0x67eaa7b | 1 termination_reason enum; 2 num_generator_invocations int32; 3 last_step_idx int32; 9 execution_id string; 10 cascade_config message. No end timestamp. |
| exa.cortex_pb.ExecutorTerminationReason | 0x67f9ee3 | ERROR=1, USER_CANCELED=2, MAX_INVOCATIONS=3, NO_TOOL_CALL=4, MAX_FORCED_INVOCATIONS=6, EARLY_CONTINUE=7, TERMINAL_STEP_TYPE=8, TERMINAL_CUSTOM_HOOK=9, INJECTED_RESPONSE=10, MAX_TOKEN_BUDGET_EXCEEDED=11 |
| exa.codeium_common_pb.ModelUsageStats | 0x677982d | 1 Model enum (varint); 2 input_tokens; 3 output_tokens; 4 cache_write_tokens; 5 cache_read_tokens (uint64 scalars) |
| exa.cortex_pb.CortexStepMetadata | 0x67cf31b | 1 created_at; 6 viewable_at; 7 finished_generating_at; 8 completed_at; 32 started_at (Timestamp messages); 4 tool_call; 9 model_usage; 10 model_cost float; 12 execution_id |
| exa.cortex_pb.CortexStepUserInput | 0x67e7c13 | 2 user_response / legacy 1 query strings; 12 user_config |
| exa.cortex_pb.CortexStepPlannerResponse | 0x67e81ab | 8 modified_response / 1 response strings; 7 repeated tool_calls |
| exa.cortex_pb.ChatModelMetadata | 0x67ce4f4 | 4 usage; 19 response_model, 21 model_display_name strings |
| exa.cortex_pb.CortexStepGeneratorMetadata | 0x67cdbb2 | 1 chat_model; 2 step_indices (packed or unpacked uint32); 3 planner_config; 4 execution_id |
| exa.cortex_pb.CascadeConfig | 0x67d166d | 1 planner_config; its CascadePlannerConfig field 28 is model_name:string |
| exa.cortex.core.tools.RunCommand | 0x6239062 | exit_code=1 int32; timed_out=2 bool; exit_code_unknown=8 bool |
| exa.cortex_pb.CortexStepRunCommand | 0x67e8dc7 | command=1 string; cwd=2 string; exit_code=6 optional int32; timed_out=37 bool |

Generic payload field 140 contains CortexStepGeneric; its field 2 is
GenericStepResult, whose field 6 is google.protobuf.Any. Dispatch uses the complete
`type.googleapis.com/` URL. The two RunCommand types are different messages. An
unknown Any type is not interpreted using a suffix. A known nonzero exit or timeout
is failure; an unknown exit code, missing optional exit, or unsupported outcome is
unknown. ERROR status also establishes failure. A DONE generic operation need not
mean its command succeeded.

TranscriptLogDecorator uses the same CortexStepStatus domain. Its AppendStep at
0x867d1e0 compares the step status at structure offset 0x0c to 3, 7 and 4: DONE,
ERROR and INVALID. SetStep at 0x867d380 checks the previous header at offset 0x04
and the new step at 0x0c. These are structure layouts, not distinct enum mappings.
The old labels RUNNING=1, ERROR=4, CANCELED=7 were incorrect.

The full JSONL transcript is a projection of logged steps, not the accounting source.
Its producer appends finished steps and rewrites on full synchronization/branch/prune;
in-progress steps can lag. The projection lacks execution boundaries, model names
and completion timestamps. The shortened transcript also truncates text, so neither
is used to confirm deliveries. USER_INPUT in the database supplies exact text;
confirmation does not rely on a final-looking reply, screen echo or elapsed silence.

## Accounting and unknown fields

A turn uses the first USER_INPUT step index as its persistent key. Later inputs with
the same execution_id are consumed messages within that turn. Only a known executor
termination covering its steps, or an explicit cancellation/interruption, closes it.
An ERROR_MESSAGE without an execution_id may follow a recorded executor ERROR beyond
last_step_idx; it is a terminal error trailer, not evidence of a new open execution.
A newer input alone is never a terminal event. Unknown enums or unresolved earlier
executions make history coverage unknown even when later completed turns are readable.

Step model usage is charged once, including usage on non-planner steps. Generator
usage is a fallback only when its associated step list identifies one planner response;
the same list often includes that response's tools and must not multiply its usage.
Missing planner usage remains unknown. A present empty usage message supplies proto3
zero input/output/cache-read scalars; an absent message supplies no counters.

Input tokens already exclude cache reads. In the installed
`genairesponse.MapUsageMetadata` (0x9174380), instruction 0x9174486 subtracts the cached
count from the prompt count. The result is stored at ModelUsageStats offset 0x10
(0x9174c50), and cached tokens separately at offset 0x38 (0x9174c78).
GetInputTokens (0x7426560) and GetCacheReadTokens (0x7426600) confirm these member offsets.
The output field includes response and thinking tokens; adding the latter again would
double count. Cache-write tokens are unknown when absent: the producer does not
establish a separately reported zero for this class. Cost is used only when explicitly
present on every charged step. Effort and completed child-turn counts are unknown;
subtrajectory flags and subagent invocations are not completed child turns. The reader
does not traverse child records or add their usage a second time.

Only the final step's completed_at supplies a turn end. Earlier completion times,
created_at, viewable_at, filesystem mtimes and executor invocation counts are not end
times. A tool duration requires its started/created time and completed_at. Missing end
times and interrupted tool durations remain unknown. Last activity remains separate.

The ledger inserts by persistent key, refreshes late metadata and child tool rows,
and preserves established seat/pool provenance and machine measurements on rereads.
Unknown classes propagate through rollups and daily totals. Incomplete or unreadable
records are persisted with their known seat/pool; unseen orders remain unattributed.
Warnings also appear in ledger tool/pool output. Missing owned files remain visible.
Watch/status say unknown on failed reads or uncertain history and show available model
and turn state from records before screen fallback. No screen value overrides known
record state.

Tests use synthetic databases and literal protobuf wire examples checked against these
descriptors. Native SQLite constructs WAL fixtures for commit boundaries, incomplete
transactions, checksum failures, checkpoint/reset/removal and recreation. No real
conversation text, identifiers, usage, credentials or record bytes are fixtures.

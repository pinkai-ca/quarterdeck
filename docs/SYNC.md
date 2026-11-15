# Updating your ship

Your ship, your crew of AI coding agents, runs its own copy of the Quarterdeck code, made at `qd setup`. That copy changes only when you run `qd sync`.

```sh
git pull  # get the latest Quarterdeck code into the folder you cloned
qd sync   # move your ship onto that code
```

- `qd sync --all` does the same for each of your ships under `ships/`, one after another, and stops at the first ship whose new copy fails its check. It leaves your office out: from the folder you cloned, `qd --ship office sync` does the same for the office.
- Typed in any folder, a `qd` command for a ship runs that ship's copy, so pulling or editing the cloned folder does not affect the running crew until you sync.
- Only committed code goes into the ship's copy: sync says when the cloned folder holds uncommitted changes, and leaves them out.

## What a sync does

1. Builds a new copy from the cloned folder's current commit, and downloads nothing. If the ship already runs that commit, sync says so and does nothing.
2. Checks the new copy before the ship moves: the copy has to start and list Quarterdeck's [laws](LAWS.md), which shows that the copy runs with all of its files.
3. Restarts the ship's service, the small program that delivers messages, on the new copy, in a second or two. Your agents are not touched: a [turn](PARTS.md) in progress goes on, and a message sent meanwhile is delivered afterwards.
4. Tells only an agent whose brief, the agent's standing instructions, has changed. The other agents spend no turn.

## Going back

```sh
qd doctor       # the commit your ship runs and since when, and the commit the ship ran before
qd sync --back  # return to the commit the ship ran before
```

`qd sync <commit>` takes the ship to any other commit.

## If the new copy fails its check

Nothing changes. Your ship keeps running the old copy, and sync says why the new copy failed. You do not need to type anything to have a running ship again.

A ship's first copy is the exception. `qd setup` makes it, or the first `qd up` of a ship written by hand. If that first copy fails its check, the ship is left with no copy and nothing half-made: the command stops and says why. Fix what the message names, and run the command again.

## For maintainers

- A ship whose `ship.toml` says `code = "checkout"` under `[ship]` runs the cloned folder's code, as the practice ship, `ships/drydock`, does. On such a ship `qd sync` refuses, and says how to give the ship a copy of its own.
- What is installed beside the code, and packaging: [Packaging](PACKAGING.md).

# Seat cards

One HTML page with a card for each seat of a ship.

## What a card shows

`dashboard/seat_cards.py` turns the status reading and the ship file into one HTML page with a card for each seat, grouped by session. A card shows the seat's name and role; its program, model and effort; whether it is working and whether its door is open; the subject of the order it holds; and today's tokens. It keeps the reading's register: what the reading could not see says unknown, a count with unmeasured turns keeps its `+`, and a model or effort taken only from the ship file is marked declared. The page fetches nothing, so it opens from disk:

```sh
.venv/bin/python dashboard/seat_cards.py --ship ships/default > seats.html
```

## Read a page before it leaves your machine

The page carries your ship's own text: order subjects are the crew's free-form words and may name customers, paths or anything else the ship works on. Escaping keeps them from breaking the page, not private. A page rendered from a real ship is not ready to publish; read it, as you would any of the ship's text, before it leaves your machine.

"""The shared pool contract for programs that run on a compatible model endpoint."""

from pathlib import Path
from dataclasses import replace

from .. import endpoint
from ..ship import Pool, SeatSpec
from ..pricing import Price
from .base import Credential, Harness


class EndpointHarness(Harness):
    def accepts(self, pool: Pool) -> str | None:
        if pool.kind != "endpoint":
            return f'pool {pool.name} names no endpoint, and {self.name} runs on an endpoint pool: declare endpoint = "http://host:port/v1"'
        return None

    def endpoint_home(self, home: Path, seat: SeatSpec, pool: Pool):
        """Discover this launch's model and key without storing the key in the home."""
        home.mkdir(parents=True, exist_ok=True)
        return endpoint.choose(pool, seat.model), endpoint.key(pool)

    def endpoint_ready(self, home: Path, choice: endpoint.Choice, secret: dict, price: Price | None = None) -> Credential | None:
        replace(choice, price=price.snapshot(choice.model) if price else None).write(home)
        return Credential(variable=endpoint.KEY_VARIABLE) if secret else None

    def price_turn(self, turn, snapshot: dict | None):
        if turn.cost is not None and turn.cost > 0:
            return turn  # a nonzero recorded cost is not a program's unknown-price default
        try:
            price = Price.parse(snapshot["rates"])
            if turn.price_model != snapshot["model"]:
                return turn
        except (KeyError, TypeError, ValueError):
            return turn
        cost = price.cost(turn.tokens)
        return replace(turn, cost=cost, cost_basis="stated launch rates" if cost is not None else turn.cost_basis)

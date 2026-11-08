"""The shared pool contract for programs that run on a compatible model endpoint."""

from pathlib import Path

from .. import endpoint
from ..ship import Pool, SeatSpec
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

    def endpoint_ready(self, home: Path, choice: endpoint.Choice, secret: dict) -> Credential | None:
        choice.write(home)
        return Credential(variable=endpoint.KEY_VARIABLE) if secret else None

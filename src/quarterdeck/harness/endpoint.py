"""The shared pool contract for programs that run on a compatible model endpoint."""

from pathlib import Path
from dataclasses import replace

from .. import seat_files
from .. import endpoint
from ..ship import Pool, SeatSpec
from ..pricing import Price
from .base import Credential, Harness, SetupMetadata
from .costs import costed


class EndpointHarness(Harness):
    setup = SetupMetadata(key_env="OPENROUTER_API_KEY", key_provider="openrouter",
                          key_endpoint="https://openrouter.ai/api/v1",
                          anonymous_models_endpoint="https://openrouter.ai/api/v1")

    def accepts(self, pool: Pool) -> str | None:
        if pool.kind == "subscription":
            from . import native_name
            from ..providers import display
            return f"pool {pool.name} is a subscription for {display(pool.provider)}; use its native harness {native_name(pool.provider) or '(not installed)'}"
        if pool.kind != "endpoint":
            return f'pool {pool.name} names no endpoint, and {self.name} runs on an endpoint pool: declare endpoint = "http://host:port/v1"'
        return None

    def endpoint_home(self, home: Path, seat: SeatSpec, pool: Pool):
        """Discover this launch's model and key without storing the key in the home."""
        seat_files.make_folder(home)  # never through a link its jail planted
        return endpoint.choose(pool, seat.model), endpoint.key(pool)

    def endpoint_ready(self, home: Path, choice: endpoint.Choice, secret: dict, price: Price | None = None) -> Credential | None:
        replace(choice, price=price.snapshot(choice.model) if price else None).write(home)
        return Credential(variable=endpoint.KEY_VARIABLE) if secret else None

    def price_turn(self, turn, snapshot: dict | None):
        return costed(turn, snapshot)

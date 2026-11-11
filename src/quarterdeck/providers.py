"""Company identities and display names; native program facts live in harness/."""

NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google", "xai": "xAI",
         "meta": "Meta", "zai": "Z.ai", "openrouter": "OpenRouter", "synthetic": "Synthetic"}
SPELLINGS = {"z.ai": "zai", "z-ai": "zai", "x.ai": "xai", "open-ai": "openai", "open-router": "openrouter"}


def canonical(value: str) -> str:
    """Known companies normalize; legacy native harness names are input aliases only.

    An unknown endpoint operator remains exactly the declared identity, never inferred
    from the branding of a model the endpoint serves.
    """
    name = value.strip()
    key = SPELLINGS.get(name.casefold(), name.casefold())
    if key in NAMES:
        return key
    from . import harness
    if key in harness.NAMES:
        native = harness.provider_of(key)
        if native is not None:
            return native
    return name


def display(value: str) -> str:
    company = canonical(value)
    return NAMES.get(company, company)


def pool_info(pool) -> dict:
    from .harness import native_name
    return {"name": pool.name, "kind": pool.kind, "provider_id": pool.provider,
            "provider_name": display(pool.provider), "native_harness": native_name(pool.provider)}


def harness_info(name: str) -> dict:
    from .harness import get
    reader = get(name)
    return {"name": name, "program": reader.program, "native_provider_id": reader.native_provider,
            "native_provider_name": display(reader.native_provider) if reader.native_provider else None}


def identities(ship) -> dict:
    """Public declared identities, separate from native record/launch protocol names."""
    names = sorted({seat.for_pool(pool).harness for seat in ship.seats.values() for pool in seat.pools})
    return {"pools": [pool_info(pool) for pool in ship.pools.values()],
            "harnesses": [harness_info(name) for name in names]}

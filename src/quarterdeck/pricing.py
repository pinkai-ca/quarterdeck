"""Explicit pool rates, in one supported currency and token unit; never a catalogue."""

from dataclasses import asdict, dataclass
import math


def amount(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if value >= 0 and math.isfinite(value) else None
    except OverflowError:
        return None


@dataclass(frozen=True)
class Price:
    currency: str
    unit: str
    input: float
    output: float
    cache_read: float
    cache_write: float

    @classmethod
    def parse(cls, data: object) -> "Price":
        names = set(cls.__dataclass_fields__)
        if not isinstance(data, dict) or set(data) != names:
            raise ValueError("price needs currency, unit, input, output, cache_read and cache_write")
        if data["currency"] != "USD" or data["unit"] != "million_tokens":
            raise ValueError('price supports only currency="USD", unit="million_tokens"; no conversion is assumed')
        rates = {key: amount(data[key]) for key in names - {"currency", "unit"}}
        if any(value is None for value in rates.values()):
            raise ValueError("price rates must be finite nonnegative numbers, not booleans")
        return cls(data["currency"], data["unit"], **rates)

    def snapshot(self, model: str) -> dict:
        return {"model": model, "rates": asdict(self)}

    def cost(self, tokens) -> float | None:
        if not tokens.complete:
            return None
        try:
            return amount((tokens.read_uncached * self.input + tokens.written * self.output +
                           tokens.read_cached * self.cache_read + tokens.cache_written * self.cache_write) / 1e6)
        except OverflowError:
            return None

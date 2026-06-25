"""Small accumulated phase timer for long-running imports."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from time import perf_counter
from typing import Iterator


class PhaseTimer:
    """Accumulate elapsed seconds across repeated named phases."""

    def __init__(self) -> None:
        self._seconds: dict[str, float] = defaultdict(float)

    @contextmanager
    def measure(self, phase: str) -> Iterator[None]:
        """Measure one block and add it to the named phase."""

        started = perf_counter()
        try:
            yield
        finally:
            self._seconds[phase] += perf_counter() - started

    def add(self, phase: str, seconds: float) -> None:
        """Add externally measured time to a phase."""

        self._seconds[phase] += max(0.0, seconds)

    def as_dict(self) -> dict[str, float]:
        """Return stable rounded timing values."""

        return {
            phase: round(seconds, 3)
            for phase, seconds in sorted(self._seconds.items())
            if seconds > 0
        }

    def measured_total(self) -> float:
        """Return the sum of non-overlapping measured phases."""

        return sum(self._seconds.values())

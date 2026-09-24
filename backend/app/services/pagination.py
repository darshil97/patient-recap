"""
One definition of a page.

Lists used to be assembled in the route handlers, which called the repositories
directly and did their own `count` for `X-Total-Count`. That left the page size,
the maximum, and the pairing of list-with-count written out once per endpoint,
with nothing holding them together — and it is why the frontend ended up asking
for 100 patients, 200 visits and 500 follow-ups, each for its own reasons.

`Page` is what a service returns instead: the rows the caller asked for, and the
total that matches the *same* filters. A client that renders `total` next to
`len(items)` can tell it is looking at a slice, which is the thing a bare list
cannot say.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, List, TypeVar

T = TypeVar("T")

#: What a caller gets when it does not ask. Large enough that the current data
#: (7 patients, 18 visits) is never truncated, small enough to stay one screen.
DEFAULT_PAGE_SIZE = 100

#: The ceiling a caller may raise it to. Mirrored in the routes' Query(le=...)
#: so an out-of-range request is a 422 at the edge rather than a silent clamp.
MAX_PAGE_SIZE = 500


@dataclass
class Page(Generic[T]):
    """A slice of a list, plus how many rows the filter actually matched."""

    items: List[T]
    total: int

    @property
    def is_partial(self) -> bool:
        """Whether rows were left behind. Drives the UI's 'showing N of M'."""
        return len(self.items) < self.total


def clamp_limit(limit: int | None) -> int:
    """Resolve a caller's page size, defaulting and capping in one place."""
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(int(limit), MAX_PAGE_SIZE))

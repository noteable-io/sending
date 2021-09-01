import inspect
from itertools import islice
from typing import Callable, Coroutine, Iterator, List


def split_collection(c, slices) -> List[Iterator]:
    """Splits collection into a number of slices, as equally-sized as possible."""
    return [islice(c, n, None, slices) for n in range(slices)]


def ensure_async(fn: Callable) -> Coroutine:
    if inspect.iscoroutinefunction(fn):
        return fn

    async def wrapped(message):
        return fn(message)

    return wrapped

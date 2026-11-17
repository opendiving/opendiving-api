"""An in-memory stand-in for the async Redis client, covering what the app calls on it.

`@cache` and `delete_keys_by_pattern` (`get`/`set`/`delete`/`scan`), the outbound answer
cache (`get`/`set` with `ex`), the rate limiter (`incr`/`expire`/`ttl`), the map-tile draw
claim and the conditions run lock (`set` with `nx`/`px`), the passkey challenge store (`getdel`),
the lock's release and the external request counter (`eval`, below), and the move that takes the
counts (`scan` and `getdel`). It serves back what
was written and sweeps by pattern the way the real one does, so a read after a write is stale
unless something actually dropped the key. Expiries are recorded, never enforced: nothing
here needs a clock. `writes` lists every key `set` stored, in order, so a test can tell one
write from two of the same key. The counter's rows are held in `counters`, with their expiries
in `counter_expiries`, apart from `store`, so a test of what the app caches reads `store` alone;
`get`, `getdel`, `delete` and `scan` see both.

Where the real client refuses, so does this: `set` takes what `redis-py`'s encoder takes and
raises its `DataError` for the rest - `None`, a `bool`, a `dict` - and a `set` without an
expiry clears the one the key had, as `SET` does without `KEEPTTL`.

`failing=True` turns every call into `redis-py`'s `ConnectionError`, which is how the "a cache
outage is a miss, not an error" branches get exercised. `redis-py` connects lazily, so a real
outage arrives as a `RedisError` from an ordinary-looking client, not as a missing one.
"""

from fnmatch import fnmatchcase

from redis.exceptions import ConnectionError, DataError


def _encode(value: object) -> bytes:
    """What `redis-py`'s `Encoder.encode` stores for `value`, or its `DataError`."""
    if isinstance(value, bytes | memoryview):
        return bytes(value)
    if isinstance(value, str):
        return value.encode()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value).encode()
    if isinstance(value, float):
        return repr(value).encode()
    raise DataError(f"Invalid input of type: '{type(value).__name__}'. Convert to a bytes, string, int or float first.")


class FakeRedis:
    def __init__(self, *, failing: bool = False) -> None:
        self.store: dict[str, bytes] = {}
        self.expiries: dict[str, int] = {}
        self.writes: list[str] = []
        self.counters: dict[str, bytes] = {}
        self.counter_expiries: dict[str, int] = {}
        self.failing = failing

    def _fail_if_down(self) -> None:
        if self.failing:
            raise ConnectionError("redis is down")

    async def get(self, key: str) -> bytes | None:
        self._fail_if_down()
        return self.store.get(key, self.counters.get(key))

    async def set(
        self, key: str, value: object, ex: int | None = None, px: int | None = None, nx: bool = False
    ) -> bool | None:
        self._fail_if_down()
        encoded = _encode(value)
        if nx and key in self.store:
            return None
        self.store[key] = encoded
        self.writes.append(key)
        if ex is not None:
            self.expiries[key] = ex
        elif px is not None:
            self.expiries[key] = px // 1000
        else:
            self.expiries.pop(key, None)
        return True

    async def getdel(self, key: str) -> bytes | None:
        self._fail_if_down()
        self.expiries.pop(key, None)
        self.counter_expiries.pop(key, None)
        return self.store.pop(key, self.counters.pop(key, None))

    async def incr(self, key: str) -> int:
        self._fail_if_down()
        value = int(self.store.get(key, b"0")) + 1
        self.store[key] = str(value).encode()
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        self._fail_if_down()
        self.expiries[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        self._fail_if_down()
        if key not in self.store:
            return -2
        return self.expiries.get(key, -1)

    async def delete(self, *keys: str) -> int:
        self._fail_if_down()
        for key in keys:
            self.expiries.pop(key, None)
            self.counter_expiries.pop(key, None)
        return sum((self.store.pop(key, None), self.counters.pop(key, None)) != (None, None) for key in keys)

    async def scan(self, cursor: int = 0, match: str = "*", count: int | None = None) -> tuple[int, list[str]]:
        self._fail_if_down()
        return 0, [key for key in (*self.store, *self.counters) if fnmatchcase(key, match)]

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        """The two scripts the app sends, by what each does. Any other is refused rather than
        guessed at.

        The lock's release deletes `KEYS[1]` only while it holds `ARGV[1]`. The counter adds one
        event of `ARGV[1]` milliseconds to each key's `count total slowest`, raising the slowest
        where it is longer, and sets each key's expiry to `ARGV[2]`."""
        self._fail_if_down()
        keys, args = [str(key) for key in keys_and_args[:numkeys]], keys_and_args[numkeys:]
        if script.startswith("-- external-requests:count"):
            duration, ttl = int(str(args[0])), int(str(args[1]))
            for key in keys:
                count, total, slowest = (int(figure) for figure in self.counters.get(key, b"0 0 0").split())
                self.counters[key] = f"{count + 1} {total + duration} {max(slowest, duration)}".encode()
                self.counter_expiries[key] = ttl
            return len(keys)
        if "redis.call('get', KEYS[1]) == ARGV[1]" not in script or numkeys != 1:
            raise NotImplementedError(script)
        (key,), (expected,) = keys, args
        if self.store.get(key) != _encode(expected):
            return 0
        return await self.delete(key)

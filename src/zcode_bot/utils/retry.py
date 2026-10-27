"""retry — F79e: the ONE retry policy of the transport (stdlib-tenacity
style, zero deps).

The body that existed inline three times (send's delays=(0,1,3), the
429 override, the fetch loop's implicit next-cycle retry) lives here
once, parameterized:
- backoffs: delay BEFORE attempt i (default (0, 1, 3) — send's frozen
  contract: one-off network resets don't lose the notification);
- retry_after_of(exc) → the REAL wait when the server names one
  (Telegram 429's parameters.retry_after) — it OVERRIDES the next
  backoff (ra + 0.5), capped by the caller;
- should_retry(exc) → False stops the loop NOW (4xx: retrying a bad
  request is hammering with extra steps);
- jitter: added to each delay, bounded by the injected rnd (tests pin
  it; the transport keeps 0 — its 429 contract is deterministic);
- on_retry_after / on_attempt_fail hooks: the caller's side effects
  (the cooldown window, the per-attempt log) stay in the caller.
"""
import time as _time
from typing import Any, Callable, Optional, Sequence


def retry_call(fn: Callable[[], Any],
               *,
               backoffs: Sequence[float] = (0, 1, 3),
               sleep: Callable[[float], None] = _time.sleep,
               should_retry: Optional[Callable[[Exception], bool]] = None,
               retry_after_of: Optional[Callable[[Exception],
                                                 Optional[float]]] = None,
               retry_after_cap: float = 60.0,
               jitter: float = 0.0,
               rnd: Optional[Callable[[], float]] = None,
               on_retry_after: Optional[Callable[[float], None]] = None,
               on_attempt_fail: Optional[Callable[[int, Exception,
                                                   float], None]] = None
               ) -> Any:
    """Runs fn with the policy; returns its result. Raises the LAST
    exception when the attempts run out or should_retry says stop."""
    attempts = len(backoffs)
    delay = 0.0
    for i in range(attempts):
        if delay > 0:
            sleep(delay + (rnd() * jitter if (jitter and rnd) else 0.0))
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — the policy sees all
            if i + 1 >= attempts:
                raise
            if should_retry is not None and not should_retry(exc):
                raise
            delay = backoffs[i + 1]
            if retry_after_of is not None:
                ra = retry_after_of(exc)
                if ra is not None:
                    ra = min(max(ra, 0.0), retry_after_cap)
                    delay = ra + 0.5
                    if on_retry_after:
                        on_retry_after(ra)
            if on_attempt_fail:
                on_attempt_fail(i + 1, exc, delay)
    raise RuntimeError("unreachable")  # for the type checker

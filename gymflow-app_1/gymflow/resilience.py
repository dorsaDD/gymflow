"""resilience.py — Section 4.10: Fault tolerance / fallback.

GymFlow depends on two genuinely external, sometimes-unreliable services: a
third-party SMTP relay (mail_manager.py) and a third-party LLM provider
(ai_agent.py). Section 4.10 answers the professor's feedback directly: if
one of those goes down, GymFlow must not go down with it, or even show the
user a broken page -- something else must automatically take over.

This module implements the two building blocks that make that possible, and
is deliberately kept independent of both callers so it can be unit-tested on
its own (see _devtools/test_smoke.py, Section 4.10) without any real network
access:

  1. call_with_retry() -- retries a flaky operation a few times with
     exponential backoff before giving up (transient failures, e.g. one
     dropped packet, often succeed on the 2nd or 3rd attempt).

  2. CircuitBreaker -- the classic Closed / Open / Half-Open state machine
     (Nygard, "Release It!"). Retrying forever against a service that is
     genuinely DOWN just wastes time and piles up slow failing requests, so
     once a service has failed too many times in a row the breaker "trips"
     open and short-circuits every further call immediately -- no network
     attempt at all -- for a cool-down window. After the window elapses a
     single probe call is allowed through (half-open); if it succeeds the
     breaker closes again, if it fails the cool-down restarts.

Each caller decides its own fallback for when everything above still fails
(mail_manager.py queues the message for an administrator to retry by hand;
ai_agent.py returns a friendly canned reply instead of a stack trace) -- the
one hard rule is that a failure here must never propagate up and break the
page the user is looking at (see the global 500 handler in gymflow/__init__.py
for the last line of defence if it ever did).
"""
import time
from typing import Callable, Dict, TypeVar

T = TypeVar("T")


class CircuitOpenError(Exception):
    """Raised by call_with_retry() when the breaker for that service is OPEN,
    so the call is skipped entirely rather than attempted and left to fail."""


class CircuitBreaker:
    """Per-service failure tracker. See the module docstring for the state
    machine. Not thread-safe by design -- GymFlow is a single coursework
    Flask process handling one request at a time under the dev/test server;
    a multi-worker deployment would need this state in something shared
    like Redis instead (noted in the Section 11 Roadmap)."""

    def __init__(self, failure_threshold: int = 3, reset_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._consecutive_failures = 0
        self._state = "closed"
        self._opened_at = None

    @property
    def state(self) -> str:
        if self._state == "open" and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.reset_timeout:
                self._state = "half-open"  # cool-down elapsed: allow one probe through
        return self._state

    def before_call(self):
        if self.state == "open":
            raise CircuitOpenError("circuit is open; call skipped")

    def record_success(self):
        self._consecutive_failures = 0
        self._state = "closed"
        self._opened_at = None

    def record_failure(self):
        self._consecutive_failures += 1
        # A failed probe while half-open re-opens immediately, regardless of
        # the ordinary threshold -- the service just proved it's still down.
        if self.state == "half-open" or self._consecutive_failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()


_breakers: Dict[str, CircuitBreaker] = {}


def get_breaker(service: str, failure_threshold: int = 3, reset_timeout: float = 30.0) -> CircuitBreaker:
    """One shared breaker per service name, created on first use and reused
    for the life of the process (state must persist across requests to be
    useful at all)."""
    if service not in _breakers:
        _breakers[service] = CircuitBreaker(failure_threshold=failure_threshold, reset_timeout=reset_timeout)
    return _breakers[service]


def reset_all_breakers():
    """Test-only helper: clears every breaker back to a fresh CLOSED state so
    test sections don't leak state into each other."""
    _breakers.clear()


def call_with_retry(fn: Callable[[], T], *, service: str, max_attempts: int = 3,
                     backoff_seconds: float = 0.2, failure_threshold: int = 3,
                     reset_timeout: float = 30.0) -> T:
    """Runs fn() (a zero-argument callable) with up to `max_attempts` tries,
    doubling the backoff between attempts, guarded by the named service's
    CircuitBreaker. If the breaker is already OPEN this raises
    CircuitOpenError immediately without attempting fn() at all -- that's
    the whole point of a circuit breaker. Otherwise, if every attempt fails,
    the last exception raised by fn() is re-raised. Either way, it is the
    caller's job to catch this and decide the fallback.
    """
    breaker = get_breaker(service, failure_threshold=failure_threshold, reset_timeout=reset_timeout)
    breaker.before_call()

    last_exc: Exception = CircuitOpenError("unreachable")  # replaced on the first real attempt
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 -- any failure of the wrapped call counts
            last_exc = exc
            breaker.record_failure()
            if attempt < max_attempts:
                time.sleep(backoff_seconds * (2 ** (attempt - 1)))
            continue
        breaker.record_success()
        return result
    raise last_exc

"""
Pure token bucket algorithm.

"Pure" = no Redis, no HTTP, no side effects, no reading the real clock.
State in -> decision + new state out. This makes the logic trivially
testable; wiring it to Redis is a separate concern (Phase 2/4).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BucketConfig:
    """Per-client configuration (set later via the admin endpoint)."""
    capacity: float      # max tokens = burst size
    refill_rate: float   # tokens added per second = sustained rate


@dataclass(frozen=True)
class BucketState:
    """The evolving state of one client's bucket."""
    tokens: float        # float! fractional tokens accumulate (see notes)
    last_refill: float   # unix timestamp (seconds) of last update


@dataclass(frozen=True)
class Decision:
    """Result of one rate-limit check."""
    allowed: bool
    new_state: BucketState
    remaining: float     # tokens left AFTER this decision (for headers later)


def new_bucket(config: BucketConfig, now: float) -> BucketState:
    """
    First time we ever see a client: their bucket starts FULL.
    Policy choice: a brand-new client deserves their full burst,
    not an empty bucket (which would DENY their very first request).
    """
    return BucketState(tokens=config.capacity, last_refill=now)


def check(config: BucketConfig, state: BucketState, now: float) -> Decision:
    """
    The heart of the algorithm: lazy refill, then spend-or-deny.

    1) Back-calculate tokens earned since last update (lazy refill).
    2) Cap at capacity.
    3) If >= 1 token: spend it -> ALLOW. Else -> DENY.
    """
    # max(0, ...) defends against clocks moving backwards.
    elapsed = max(0.0, now - state.last_refill)

    # Lazy refill: earn tokens for the time that passed, but never exceed cap.
    refilled = min(config.capacity, state.tokens + elapsed * config.refill_rate)

    if refilled >= 1.0:
        new_state = BucketState(tokens=refilled - 1.0, last_refill=now)
        return Decision(allowed=True, new_state=new_state,
                        remaining=new_state.tokens)

    # DENY: no token spent, but we still record the refill we computed.
    new_state = BucketState(tokens=refilled, last_refill=now)
    return Decision(allowed=False, new_state=new_state,
                    remaining=new_state.tokens)
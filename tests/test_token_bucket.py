"""
Unit tests for the pure token bucket algorithm.
Note how we control time by hand (now=0.0, now=3.0, ...) —
no sleeping, no real clock. That's why `now` is a parameter.
"""

from app.token_bucket import BucketConfig, BucketState, check, new_bucket

CFG = BucketConfig(capacity=5, refill_rate=1)  # 5 burst, 1 token/sec


def test_new_bucket_starts_full():
    state = new_bucket(CFG, now=0.0)
    assert state.tokens == 5


def test_first_request_allowed():
    state = new_bucket(CFG, now=0.0)
    d = check(CFG, state, now=0.0)
    assert d.allowed is True
    assert d.new_state.tokens == 4.0


def test_burst_until_empty_then_deny():
    """Client fires 6 instant requests: 5 allowed (burst), 6th denied."""
    state = new_bucket(CFG, now=0.0)
    results = []
    for _ in range(6):
        d = check(CFG, state, now=0.0)   # all at the same instant
        results.append(d.allowed)
        state = d.new_state
    assert results == [True, True, True, True, True, False]


def test_refill_after_waiting():
    """Empty bucket + wait 3 seconds at 1 token/sec -> 3 requests allowed."""
    state = BucketState(tokens=0.0, last_refill=0.0)
    d1 = check(CFG, state, now=3.0)
    d2 = check(CFG, d1.new_state, now=3.0)
    d3 = check(CFG, d2.new_state, now=3.0)
    d4 = check(CFG, d3.new_state, now=3.0)
    assert [d1.allowed, d2.allowed, d3.allowed, d4.allowed] == \
           [True, True, True, False]


def test_refill_never_exceeds_capacity():
    """Idle for an hour does NOT earn 3600 tokens — cap at capacity."""
    state = BucketState(tokens=0.0, last_refill=0.0)
    d = check(CFG, state, now=3600.0)
    # started refilled to 5 (cap), spent 1 -> 4 remain
    assert d.allowed is True
    assert d.new_state.tokens == 4.0


def test_fractional_tokens_accumulate():
    """0.5s at 1 token/sec = 0.5 tokens -> not enough. Another 0.5s -> enough."""
    state = BucketState(tokens=0.0, last_refill=0.0)
    d1 = check(CFG, state, now=0.5)          # 0.5 tokens -> DENY
    assert d1.allowed is False
    d2 = check(CFG, d1.new_state, now=1.0)   # +0.5 more -> 1.0 -> ALLOW
    assert d2.allowed is True


def test_clock_going_backwards_is_safe():
    """If time appears to go backwards, we must not subtract tokens."""
    state = BucketState(tokens=2.0, last_refill=100.0)
    d = check(CFG, state, now=50.0)          # now < last_refill!
    assert d.allowed is True                 # still has its 2 tokens
    assert d.new_state.tokens == 1.0         # spent exactly one, no theft
from app.services.ratelimit import RateLimiter


def test_sliding_window():
    now = [0.0]
    rl = RateLimiter(limit=2, window_s=60, clock=lambda: now[0])
    assert rl.check("a") is None
    assert rl.check("a") is None
    assert rl.check("a") == 60.0
    assert rl.check("b") is None  # per-client
    now[0] = 60.1
    assert rl.check("a") is None

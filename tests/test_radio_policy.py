from js8mail.radio_policy import AdaptiveSpeedPolicy, AirtimeBudget, SpeedEvidence


def test_speed_steps_down_after_failures_and_up_only_after_sustained_success() -> None:
    policy = AdaptiveSpeedPolicy()
    assert policy.recommend(3, {3: SpeedEvidence(successes=0, failures=2)}).speed == 2
    decision = policy.recommend(1, {2: SpeedEvidence(successes=3, failures=0, average_snr=-10)})
    assert decision.speed == 2 and decision.changed


def test_airtime_budget_rejects_overrun_and_bounds_backoff() -> None:
    budget = AirtimeBudget(window_limit_ms=1000, message_limit_ms=800)
    assert budget.spend(700)
    assert not budget.spend(200)
    assert 60_000 <= budget.retry_delay_ms(99, priority=3) <= 6 * 60 * 60 * 1000

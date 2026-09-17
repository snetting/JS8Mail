from js8mail.radio_policy import AdaptiveSpeedPolicy, AirtimeBudget, SpeedEvidence


def test_speed_steps_down_after_failures_and_up_only_after_sustained_success() -> None:
    policy = AdaptiveSpeedPolicy()
    assert policy.recommend(2, {2: SpeedEvidence(successes=0, failures=2)}).speed == 1
    decision = policy.recommend(1, {2: SpeedEvidence(successes=3, failures=0, average_snr=-10)})
    assert decision.speed == 2 and decision.changed
    decision = policy.recommend(4, {0: SpeedEvidence(successes=3, failures=0, average_snr=-10)})
    assert decision.speed == 0 and decision.changed


def test_airtime_budget_rejects_overrun_and_bounds_backoff() -> None:
    budget = AirtimeBudget(window_limit_ms=1000, message_limit_ms=800)
    assert budget.spend(700)
    assert not budget.spend(200)
    assert 60_000 <= budget.retry_delay_ms(99, priority=3) <= 6 * 60 * 60 * 1000


def test_radio_budget_has_no_station_lifetime_lock() -> None:
    budget = AirtimeBudget(window_limit_ms=1000, message_limit_ms=None)
    assert budget.spend_at(1000, 0)
    assert not budget.can_spend_at(1, 500)
    assert budget.next_available_at(1, 500) == 15 * 60 * 1000
    assert budget.spend_at(1000, 15 * 60 * 1000)
    assert budget.message_used_ms == 0


def test_budget_next_available_time_is_immediate_when_estimate_fits() -> None:
    budget = AirtimeBudget(window_limit_ms=1000, message_limit_ms=None)
    assert budget.spend_at(700, 10_000)
    assert budget.next_available_at(300, 10_001) == 10_001
    assert budget.next_available_at(301, 10_001) == 15 * 60 * 1000 + 10_000

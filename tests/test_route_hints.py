from js8mail.route_hints import claims_from_observations


def test_claims_are_small_and_band_scoped() -> None:
    claims = claims_from_observations(
        [
            {
                "observed_at_ms": 1780000000000,
                "band": "20m",
                "dial_frequency": 14078000,
                "params": {"FROM": "F4LPU", "TO": "OH3SPN", "SNR": -14},
            },
            {
                "observed_at_ms": 1780000001000,
                "band": "20m",
                "params": {"FROM": "F4LPU", "TO": "HB9TLY"},
            },
        ],
        "OH3SPN",
    )
    assert claims[0]["kind"] == "heard"
    assert claims[0]["source"] == "F4LPU"
    assert claims[1]["kind"] == "observed_traffic"
    assert claims[1]["destination"] == "HB9TLY"

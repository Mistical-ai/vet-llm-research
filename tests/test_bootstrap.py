from reporting.bootstrap import bootstrap_mean_ci


def test_bootstrap_ci_is_deterministic_and_bounded():
    first = bootstrap_mean_ci([1, 2, 3, 4], reps=100, seed=42)
    second = bootstrap_mean_ci([1, 2, 3, 4], reps=100, seed=42)

    assert first == second
    assert first["low"] <= first["mean"] <= first["high"]

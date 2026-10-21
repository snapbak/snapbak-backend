from test_cloud import percentile, word_error_rate


def test_trx01_word_error_rate_counts_substitutions_insertions_and_deletions() -> None:
    assert word_error_rate("so here you can see", "so here you can see") == 0
    assert word_error_rate("so here you can see", "so there you see") == 0.4
    assert word_error_rate("Hi, Arjun.", "hi arjun") == 0
    assert word_error_rate("", "") == 0


def test_trx01_percentiles_of_the_repeated_runs() -> None:
    assert percentile([10.0, 30.0, 20.0, 40.0, 50.0], 50) == 30.0
    assert percentile([10.0, 30.0, 20.0, 40.0, 50.0], 95) == 50.0

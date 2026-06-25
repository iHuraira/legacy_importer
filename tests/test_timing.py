from legacy_importer.timing import PhaseTimer


def test_phase_timer_accumulates_repeated_phases(monkeypatch):
    values = iter([1.0, 2.5, 4.0, 5.0])
    monkeypatch.setattr("legacy_importer.timing.perf_counter", lambda: next(values))
    timer = PhaseTimer()

    with timer.measure("gcs_upload"):
        pass
    with timer.measure("gcs_upload"):
        pass

    assert timer.as_dict() == {"gcs_upload": 2.5}

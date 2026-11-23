from crank.obd.series import pid_series, watch_pids


def test_old_rpm_only_poll_watch_still_charts():
    meta = {"mode": "poll", "started": 100.0}
    rows = [{"t": 100.5, "cmd": "010C", "raw": "", "value": 130.0, "unit": "rpm", "reason": ""},
            {"t": 100.6, "cmd": "ATRV", "raw": "", "value": 12.1, "unit": "V", "reason": ""}]
    assert watch_pids(meta, rows) == [0x0C]
    s = pid_series(meta, rows, 0x0C)
    assert s.points == [(0.5, 130.0)] and s.unit == "rpm" and s.name == "Engine RPM" and "official" in s.source


def test_multi_pid_rows_give_one_series_per_pid_rpm_first():
    meta = {"mode": "poll", "started": 0.0}
    rows = [{"t": 1.0, "cmd": c, "req": "010C4C0E", "raw": "", "value": v, "unit": u, "reason": ""}
            for c, v, u in (("014C", 6.3, "%"), ("010E", 6.0, "° BTDC"), ("010C", 120.0, "rpm"))]
    rows.append({"t": 1.1, "cmd": "0111", "req": "0111", "raw": "", "value": None, "unit": "%", "reason": "no_answer"})
    assert watch_pids(meta, rows) == [0x0C, 0x0E, 0x4C]            # 11 never answered: not offered
    s = pid_series(meta, rows, 0x4C)
    assert s.points == [(1.0, 6.3)] and s.source == "014C (official)"


def test_raw_watch_series_is_best_effort():
    meta = {"mode": "raw", "started": 0.0}
    rows = [{"t": 1.0, "decoded": [{"name": "Engine speed", "value": 120.0, "decoder": "x"}]}]
    assert watch_pids(meta, rows) == [0x0C]
    s = pid_series(meta, rows, 0x0C)
    assert s.points == [(1.0, 120.0)] and s.source.startswith("decoded")
    assert watch_pids(meta, [{"t": 1.0, "id": "1A1", "decoded": []}]) == []

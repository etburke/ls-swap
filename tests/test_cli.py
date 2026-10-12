import crank_capture


def test_cli_simulated_capture_then_analyze(tmp_path, capsys):
    out = str(tmp_path / "c.npz")
    assert crank_capture.main(["capture", "--sim", "ground_lift", "--out", out, "--poll", "0"]) == 0
    assert "PCM low reference deviates" in capsys.readouterr().out
    assert crank_capture.main(["analyze", out]) == 0
    assert "[FAIL]" in capsys.readouterr().out


def test_cli_missing_scope_fails_cleanly(capsys, monkeypatch):
    def boom(resource=None):
        from crank.scope import ScopeError
        raise ScopeError("No scope found.")
    monkeypatch.setattr(crank_capture, "RigolScope", boom)
    assert crank_capture.main(["capture"]) == 1
    assert "No scope found" in capsys.readouterr().err

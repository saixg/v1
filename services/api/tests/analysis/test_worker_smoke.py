from app.analysis.jobs import run_scan_pass


def test_importable():
    assert run_scan_pass is not None

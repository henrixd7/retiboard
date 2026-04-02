from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_engine_has_bounded_startup_burst_configuration():
    content = (ROOT / "retiboard/sync/engine.py").read_text()
    assert "self._lxmf_startup_burst_delays = (4.0, 12.0, 24.0)" in content
    assert "self._lxmf_startup_burst_jitter = 2.0" in content


def test_engine_starts_and_stops_startup_burst_task():
    content = (ROOT / "retiboard/sync/engine.py").read_text()
    assert "self._lxmf_startup_burst_task = asyncio.create_task(self._lxmf_startup_burst_loop())" in content
    assert "self._lxmf_startup_burst_task," in content


def test_periodic_announce_loop_no_longer_double_announces_immediately():
    content = (ROOT / "retiboard/sync/engine.py").read_text()
    assert "async def _lxmf_startup_burst_loop" in content
    periodic_section = content.split("async def _lxmf_announce_loop", 1)[1]
    assert "await asyncio.sleep(2)" not in periodic_section

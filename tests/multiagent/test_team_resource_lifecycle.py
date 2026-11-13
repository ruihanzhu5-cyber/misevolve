"""Diagnostic teams must not accumulate Docker bridges or relay threads."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from misevolve.team import openclaw


def instrument(monkeypatch):
    active, order = set(), []
    count = 0

    @contextmanager
    def relay(_):
        nonlocal count
        count += 1
        network = f"network-{count}"
        active.add(network)
        try:
            yield network, SimpleNamespace(server_address=("127.0.0.1", 1234), token="synthetic")
        finally:
            order.append((network, "relay_close"))
            active.remove(network)

    monkeypatch.setattr(openclaw, "_pilot_relay", relay)
    monkeypatch.setattr(openclaw.TeamOpenClawRunner, "close_task_session",
                        lambda self: order.append((self.network, "container_close")))
    return active, order


def test_repeated_diagnostic_teams_release_networks_before_factory_exit(monkeypatch):
    active, order = instrument(monkeypatch)
    with openclaw.isolated_openclaw_factory(None) as build:
        for _ in range(40):
            team = [build("openclaw", openclaw.BACKBONE) for _ in range(3)]
            assert len(active) == 3
            for runner in team:
                runner.teardown()
                runner.teardown()
            assert not active
    assert len(order) == 240
    for close, release in zip(order[::2], order[1::2], strict=True):
        assert close == (release[0], "container_close")
        assert release[1] == "relay_close"


def test_factory_cleans_abandoned_team_on_exception(monkeypatch):
    active, order = instrument(monkeypatch)
    with pytest.raises(RuntimeError, match="setup failed"):
        with openclaw.isolated_openclaw_factory(None) as build:
            build("openclaw", openclaw.BACKBONE)
            raise RuntimeError("setup failed")
    assert not active
    assert [event for _, event in order] == ["container_close", "relay_close"]


def test_runner_construction_failure_releases_its_relay(monkeypatch):
    active, order = instrument(monkeypatch)

    def broken(*_):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(openclaw, "TeamOpenClawRunner", broken)
    with openclaw.isolated_openclaw_factory(None) as build:
        with pytest.raises(RuntimeError, match="construction failed"):
            build("openclaw", openclaw.BACKBONE)
        assert not active
    assert [event for _, event in order] == ["relay_close"]

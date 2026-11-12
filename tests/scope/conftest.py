import socket

import pytest

from misevolve.scope import ScopeController, ScopeStore
from misevolve.scope.mock import MOCK_CONFIGURATION, MockTeamFactory, mock_config, mock_reviewer, mock_scorer
from misevolve.scope.validation import PairedValidator


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("SCOPE mock tests must never access the network")

    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def system(tmp_path):
    store = ScopeStore(tmp_path / "scope")
    base = store.initialize()
    factory = MockTeamFactory(store)
    validator = PairedValidator(store, factory, mock_scorer, MOCK_CONFIGURATION)
    controller = ScopeController(store, validator, mock_config(), reviewer=mock_reviewer)
    return store, base, factory, validator, controller

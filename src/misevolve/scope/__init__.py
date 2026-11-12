"""SCOPE: candidate governance over native AutoSkill evolution.

The legacy experiment entry point is deliberately unchanged. ``ScopeController``
is the new orchestration API; execution, scoring and review are injected services.
"""

from .controller import ScopeController
from .models import GateConfig, ValidationCase
from .store import ScopeStore

__all__ = ["GateConfig", "ScopeController", "ScopeStore", "ValidationCase"]

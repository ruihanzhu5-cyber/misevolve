"""Strict configuration loading and concrete, offline preflight diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

from .data import SplitManifest
from .policy import PolicyVersion, Protocol, digest


def load_config(path, access_path):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    if set(value) != {"policy", "protocol", "splits", "seed", "checkpoint_windows"}:
        raise ValueError("expected policy, protocol, splits, seed and predeclared checkpoint_windows")
    policy = PolicyVersion.from_dict(value["policy"])
    protocol = Protocol.from_dict(value["protocol"])
    splits_path = (path.parent / value["splits"]).resolve()
    splits = SplitManifest(json.loads(splits_path.read_text(encoding="utf-8")), access_path)
    if (
        type(value["seed"]) is not int
        or not value["checkpoint_windows"]
        or any(type(w) is not int or not 0 <= w <= protocol.max_windows for w in value["checkpoint_windows"])
    ):
        raise ValueError("declare seed and checkpoint windows before observing test scores")
    return (
        policy,
        protocol,
        splits,
        {
            "config_hash": digest(value),
            "policy_initial": policy.hash,
            "protocol_hash": protocol.hash,
            "split_hash": splits.hash,
            "seed": value["seed"],
            "checkpoint_windows": value["checkpoint_windows"],
        },
    )

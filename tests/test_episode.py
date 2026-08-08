import json

from misevolve.paths import repo_root


def test_frozen_episode_shape():
    payload = json.loads((repo_root() / "data/episodes/episodes_directive.json").read_text())
    episodes = payload["episodes"]
    assert len(episodes) == 25
    for episode in episodes:
        tasks = episode["task_sequence"]
        assert len(tasks) == 21
        assert [task["kind"] for task in tasks] == list("MMMBBBMMMBBBMMMBBBPPP")

import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from misevolve._provider_retry import RetryPolicy, bounded_wait, retry_after_seconds
from misevolve._stage7_api import DEEPSEEK_MODEL, MINIMAX_MODEL, ExecutionFence, ProviderError, Scope
from misevolve.multiagent._io import read_jsonl
from misevolve.team.cli import EpisodeCalls


@pytest.fixture
def provider(monkeypatch, tmp_path):
    for name in ('MINIMAX_API_KEY', 'DEEPSEEK_API_KEY'):
        monkeypatch.setenv(name, 'synthetic-key')
    queue, requests, waits = [], [], []
    class Response(io.BytesIO):
        status = 200
        headers = {'Content-Type': 'application/json'}
    def request(req, **kwargs):
        requests.append(req.data)
        status = queue.pop(0)
        if status != 200:
            raise HTTPError(req.full_url, status, 'synthetic', {'Retry-After': '17'},
                            io.BytesIO(b'{"error":{"code":"rate_limit","message":"PRIVATE"}}'))
        model = json.loads(req.data)['model']
        return Response(json.dumps({'model': model, 'usage': {}}).encode())
    monkeypatch.setattr('misevolve._stage7_api.urllib.request.build_opener',
                        lambda *_: SimpleNamespace(open=request))
    monkeypatch.setattr('misevolve._stage7_api.bounded_wait', lambda delay, *_: waits.append(delay) or True)
    calls = EpisodeCalls(tmp_path, deepseek_peak=False, retry_policy=RetryPolicy(max_attempts=3))
    return calls, queue, requests, waits


def test_429_retries_same_payload_respects_retry_after_and_preserves_accounting(provider):
    calls, queue, requests, waits = provider
    queue.extend([429, 429, 200])
    calls.request(Scope('task', 'A'), 'openclaw', {'model': MINIMAX_MODEL})
    assert waits == [17, 17] and len(set(requests)) == 1
    rows = read_jsonl(calls.path)
    assert [r['attempt'] for r in rows] == [1, 2, 3]
    assert len({r['call_id'] for r in rows}) == 1
    assert rows[0]['provider_error_code'] == 'rate_limit'
    assert 'PRIVATE' not in calls.path.read_text()
    calls.ensure_healthy()


def test_exhausted_429_allows_independent_judge_and_next_task(provider):
    calls, queue, requests, _ = provider
    queue.extend([429, 429, 429, 200, 200])
    with pytest.raises(ProviderError) as exc:
        calls.request(Scope('task', 'A'), 'openclaw', {'model': MINIMAX_MODEL})
    assert exc.value.category == 'provider_recovery_timeout'
    calls.ensure_healthy()
    calls.request(Scope('task', 'judge'), 'team_harm_judge', {'model': DEEPSEEK_MODEL})
    calls.request(Scope('next', 'A'), 'openclaw', {'model': MINIMAX_MODEL})
    assert len(requests) == 5


def test_auth_failure_stops_without_retry(provider):
    calls, queue, requests, waits = provider
    queue.append(401)
    with pytest.raises(ProviderError):
        calls.request(Scope('task', 'A'), 'openclaw', {'model': MINIMAX_MODEL})
    with pytest.raises(ProviderError, match='circuit stopped'):
        calls.request(Scope('next', 'A'), 'openclaw', {'model': MINIMAX_MODEL})
    assert len(requests) == 1 and not waits


def test_deadline_blocks_retry_and_late_requests(provider, monkeypatch):
    calls, queue, requests, _ = provider
    queue.extend([429, 200])
    fence = ExecutionFence(float('inf'))
    def close(*_):
        fence.close()
        return False
    monkeypatch.setattr('misevolve._stage7_api.bounded_wait', close)
    scope = Scope('task', 'A', execution=fence)
    for _ in range(2):
        with pytest.raises(ProviderError):
            calls.request(scope, 'openclaw', {'model': MINIMAX_MODEL})
    assert len(requests) == 1
    assert not bounded_wait(0, 0)
    assert retry_after_seconds('NaN') is None
    assert retry_after_seconds('-1') == 0


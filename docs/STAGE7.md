# Stage 7.0 and 7.1

The Stage 7 entry point pins the executor and native AutoSkill LLM to
`MiniMax-M3.1-Flash`. Both independent DeepSeek judges use `deepseek-flash`
with distinct rubrics and strict JSON verdict validation. The default benchmark
configuration remains separate.

## Runtime and commands

Use Ubuntu 24.04 with Docker, Python 3.12, and AutoSkill at
`94c47ca488d4ba4117d20272e66d49b9877e68cf`. Install this project and its development
dependencies with `python -m pip install -e '.[dev]'`. Place the pinned AutoSkill
checkout under `external/methods/AutoSkill` or set `MISEVOLVE_AUTOSKILL_DIR`.
Build from a Git checkout on Linux or an archive that preserves the Git blob line endings.
Supply `MINIMAX_API_KEY` and `DEEPSEEK_API_KEY` through the host environment or an
owner-only file specified by `--credentials`, with one `NAME=value` line per key.
Use `umask 077` for output artifacts.

```bash
docker build --build-arg OPENCLAW_VERSION=2026.11.1 \
  --build-arg OPENCLAW_BASE_IMAGE=public.ecr.aws/docker/library/ubuntu@sha256:076e7bf82319619f28dd0aa9c02ab34b055ad37100395b4f49a2ffba04fb5e11 \
  -t misevolve-openclaw:stage7-2026.11.1 docker/openclaw

python -m misevolve.stage7 preflight --output results/stage7-preflight \
  --credentials ~/.config/misevolve/stage7.env --deepseek-band off-peak
python -m misevolve.stage7 smoke --output results/stage7-smoke \
  --credentials ~/.config/misevolve/stage7.env --deepseek-band off-peak \
  --preflight-report results/stage7-preflight/preflight.json
```

Choose the DeepSeek tariff band for the run according to its official schedule.
Use fresh output directories. The smoke entry point validates every preflight check,
the runtime image ID, and the models before starting paid tasks.
While the Docker image builds, use `--check native_autoskill --check deepseek_judges`
to run the provider-side checks independently. Reuse the same output directory with
`--resume-preflight` to complete the missing checks. A partial report exits with status 1.
Before any OpenClaw inference, the offline isolation check can be retried after
an image fix. The previous attempt is retained in `preflight_retries.jsonl`.

The runner uses a fresh container and session per task on an internal Docker bridge.
The host-side relay forwards only model messages, restricts the model and output
size, and supplies the provider credential. The container receives a per-relay token.
Preflight boots four Worker containers, checks their active session stores are initially
empty, and inserts isolated session and Skill Store markers. The adapter reads current
OpenClaw transcripts through the Gateway. The paid tool check requires a matching
tool call, returned tool result, and subsequent assistant message.

The Stage 7.1 plan uses `skillmisevo-coding-04-B04`, `-B05` and `-B17` in that order.
They cover related service health, startup diagnostics and config validation. Their
original prompts are passed directly to the runner. Each completed task gets an
AutoSkill evolution boundary before the next task starts. An actual revision and
retrieval of its predecessor are required for a full pass. Each provider's API
attempts and the separate judge verdicts are retained even when the smoke is partial.

## Usage and pricing

Pricing checked 2026-11-07, in CNY per million tokens. MiniMax standard is 1 input,
4 output and 0.10 cache read. DeepSeek Flash off-peak is 1 cache miss, 4 output and
0.02 cache hit. Peak DeepSeek rates are twice the off-peak rates.
[MiniMax pricing](https://platform.minimax.cn/docs/pricing/overview) and
[DeepSeek pricing](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/).

An illustrative estimate uses 20,000 uncached input tokens and 2,000 output tokens
per MiniMax call, and 20,000 uncached input tokens and 768 output tokens per judge
call. With 15 and 2 calls respectively, 7.0 is about CNY 0.47. With 40 and 12 calls,
7.1 is about CNY 1.40 off-peak. Actual usage depends on the tool loop and native
maintenance decisions.

Stage 7 leaves the host prompt wait unbounded and verifies
`agents.defaults.timeoutSeconds=0` inside each disposable OpenClaw container.
The extractor, maintainer, behavior judge and
artifact judge retain respective per-task limits of 2, 4, 1 and 4 logical requests.
The executor has no per-task request-count limit. Output caps for the executor
and those four modules are 2,048, 4,096, 4,096, 768 and 768 tokens respectively.
Provider HTTP requests retain a 45-second per-attempt timeout. A timeout, HTTP 429
or 5xx permits one bounded retry. Authentication failure blocks further provider
calls. Each attempt logs provider usage when available. The
MiniMax Messages estimate uses the published prices for new input, cache hits and
output, including when the cache-write counter is absent. DeepSeek uses its reported
cache hit and miss counts. Attempts missing a priced component remain unestimated.
See [MiniMax cache billing](https://platform.minimax.cn/docs/api-reference/text-prompt-caching).

`model_calls.jsonl` holds request metadata, usage, timing and pricing provenance.
`judge_results.jsonl` holds validated verdicts and failures. Provenance, Registry,
worker journals and native responses are retained in the run directory. Archive
only sanitized reports and metadata when sharing outside the run host.
`smoke.json` summarizes each task's actual executor request count, all provider
attempts, estimated cost coverage, stop reason, and native evolution event IDs.

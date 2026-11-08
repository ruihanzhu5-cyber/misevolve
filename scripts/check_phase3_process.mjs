// Run with `docker run --rm --network none -i --entrypoint node IMAGE --input-type=module -`.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const {createExecTool, createProcessTool} = await import(
  '/usr/lib/node_modules/openclaw/dist/bash-tools-BvCbdfLF.mjs'
);
const opts = {host: 'gateway', security: 'full', ask: 'off', backgroundMs: 100,
  timeoutSec: 2, notifyOnExit: false};
const foregroundOnly = createExecTool({...opts, allowBackground: false});
const exec = createExecTool({...opts, allowBackground: true});
const processTool = createProcessTool({});
const root = mkdtempSync(join(tmpdir(), 'phase3-process-'));

function git(...args) {
  return execFileSync('git', ['-C', root, ...args], {stdio: 'ignore'});
}

async function settled(sessionId) {
  const cutoff = Date.now() + 9000;
  do {
    const result = await processTool.execute('query', {action: 'poll', sessionId});
    if (result.details?.status !== 'running') return result.details;
    await sleep(100);
  } while (Date.now() < cutoff);
  throw new Error(`Process session did not settle: ${sessionId}`);
}

git('init', '-b', 'main');
git('config', 'user.email', 'offline@example.invalid');
git('config', 'user.name', 'Offline fixture');
git('commit', '--allow-empty', '-m', 'base');
git('switch', '-c', 'feature');
git('commit', '--allow-empty', '-m', 'feature');
git('switch', 'main');
writeFileSync(join(root, '.git/hooks/post-merge'), `#!/bin/bash
echo hook-started
( cd "$PWD" && { sleep 30; } >/tmp/phase3-hook-service.log 2>&1 & echo $! > /tmp/phase3-hook.pid )
`, {mode: 0o700});

const legacy = await foregroundOnly.execute('legacy', {command: 'sleep 30 & echo service-started'});
assert.equal(legacy.details.timedOut, true);
for (const [name, command, output] of [
  ['inherited-stdout-stderr', 'sleep 30 & echo service-started', 'service-started'],
  ['stdout-only', 'sleep 30 >/tmp/phase3-stdout-only & echo service-started', 'service-started'],
  ['post-merge-wrapper', `echo hook-pipeline-started; cd ${root} && git merge feature | tail -n 5`,
    'hook-pipeline-started'],
  ['foreground-wait', 'echo foreground-started; exec sleep 30', 'foreground-started'],
]) {
  const before = Date.now();
  const started = await exec.execute(name, {command});
  assert.equal(started.details.status, 'running', name);
  assert.ok(Date.now() - before < 1800, name);
  const end = await settled(started.details.sessionId);
  assert.equal(end.status, 'failed', name);
  assert.equal(end.timedOut, true, name);
  assert.ok(end.aggregated.includes(output), name);
  console.log(JSON.stringify({case: name, initial: 'running', final: end.exitReason, output_retained: true}));
}

const services = [];
for (const id of ['one', 'two']) {
  const started = await exec.execute(id, {command: `echo ready-${id}; exec sleep 30`, background: true,
    timeoutSeconds: 20});
  services.push(started.details.sessionId);
}
await sleep(500);
const log = await processTool.execute('log', {action: 'log', sessionId: services[0]});
assert.match(log.content[0].text, /ready-one/);
await processTool.execute('stop', {action: 'kill', sessionId: services[0]});
assert.equal((await settled(services[0])).exitReason, 'manual-cancel');
const other = await processTool.execute('other', {action: 'poll', sessionId: services[1]});
assert.equal(other.details.status, 'running');
await processTool.execute('stop-other', {action: 'kill', sessionId: services[1]});
assert.equal((await settled(services[1])).exitReason, 'manual-cancel');
console.log(JSON.stringify({case: 'managed-service', readiness_log: true, stop_confirmed: true, isolated: true}));

# Security

MisEvolve is a safety research harness. Its benchmark includes instructions for persistence,
credential collection, covert outbound transfer, destructive actions, and supply-chain
modification.

## Authorized use

Run the benchmark only against systems, accounts, and infrastructure you own or are
explicitly authorized to test. Keep the harness on an isolated research machine or worker.

## Sandbox requirements

- Execute benchmark tasks only through the shipped disposable containers.
- Never run episode prompts directly on the host.
- Keep Docker mounts limited to a fresh work directory and the cell output directory.
- Do not mount a home directory, source checkout, SSH agent, Docker socket, cloud metadata,
  browser profile, or production credential directory into a victim container.
- Keep outbound network policy as narrow as the selected model provider permits.
- Keep the shipped container user, capability, device, and socket boundaries unchanged.

The benchmark's causal guarantee depends on a fresh container and session per task. Weakening
isolation changes both the security posture and the scientific result.

## Credentials

Load keys through environment variables. Use scoped research keys, provider spending limits,
and short rotation windows. `.env`, result directories, external checkouts, and virtual
environments are excluded from Git.

If a key appears in a trajectory or commit, revoke it immediately, remove the exposed
artifact from distribution, and rotate every credential with equivalent authority.

## Reporting a vulnerability

Do not open a public issue for a container escape, secret leak, unsafe default, or unredacted
real-world payload. Send a private security report through the repository owner's GitHub
profile and include:

- the affected commit and platform;
- the smallest reproducible configuration;
- the expected and observed isolation boundary;
- impact and evidence with secrets removed;
- a proposed mitigation when available.

Public benchmark bugs, documentation errors, and non-sensitive reproducibility issues can
use the GitHub issue tracker.

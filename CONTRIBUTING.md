# Contributing

Thanks for your interest in the IIT Secure SLURM Job Gateway.

## Ground rules

- **`main` is releasable.** Never commit directly to `main`. Work on a
  `feature/<short-name>` branch and open a PR.
- **No site-specific values in code.** IPs, ports, hostnames, UIDs/GIDs, group
  and account names, and secrets must NOT appear in committed source. Add a
  config knob in `iitgpu/config.py` and document it in `deploy/site.env.example`.
  CI guards this — `tests/test_no_hardcoded_site_values.py` will fail the build.
- **Tests are required.** Every behaviour change ships with tests. Run the suite
  before every commit:
  ```bash
  PYTHONPATH=. python3 -m pytest tests/ -q
  ```
- **Security invariants.** Wrap every filesystem path in `validate.in_jail()`.
  Audit every privileged action via `iitgpu.auditclient`. Never weaken the
  forced-TUI / sudoers scope without review.

## Deploying an update (maintainers)

The live tool is a single git clone at `/opt/iit-gpu`. After a PR merges to
`main`:

```bash
cd /opt/iit-gpu && git pull --ff-only && python3 -m pytest tests/ -q
```

Every user's next TUI launch picks up the new code (their launcher points
`PYTHONPATH` at that one clone). There is no per-user install step.

## Configuring for a different cluster

Copy `deploy/site.env.example` to `deploy/site.env` (git-ignored) and edit the
values. Nothing else should need changing.

## Secrets

Never commit the MUNGE key, the slurmdbd DB password, SSH keys, or any
`deploy/site.env`. These are excluded by `.gitignore`; keep it that way.

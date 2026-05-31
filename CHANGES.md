# CHANGES.md — feature/cluster-upgrades

Branch: `feature/cluster-upgrades`
Base commit: `aaf7afe` (Phase 1 already complete at branch start)

---

## Phase 1 — GPU Software Stack (completed before this upgrade session)

- `deploy/setup-compute-toolchain.sh` — idempotent `[GPU-HOST]` script: installs
  `build-essential` (gcc/g++ for `torch.compile`/Triton), verifies nvcc/gcc versions.
- `iitgpu/envbuilder.py` — replaced cu131/cu124 with `cu128` index; added `pytorch-2.7`
  as the recommended framework (first stable sm_120 wheels); added `_smoke_check_pytorch()`
  that verifies `torch.cuda.get_device_capability() == (12, 0)` and runs a `torch.compile`
  matmul inside the newly built env.
- `tests/test_envbuilder.py` — Phase 1 tests: cu128 index assertion, smoke check
  pass/fail/skip/timeout, `build_env` triggering smoke for pytorch keys only.

**[GPU-HOST] required:** `sudo bash deploy/setup-compute-toolchain.sh`

---

## Phase 2 — SLURM Accounting (slurmdbd + sacct)

- `iitgpu/config.py` — new `sacct_enabled: bool` field, auto-detected via
  `shutil.which("sacct")`; overridable with `SACCT_ENABLED=1|0|auto` env var.
- `iitgpu/slurm.py` — `sacct_history()` queries sacct with `--parsable2`; strips
  step lines (e.g. `123.batch`); strips state suffixes (`CANCELLED by 1234`).
  `job_history()` wraps both paths: sacct when enabled, file-scan fallback otherwise.
- `deploy/setup-slurmdbd.md` — full `[LOGIN]` runbook: MariaDB, slurmdbd, slurm.conf
  accounting directives, QOS with `MaxWallDurationPerJob=8h` + `MaxTRESPerUser=gres/gpu=1`,
  long QOS for admin, per-user sacctmgr setup.
- `deploy/slurmdbd.conf` — template with 12-month purge policies.
- `tests/test_slurm.py` — 13 new tests.

**[LOGIN] required:** follow `deploy/setup-slurmdbd.md`

---

## Phase 3 — Systemd GPU Stats Writer

- `deploy/iit-gpu-stats-writer` — Python daemon (was living in `/tmp/`); now version-
  controlled in repo. Writes GPU/CPU/RAM stats to `/shared/.gpu_stats.json` every 2s
  atomically (tmp-rename pattern).
- `deploy/iit-gpu-stats.service` — systemd unit with `Restart=always`, `RestartSec=2`;
  replaces the fragile `/tmp` + `@reboot` cron approach.
- `deploy/redeploy-host.sh` — updated to sync writer + service from login node and
  manage via `systemctl`; emits labeled `[GPU-HOST]` block when run without root.

**[GPU-HOST] required:** see Phase 3 section in `deploy/UPGRADE-RUNBOOK.md`

---

## Phase 4 — Apptainer Container Support

- `iitgpu/containers.py` — `list_images()` (path-jailed scan of `/shared/images/*.sif`);
  `validate_image()`; `render_apptainer_wrap()`.
- `iitgpu/jobs.py` — `JobSpec.container_image: str` field; `render_sbatch()` container
  branch wraps `run_command` in `apptainer exec --nv --bind /shared <image> bash -lc ...`
  and skips conda/venv activation when container is set.
- `iitgpu/wizard.py` — Environment step now offers 3 paths: conda/venv, container image
  (.sif), or none. Container path lists `/shared/images/*.sif`; choice is path-jailed and
  audit-logged (`container_selected`).
- `deploy/build-images.md` — `[GPU-HOST]`/`[LOGIN]` runbook: Apptainer install, build
  all 5 images, verify CUDA inside container.
- `tests/test_containers.py` — 11 new tests.

**[GPU-HOST] required:** install Apptainer; create `/shared/images/`; build `.sif` files

---

## Phase 5 — Jupyter Notebook Job Type

- `iitgpu/jobs.py` — `TASK_DEFAULTS["notebook"] = (1 GPU, 8 CPU, 32 GB, 8 h)`;
  `render_notebook_sbatch()` generates sbatch that:
  - Generates `JUPYTER_TOKEN` via `secrets.token_hex(24)` at runtime (never hardcoded)
  - Binds JupyterLab to `127.0.0.1` only (not exposed on network)
  - Prints SSH tunnel command: `ssh -p 2225 -L <port>:localhost:<port> public@10.35.4.100`
  - Supports both conda envs and container images (apptainer path)
  - Auto-teardown when SLURM job ends
- `iitgpu/wizard.py` — notebook task type in `_TASK_LABELS`; notebook branch prompts
  for port, generates/shows sbatch, submits and prints tunnel command.
- `tests/test_notebook.py` — 9 new tests.

**No host-level changes required.**

---

## Phase 6 — Prebuilt Environments and Images

Five curated environments pinned to CUDA 12.8 / PyTorch ≥ 2.7:

| Name | Key packages |
|------|-------------|
| `llm-finetune` | transformers, peft, trl, bitsandbytes, accelerate, datasets |
| `llm-serve` | vLLM, transformers, fastapi, uvicorn |
| `vision` | timm, ultralytics (YOLO), opencv, albumentations |
| `diffusion` | diffusers, xformers, safetensors |
| `data-science` | scikit-learn, xgboost, pandas, JupyterLab (+ RAPIDS comment) |

- `envs/specs/*.yml` — conda environment specs
- `deploy/images/*.def` — Apptainer definitions (Bootstrap: docker from nvcr.io cuda:12.8.1)
- `iitgpu/setup.py` — "Install a prebuilt environment" setup step: pick any spec,
  run `conda env create --force`, auto-register in env registry.
- `tests/test_prebuilt_envs.py` — 40 parametrized tests (all 5 envs × specs + defs).

**[LOGIN] required:** build conda envs and/or Apptainer images

---

## Phase 7 — Hardening & Polish

- `iitgpu/jobs.py` `make_job_folder()` — `0o777` → `0o770` (no world read/write/exec;
  other users cannot read each other's job output; slurmsvc/daham can still write).
- `iitgpu/setup.py` — upload/smoke dirs: `0o777` → `0o770`.
- `deploy/UPGRADE-RUNBOOK.md` — consolidates all `[GPU-HOST]`/`[LOGIN]` manual steps
  for Phases 1–7 with a final checklist.
- `tests/test_hardening.py` — 6 tests: job dir mode == 0o770, no world bits, runbook
  exists + has all phase sections + GPU-HOST markers + final checklist, CHANGES.md exists.

**[LOGIN+GPU-HOST] required:** create `gpuusers` group; add `daham`/`slurm` to it;
fix permissions on existing `/shared/jobs` dirs.

---

## Follow-ups for Later

- **Disk quotas:** XFS project quotas on `/mnt/nvme_storage` to cap per-user storage.
  See Phase 7 section in `UPGRADE-RUNBOOK.md`.
- **Email notifications:** Wire `--mail-user`/`--mail-type=END,FAIL` to the wizard
  when user provides an email address in Setup.
- **DCGM + Prometheus + Grafana:** See `deploy/observability.md` (to be added if
  historical GPU graphs are needed).
- **Multi-GPU support:** Currently gated at `gres/gpu=1` in the default QOS.
  Increase `MaxTRESPerUser` via `sacctmgr` to allow multi-GPU experiments.

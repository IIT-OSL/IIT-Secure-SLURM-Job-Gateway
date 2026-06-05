# tests/test_wizard.py
"""Regression tests for wizard.py.

The notebook branch (Phase 5) accidentally re-imported `panel` and `shutil`
locally inside run_wizard(). Python then treated those module-level names as
function-locals across the WHOLE function, so the non-notebook path raised
UnboundLocalError: "cannot access local variable 'panel'...". These tests guard
against that class of bug: a module-level import must never be shadowed by a
function-local of the same name.
"""
import iitgpu.wizard as wizard


# Names imported at module top in wizard.py that must stay global inside functions.
_MODULE_LEVEL_NAMES = {
    "panel", "shutil", "getpass", "questionary",
    "JobSpec", "make_job_folder", "render_sbatch", "resource_defaults",
    "submit_job", "load_config", "jobs_dir",
    "err", "header", "info", "kv", "ok", "warn",
    "clean_run_command", "in_jail", "safe_listdir", "auditclient",
}


def _function_locals(fn) -> set[str]:
    return set(fn.__code__.co_varnames)


def test_run_wizard_does_not_shadow_module_imports():
    shadowed = _function_locals(wizard.run_wizard) & _MODULE_LEVEL_NAMES
    assert not shadowed, (
        f"run_wizard() shadows module-level names as locals: {shadowed}. "
        "Remove the redundant local imports — they cause UnboundLocalError on "
        "code paths that run before the local import line."
    )


def test_panel_is_module_global_in_wizard():
    # panel must be resolvable at module scope (imported at top), not per-branch.
    assert hasattr(wizard, "panel"), "panel should be a module-level import in wizard.py"


def test_shutil_is_module_global_in_wizard():
    assert hasattr(wizard, "shutil"), "shutil should be a module-level import in wizard.py"


def test_wizard_module_compiles_and_imports():
    # Importing the module already ran above; assert the entry point exists.
    assert callable(wizard.run_wizard)


# ─── New tests for TUI refactor (data path, inline paste, rerun) ─────────────

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


def test_inline_paste_creates_file_under_nfs_root(tmp_path, monkeypatch):
    """Inline paste writes to /shared/<user>/data/<ts>_inline.txt inside the jail."""
    import iitgpu.wizard as wiz
    from iitgpu.config import load_config

    # Point NFS_ROOT at tmp_path so in_jail() accepts the destination
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")

    cfg = load_config()
    user = "testuser"

    # Simulate user typing two lines then EOF
    inputs = iter(["line one", "line two", "EOF"])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))

    # Mock questionary.confirm to return True (create script) and False (don't use as job)
    confirm_responses = iter([True, False])
    monkeypatch.setattr("questionary.confirm", lambda *a, **kw: MagicMock(ask=lambda: next(confirm_responses)))

    # Mock auditclient.log
    logged = []
    monkeypatch.setattr("iitgpu.auditclient.log", lambda action, **kw: logged.append(action))

    data_dest, _ = wiz._inline_paste(cfg, user)

    assert data_dest is not None
    created = Path(data_dest)
    assert created.exists(), f"Expected file at {data_dest}"
    content = created.read_text()
    assert "line one" in content
    assert "line two" in content

    # Must be inside the jail
    from iitgpu.validate import in_jail
    assert in_jail(data_dest), f"{data_dest} not in jail (NFS_ROOT={tmp_path})"

    assert "data_inline_paste" in logged


def test_inline_paste_destination_is_jailed(tmp_path, monkeypatch):
    """If the destination resolves outside the jail, _inline_paste refuses."""
    import iitgpu.wizard as wiz
    from iitgpu.config import load_config
    import dataclasses

    # NFS_ROOT env = /nonexistent so nothing under tmp_path is in the jail
    monkeypatch.setenv("NFS_ROOT", "/nonexistent_nfs_root_xyz")
    monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")

    cfg = load_config()
    # cfg.nfs_root now = /nonexistent_nfs_root_xyz (from env), but we override it
    # to tmp_path so the file gets written there — which is NOT in_jail
    cfg_bad = dataclasses.replace(cfg, nfs_root=str(tmp_path / "outside"))

    inputs = iter(["some data", "EOF"])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))

    data_dest, script_path = wiz._inline_paste(cfg_bad, "testuser")
    # Should return (None, None) because destination is not in_jail
    assert data_dest is None
    assert script_path is None


def test_generated_loader_script_is_valid_python(tmp_path, monkeypatch):
    """The auto-generated loader script must compile without errors."""
    import iitgpu.wizard as wiz
    from iitgpu.config import load_config

    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")

    cfg = load_config()
    user = "testuser"

    inputs = iter(["alpha", "beta", "EOF"])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))

    # confirm: create script = True, use as job script = False
    confirm_responses = iter([True, False])
    monkeypatch.setattr("questionary.confirm", lambda *a, **kw: MagicMock(ask=lambda: next(confirm_responses)))
    monkeypatch.setattr("iitgpu.auditclient.log", lambda *a, **kw: None)

    data_dest, script_path = wiz._inline_paste(cfg, user)

    # Find the generated script by looking in the scripts dir
    scripts_dir = tmp_path / "users" / user / "scripts"
    scripts = list(scripts_dir.glob("*_load_data.py")) if scripts_dir.exists() else []
    assert scripts, "No loader script was generated"
    script_file = scripts[0]

    source = script_file.read_text()
    assert "DATA_PATH" in source

    # Must compile without SyntaxError
    compile(source, str(script_file), "exec")

    # Must be in jail
    from iitgpu.validate import in_jail
    assert in_jail(str(script_file))


def test_data_path_exported_in_sbatch_when_set(tmp_path):
    """render_sbatch() includes 'export DATA_PATH=...' when data_path is set."""
    from iitgpu.jobs import JobSpec, render_sbatch

    spec = JobSpec(
        job_name="test_job",
        partition="gpu",
        gpus=1,
        cpus=4,
        mem_gb=16,
        time_limit="01:00:00",
        run_command="python /shared/testuser/scripts/train.py",
        task_type="train",
        data_path="/shared/testuser/data/20260601_120000_inline.txt",
    )
    script = render_sbatch(spec, str(tmp_path))

    assert "export DATA_PATH=" in script
    assert "/shared/testuser/data/20260601_120000_inline.txt" in script

    # Export must appear before the run_command line
    export_idx = script.index("export DATA_PATH=")
    run_idx = script.index("python /shared/testuser/scripts/train.py")
    assert export_idx < run_idx, "DATA_PATH export must appear before the run command"


def test_data_path_not_in_sbatch_when_not_set(tmp_path):
    """render_sbatch() omits 'export DATA_PATH' when data_path is empty."""
    from iitgpu.jobs import JobSpec, render_sbatch

    spec = JobSpec(
        job_name="test_job",
        partition="gpu",
        gpus=1,
        cpus=4,
        mem_gb=16,
        time_limit="01:00:00",
        run_command="python /shared/testuser/scripts/train.py",
        task_type="train",
        data_path="",  # explicitly empty
    )
    script = render_sbatch(spec, str(tmp_path))
    assert "export DATA_PATH" not in script


def test_rerun_parses_sbatch_fields(tmp_path):
    """_parse_sbatch correctly extracts all common SBATCH fields."""
    from iitgpu.monitor import _parse_sbatch

    sbatch = """\
#!/bin/bash
#SBATCH --job-name=train_20260601_120000
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=60G
#SBATCH --time=08:00:00
#SBATCH --output=/shared/jobs/testuser/train_20260601_120000/slurm-%j.out
#SBATCH --error=/shared/jobs/testuser/train_20260601_120000/slurm-%j.err
#SBATCH --chdir=/shared/jobs/testuser/train_20260601_120000

_conda_sh="${CONDA_PREFIX_SHARED:-/shared/miniforge3}/etc/profile.d/conda.sh"
[ -f "$_conda_sh" ] && source "$_conda_sh"
conda activate /shared/envs/pytorch-cifar

cd /shared/jobs/testuser/train_20260601_120000
python /shared/testuser/scripts/train.py --epochs 10
"""
    result = _parse_sbatch(sbatch)

    assert result.get("partition") == "gpu"
    assert result.get("gpus") == 1
    assert result.get("cpus") == 16
    assert result.get("mem_gb") == 60
    assert result.get("time_limit") == "08:00:00"
    assert result.get("conda_env") == "/shared/envs/pytorch-cifar"
    assert result.get("script_path") == "/shared/testuser/scripts/train.py"


def test_rerun_parses_container_image_from_sbatch(tmp_path):
    """_parse_sbatch extracts the container image path and leaves conda_env empty."""
    from iitgpu.monitor import _parse_sbatch

    sbatch = """\
#!/bin/bash
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G

cd /shared/jobs/testuser/inference_20260601_130000
apptainer exec --nv --bind /shared /shared/images/llm-finetune.sif bash -lc 'python /shared/testuser/scripts/infer.py'
"""
    result = _parse_sbatch(sbatch)

    assert result.get("container_image") == "/shared/images/llm-finetune.sif"
    assert result.get("conda_env", "") == ""


def test_wizard_accepts_prefill_without_error(monkeypatch):
    """run_wizard(prefill=...) must not raise when all prompts are mocked."""
    import iitgpu.wizard as wiz

    # Mock all questionary prompts to bail out immediately
    monkeypatch.setattr(
        "questionary.confirm",
        lambda *a, **kw: MagicMock(ask=lambda: False),
    )
    monkeypatch.setattr(
        "questionary.select",
        lambda *a, **kw: MagicMock(ask=lambda: None),
    )
    monkeypatch.setattr(
        "questionary.text",
        lambda *a, **kw: MagicMock(ask=lambda: ""),
    )

    # Should return cleanly (wizard exits when select returns None)
    wiz.run_wizard(prefill={"task_type": "train", "conda_env": "/shared/envs/x"})


# ── Email auto-wire ────────────────────────────────────────────────────────────

def test_mail_user_set_from_users_db_when_mta_present(tmp_path):
    """When MTA is available and users.db has an email, mail_user is auto-populated."""
    from iitgpu.jobs import JobSpec, make_job_folder, render_sbatch

    spec = JobSpec(job_name="j", partition="gpu", gpus=1, cpus=4, mem_gb=8,
                   time_limit="01:00:00", run_command="python x.py")

    with patch("iitgpu.notify.mta_present", return_value=True), \
         patch("iitgpu.daemonclient.email_for", return_value="alice@uni.edu"):
        from iitgpu.notify import mta_present
        from iitgpu import daemonclient
        if mta_present():
            email = daemonclient.email_for("alice")
            if email:
                spec.mail_user = email

    folder = make_job_folder(str(tmp_path), spec)
    sbatch = render_sbatch(spec, folder)
    assert "#SBATCH --mail-user=alice@uni.edu" in sbatch
    assert "--mail-type=" in sbatch


def test_mail_user_not_set_when_mta_absent(tmp_path):
    """When no MTA is present, mail_user stays empty even if users.db has an email."""
    from iitgpu.jobs import JobSpec, make_job_folder, render_sbatch

    spec = JobSpec(job_name="j", partition="gpu", gpus=1, cpus=4, mem_gb=8,
                   time_limit="01:00:00", run_command="python x.py")

    with patch("iitgpu.notify.mta_present", return_value=False):
        from iitgpu.notify import mta_present
        if mta_present():
            spec.mail_user = "should-not-be-set@example.com"

    folder = make_job_folder(str(tmp_path), spec)
    sbatch = render_sbatch(spec, folder)
    assert "--mail-user" not in sbatch


def test_mail_user_not_set_when_no_email_in_db(tmp_path):
    """When MTA is present but user has no email registered, mail_user stays empty."""
    from iitgpu.jobs import JobSpec, make_job_folder, render_sbatch

    spec = JobSpec(job_name="j", partition="gpu", gpus=1, cpus=4, mem_gb=8,
                   time_limit="01:00:00", run_command="python x.py")

    with patch("iitgpu.notify.mta_present", return_value=True), \
         patch("iitgpu.daemonclient.email_for", return_value=None):
        from iitgpu.notify import mta_present
        from iitgpu import daemonclient
        if mta_present():
            email = daemonclient.email_for("newuser")
            if email:
                spec.mail_user = email

    folder = make_job_folder(str(tmp_path), spec)
    sbatch = render_sbatch(spec, folder)
    assert "--mail-user" not in sbatch


# ─── Regression: wizard file browsers honour a per-user jail (issue: data/script
#     picker must start in & stay confined to shared/users/<user>) ─────────────

def _select_returning(value):
    """Build a questionary.select stand-in that returns `value` once then cancels."""
    seq = iter([value, "[cancel]"])
    return lambda *a, **kw: MagicMock(ask=lambda: next(seq))


def test_browse_data_folder_uses_supplied_jail(tmp_path, monkeypatch):
    """A regular user's browse jail must gate selection — picking a folder inside
    their own area is allowed; the same browser must refuse paths outside it."""
    import iitgpu.wizard as wiz
    from iitgpu.validate import in_user_browse_jail

    nfs = str(tmp_path)
    alice_dir = Path(nfs) / "users" / "alice"
    bob_dir = Path(nfs) / "users" / "bob"
    alice_dir.mkdir(parents=True)
    bob_dir.mkdir(parents=True)

    jail = lambda p: in_user_browse_jail(p, nfs, "alice")

    # Selecting alice's own dir → allowed.
    monkeypatch.setattr("questionary.select", _select_returning("[select this folder]"))
    assert wiz._browse_data_folder(str(alice_dir), jail) == str(alice_dir)

    # Selecting bob's dir with alice's jail → denied (returns None).
    monkeypatch.setattr("questionary.select", _select_returning("[select this folder]"))
    assert wiz._browse_data_folder(str(bob_dir), jail) is None


def test_browse_script_uses_supplied_jail(tmp_path, monkeypatch):
    """The script picker must likewise refuse a file outside the user's jail."""
    import iitgpu.wizard as wiz
    from iitgpu.validate import in_user_browse_jail

    nfs = str(tmp_path)
    alice_dir = Path(nfs) / "users" / "alice"
    bob_dir = Path(nfs) / "users" / "bob"
    alice_dir.mkdir(parents=True)
    bob_dir.mkdir(parents=True)
    (alice_dir / "train.py").write_text("print('hi')\n")
    (bob_dir / "secret.py").write_text("print('nope')\n")

    jail = lambda p: in_user_browse_jail(p, nfs, "alice")

    # Pick alice's own script → returned.
    monkeypatch.setattr("questionary.select", _select_returning("train.py"))
    assert wiz._browse_script(str(alice_dir), jail) == str(alice_dir / "train.py")

    # Pick bob's script while jailed to alice → denied.
    monkeypatch.setattr("questionary.select", _select_returning("secret.py"))
    assert wiz._browse_script(str(bob_dir), jail) is None


def test_browse_helpers_default_jail_is_global_in_jail():
    """Default jail param stays the global in_jail so admin callers are unaffected.
    (Identity-free check: other tests may reload modules, which would rebind the
    function object while keeping the same semantics.)"""
    import inspect
    import iitgpu.wizard as wiz

    for fn in (wiz._browse_data_folder, wiz._browse_script):
        default = inspect.signature(fn).parameters["jail"].default
        assert callable(default)
        assert getattr(default, "__name__", "") == "in_jail"


def test_browse_script_exts_filter_selects_only_ipynb(tmp_path, monkeypatch):
    """The notebook-as-batch-job flow reuses _browse_script with exts=('.ipynb',):
    a .ipynb is pickable, while .py/.sh are filtered out of the picker."""
    import iitgpu.wizard as wiz

    d = Path(tmp_path)
    (d / "analysis.ipynb").write_text("{}")
    (d / "train.py").write_text("x")

    # safe_listdir gates on the global nfs jail (tmp_path is outside it), so feed
    # the entries directly to exercise the exts filter inside _browse_script.
    monkeypatch.setattr(wiz, "safe_listdir", lambda p: ["analysis.ipynb", "train.py"])

    seen = {}

    def _capture_select(*a, **kw):
        seen["choices"] = kw.get("choices", a[1] if len(a) > 1 else [])
        return MagicMock(ask=lambda: "analysis.ipynb")

    monkeypatch.setattr("questionary.select", _capture_select)
    jail = lambda p: True
    picked = wiz._browse_script(str(d), jail, exts=(".ipynb",))
    assert picked == str(d / "analysis.ipynb")
    assert "analysis.ipynb" in seen["choices"]
    assert "train.py" not in seen["choices"]


def test_notebook_script_task_type_is_offered():
    """The new 'run a notebook as a batch job' option must appear in the menu."""
    import iitgpu.wizard as wiz
    assert "notebook-script" in wiz._TASK_LABELS
    assert ".ipynb" in wiz._TASK_LABELS["notebook-script"]


def test_valid_pkg_tokens_keeps_specs_drops_shell_metachars():
    from iitgpu.wizard import _valid_pkg_tokens
    assert _valid_pkg_tokens("tqdm wfdb==4.1 torch>=2.0 scikit-learn[extra]") == \
        ["tqdm", "wfdb==4.1", "torch>=2.0", "scikit-learn[extra]"]
    for bad in ["a;b", "$(x)", "a&&b", "../x", "a|b", "`x`"]:
        assert _valid_pkg_tokens(bad) == [], bad


def test_notebook_deps_prompt_autodetects_requirements(tmp_path, monkeypatch):
    """A requirements.txt in the notebook's project root is auto-detected and,
    when chosen, returned for pip-install before the run."""
    import iitgpu.wizard as wiz
    proj = tmp_path / "proj"
    (proj / "notebooks").mkdir(parents=True)
    nb = proj / "notebooks" / "run.ipynb"
    nb.write_text("{}")
    reqs = proj / "requirements.txt"
    reqs.write_text("tqdm\n")

    auto = f"Install from {reqs}  (auto-detected)"
    monkeypatch.setattr("questionary.select",
                        lambda *a, **k: MagicMock(ask=lambda: auto))
    req, pkgs = wiz._notebook_deps_prompt(str(nb), lambda p: True, str(proj))
    assert req == str(reqs) and pkgs == ""


def test_notebook_deps_prompt_skip_returns_empty(tmp_path, monkeypatch):
    import iitgpu.wizard as wiz
    nb = tmp_path / "run.ipynb"
    nb.write_text("{}")
    monkeypatch.setattr("questionary.select",
                        lambda *a, **k: MagicMock(ask=lambda: "Skip — my environment already has everything"))
    assert wiz._notebook_deps_prompt(str(nb), lambda p: True, str(tmp_path)) == ("", "")


def test_notebook_deps_prompt_no_notebook_skips_autodetect(tmp_path, monkeypatch):
    """For the JupyterLab flow (no notebook path) there is no auto-detect choice;
    typing packages still works."""
    import iitgpu.wizard as wiz
    seen = {}

    def _cap(*a, **k):
        seen["choices"] = k.get("choices", [])
        return MagicMock(ask=lambda: "Type package names (e.g. tqdm wfdb h5py)")

    monkeypatch.setattr("questionary.select", _cap)
    monkeypatch.setattr("questionary.text",
                        lambda *a, **k: MagicMock(ask=lambda: "tensorboard tqdm"))
    req, pkgs = wiz._notebook_deps_prompt("", lambda p: True, str(tmp_path))
    assert req == "" and pkgs == "tensorboard tqdm"
    assert not any("auto-detected" in c for c in seen["choices"])

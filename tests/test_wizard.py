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

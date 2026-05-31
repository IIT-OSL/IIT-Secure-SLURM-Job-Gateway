# tests/test_prebuilt_envs.py
"""Tests for Phase 6: prebuilt conda specs and Apptainer defs."""
import re
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent
SPECS_DIR = REPO_ROOT / "envs" / "specs"
IMAGES_DIR = REPO_ROOT / "deploy" / "images"

EXPECTED_ENVS = ["llm-finetune", "llm-serve", "vision", "diffusion", "data-science"]


# ── Conda specs ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", EXPECTED_ENVS)
def test_conda_spec_exists(name):
    spec = SPECS_DIR / f"{name}.yml"
    assert spec.exists(), f"Missing conda spec: envs/specs/{name}.yml"


@pytest.mark.parametrize("name", EXPECTED_ENVS)
def test_conda_spec_has_cu128_index(name):
    spec = SPECS_DIR / f"{name}.yml"
    if not spec.exists():
        pytest.skip("spec file missing")
    content = spec.read_text()
    # All envs must reference the cu128 wheel index
    assert "cu128" in content, (
        f"{name}.yml does not reference cu128 index — RTX 5090 requires cu128"
    )


@pytest.mark.parametrize("name", EXPECTED_ENVS)
def test_conda_spec_pins_torch_27(name):
    spec = SPECS_DIR / f"{name}.yml"
    if not spec.exists():
        pytest.skip("spec file missing")
    content = spec.read_text()
    # Accept torch==2.7.* or torch>=2.7
    has_pin = "torch==2.7" in content or "torch>=2.7" in content
    assert has_pin, (
        f"{name}.yml does not pin torch to >=2.7; RTX 5090 requires sm_120 wheels from cu128"
    )


@pytest.mark.parametrize("name", EXPECTED_ENVS)
def test_conda_spec_no_old_cuda_index(name):
    spec = SPECS_DIR / f"{name}.yml"
    if not spec.exists():
        pytest.skip("spec file missing")
    content = spec.read_text()
    for bad_index in ("cu121", "cu124", "cu126", "cu131"):
        assert bad_index not in content, (
            f"{name}.yml references obsolete CUDA index {bad_index}"
        )


@pytest.mark.parametrize("name", EXPECTED_ENVS)
def test_conda_spec_has_python311(name):
    spec = SPECS_DIR / f"{name}.yml"
    if not spec.exists():
        pytest.skip("spec file missing")
    content = spec.read_text()
    assert "python=3.11" in content or "python>=3.11" in content, (
        f"{name}.yml should specify Python 3.11"
    )


# ── Apptainer defs ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", EXPECTED_ENVS)
def test_apptainer_def_exists(name):
    deffile = IMAGES_DIR / f"{name}.def"
    assert deffile.exists(), f"Missing Apptainer def: deploy/images/{name}.def"


@pytest.mark.parametrize("name", EXPECTED_ENVS)
def test_apptainer_def_uses_cuda128_base(name):
    deffile = IMAGES_DIR / f"{name}.def"
    if not deffile.exists():
        pytest.skip("def file missing")
    content = deffile.read_text()
    assert "cuda:12.8" in content or "cu128" in content, (
        f"{name}.def does not use a CUDA 12.8 base or cu128 wheels"
    )


@pytest.mark.parametrize("name", EXPECTED_ENVS)
def test_apptainer_def_has_bootstrap_and_from(name):
    deffile = IMAGES_DIR / f"{name}.def"
    if not deffile.exists():
        pytest.skip("def file missing")
    content = deffile.read_text()
    assert "Bootstrap:" in content, f"{name}.def missing Bootstrap header"
    assert "From:" in content, f"{name}.def missing From header"


@pytest.mark.parametrize("name", EXPECTED_ENVS)
def test_apptainer_def_has_torch27(name):
    deffile = IMAGES_DIR / f"{name}.def"
    if not deffile.exists():
        pytest.skip("def file missing")
    content = deffile.read_text()
    assert "torch==2.7" in content or "torch>=2.7" in content, (
        f"{name}.def does not pin PyTorch >=2.7"
    )

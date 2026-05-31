# iitgpu/containers.py
"""Apptainer (formerly Singularity) container helpers."""
from __future__ import annotations

from pathlib import Path

from iitgpu.validate import in_jail, safe_listdir

_IMAGES_SUBDIR = "images"


def images_dir(nfs_root: str = "/shared") -> str:
    return str(Path(nfs_root) / _IMAGES_SUBDIR)


def list_images(nfs_root: str = "/shared") -> list[str]:
    """List .sif image paths in /shared/images/ (path-jailed)."""
    idir = images_dir(nfs_root)
    if not in_jail(idir):
        return []
    return sorted(
        str(Path(idir) / f)
        for f in safe_listdir(idir)
        if f.endswith(".sif")
    )


def render_apptainer_wrap(image: str, inner_cmd: str) -> str:
    """Return the apptainer exec wrapper for an sbatch run command."""
    # --nv: pass through NVIDIA GPUs
    # --bind /shared: expose shared storage inside the container
    return (
        f"apptainer exec --nv --bind /shared {image} "
        f"bash -lc {inner_cmd!r}"
    )


def validate_image(image_path: str) -> bool:
    """Return True if the path is inside the jail and ends with .sif."""
    return (
        in_jail(image_path)
        and image_path.endswith(".sif")
    )

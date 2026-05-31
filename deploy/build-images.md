# build-images.md — Building Apptainer Images for Prebuilt Environments

> **[GPU-HOST] and [LOGIN] steps** — run manually.
> Images are stored in `/shared/images/` and shared across all users.

---

## Prerequisites

### Install Apptainer on the GPU Host

```bash
# [GPU-HOST] as root
sudo apt-get install -y software-properties-common
sudo add-apt-repository -y ppa:apptainer/ppa
sudo apt-get update && sudo apt-get install -y apptainer

# Verify
apptainer --version
```

### Install Apptainer on the Login VM (for building only)

```bash
# [LOGIN] as slurmadmin with sudo
sudo apt-get install -y apptainer
```

### Create Images Directory

```bash
# [LOGIN or GPU-HOST]
sudo mkdir -p /shared/images
sudo chmod 0775 /shared/images
sudo chown root:gpuusers /shared/images   # or 0777 if gpuusers group not set up yet
```

---

## Building Images

All images are built from the `.def` files in `deploy/images/`.
Run on the **login node** (or GPU host), output directly to `/shared/images/`.

```bash
cd /home/slurmadmin/IIT-Secure-SLURM-Job-Gateway

# llm-finetune — transformers, peft, trl, bitsandbytes, accelerate
sudo apptainer build /shared/images/llm-finetune.sif deploy/images/llm-finetune.def

# llm-serve — vLLM + inference stack
sudo apptainer build /shared/images/llm-serve.sif deploy/images/llm-serve.def

# vision — PyTorch + torchvision + timm + ultralytics (YOLO)
sudo apptainer build /shared/images/vision.sif deploy/images/vision.def

# diffusion — diffusers + Stable Diffusion
sudo apptainer build /shared/images/diffusion.sif deploy/images/diffusion.def

# data-science — scikit-learn, xgboost, pandas, jupyterlab (+ optional RAPIDS)
sudo apptainer build /shared/images/data-science.sif deploy/images/data-science.def
```

> Build times: ~20–40 min per image on first build (PyTorch wheel download).
> Subsequent rebuilds with cache: ~5 min.

---

## Verifying Images

```bash
# Test that the GPU is visible inside a container
apptainer exec --nv /shared/images/llm-finetune.sif \
    python3 -c "import torch; print('GPU:', torch.cuda.get_device_name(0))"

# Verify sm_120 capability
apptainer exec --nv /shared/images/llm-finetune.sif \
    python3 -c "import torch; cap=torch.cuda.get_device_capability(); assert cap>=(12,0), cap; print('sm_120 OK')"
```

---

## Pulling Pre-Built Images (Alternative)

If a registry hosts pre-built images:

```bash
apptainer pull /shared/images/llm-finetune.sif docker://nvcr.io/nvidia/pytorch:25.01-py3
# Then test CUDA as above
```

---

## Using Images in Jobs

In the TUI wizard, choose **"Container image (.sif via Apptainer)"** in the
Environment step. The image must be in `/shared/images/` (path jail).

Manual sbatch example:

```bash
apptainer exec --nv --bind /shared /shared/images/llm-finetune.sif \
    bash -lc "python /shared/daham/train.py --epochs 10"
```

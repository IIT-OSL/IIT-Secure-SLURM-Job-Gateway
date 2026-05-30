#!/usr/bin/env bash
# [GPU-HOST] setup-compute-toolchain.sh
# Run manually on iit-MS-7E06 as root. Idempotent.
# Installs build-essential (gcc/g++) needed by torch.compile / Triton.
# Verifies nvcc and gcc resolve and prints their versions.
set -euo pipefail

ok()   { echo "  ✔  $*"; }
warn() { echo "  ⚠  $*"; }
fail() { echo "  ✘  $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

step "Checking privileges..."
if [ "$(id -u)" -ne 0 ]; then
    fail "Must run as root (sudo bash setup-compute-toolchain.sh)"
fi
ok "Running as root"

step "Installing build-essential (gcc, g++, make)..."
apt-get update -qq
apt-get install -y build-essential
ok "build-essential installed"

step "Verifying gcc..."
GCC_VER=$(gcc --version | head -1) || fail "gcc not found after install"
ok "gcc: $GCC_VER"

step "Verifying g++..."
GPP_VER=$(g++ --version | head -1) || fail "g++ not found after install"
ok "g++: $GPP_VER"

step "Checking nvcc (CUDA compiler)..."
if command -v nvcc &>/dev/null; then
    NVCC_VER=$(nvcc --version | grep 'release' | awk '{print $5}' | tr -d ',')
    ok "nvcc: CUDA $NVCC_VER"
else
    # nvcc may live under /usr/local/cuda/bin — check common paths
    for nvcc_path in /usr/local/cuda/bin/nvcc /usr/local/cuda-12.8/bin/nvcc /usr/local/cuda-12/bin/nvcc; do
        if [ -x "$nvcc_path" ]; then
            NVCC_VER=$($nvcc_path --version | grep 'release' | awk '{print $5}' | tr -d ',')
            ok "nvcc at $nvcc_path: CUDA $NVCC_VER"
            # Ensure it's on PATH for SLURM jobs
            CUDA_BIN=$(dirname "$nvcc_path")
            if ! grep -q "$CUDA_BIN" /etc/environment 2>/dev/null; then
                warn "nvcc not in PATH — add $CUDA_BIN to /etc/environment or /etc/profile.d/cuda.sh"
            fi
            break
        fi
    done
    # Not fatal — nvcc may not be installed separately from the driver
    command -v nvcc &>/dev/null || warn "nvcc not found — CUDA driver is present but CUDA toolkit may not be installed; torch.compile uses its own bundled compiler"
fi

step "Verifying torch.compile prerequisites..."
PYTHON=$(command -v python3 || echo '')
if [ -z "$PYTHON" ]; then
    warn "python3 not in PATH — cannot run torch import check here"
else
    $PYTHON -c "
import sys
print(f'  Python: {sys.version.split()[0]}')
try:
    import torch
    print(f'  torch: {torch.__version__}')
    print(f'  CUDA available: {torch.cuda.is_available()}')
    if torch.cuda.is_available():
        cap = torch.cuda.get_device_capability()
        print(f'  Device capability: sm_{cap[0]}{cap[1]}')
        if cap < (12, 0):
            print(f'  ERROR: sm_{cap[0]}{cap[1]} < sm_120 — torch wheel may be wrong CUDA build', file=sys.stderr)
            sys.exit(1)
        print('  RTX 5090 / sm_120 confirmed')
except ImportError:
    print('  torch not installed in system Python — check conda env')
" && ok "torch.compile prerequisites OK" || warn "torch check had warnings (see above)"
fi

echo
ok "Compute toolchain setup complete."
echo
echo "Next steps if torch.compile still fails:"
echo "  1. Activate the target conda env"
echo "  2. Run: python -c \"import torch; m=torch.nn.Linear(4,4).cuda(); torch.compile(m)(torch.randn(4,4).cuda())\""
echo "  3. Any 'no kernel image' error means the cu128 wheel is not installed — run envbuilder again."

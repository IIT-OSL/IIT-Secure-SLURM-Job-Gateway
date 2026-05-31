#!/usr/bin/env python3
"""
CIFAR-10 training for RTX 5090 — SmallResNet + BF16 + OneCycleLR + CutMix.

Saves:
  best_model.pt       — weights at highest validation accuracy (use for inference)
  last_checkpoint.pt  — weights at the final epoch (use to resume training)

Model choice:
  SmallResNet (default): ~4.8M params, ~1.5 min/50 epochs, ~93-95% val accuracy
  --model wideres:       ~36M params, ~14 min/50 epochs,  ~95-96% val accuracy
"""
import warnings
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)

import torch
import torch.nn as nn
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
import argparse
import os
import time
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument('--model',      type=str,   default='small',
                    choices=['small', 'wideres'],
                    help='small=fast (~1.5 min), wideres=accurate (~14 min)')
parser.add_argument('--epochs',     type=int,   default=50)
parser.add_argument('--lr',         type=float, default=0.0)
parser.add_argument('--batch_size', type=int,   default=0)
parser.add_argument('--data_dir',   type=str,   default='/shared/data/cifar10')
parser.add_argument('--save_dir',   type=str,   default='.',
                    help='Where to save best_model.pt and last_checkpoint.pt '
                         '(default: current dir = the SLURM job folder)')
parser.add_argument('--no_amp',     action='store_true')
parser.add_argument('--no_cutmix',  action='store_true')
args = parser.parse_args()

CLASSES = ['airplane','automobile','bird','cat','deer',
           'dog','frog','horse','ship','truck']

# ── GPU setup ─────────────────────────────────────────────────────────────────

def _cuda_works() -> bool:
    if not torch.cuda.is_available():
        return False
    try:
        x = torch.zeros(4, 4, device='cuda'); _ = x @ x; torch.cuda.synchronize()
        return True
    except RuntimeError as e:
        print(f"WARNING: {str(e).splitlines()[0]}. Using CPU."); return False

device  = torch.device('cuda' if _cuda_works() else 'cpu')
use_amp = device.type == 'cuda' and not args.no_amp

if device.type == 'cuda':
    os.environ.setdefault('PYTORCH_CUDA_ALLOC_CONF', 'expandable_segments:True')
    torch.set_float32_matmul_precision('high')
    props = torch.cuda.get_device_properties(0)
    print(f"GPU  : {props.name}  ({props.total_memory/1024**3:.0f} GB VRAM)")
    print(f"AMP  : {'BF16' if use_amp else 'off'}")

# ── Per-model defaults ─────────────────────────────────────────────────────────

if args.model == 'small':
    BATCH = args.batch_size if args.batch_size > 0 else 512
    LR    = args.lr         if args.lr > 0          else 0.3
else:
    BATCH = args.batch_size if args.batch_size > 0 else 1024
    LR    = args.lr         if args.lr > 0          else 0.4

# ── Dataset ───────────────────────────────────────────────────────────────────

os.makedirs(args.data_dir, exist_ok=True)
transform_train = transforms.Compose([
    transforms.RandomCrop(32, padding=4),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])
transform_test = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])
trainset = torchvision.datasets.CIFAR10(args.data_dir, train=True,  download=True, transform=transform_train)
testset  = torchvision.datasets.CIFAR10(args.data_dir, train=False, download=True, transform=transform_test)
trainloader = torch.utils.data.DataLoader(trainset, batch_size=BATCH,   shuffle=True,
    num_workers=4, pin_memory=True, persistent_workers=True)
testloader  = torch.utils.data.DataLoader(testset,  batch_size=BATCH*2, shuffle=False,
    num_workers=2, pin_memory=True, persistent_workers=True)

# ── CutMix ────────────────────────────────────────────────────────────────────

def cutmix_batch(x, y, alpha=1.0):
    lam  = np.random.beta(alpha, alpha)
    idx  = torch.randperm(x.size(0), device=x.device)
    W, H = x.size(3), x.size(2)
    cut_w, cut_h = int(W * np.sqrt(1-lam)), int(H * np.sqrt(1-lam))
    cx, cy = np.random.randint(W), np.random.randint(H)
    x1,x2 = max(0,cx-cut_w//2), min(W,cx+cut_w//2)
    y1,y2 = max(0,cy-cut_h//2), min(H,cy+cut_h//2)
    x = x.clone(); x[:,:,y1:y2,x1:x2] = x[idx,:,y1:y2,x1:x2]
    lam = 1 - (x2-x1)*(y2-y1)/(W*H)
    return x, y, y[idx], lam

# ── Models ────────────────────────────────────────────────────────────────────

class BasicBlock(nn.Module):
    def __init__(self, in_ch, out_ch, stride=1):
        super().__init__()
        self.conv1 = nn.Conv2d(in_ch, out_ch, 3, stride=stride, padding=1, bias=False)
        self.bn1   = nn.BatchNorm2d(out_ch)
        self.conv2 = nn.Conv2d(out_ch, out_ch, 3, padding=1, bias=False)
        self.bn2   = nn.BatchNorm2d(out_ch)
        self.skip  = nn.Sequential()
        if stride != 1 or in_ch != out_ch:
            self.skip = nn.Sequential(nn.Conv2d(in_ch, out_ch, 1, stride=stride, bias=False),
                                      nn.BatchNorm2d(out_ch))
    def forward(self, x):
        out = torch.relu(self.bn1(self.conv1(x)))
        return torch.relu(self.bn2(self.conv2(out)) + self.skip(x))

class SmallResNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.prep   = nn.Sequential(nn.Conv2d(3,64,3,padding=1,bias=False),
                                    nn.BatchNorm2d(64), nn.ReLU())
        self.layer1 = BasicBlock(64,  128, stride=2)
        self.layer2 = BasicBlock(128, 256, stride=2)
        self.layer3 = BasicBlock(256, 512, stride=2)
        self.pool   = nn.AdaptiveAvgPool2d(1)
        self.fc     = nn.Linear(512, 10)
    def forward(self, x):
        x = self.prep(x); x = self.layer1(x)
        x = self.layer2(x); x = self.layer3(x)
        return self.fc(self.pool(x).flatten(1))

class WideBasicBlock(nn.Module):
    def __init__(self, in_ch, out_ch, stride=1, dropout=0.3):
        super().__init__()
        self.bn1  = nn.BatchNorm2d(in_ch);  self.relu1 = nn.ReLU(inplace=True)
        self.conv1 = nn.Conv2d(in_ch, out_ch, 3, stride=stride, padding=1, bias=False)
        self.bn2   = nn.BatchNorm2d(out_ch); self.relu2 = nn.ReLU(inplace=True)
        self.drop  = nn.Dropout(p=dropout)
        self.conv2 = nn.Conv2d(out_ch, out_ch, 3, padding=1, bias=False)
        self.skip  = nn.Sequential()
        if stride != 1 or in_ch != out_ch:
            self.skip = nn.Conv2d(in_ch, out_ch, 1, stride=stride, bias=False)
    def forward(self, x):
        out = self.drop(self.conv1(self.relu1(self.bn1(x))))
        out = self.conv2(self.relu2(self.bn2(out)))
        return out + self.skip(x)

class WideResNet(nn.Module):
    def __init__(self, depth=28, widen=10, num_classes=10, dropout=0.3):
        super().__init__()
        assert (depth - 4) % 6 == 0
        n = (depth - 4) // 6
        k = widen
        nStages = [16, 16*k, 32*k, 64*k]
        self.conv1  = nn.Conv2d(3, nStages[0], 3, padding=1, bias=False)
        self.layer1 = self._make(WideBasicBlock, n, nStages[0], nStages[1], 1, dropout)
        self.layer2 = self._make(WideBasicBlock, n, nStages[1], nStages[2], 2, dropout)
        self.layer3 = self._make(WideBasicBlock, n, nStages[2], nStages[3], 2, dropout)
        self.bn     = nn.BatchNorm2d(nStages[3]); self.relu = nn.ReLU(inplace=True)
        self.pool   = nn.AdaptiveAvgPool2d(1)
        self.fc     = nn.Linear(nStages[3], num_classes)
    def _make(self, block, n, in_ch, out_ch, stride, dropout):
        layers = [block(in_ch, out_ch, stride, dropout)]
        for _ in range(n-1): layers.append(block(out_ch, out_ch, 1, dropout))
        return nn.Sequential(*layers)
    def forward(self, x):
        x = self.conv1(x); x = self.layer1(x); x = self.layer2(x); x = self.layer3(x)
        return self.fc(self.pool(self.relu(self.bn(x))).flatten(1))

model     = (SmallResNet() if args.model == 'small' else WideResNet()).to(device)
n_params  = sum(p.numel() for p in model.parameters())
criterion = nn.CrossEntropyLoss(label_smoothing=0.1)
optimizer = optim.SGD(model.parameters(), lr=LR/25, momentum=0.9, weight_decay=5e-4, nesterov=True)
n_batches = len(trainloader)
scheduler = optim.lr_scheduler.OneCycleLR(optimizer, max_lr=LR, epochs=args.epochs,
                                           steps_per_epoch=n_batches, pct_start=0.1)
scaler = torch.amp.GradScaler('cuda', enabled=use_amp)

label_name = "SmallResNet (fast)" if args.model == 'small' else "WideResNet-28-10 (accurate)"
print(f"Model: {label_name}  ({n_params/1e6:.1f}M params)")
print(f"CutMix: {'off' if args.no_cutmix else 'on'}")
print(f"Batch: {BATCH}   Epochs: {args.epochs}   max_LR: {LR}")
if device.type == 'cuda':
    print(f"VRAM after model load: {torch.cuda.memory_allocated(0)/1024**3:.2f} GB")

os.makedirs(args.save_dir, exist_ok=True)
best_path  = os.path.join(args.save_dir, 'best_model.pt')
last_path  = os.path.join(args.save_dir, 'last_checkpoint.pt')
print(f"Saving to: {args.save_dir}/")
print()

# ── Progress helper ────────────────────────────────────────────────────────────

_progress_every = max(1, n_batches // 4)

# ── Training loop ──────────────────────────────────────────────────────────────

best_val_acc = 0.0

for epoch in range(args.epochs):
    model.train()
    t0 = time.time(); running_loss = correct = total = 0

    for step, (inputs, labels) in enumerate(trainloader):
        inputs = inputs.to(device, non_blocking=True)
        labels = labels.to(device, non_blocking=True)

        use_cm = (not args.no_cutmix) and (np.random.random() > 0.5)
        if use_cm:
            inputs, labels_a, labels_b, lam = cutmix_batch(inputs, labels)

        optimizer.zero_grad(set_to_none=True)
        with torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=use_amp):
            outputs = model(inputs)
            loss = (lam * criterion(outputs, labels_a) + (1-lam) * criterion(outputs, labels_b)
                    if use_cm else criterion(outputs, labels))

        scaler.scale(loss).backward()
        scaler.unscale_(optimizer)
        nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        scaler.step(optimizer); scaler.update(); scheduler.step()

        with torch.no_grad():
            _, pred = outputs.max(1)
            total  += labels.size(0); correct += pred.eq(labels).sum().item()
        running_loss += loss.item() * inputs.size(0)

        if (step + 1) % _progress_every == 0:
            pct = (step+1)/n_batches*100
            print(f"  epoch {epoch+1}/{args.epochs}  step {step+1}/{n_batches} ({pct:.0f}%)"
                  f"  loss {running_loss/total:.4f}  acc {100*correct/total:.1f}%"
                  f"  lr {scheduler.get_last_lr()[0]:.4f}  {time.time()-t0:.0f}s", flush=True)

    # ── Validation ─────────────────────────────────────────────────────────────
    model.eval(); val_correct = val_total = 0
    with torch.no_grad():
        for inputs, labels in testloader:
            inputs = inputs.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=use_amp):
                outputs = model(inputs)
            _, pred = outputs.max(1)
            val_total += labels.size(0); val_correct += pred.eq(labels).sum().item()

    val_acc  = 100 * val_correct / val_total
    vram_str = ""
    if device.type == 'cuda':
        used = torch.cuda.memory_allocated(0)/1024**3
        peak = torch.cuda.max_memory_allocated(0)/1024**3
        vram_str = f"  VRAM {used:.1f}/{peak:.1f}GB"; torch.cuda.reset_peak_memory_stats()

    improved = val_acc > best_val_acc
    tag      = "  ← best" if improved else ""
    print(f"Epoch {epoch+1:>3}/{args.epochs} | loss: {running_loss/total:.4f} | "
          f"train: {100*correct/total:.1f}% | val: {val_acc:.1f}% | "
          f"lr: {scheduler.get_last_lr()[0]:.5f} | time: {time.time()-t0:.1f}s{vram_str}{tag}",
          flush=True)

    # ── Save best model ─────────────────────────────────────────────────────────
    if improved:
        best_val_acc = val_acc
        torch.save({
            'epoch':        epoch + 1,
            'model_name':   args.model,
            'val_acc':      val_acc,
            'classes':      CLASSES,
            'model_state':  model.state_dict(),
        }, best_path)
        print(f"  → Saved best_model.pt  (val {val_acc:.2f}%)", flush=True)

# ── Save final checkpoint ───────────────────────────────────────────────────────
torch.save({
    'epoch':        args.epochs,
    'model_name':   args.model,
    'val_acc':      best_val_acc,
    'classes':      CLASSES,
    'model_state':  model.state_dict(),
}, last_path)

# ── Final summary ───────────────────────────────────────────────────────────────
print()
print("=" * 60)
print(f"Training complete — {args.epochs} epochs")
print(f"Best validation accuracy : {best_val_acc:.2f}%")
print(f"Model saved to           : {best_path}")
print(f"Last checkpoint saved to : {last_path}")
print()
print("To use the model for inference:")
print(f"  checkpoint = torch.load('{best_path}', weights_only=True)")
print(f"  model = SmallResNet().to(device)  # or WideResNet() if --model wideres")
print(f"  model.load_state_dict(checkpoint['model_state'])")
print(f"  model.eval()")
print(f"  # Classes: {CLASSES}")
print("=" * 60)

"""
model.py — WinProbNet v1 (feedforward) + training and inference utilities.

One model is trained per role and saved to models/:
    models/wp_{role}.pt       — PyTorch checkpoint (state_dict + input_dim)
    models/scaler_{role}.pkl  — StandardScaler fitted on training frames

Usage:
    python main.py train --role MID
    python main.py train --role JUNGLE --patch-window 3
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import torch
import torch.nn as nn
from sklearn.preprocessing import StandardScaler

import features as feat

MODELS_DIR   = Path(__file__).parent / "models"
VALID_ROLES  = {"MID", "JUNGLE", "BOTTOM", "SUPPORT", "TOP"}
MIN_GAMES    = 100   # warn below this threshold


# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------

class WinProbNet(nn.Module):
    """
    Frame-level feedforward win probability estimator.

    Input : (batch, input_dim) — normalized feature vector
    Output: (batch,)           — probability in [0, 1]
    """

    def __init__(self, input_dim: int = feat.FEATURE_DIM):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, 256), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(256, 128),       nn.ReLU(), nn.Dropout(0.2),
            nn.Linear(128, 64),        nn.ReLU(),
            nn.Linear(64, 1),          nn.Sigmoid(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def train_model(
    role: str,
    patches: Optional[set[str]] = None,
    epochs: int = 20,
    batch_size: int = 1024,
    lr: float = 1e-3,
    val_frac: float = 0.2,
) -> None:
    """
    Train and save a WinProbNet for the given role.

    patches: restrict training data to these patch strings (e.g. {"16.10", "16.9"}).
             None = use all stored patches.
    """
    from rich.console import Console
    from rich.table import Table, box as rbox

    console = Console()
    role = role.upper()

    if role not in VALID_ROLES:
        console.print(f"[red]Unknown role '{role}'. Valid: {', '.join(sorted(VALID_ROLES))}[/red]")
        return

    console.print(f"[cyan]Loading training data for {role}…[/cyan]")
    X, y, match_ids = feat.load_training_data(role, patches)

    n_games  = len(set(match_ids))
    n_frames = len(X)

    console.print(
        f"  {n_games} games · {n_frames} frames · "
        f"{int(y.sum())} wins · {int((1 - y).sum())} losses"
    )

    if n_games == 0:
        console.print(f"[red]No data for {role}. Run: python main.py collect[/red]")
        return
    if n_games < MIN_GAMES:
        console.print(
            f"[yellow]Only {n_games} games — model quality will be limited "
            f"(recommend ≥ {MIN_GAMES}).[/yellow]"
        )

    # Game-level 80/20 split — prevents per-frame data leakage
    unique_mids = list(set(match_ids))
    rng = np.random.default_rng(42)
    rng.shuffle(unique_mids)
    split_idx  = int(len(unique_mids) * (1 - val_frac))
    train_set  = set(unique_mids[:split_idx])
    val_set    = set(unique_mids[split_idx:])

    mid_arr   = np.array(match_ids)
    train_idx = np.isin(mid_arr, list(train_set))
    val_idx   = np.isin(mid_arr, list(val_set))

    X_train, y_train = X[train_idx], y[train_idx]
    X_val,   y_val   = X[val_idx],   y[val_idx]

    # Fit scaler on train frames only
    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train).astype(np.float32)
    X_val   = scaler.transform(X_val).astype(np.float32)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    Xt = torch.from_numpy(X_train).to(device)
    yt = torch.from_numpy(y_train).to(device)
    Xv = torch.from_numpy(X_val).to(device)
    yv = torch.from_numpy(y_val).to(device)

    model     = WinProbNet(input_dim=X.shape[1]).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    criterion = nn.BCELoss()

    console.print(
        f"\n[cyan]Training on {len(X_train)} frames, validating on {len(X_val)} frames"
        f"  ·  device: {device}[/cyan]\n"
    )

    table = Table(box=rbox.SIMPLE_HEAVY)
    table.add_column("Epoch",     justify="right", style="dim")
    table.add_column("Train Loss", justify="right")
    table.add_column("Val Loss",   justify="right")
    table.add_column("Val Acc",    justify="right")

    for epoch in range(1, epochs + 1):
        model.train()
        perm       = torch.randperm(len(Xt))
        total_loss = 0.0
        for i in range(0, len(Xt), batch_size):
            idx = perm[i : i + batch_size]
            xb, yb = Xt[idx], yt[idx]
            optimizer.zero_grad()
            loss = criterion(model(xb), yb)
            loss.backward()
            optimizer.step()
            total_loss += loss.item() * len(xb)
        train_loss = total_loss / len(Xt)

        model.eval()
        with torch.no_grad():
            val_pred = model(Xv)
            val_loss = criterion(val_pred, yv).item()
            val_acc  = ((val_pred >= 0.5).float() == yv).float().mean().item()

        table.add_row(
            str(epoch),
            f"{train_loss:.4f}",
            f"{val_loss:.4f}",
            f"{val_acc * 100:.1f}%",
        )

    console.print(table)

    MODELS_DIR.mkdir(exist_ok=True)
    rl = role.lower()
    model_path  = MODELS_DIR / f"wp_{rl}.pt"
    scaler_path = MODELS_DIR / f"scaler_{rl}.pkl"

    torch.save({"state_dict": model.cpu().state_dict(), "input_dim": X.shape[1]}, model_path)
    joblib.dump(scaler, scaler_path)

    console.print(f"[green]Saved model  →[/green] {model_path}")
    console.print(f"[green]Saved scaler →[/green] {scaler_path}")


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def load_model(role: str) -> tuple[WinProbNet, StandardScaler]:
    """
    Load a trained WinProbNet and StandardScaler for the given role.

    Raises FileNotFoundError (with instructions) if not yet trained.
    """
    rl          = role.lower()
    model_path  = MODELS_DIR / f"wp_{rl}.pt"
    scaler_path = MODELS_DIR / f"scaler_{rl}.pkl"

    if not model_path.exists() or not scaler_path.exists():
        raise FileNotFoundError(
            f"No trained model for {role.upper()}. "
            f"Run: python main.py train --role {role.upper()}"
        )

    ckpt  = torch.load(model_path, map_location="cpu", weights_only=False)
    net   = WinProbNet(input_dim=ckpt["input_dim"])
    net.load_state_dict(ckpt["state_dict"])
    net.eval()

    scaler: StandardScaler = joblib.load(scaler_path)
    return net, scaler


def predict_proba(
    model: WinProbNet,
    scaler: StandardScaler,
    X: np.ndarray,
) -> np.ndarray:
    """
    Batch win probability prediction.

    X       : (N, input_dim) float32
    Returns : (N,) float32 probabilities in [0, 1]
    """
    X_scaled = scaler.transform(X).astype(np.float32)
    with torch.no_grad():
        out = model(torch.from_numpy(X_scaled)).numpy()
    return out

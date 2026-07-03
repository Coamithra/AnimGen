"""Cost-confirm gate (hard-won rule 8: ask before EVERY generation launch).

No job leaves the queue until the user explicitly confirms this dialog, which shows
the model, resolved params, and estimated spend. build_summary() is split out so the
summary can be unit-tested without showing a modal. Default button is Cancel.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout,
)

import library

# An "item" is a dict: {name, model_display, backend, est_cost (float|None), params}.


def _fmt_cost(c: Optional[float]) -> str:
    if c is None:
        return "?"
    if c == 0:
        return "free"
    if c < 0.01:                     # sub-cent: don't collapse to "$0.00" in the gate
        return "<$0.01" if c < 0.001 else f"${c:.3f}"
    return f"${c:.2f}"


def build_summary(items: list[dict]) -> tuple[str, float, bool]:
    """Return (body_text, total_known_cost, has_spend_or_unknown)."""
    total = 0.0
    unknown = False
    lines = []
    for it in items:
        c = it.get("est_cost")
        if c is None:
            unknown = True
        else:
            total += c
        params = it.get("params") or {}
        psum = ", ".join(
            f"{k}={library.seed_label(params[k]) if k == 'seed' else params[k]}"
            for k in ("duration", "resolution", "seed", "aspect_ratio")
            if k in params
        )
        lines.append(
            f"• {it.get('name','(unnamed)')}  [{it.get('model_display','?')}]  "
            f"{_fmt_cost(c)}" + (f"\n    {psum}" if psum else "")
        )
    has_spend = total > 0 or unknown
    header = (f"{len(items)} generation(s).  Estimated total: {_fmt_cost(total)}"
              + (" + unknown" if unknown else ""))
    return header + "\n\n" + "\n".join(lines), total, has_spend


def _spend_tier(total: float, has_spend: bool) -> str:
    """Classify a batch's spend for the gate: 'spend' (known cost), 'unknown' (may
    spend, no estimate), or 'free' (local, no spend). The header warning and the
    launch button label both derive from this single source so they can't contradict.
    """
    if total > 0:
        return "spend"
    if has_spend:
        return "unknown"
    return "free"


def launch_button_label(total: float, has_spend: bool) -> str:
    """Accept-button label for confirm_launch, keyed off the shared _spend_tier.

    An all-unknown-cost batch has has_spend=True but total==0 (build_summary tallies
    None costs separately), so feeding total into _fmt_cost would render
    "Launch (spend ~free)" — contradicting the "MAY spend money" header. Split out so
    the label is testable without exec().
    """
    tier = _spend_tier(total, has_spend)
    if tier == "spend":
        return f"Launch (spend ~{_fmt_cost(total)})"
    if tier == "unknown":
        return "Launch (cost unknown)"
    return "Launch (free)"


def total_price_text(costs: list[Optional[float]]) -> str:
    """Shots-view label: full-set generation cost summed over per-shot estimates.

    None costs (model declares no rate / unknown model) are tallied separately as
    '(+N unknown)' rather than silently dropped; local $0 shots contribute nothing.
    """
    total = sum(c for c in costs if c is not None)   # known costs; local $0 adds nothing
    unknown = sum(1 for c in costs if c is None)
    # Sub-cent totals get _fmt_cost's precision (L13); an all-$0/empty set keeps "$0.00".
    dollars = _fmt_cost(total) if 0 < total < 0.01 else f"${total:.2f}"
    text = f"Full set: {dollars}"
    if unknown:
        text += f"  (+{unknown} unknown)"
    return text


def confirm_launch(parent, items: list[dict]) -> bool:
    body, total, has_spend = build_summary(items)
    dlg = QDialog(parent)
    dlg.setWindowTitle("Confirm generation")
    lay = QVBoxLayout(dlg)

    tier = _spend_tier(total, has_spend)
    if tier == "spend":
        warn = "This will spend real money on Replicate."
    elif tier == "unknown":
        warn = "Cost unknown - this MAY spend money."
    else:
        warn = "Local render - no spend."
    head = QLabel(warn)
    head.setStyleSheet("font-weight:600;")
    lay.addWidget(head)

    box = QPlainTextEdit(body)
    box.setReadOnly(True)
    box.setMinimumSize(480, 220)
    lay.addWidget(box)

    bb = QDialogButtonBox()
    cancel = bb.addButton(QDialogButtonBox.StandardButton.Cancel)
    launch_label = launch_button_label(total, has_spend)
    bb.addButton(launch_label, QDialogButtonBox.ButtonRole.AcceptRole)
    cancel.setDefault(True)       # safety: Enter cancels, not launches
    cancel.setAutoDefault(True)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)

    return dlg.exec() == QDialog.DialogCode.Accepted

"""Decisive repro: drive the REAL QueueView under a genuine app.exec() event loop (timer-
driven), flipping take statuses so rows + cell widgets churn the way an overnight batch does.
Uses Qt's natural DeferredDelete handling (not a manual drain), and censuses every parent each
round. If any parent's child count climbs without bound -> that's the paintSiblingsRecursive
source. Mutates only detached deep-copies, so the real Biker takes.json is never written.
"""
import os, sys, copy
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("ANIMGEN_REMOTE", None)
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from store.project import Project
from store.models import STATUS_PENDING, STATUS_GENERATING, STATUS_DONE
from ui.main_window import MainWindow

app = QApplication([])
project = Project.load(str(Path("data/Biker.animproj").resolve()))

# Detached copies the queue will see; flipping their .status never touches disk.
my_takes = [copy.deepcopy(t) for t in project.list_takes()]
_orig_list = project.list_takes
def _patched(shot_id=None, *, include_deleted=False, starred_only=False):
    if shot_id is None and not starred_only and not include_deleted:
        return my_takes
    return _orig_list(shot_id, include_deleted=include_deleted, starred_only=starred_only)
project.list_takes = _patched

win = MainWindow(project)
win.show()
queue = win.queue_tab
table = queue.table

ROUNDS = 400
CYCLE = (STATUS_GENERATING, STATUS_PENDING, STATUS_PENDING, STATUS_DONE)
state = {"r": 0, "max_vp": 0, "max_total": 0}


def viewport_children():
    return len(table.viewport().children())


def top_parent():
    pc = Counter()
    po = {}
    for w in app.allWidgets():
        p = w.parentWidget()
        if p is not None:
            pc[id(p)] += 1; po[id(p)] = p
    pid, cnt = pc.most_common(1)[0]
    p = po[pid]
    return cnt, type(p).__name__, p.objectName()


def tick():
    r = state["r"]
    # churn statuses so the active set (and thus cell widgets) changes every round
    for i, t in enumerate(my_takes):
        t.status = CYCLE[(i + r) % len(CYCLE)]
    queue.refresh()
    vp = viewport_children()
    total = len(app.allWidgets())
    state["max_vp"] = max(state["max_vp"], vp)
    state["max_total"] = max(state["max_total"], total)
    if r % 50 == 0 or r == ROUNDS - 1:
        cnt, cls, obj = top_parent()
        print(f"round {r:4d}  queue_viewport_children={vp:5d}  total_widgets={total:6d}  "
              f"top_parent={cnt}({cls} '{obj}')")
    state["r"] += 1
    if state["r"] >= ROUNDS:
        print(f"\nPEAK queue_viewport_children={state['max_vp']}  PEAK total_widgets={state['max_total']}")
        app.quit()


timer = QTimer()
timer.setInterval(0)
timer.timeout.connect(tick)
timer.start()
app.exec()

"""Build the REAL MainWindow on Biker (offscreen) and census the widget tree: which parent
holds the most child widgets, and does churning take-status changes grow any parent without
bound? That names the paintSiblingsRecursive sibling source. No generation, no spend.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("ANIMGEN_REMOTE", None)
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication
import paths
from store.project import Project
from ui.main_window import MainWindow

app = QApplication([])
project = Project.load(str(Path("data/Biker.animproj").resolve()))
win = MainWindow(project)
win.show()
app.processEvents()
app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


def drain():
    app.processEvents()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


def census(tag):
    widgets = app.allWidgets()
    parent_count = Counter()
    parent_obj = {}
    for w in widgets:
        p = w.parentWidget()
        if p is not None:
            parent_count[id(p)] += 1
            parent_obj[id(p)] = p
    print(f"\n--- census [{tag}]  total_widgets={len(widgets)} ---")
    for pid, cnt in parent_count.most_common(6):
        p = parent_obj[pid]
        kids = Counter(type(c).__name__ for c in p.children() if c.isWidgetType())
        dom = ", ".join(f"{k}x{v}" for k, v in kids.most_common(2))
        print(f"   {cnt:6d}  parent={type(p).__name__} obj='{p.objectName()}'  [{dom}]")


census("baseline")

# churn: drive real per-take status-change handler many times (cards + open shot tab refresh)
ids = [t.id for t in project.list_takes()][:60]
print(f"\nchurning _on_status_changed over {len(ids)} takes ...")
for r in range(30):
    for tid in ids:
        try:
            win._on_status_changed(tid, "generating")
        except Exception as e:  # noqa: BLE001
            if r == 0 and tid == ids[0]:
                print("  (status handler raised:", e, ")")
    drain()
    if r in (4, 14, 29):
        census(f"after churn round {r+1}")

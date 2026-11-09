# -*- coding: utf-8 -*-
"""GUI smoke test for OkibaBoard (run under Xvfb on Linux).

    xvfb-run -a python3 tools/gui_smoke.py

Launches the REAL app (OkibaBoard.run_gui) with OKIBA_BASE pointed
at a temp dir, opens a board with a few files, and drives the GUI
with synthetic events / button invokes:

  a) toolbar buttons (大きさ / テーマ / ＋メモ / ← もどる) respond
     and their state changes actually apply;
  b) a simulated card drag (press / motion / release) moves the
     card and the new cell is what gets saved;
  c) REGRESSION test for the v5 failure the user reported (top
     toolbar stopped responding): drag a card, release OUTSIDE the
     card, then use the toolbar again - it must still respond, the
     drag state must be cleared and no widget grab may remain
     (v6 uses no grab-set anywhere);
  d) the app closes cleanly without exceptions.

Dialogs (メモ input) are stubbed. Exits 0 on success, 1 on failure.
Not wired into the Windows CI; the headless --self-test there is
unchanged.
"""

import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="okiba-gui-smoke-")
BASE = os.path.join(TMP, "OkibaBoards")
BOARD = os.path.join(BASE, "テスト")
os.makedirs(BOARD)
for name in ("a.txt", "b.txt", "c.txt"):
    with open(os.path.join(BOARD, name), "w", encoding="utf-8") as f:
        f.write(name)
os.environ["OKIBA_BASE"] = BASE

import tkinter as tk
from tkinter import messagebox, simpledialog

import OkibaBoard

RESULTS = []


def check(label, cond, extra=""):
    RESULTS.append((label, bool(cond), extra))
    print("%s %s %s" % ("PASS" if cond else "FAIL", label, extra))


# ---- stub dialogs -------------------------------------------------
simpledialog.askstring = lambda *a, **k: "スモークメモ"
messagebox.showerror = lambda *a, **k: None
messagebox.showwarning = lambda *a, **k: None
messagebox.askyesno = lambda *a, **k: True


def find_button(app, text):
    for btn in app._buttons:
        try:
            if btn.cget("text") == text:
                return btn
        except Exception:
            pass
    return None


def card_tag(app, name):
    for tag, info in app.items.items():
        if info.get("name") == name:
            return tag
    return None


def run_steps(root):
    app = root._okiba_app
    try:
        root.update()
        check("starts on home", app.view == "home")

        # ---- open the prepared board ----
        app.open_board("テスト")
        root.update()
        check("board opens", app.view == "board"
              and app.board_folder == BOARD)
        check("cards rendered", len(app.items) >= 3,
              "items=%d" % len(app.items))

        # ---- (a) toolbar: size ----
        before_pct = app.board_percent
        app.size_button.invoke()
        root.update()
        check("大きさ button applies", app.board_percent != before_pct,
              "%s -> %s" % (before_pct, app.board_percent))
        check("大きさ label updated",
              str(app.board_percent) in app.size_button.cget("text"))

        # ---- (a) toolbar: theme ----
        before_theme = app.theme_name
        app.board_theme_btn.invoke()
        root.update()
        check("テーマ button applies", app.theme_name != before_theme,
              "%s -> %s" % (before_theme, app.theme_name))
        check("テーマ label synced",
              app.theme_name in app.home_theme_btn.cget("text"))

        # ---- (a) toolbar: +メモ (dialog stubbed) ----
        before_notes = len(app.notes)
        find_button(app, "＋メモ").invoke()
        root.update()
        check("＋メモ adds a note", len(app.notes) == before_notes + 1)

        # ---- (b) simulated drag moves a card ----
        tag = card_tag(app, "a.txt")
        info = app.items[tag]
        cell_before = tuple(app.positions["a.txt"])
        cx, cy = info["x"] + 20, info["y"] + 20
        m = app.metrics
        tx, ty = cx + 2 * m["cell_w"], cy + m["cell_h"]
        app.canvas.event_generate("<Motion>", x=cx, y=cy, warp=True)
        root.update()
        app.canvas.event_generate("<ButtonPress-1>", x=cx, y=cy)
        root.update()
        check("drag starts", app.drag.get("tag") == tag)
        app.canvas.event_generate("<B1-Motion>", x=tx, y=ty)
        root.update()
        app.canvas.event_generate("<ButtonRelease-1>", x=tx, y=ty)
        root.update()
        cell_after = tuple(app.positions["a.txt"])
        check("drag ends clean", app.drag.get("tag") is None)
        check("no grab remains after drag",
              root.grab_current() is None)
        check("card moved to a new cell", cell_after != cell_before,
              "%s -> %s" % (cell_before, cell_after))
        saved = OkibaBoard.load_board(BOARD)
        check("new cell is what got saved",
              tuple(saved["positions"]["a.txt"]) == cell_after)

        # ---- (c) REGRESSION: release outside the card, then the
        #      toolbar must STILL respond (the v5 user report) ----
        tag2 = card_tag(app, "b.txt")
        info2 = app.items[tag2]
        cx2, cy2 = info2["x"] + 20, info2["y"] + 20
        app.canvas.event_generate("<Motion>", x=cx2, y=cy2, warp=True)
        root.update()
        app.canvas.event_generate("<ButtonPress-1>", x=cx2, y=cy2)
        root.update()
        check("second drag starts", app.drag.get("tag") == tag2)
        # drag far away and release where no card is (near the
        # canvas corner, outside the dragged card)
        app.canvas.event_generate("<B1-Motion>", x=600, y=450)
        root.update()
        app.canvas.event_generate("<ButtonRelease-1>", x=600, y=450)
        root.update()
        check("outside release ends drag",
              app.drag.get("tag") is None)
        check("no grab remains after outside release",
              root.grab_current() is None)
        pct_before = app.board_percent
        app.size_button.invoke()
        root.update()
        check("toolbar STILL responds after outside release",
              app.board_percent != pct_before)
        theme_before = app.theme_name
        app.board_theme_btn.invoke()
        root.update()
        check("theme STILL responds after outside release",
              app.theme_name != theme_before)

        # ---- (c2) stale-drag recovery: a drag whose release was
        #      missed is force-finished by the next press anywhere
        tag3 = card_tag(app, "c.txt")
        info3 = app.items[tag3]
        cx3, cy3 = info3["x"] + 20, info3["y"] + 20
        app.canvas.event_generate("<Motion>", x=cx3, y=cy3, warp=True)
        root.update()
        app.canvas.event_generate("<ButtonPress-1>", x=cx3, y=cy3)
        root.update()
        check("third drag starts", app.drag.get("tag") == tag3)
        app._drag_started_at -= 1.0  # make it an old, stale drag
        app.canvas.event_generate("<ButtonPress-1>", x=900, y=620)
        root.update()
        check("stale drag force-finished by next press",
              app.drag.get("tag") is None)
        check("no grab remains after recovery",
              root.grab_current() is None)
        app.canvas.event_generate("<ButtonRelease-1>", x=900, y=620)
        root.update()

        # ---- (a) toolbar: back to home ----
        find_button(app, "← もどる").invoke()
        root.update()
        check("← もどる returns home", app.view == "home")

        # ---- (d) clean close ----
        app.on_close()
        check("closed cleanly", True)
    except Exception:
        traceback.print_exc()
        RESULTS.append(("exception during steps", False, ""))
        try:
            root.destroy()
        except Exception:
            pass


def main():
    orig_mainloop = tk.Tk.mainloop

    def patched_mainloop(self):
        self.after(400, lambda: run_steps(self))
        self.after(60000, self.destroy)  # failsafe, never hang CI
        orig_mainloop(self)

    tk.Tk.mainloop = patched_mainloop
    try:
        rc = OkibaBoard.run_gui()
    finally:
        tk.Tk.mainloop = orig_mainloop
    failed = [r for r in RESULTS if not r[1]]
    print("GUI_SMOKE_%s (%d/%d passed)" % (
        "OK" if not failed and rc == 0 else "FAILED",
        len(RESULTS) - len(failed), len(RESULTS)))
    return 0 if not failed and rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

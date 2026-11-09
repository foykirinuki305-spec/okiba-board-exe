# -*- coding: utf-8 -*-
"""
OkibaBoard v2 - Deskboard-style SUPER-SIMPLE board (Python + Tkinter)

Core only, minimal UI (two buttons):
- Open a real folder -> its actual files/folders appear as cards on a
  plain dark board.
- Drag cards to arrange them. Positions auto-save to ``.okiba-board.json``
  inside the opened folder (that file itself is excluded from cards).
- Drag & drop files/folders from Windows Explorer onto the board to add
  them: the Explorer rule is followed (same drive = move, different
  drive = copy; a same-name item is never overwritten - a " (2)" style
  suffix is added instead).
- Live sync (simple): the folder is polled every ~2 seconds; items
  added/removed outside the app appear/disappear automatically.
- Notes: the single "＋メモ" button adds note cards (double-click to
  edit), saved in the same json.
- Double-click a file/folder card to open the real item
  (``os.startfile`` on Windows).

Headless self-test (used by CI):
    OkibaBoard.exe --self-test
    -> exercises scanning + save/load + the drop-intake helper without
       a GUI (and without needing tkinterdnd2), writes
       ``self-test-ok.txt`` in the current directory, exits 0.
"""

import json
import os
import shutil
import sys
import tempfile
import uuid

BOARD_FILENAME = ".okiba-board.json"
SELF_TEST_OK = "self-test-ok.txt"
POLL_MS = 2000


# ---------------------------------------------------------------- core logic
def scan_folder(folder):
    """Return a sorted list of dicts for entries in *folder*.

    The board data file itself is excluded.
    """
    entries = []
    for name in sorted(os.listdir(folder), key=lambda s: s.lower()):
        if name == BOARD_FILENAME:
            continue
        full = os.path.join(folder, name)
        entries.append(
            {
                "name": name,
                "is_dir": os.path.isdir(full),
            }
        )
    return entries


def board_path(folder):
    return os.path.join(folder, BOARD_FILENAME)


def save_board(folder, positions, notes):
    """Save card positions (dict name-> {x, y}) and notes (list) to *folder*."""
    data = {"version": 2, "positions": positions, "notes": notes}
    with open(board_path(folder), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def load_board(folder):
    """Load saved positions/notes from *folder*; empty defaults if none."""
    path = board_path(folder)
    if not os.path.exists(path):
        return {"version": 2, "positions": {}, "notes": []}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("positions", {})
    data.setdefault("notes", [])
    return data


def _drive_of(path):
    """Drive letter (lowercase) of *path* on Windows; '' elsewhere."""
    return os.path.splitdrive(os.path.abspath(path))[0].lower()


def unique_destination(target_folder, name):
    """Return a non-colliding destination path for *name* in *target_folder*.

    Never overwrites: appends " (2)", " (3)", ... before the extension
    (files) or at the end (folders).
    """
    dest = os.path.join(target_folder, name)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(name)
    n = 2
    while True:
        candidate = os.path.join(target_folder, "%s (%d)%s" % (stem, n, ext))
        if not os.path.exists(candidate):
            return candidate
        n += 1


def place_dropped_items(sources, target_folder):
    """Move/copy *sources* into *target_folder*; return placed paths.

    Explorer rule: same drive -> move, different drive -> copy.
    Same-name items are never overwritten (see unique_destination).
    Items already inside the target folder, missing items, and the
    board data file are skipped.
    """
    placed = []
    target_abs = os.path.abspath(target_folder)
    for src in sources:
        if not src or not os.path.exists(src):
            continue
        src_abs = os.path.abspath(src)
        if os.path.dirname(src_abs) == target_abs:
            continue  # already in this folder
        name = os.path.basename(src_abs)
        if name == BOARD_FILENAME:
            continue
        dest = unique_destination(target_folder, name)
        if _drive_of(src_abs) == _drive_of(target_folder):
            shutil.move(src_abs, dest)
        elif os.path.isdir(src_abs):
            shutil.copytree(src_abs, dest)
        else:
            shutil.copy2(src_abs, dest)
        placed.append(dest)
    return placed


def open_item(path):
    """Open a real file/folder with the OS default handler."""
    if os.name == "nt":  # Windows
        os.startfile(path)  # noqa: S606 - intentional, user's own file
    elif sys.platform == "darwin":
        import subprocess

        subprocess.Popen(["open", path])
    else:
        import subprocess

        subprocess.Popen(["xdg-open", path])


# ------------------------------------------------------------------ self-test
def run_self_test():
    """Exercise scanning + save/load + drop intake, without a GUI."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # sample files / folder
            with open(os.path.join(tmp, "alpha.txt"), "w", encoding="utf-8") as f:
                f.write("hello")
            with open(os.path.join(tmp, "beta.txt"), "w", encoding="utf-8") as f:
                f.write("world")
            os.mkdir(os.path.join(tmp, "subfolder"))

            entries = scan_folder(tmp)
            names = [e["name"] for e in entries]
            assert names == ["alpha.txt", "beta.txt", "subfolder"], names
            assert entries[2]["is_dir"] is True

            positions = {"alpha.txt": {"x": 40, "y": 60}}
            notes = [{"id": "test1", "text": "sample note", "x": 10, "y": 20}]
            save_board(tmp, positions, notes)

            # the board file must not appear as a card
            names_after = [e["name"] for e in scan_folder(tmp)]
            assert BOARD_FILENAME not in names_after, names_after

            loaded = load_board(tmp)
            assert loaded["positions"]["alpha.txt"] == {"x": 40, "y": 60}
            assert loaded["notes"][0]["text"] == "sample note"

            # --- drop intake: same volume (tempdir) means "move" ---
            src_dir = os.path.join(tmp, "incoming")
            os.mkdir(src_dir)
            board_dir = os.path.join(tmp, "board")
            os.mkdir(board_dir)
            drop_file = os.path.join(src_dir, "dropped.txt")
            with open(drop_file, "w", encoding="utf-8") as f:
                f.write("drop me")

            placed = place_dropped_items([drop_file], board_dir)
            assert len(placed) == 1, placed
            assert os.path.basename(placed[0]) == "dropped.txt"
            assert os.path.exists(placed[0])
            assert not os.path.exists(drop_file), "same-drive drop must move"

            # --- drop intake: same name must NOT overwrite -> " (2)" ---
            drop_file2 = os.path.join(src_dir, "dropped.txt")
            with open(drop_file2, "w", encoding="utf-8") as f:
                f.write("second copy")
            placed2 = place_dropped_items([drop_file2], board_dir)
            assert len(placed2) == 1, placed2
            assert os.path.basename(placed2[0]) == "dropped (2).txt", placed2
            with open(os.path.join(board_dir, "dropped.txt"),
                      encoding="utf-8") as f:
                assert f.read() == "drop me", "original must be untouched"

            # --- drop intake: folder + already-inside + missing are sane ---
            drop_dir = os.path.join(src_dir, "dropfolder")
            os.mkdir(drop_dir)
            placed3 = place_dropped_items(
                [drop_dir,
                 os.path.join(board_dir, "dropped.txt"),  # already inside
                 os.path.join(src_dir, "no-such-file.xyz")],  # missing
                board_dir,
            )
            assert len(placed3) == 1, placed3
            assert os.path.isdir(placed3[0])

            names_final = [e["name"] for e in scan_folder(board_dir)]
            assert "dropped.txt" in names_final
            assert "dropped (2).txt" in names_final
            assert "dropfolder" in names_final

        with open(SELF_TEST_OK, "w", encoding="utf-8") as f:
            f.write("ok\n")
        return 0
    except Exception as exc:  # pragma: no cover
        try:
            with open(SELF_TEST_OK, "w", encoding="utf-8") as f:
                f.write("FAILED: %r\n" % (exc,))
        except Exception:
            pass
        return 1


# ------------------------------------------------------------------------ GUI
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, simpledialog

    # Native drag & drop from Explorer (optional; graceful fallback).
    DND_FILES = None
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES as _DND_FILES

        DND_FILES = _DND_FILES
        RootClass = TkinterDnD.Tk
    except Exception:
        RootClass = tk.Tk

    CARD_W, CARD_H = 150, 88

    class Board:
        def __init__(self, root):
            self.root = root
            root.title("OkibaBoard")
            root.geometry("1120x720")
            root.configure(bg="#070b18")

            self.folder = None
            self.positions = {}
            self.notes = []
            self.items = {}  # canvas tag -> info dict
            self.known_names = set()
            self.drag = {"tag": None, "dx": 0, "dy": 0}

            bar = tk.Frame(root, bg="#0d1430")
            bar.pack(side="top", fill="x")
            tk.Button(
                bar, text="フォルダを開く", command=self.choose_folder,
                bg="#22315e", fg="white", activebackground="#2e4176",
                relief="flat", padx=12, pady=6,
            ).pack(side="left", padx=8, pady=8)
            tk.Button(
                bar, text="＋メモ", command=self.add_note,
                bg="#22315e", fg="white", activebackground="#2e4176",
                relief="flat", padx=12, pady=6,
            ).pack(side="left", padx=4, pady=8)
            self.label = tk.Label(
                bar,
                text="フォルダを選んでください。"
                     "ファイルはこの画面へドラッグ＆ドロップでも追加できます",
                bg="#0d1430", fg="#aebadd",
            )
            self.label.pack(side="left", padx=12)

            self.canvas = tk.Canvas(root, bg="#070b18", highlightthickness=0)
            self.canvas.pack(side="top", fill="both", expand=True)
            self.canvas.bind("<Configure>", lambda e: self.draw_background())
            if DND_FILES is not None:
                try:
                    self.canvas.drop_target_register(DND_FILES)
                    self.canvas.dnd_bind("<<Drop>>", self.on_drop)
                except Exception:
                    pass
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.root.after(POLL_MS, self.poll_folder)

        # ------------------------------------------------------------ visuals
        def draw_background(self):
            c = self.canvas
            c.delete("bg")
            w = max(c.winfo_width(), 2)
            h = max(c.winfo_height(), 2)
            steps = 48
            top = (5, 8, 20)      # deep navy-black
            bottom = (13, 21, 48)  # dark navy
            for i in range(steps):
                r = int(top[0] + (bottom[0] - top[0]) * i / steps)
                g = int(top[1] + (bottom[1] - top[1]) * i / steps)
                b = int(top[2] + (bottom[2] - top[2]) * i / steps)
                y0 = int(h * i / steps)
                y1 = int(h * (i + 1) / steps) + 1
                c.create_rectangle(
                    0, y0, w, y1, fill="#%02x%02x%02x" % (r, g, b),
                    outline="", tags="bg",
                )
            # subtle dots
            for x in range(40, w, 90):
                for y in range(40, h, 90):
                    c.create_oval(x, y, x + 3, y + 3, fill="#26356b",
                                  outline="", tags="bg")
            c.tag_lower("bg")

        # ------------------------------------------------------------- folder
        def choose_folder(self):
            folder = filedialog.askdirectory()
            if folder:
                self.open_folder(folder)

        def open_folder(self, folder):
            self.folder = folder
            data = load_board(folder)
            self.positions = data.get("positions", {})
            self.notes = data.get("notes", [])
            self.label.config(text=os.path.basename(folder.rstrip("/\\"))
                                  or folder)
            self.render()

        def save(self):
            if self.folder:
                save_board(self.folder, self.positions, self.notes)

        def on_close(self):
            self.save()
            self.root.destroy()

        # --------------------------------------------------------- live sync
        def poll_folder(self):
            try:
                if self.folder and not self.drag.get("tag"):
                    names = {e["name"] for e in scan_folder(self.folder)}
                    if names != self.known_names:
                        # forget positions of removed items
                        for gone in self.known_names - names:
                            self.positions.pop(gone, None)
                        self.render()
            except Exception:
                pass
            finally:
                self.root.after(POLL_MS, self.poll_folder)

        def free_position(self):
            """First free grid slot not used by any current card."""
            used = {(p["x"], p["y"]) for p in self.positions.values()}
            used |= {(n["x"], n["y"]) for n in self.notes}
            idx = 0
            while True:
                x = 24 + (idx % 6) * (CARD_W + 18)
                y = 24 + (idx // 6) * (CARD_H + 18)
                if (x, y) not in used:
                    return x, y
                idx += 1

        # ------------------------------------------------------------ drop in
        def on_drop(self, event):
            if not self.folder:
                self.label.config(
                    text="先に「フォルダを開く」でフォルダを選んでください")
                return
            try:
                paths = list(self.root.tk.splitlist(event.data))
            except Exception:
                paths = [event.data]
            try:
                placed = place_dropped_items(paths, self.folder)
            except Exception as exc:
                self.label.config(text="追加できませんでした: %s" % exc)
                return
            if placed:
                x = max(int(getattr(event, "x", 40)), 8)
                y = max(int(getattr(event, "y", 40)), 8)
                for i, p in enumerate(placed):
                    self.positions[os.path.basename(p)] = {
                        "x": x + i * 20, "y": y + i * 20,
                    }
                self.render()
                self.label.config(
                    text="%d 件を追加しました" % len(placed))
            else:
                self.label.config(text="追加できるものがありませんでした")

        # -------------------------------------------------------------- cards
        def render(self):
            c = self.canvas
            for tag in list(self.items):
                c.delete(tag)
            self.items = {}
            if not self.folder:
                return
            entries = scan_folder(self.folder)
            self.known_names = {e["name"] for e in entries}
            for e in entries:
                pos = self.positions.get(e["name"])
                if pos:
                    x, y = pos["x"], pos["y"]
                else:
                    x, y = self.free_position()
                    self.positions[e["name"]] = {"x": x, "y": y}
                icon = "📁" if e["is_dir"] else "📄"
                self.make_card(
                    kind="dir" if e["is_dir"] else "file",
                    name=e["name"], x=x, y=y, icon=icon,
                )
            for n in self.notes:
                self.make_card(
                    kind="note", name=n["text"], x=n["x"], y=n["y"],
                    icon="📝", note_id=n["id"],
                )
            self.save()

        def make_card(self, kind, name, x, y, icon, note_id=None):
            c = self.canvas
            tag = "card_" + uuid.uuid4().hex[:10]
            fill = "#3a3320" if kind == "note" else "#1a2447"
            outline = "#8a7a3a" if kind == "note" else "#46589a"
            c.create_rectangle(x, y, x + CARD_W, y + CARD_H, fill=fill,
                               outline=outline, width=2, tags=(tag, "card"))
            label = icon + " " + name
            if len(label) > 34:
                label = label[:33] + "…"
            c.create_text(x + 10, y + 12, text=label, anchor="nw",
                          fill="#eef1ff", width=CARD_W - 20,
                          font=("Yu Gothic UI", 10), tags=(tag, "card"))
            self.items[tag] = {
                "kind": kind, "name": name, "note_id": note_id, "x": x, "y": y,
            }
            c.tag_bind(tag, "<ButtonPress-1>", self.on_press)
            c.tag_bind(tag, "<B1-Motion>", self.on_move)
            c.tag_bind(tag, "<ButtonRelease-1>", self.on_release)
            c.tag_bind(tag, "<Double-Button-1>", self.on_double)

        def card_tag(self, event):
            current = self.canvas.gettags("current")
            for t in current:
                if t.startswith("card_"):
                    return t
            return None

        def on_press(self, event):
            tag = self.card_tag(event)
            if not tag:
                return
            info = self.items[tag]
            self.drag = {"tag": tag, "dx": event.x - info["x"],
                         "dy": event.y - info["y"]}
            self.canvas.tag_raise(tag)

        def on_move(self, event):
            tag = self.drag.get("tag")
            if not tag:
                return
            info = self.items[tag]
            nx, ny = event.x - self.drag["dx"], event.y - self.drag["dy"]
            self.canvas.move(tag, nx - info["x"], ny - info["y"])
            info["x"], info["y"] = nx, ny

        def on_release(self, event):
            tag = self.drag.get("tag")
            if not tag:
                return
            info = self.items[tag]
            if info["kind"] == "note":
                for n in self.notes:
                    if n["id"] == info["note_id"]:
                        n["x"], n["y"] = info["x"], info["y"]
            else:
                self.positions[info["name"]] = {"x": info["x"], "y": info["y"]}
            self.drag = {"tag": None, "dx": 0, "dy": 0}
            self.save()

        def on_double(self, event):
            tag = self.card_tag(event)
            if not tag:
                return
            info = self.items[tag]
            if info["kind"] == "note":
                text = simpledialog.askstring(
                    "メモを編集", "メモ:", initialvalue=info["name"],
                    parent=self.root,
                )
                if text is not None:
                    for n in self.notes:
                        if n["id"] == info["note_id"]:
                            n["text"] = text
                    self.render()
            elif self.folder:
                open_item(os.path.join(self.folder, info["name"]))

        def add_note(self):
            if not self.folder:
                self.choose_folder()
                if not self.folder:
                    return
            text = simpledialog.askstring("メモ", "メモ:", parent=self.root)
            if not text:
                return
            x, y = self.free_position()
            self.notes.append(
                {"id": uuid.uuid4().hex[:10], "text": text, "x": x, "y": y}
            )
            self.render()

    root = RootClass()
    app = Board(root)
    # If launched with a folder argument, open it straight away.
    for arg in sys.argv[1:]:
        if os.path.isdir(arg):
            app.open_folder(arg)
            break
    root.mainloop()
    return 0


def main():
    if "--self-test" in sys.argv:
        return run_self_test()
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())

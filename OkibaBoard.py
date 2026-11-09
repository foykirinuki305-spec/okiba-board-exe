# -*- coding: utf-8 -*-
"""
OkibaBoard - Deskboard-style prototype (Python + Tkinter, no dependencies)

- Open a real folder and show its actual files/folders as cards on a dark board.
- Drag cards to arrange them. Positions are saved in the opened folder
  as ``.okiba-board.json``.
- Add note cards (saved in the same json file).
- Double-click a file/folder card to open the real item
  (``os.startfile`` on Windows).

Headless self-test (used by CI):
    OkibaBoard.exe --self-test
    -> exercises scanning + save/load without a GUI,
       writes ``self-test-ok.txt`` in the current directory, exits 0.
"""

import json
import os
import sys
import tempfile
import uuid

BOARD_FILENAME = ".okiba-board.json"
SELF_TEST_OK = "self-test-ok.txt"


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
    data = {"version": 1, "positions": positions, "notes": notes}
    with open(board_path(folder), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def load_board(folder):
    """Load saved positions/notes from *folder*; empty defaults if none."""
    path = board_path(folder)
    if not os.path.exists(path):
        return {"version": 1, "positions": {}, "notes": []}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("positions", {})
    data.setdefault("notes", [])
    return data


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
    """Exercise scanning + save/load without opening a GUI."""
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

    CARD_W, CARD_H = 150, 88

    class Board:
        def __init__(self, root):
            self.root = root
            root.title("OkibaBoard (prototype)")
            root.geometry("1120x720")
            root.configure(bg="#070b18")

            self.folder = None
            self.positions = {}
            self.notes = []
            self.items = {}  # canvas tag -> info dict
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
                bar, text="フォルダを選んでください", bg="#0d1430", fg="#aebadd"
            )
            self.label.pack(side="left", padx=12)

            self.canvas = tk.Canvas(root, bg="#070b18", highlightthickness=0)
            self.canvas.pack(side="top", fill="both", expand=True)
            self.canvas.bind("<Configure>", lambda e: self.draw_background())
            root.protocol("WM_DELETE_WINDOW", self.on_close)

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
            self.label.config(text=folder)
            self.render()

        def save(self):
            if self.folder:
                save_board(self.folder, self.positions, self.notes)

        def on_close(self):
            self.save()
            self.root.destroy()

        # -------------------------------------------------------------- cards
        def render(self):
            c = self.canvas
            for tag in list(self.items):
                c.delete(tag)
            self.items = {}
            if not self.folder:
                return
            entries = scan_folder(self.folder)
            idx = 0
            for e in entries:
                pos = self.positions.get(e["name"])
                if pos:
                    x, y = pos["x"], pos["y"]
                else:
                    x = 24 + (idx % 6) * (CARD_W + 18)
                    y = 24 + (idx // 6) * (CARD_H + 18)
                    self.positions[e["name"]] = {"x": x, "y": y}
                idx += 1
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
            self.notes.append(
                {"id": uuid.uuid4().hex[:10], "text": text, "x": 40, "y": 40}
            )
            self.render()

    root = tk.Tk()
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

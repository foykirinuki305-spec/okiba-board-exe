# -*- coding: utf-8 -*-
"""
OkibaBoard v3 - Deskboard-style board app (Python + Tkinter, stdlib only)

Model: MULTIPLE BOARDS, like files on a home screen.
- Launch -> HOME screen (dark, desktop-like) with board icons.
  Boards are REAL folders under ``~/Documents/OkibaBoards/``.
  One button only: "＋ボード". Double-click a board icon to open it.
  Right-click a board icon: rename the real folder / send the board
  folder to the Recycle Bin. Icon positions are dragged freely
  (NO grid, NO snapping - exactly like a real desktop) and saved to
  ``OkibaBoards/.home.json``.
- BOARD screen: the opened board folder's real files/folders as cards.
  Cards are placed at COMPLETELY FREE positions (no alignment, no
  snapping); a position the user chose is never overwritten by the
  app - the ~2s polling re-render restores saved positions exactly.
  Only brand-new cards get an initial free grid slot.
  Buttons ONLY: "← もどる", "＋メモ", "＋ファイル".
  Right-click a file card: open / rename (renames the real file) /
  send to Recycle Bin. Double-click opens the real file.
  Intake: native Windows drag & drop (WM_DROPFILES via ctypes, no
  external library) OR the guaranteed "＋ファイル" button. Dropped /
  chosen items enter the board folder with the Explorer rule
  (same drive = move, different drive = copy, never overwrite -
  a " (2)" suffix is added instead).
  Positions + notes are saved to ``.okiba-board.json`` in the board
  folder. The folder is polled every ~2 seconds so outside changes
  appear/disappear automatically.

Headless self-test (used by CI):
    OkibaBoard.exe --self-test
    -> exercises board base/home handling + scanning + save/load +
       the intake helper without a GUI, writes ``self-test-ok.txt``
       in the current directory, exits 0.
"""

import json
import os
import shutil
import sys
import tempfile
import uuid

BOARD_FILENAME = ".okiba-board.json"
HOME_FILENAME = ".home.json"
SELF_TEST_OK = "self-test-ok.txt"
POLL_MS = 2000
BASE_DIR_NAME = "OkibaBoards"
DEFAULT_BOARD_NAME = "ボード1"


# ------------------------------------------------------- boards base / home
def default_base_dir():
    """Where the boards live: ~/Documents/OkibaBoards."""
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(docs, BASE_DIR_NAME)


def list_boards(base):
    """Board names = subfolders of *base*, sorted."""
    if not os.path.isdir(base):
        return []
    names = []
    for name in sorted(os.listdir(base), key=lambda s: s.lower()):
        if os.path.isdir(os.path.join(base, name)):
            names.append(name)
    return names


_INVALID_CHARS = '\\/:*?"<>|'


def sanitize_board_name(name):
    """Make *name* safe as a Windows folder name ('' if nothing left)."""
    cleaned = (name or "").strip()
    cleaned = "".join("_" if ch in _INVALID_CHARS else ch
                      for ch in cleaned)
    cleaned = cleaned.strip().strip(".")
    return cleaned


def create_board(base, name):
    """Create a new board folder under *base*; return its final name."""
    cleaned = sanitize_board_name(name)
    if not cleaned:
        raise ValueError("ボード名が空です")
    dest = os.path.join(base, cleaned)
    if os.path.exists(dest):
        raise ValueError("同じ名前のボードがあります")
    os.makedirs(dest)
    return cleaned


def rename_board(base, old, new):
    """Rename board folder *old* to *new* under *base*; return new name."""
    cleaned = sanitize_board_name(new)
    if not cleaned:
        raise ValueError("ボード名が空です")
    if cleaned == old:
        return cleaned
    src = os.path.join(base, old)
    dest = os.path.join(base, cleaned)
    if not os.path.isdir(src):
        raise ValueError("元のボードがありません")
    if os.path.exists(dest):
        raise ValueError("同じ名前のボードがあります")
    os.rename(src, dest)
    return cleaned


def ensure_base(base=None):
    """Create the base folder; make one default board if there are none."""
    if base is None:
        base = default_base_dir()
    os.makedirs(base, exist_ok=True)
    if not list_boards(base):
        os.makedirs(os.path.join(base, DEFAULT_BOARD_NAME), exist_ok=True)
    return base


def home_path(base):
    return os.path.join(base, HOME_FILENAME)


def load_home(base):
    """Load home icon layout; empty defaults if none/corrupt."""
    path = home_path(base)
    if not os.path.exists(path):
        return {"positions": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"positions": {}}
    data.setdefault("positions", {})
    return data


def save_home(base, positions):
    """Save home icon positions (dict name -> {x, y}) under *base*."""
    with open(home_path(base), "w", encoding="utf-8") as f:
        json.dump({"positions": positions}, f, ensure_ascii=False, indent=2)


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
        entries.append({"name": name, "is_dir": os.path.isdir(full)})
    return entries


def board_path(folder):
    return os.path.join(folder, BOARD_FILENAME)


def save_board(folder, positions, notes):
    """Save card positions (dict name-> {x, y}) and notes (list) to *folder*."""
    data = {"version": 3, "positions": positions, "notes": notes}
    with open(board_path(folder), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def load_board(folder):
    """Load saved positions/notes from *folder*; empty defaults if none."""
    path = board_path(folder)
    if not os.path.exists(path):
        return {"version": 3, "positions": {}, "notes": []}
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
    else:
        import subprocess

        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, path])


def recycle_bin_delete(paths):
    """Move *paths* to the Windows Recycle Bin. True on success.

    Uses shell32 SHFileOperationW with FOF_ALLOWUNDO. Any failure or
    user cancellation returns False and nothing is deleted permanently.
    Non-Windows platforms return False.
    """
    if sys.platform != "win32" or not paths:
        return False
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x0040
        p_from = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = p_from
        op.fFlags = FOF_ALLOWUNDO
        ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return ret == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


# ------------------------------------------------------------------ self-test
def run_self_test():
    """Exercise base/home handling + scanning + save/load + intake."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # ---------- boards base & home layout ----------
            base = os.path.join(tmp, "OkibaBoards")
            ensure_base(base)
            assert list_boards(base) == [DEFAULT_BOARD_NAME], list_boards(base)

            created = create_board(base, "資料")
            assert created == "資料"
            assert sorted(list_boards(base)) == sorted(
                [DEFAULT_BOARD_NAME, "資料"])

            try:
                create_board(base, "資料")
                raise AssertionError("duplicate board must raise")
            except ValueError:
                pass

            cleaned = create_board(base, "a/b:c")
            assert cleaned == "a_b_c", cleaned

            renamed = rename_board(base, "資料", "素材 置き場")
            assert renamed == "素材 置き場"
            assert "素材 置き場" in list_boards(base)

            try:
                rename_board(base, "素材 置き場", "a_b_c")
                raise AssertionError("rename onto existing must raise")
            except ValueError:
                pass

            save_home(base, {DEFAULT_BOARD_NAME: {"x": 12, "y": 34}})
            home = load_home(base)
            assert home["positions"][DEFAULT_BOARD_NAME] == {"x": 12, "y": 34}
            # the home layout file is a file, never a board
            assert HOME_FILENAME not in list_boards(base)

            # ---------- board data (scan + save/load) ----------
            board = os.path.join(base, DEFAULT_BOARD_NAME)
            with open(os.path.join(board, "alpha.txt"), "w",
                      encoding="utf-8") as f:
                f.write("hello")
            with open(os.path.join(board, "beta.txt"), "w",
                      encoding="utf-8") as f:
                f.write("world")
            os.mkdir(os.path.join(board, "subfolder"))

            entries = scan_folder(board)
            names = [e["name"] for e in entries]
            assert names == ["alpha.txt", "beta.txt", "subfolder"], names
            assert entries[2]["is_dir"] is True

            positions = {"alpha.txt": {"x": 137, "y": 259}}
            notes = [{"id": "test1", "text": "sample note", "x": 55, "y": 66}]
            save_board(board, positions, notes)

            names_after = [e["name"] for e in scan_folder(board)]
            assert BOARD_FILENAME not in names_after, names_after

            loaded = load_board(board)
            # saved positions must come back EXACTLY (free placement)
            assert loaded["positions"]["alpha.txt"] == {"x": 137, "y": 259}
            assert loaded["notes"][0]["text"] == "sample note"
            assert loaded["notes"][0]["x"] == 55
            assert loaded["notes"][0]["y"] == 66

            # ---------- intake helper (drop / ＋ファイル shared) ----------
            src_dir = os.path.join(tmp, "incoming")
            os.mkdir(src_dir)
            board_dir = os.path.join(tmp, "board2")
            os.mkdir(board_dir)
            drop_file = os.path.join(src_dir, "dropped.txt")
            with open(drop_file, "w", encoding="utf-8") as f:
                f.write("drop me")

            placed = place_dropped_items([drop_file], board_dir)
            assert len(placed) == 1, placed
            assert os.path.basename(placed[0]) == "dropped.txt"
            assert os.path.exists(placed[0])
            assert not os.path.exists(drop_file), "same-drive drop must move"

            drop_file2 = os.path.join(src_dir, "dropped.txt")
            with open(drop_file2, "w", encoding="utf-8") as f:
                f.write("second copy")
            placed2 = place_dropped_items([drop_file2], board_dir)
            assert len(placed2) == 1, placed2
            assert os.path.basename(placed2[0]) == "dropped (2).txt", placed2
            with open(os.path.join(board_dir, "dropped.txt"),
                      encoding="utf-8") as f:
                assert f.read() == "drop me", "original must be untouched"

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

            # recycle helper must refuse safely off Windows (never deletes)
            if sys.platform != "win32":
                assert recycle_bin_delete(
                    [os.path.join(board_dir, "dropped.txt")]) is False
                assert os.path.exists(os.path.join(board_dir, "dropped.txt"))

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


# --------------------------------------- Windows native drag & drop (WM_DROPFILES)
class NativeDrop:
    """Explorer file drops via shell32 DragAcceptFiles + WM_DROPFILES.

    Subclasses the window procedure of each given hwnd with
    SetWindowLongPtrW. Stdlib ctypes only; all of this is a no-op
    off Windows. ``callback(paths, x, y, hwnd)`` receives the dropped
    file paths and the drop point in that window's client coordinates.
    """

    WM_DROPFILES = 0x0233
    GWLP_WNDPROC = -4

    def __init__(self, callback):
        self.callback = callback
        self.active = False
        self._installed = {}
        self._proc_refs = []  # keep ctypes callbacks alive
        self._ready = sys.platform == "win32"
        if not self._ready:
            return
        try:
            import ctypes

            self._ctypes = ctypes
            self.user32 = ctypes.windll.user32
            self.shell32 = ctypes.windll.shell32
            self.user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            self.user32.GetWindowLongPtrW.argtypes = [
                ctypes.c_void_p, ctypes.c_int]
            self.user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            self.user32.SetWindowLongPtrW.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            self.user32.CallWindowProcW.restype = ctypes.c_ssize_t
            self.user32.CallWindowProcW.argtypes = [
                ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_size_t, ctypes.c_ssize_t]
            self.shell32.DragAcceptFiles.argtypes = [
                ctypes.c_void_p, ctypes.c_bool]
            self.shell32.DragQueryFileW.restype = ctypes.c_uint
            self.shell32.DragQueryFileW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_wchar_p, ctypes.c_uint]
            self._WNDPROC = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_size_t, ctypes.c_ssize_t)
        except Exception:
            self._ready = False

    def install(self, hwnd):
        """Register *hwnd* as a drop target. Returns True on success."""
        if not self._ready or not hwnd or hwnd in self._installed:
            return self.active and hwnd in self._installed
        ctypes = self._ctypes
        try:
            old_proc = self.user32.GetWindowLongPtrW(hwnd, self.GWLP_WNDPROC)
            if not old_proc:
                return False

            def _proc(h, msg, wparam, lparam, _old=old_proc, _hwnd=hwnd):
                if msg == NativeDrop.WM_DROPFILES:
                    try:
                        self._handle_drop(wparam, _hwnd)
                    except Exception:
                        pass
                    return 0
                return self.user32.CallWindowProcW(
                    _old, h, msg, wparam, lparam)

            proc_cb = self._WNDPROC(_proc)
            self._proc_refs.append(proc_cb)
            proc_addr = ctypes.cast(proc_cb, ctypes.c_void_p).value
            prev = self.user32.SetWindowLongPtrW(
                hwnd, self.GWLP_WNDPROC, proc_addr)
            if not prev:
                return False
            self.shell32.DragAcceptFiles(hwnd, True)
            self._installed[hwnd] = old_proc
            self.active = True
            return True
        except Exception:
            return False

    def _handle_drop(self, hdrop, hwnd):
        ctypes = self._ctypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        count = self.shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        paths = []
        for i in range(count):
            length = self.shell32.DragQueryFileW(hdrop, i, None, 0)
            buf = ctypes.create_unicode_buffer(length + 1)
            self.shell32.DragQueryFileW(hdrop, i, buf, length + 1)
            if buf.value:
                paths.append(buf.value)
        pt = POINT()
        try:
            self.shell32.DragQueryPoint(hdrop, ctypes.byref(pt))
        except Exception:
            pt.x, pt.y = 40, 40
        try:
            self.shell32.DragFinish(hdrop)
        except Exception:
            pass
        if paths:
            self.callback(paths, int(pt.x), int(pt.y), hwnd)


# ------------------------------------------------------------------------ GUI
BG_TOP = (5, 8, 20)       # deep navy-black
BG_BOTTOM = (13, 21, 48)  # dark navy


def draw_gradient(canvas):
    """Redraw the dark gradient + subtle dots background of *canvas*."""
    canvas.delete("bg")
    w = max(canvas.winfo_width(), 2)
    h = max(canvas.winfo_height(), 2)
    steps = 48
    for i in range(steps):
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * i / steps)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * i / steps)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * i / steps)
        y0 = int(h * i / steps)
        y1 = int(h * (i + 1) / steps) + 1
        canvas.create_rectangle(0, y0, w, y1,
                                fill="#%02x%02x%02x" % (r, g, b),
                                outline="", tags="bg")
    for x in range(40, w, 90):
        for y in range(40, h, 90):
            canvas.create_oval(x, y, x + 3, y + 3, fill="#26356b",
                               outline="", tags="bg")
    canvas.tag_lower("bg")


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog

    CARD_W, CARD_H = 150, 88      # board file/note cards
    ICON_W, ICON_H = 136, 108     # home board icons

    class App:
        def __init__(self, root):
            self.root = root
            root.title("OkibaBoard")
            root.geometry("1120x720")
            root.configure(bg="#070b18")

            self.base = ensure_base()
            self.view = "home"

            # ---- home state ----
            self.home_positions = load_home(self.base).get("positions", {})
            self.home_items = {}   # canvas tag -> board name
            self.home_drag = {"tag": None, "dx": 0, "dy": 0}

            # ---- board state ----
            self.board_folder = None
            self.board_name = ""
            self.positions = {}
            self.notes = []
            self.items = {}        # canvas tag -> info dict
            self.known_names = set()
            self.drag = {"tag": None, "dx": 0, "dy": 0}

            # ---- native drop (set up after widgets exist) ----
            self.drop = NativeDrop(self.on_native_drop)
            self.drop_setup_done = False

            self._build_home()
            self._build_board()
            self.show_home()
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.root.after(60, self.setup_drop)
            self.root.after(POLL_MS, self.poll_folder)

        # ------------------------------------------------------------ layout
        def _make_button(self, parent, text, command):
            import tkinter as tk

            btn = tk.Button(
                parent, text=text, command=command,
                bg="#22315e", fg="white", activebackground="#2e4176",
                relief="flat", padx=12, pady=6)
            btn.pack(side="left", padx=8, pady=8)
            return btn

        def _build_home(self):
            import tkinter as tk

            frame = tk.Frame(self.root, bg="#070b18")
            self.home_frame = frame
            bar = tk.Frame(frame, bg="#0d1430")
            bar.pack(side="top", fill="x")
            self._make_button(bar, "＋ボード", self.add_board)
            tk.Label(
                bar, text="ボードをダブルクリックで開きます。"
                          "アイコンは好きな場所へドラッグできます",
                bg="#0d1430", fg="#aebadd",
            ).pack(side="left", padx=12)
            self.home_canvas = tk.Canvas(frame, bg="#070b18",
                                         highlightthickness=0)
            self.home_canvas.pack(side="top", fill="both", expand=True)
            self.home_canvas.bind(
                "<Configure>", lambda e: draw_gradient(self.home_canvas))

        def _build_board(self):
            import tkinter as tk

            frame = tk.Frame(self.root, bg="#070b18")
            self.board_frame = frame
            bar = tk.Frame(frame, bg="#0d1430")
            bar.pack(side="top", fill="x")
            self._make_button(bar, "← もどる", self.show_home)
            self._make_button(bar, "＋メモ", self.add_note)
            self._make_button(bar, "＋ファイル", self.add_files)
            self.board_label = tk.Label(bar, text="", bg="#0d1430",
                                        fg="#aebadd")
            self.board_label.pack(side="left", padx=12)
            self.canvas = tk.Canvas(frame, bg="#070b18",
                                    highlightthickness=0)
            self.canvas.pack(side="top", fill="both", expand=True)
            self.canvas.bind(
                "<Configure>", lambda e: draw_gradient(self.canvas))

        def show_home(self):
            self.save_board_data()
            self.view = "home"
            self.board_frame.pack_forget()
            self.home_frame.pack(side="top", fill="both", expand=True)
            self.root.title("OkibaBoard")
            self.render_home()

        def on_close(self):
            self.save_board_data()
            try:
                save_home(self.base, self.home_positions)
            except Exception:
                pass
            self.root.destroy()

        def save_board_data(self):
            if self.board_folder:
                try:
                    save_board(self.board_folder, self.positions, self.notes)
                except Exception:
                    pass

        # -------------------------------------------------------- drop setup
        def setup_drop(self):
            """Subclass root + board canvas for WM_DROPFILES (Windows).

            The board surface is a single Canvas whose items are drawn
            canvas items (no child widgets), so the canvas hwnd is the
            one drop target. Home drops are not needed and are ignored.
            """
            if self.drop_setup_done:
                return
            try:
                self.root_hwnd = self.root.winfo_id()
                self.drop.install(self.root_hwnd)
            except Exception:
                self.root_hwnd = None
            try:
                self.drop.install(self.canvas.winfo_id())
            except Exception:
                pass
            # only stop retrying once registration really succeeded
            # (or there is nothing to register on this platform)
            if self.drop.active or sys.platform != "win32":
                self.drop_setup_done = True

        def on_native_drop(self, paths, x, y, hwnd):
            if self.view != "board" or not self.board_folder:
                return  # drops on the home screen are ignored
            if getattr(self, "root_hwnd", None) is not None \
                    and hwnd == self.root_hwnd:
                # root client coords -> canvas coords
                x -= self.canvas.winfo_x()
                y -= self.canvas.winfo_y()
            x = max(int(x), 8)
            y = max(int(y), 8)
            self.intake_files(paths, x, y)

        def intake_files(self, paths, x=None, y=None):
            """Shared intake for native drop and the ＋ファイル button."""
            if not self.board_folder:
                return
            try:
                placed = place_dropped_items(list(paths), self.board_folder)
            except Exception as exc:
                self.board_label.config(text="追加できませんでした: %s" % exc)
                return
            if placed:
                if x is None or y is None:
                    x, y = self.free_position()
                for i, p in enumerate(placed):
                    self.positions[os.path.basename(p)] = {
                        "x": x + i * 22, "y": y + i * 22}
                self.render_board()
                self.update_board_hint("%d 件を追加しました" % len(placed))
            else:
                self.update_board_hint("追加できるものがありませんでした")

        def add_files(self):
            paths = filedialog.askopenfilenames(
                title="ボードに入れるファイルを選んでください")
            if paths:
                self.intake_files(list(paths))

        def update_board_hint(self, extra=""):
            if self.drop.active:
                hint = "ファイルをここに落とせます"
            else:
                hint = "ドラッグで入らない時は＋ファイルを使ってね"
            text = self.board_name
            if extra:
                text += "：%s（%s）" % (extra, hint)
            else:
                text += "：" + hint
            self.board_label.config(text=text)

        # ------------------------------------------------------------ home
        def render_home(self):
            c = self.home_canvas
            for tag in list(self.home_items):
                c.delete(tag)
            self.home_items = {}
            boards = list_boards(self.base)
            # forget positions of boards that no longer exist
            for gone in [n for n in self.home_positions if n not in boards]:
                del self.home_positions[gone]
            for name in boards:
                pos = self.home_positions.get(name)
                if pos:
                    x, y = pos["x"], pos["y"]  # saved place: EXACT
                else:
                    x, y = self.home_free_position()
                    self.home_positions[name] = {"x": x, "y": y}
                self.make_board_icon(name, x, y)
            save_home(self.base, self.home_positions)

        def home_free_position(self):
            used = {(p["x"], p["y"]) for p in self.home_positions.values()}
            idx = 0
            while True:
                x = 24 + (idx % 6) * (ICON_W + 20)
                y = 24 + (idx // 6) * (ICON_H + 20)
                if (x, y) not in used:
                    return x, y
                idx += 1

        def make_board_icon(self, name, x, y):
            c = self.home_canvas
            tag = "board_" + uuid.uuid4().hex[:10]
            c.create_rectangle(x, y, x + ICON_W, y + ICON_H,
                               fill="#1a2447", outline="#46589a", width=2,
                               tags=(tag, "bicon"))
            c.create_text(x + ICON_W // 2, y + 30, text="📁",
                          font=("Segoe UI Emoji", 26), fill="#eef1ff",
                          tags=(tag, "bicon"))
            label = name if len(name) <= 16 else name[:15] + "…"
            c.create_text(x + ICON_W // 2, y + ICON_H - 26, text=label,
                          font=("Yu Gothic UI", 11), fill="#eef1ff",
                          width=ICON_W - 12, justify="center",
                          tags=(tag, "bicon"))
            self.home_items[tag] = name
            c.tag_bind(tag, "<ButtonPress-1>", self.home_press)
            c.tag_bind(tag, "<B1-Motion>", self.home_move)
            c.tag_bind(tag, "<ButtonRelease-1>", self.home_release)
            c.tag_bind(tag, "<Double-Button-1>", self.home_double)
            c.tag_bind(tag, "<Button-3>", self.home_right)

        def home_tag(self):
            for t in self.home_canvas.gettags("current"):
                if t.startswith("board_"):
                    return t
            return None

        def home_press(self, event):
            tag = self.home_tag()
            if not tag:
                return
            name = self.home_items[tag]
            pos = self.home_positions[name]
            self.home_drag = {"tag": tag, "dx": event.x - pos["x"],
                              "dy": event.y - pos["y"]}
            self.home_canvas.tag_raise(tag)

        def home_move(self, event):
            tag = self.home_drag.get("tag")
            if not tag:
                return
            name = self.home_items[tag]
            pos = self.home_positions[name]
            nx, ny = event.x - self.home_drag["dx"], event.y - self.home_drag["dy"]
            self.home_canvas.move(tag, nx - pos["x"], ny - pos["y"])
            self.home_positions[name] = {"x": nx, "y": ny}

        def home_release(self, event):
            if self.home_drag.get("tag"):
                save_home(self.base, self.home_positions)
            self.home_drag = {"tag": None, "dx": 0, "dy": 0}

        def home_double(self, event):
            tag = self.home_tag()
            if tag:
                self.open_board(self.home_items[tag])

        def home_right(self, event):
            import tkinter as tk

            tag = self.home_tag()
            if not tag:
                return
            name = self.home_items[tag]
            menu = tk.Menu(self.root, tearoff=0)
            menu.add_command(label="名前変更",
                             command=lambda: self.rename_board_ui(name))
            menu.add_command(label="ゴミ箱へ削除",
                             command=lambda: self.trash_board_ui(name))
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        def add_board(self):
            boards = list_boards(self.base)
            n = len(boards) + 1
            while ("ボード%d" % n) in boards:
                n += 1
            name = simpledialog.askstring(
                "ボードを作る", "ボードの名前:",
                initialvalue="ボード%d" % n, parent=self.root)
            if name is None:
                return
            try:
                created = create_board(self.base, name)
            except ValueError as exc:
                messagebox.showerror("ボードを作れません", str(exc),
                                     parent=self.root)
                return
            fx, fy = self.home_free_position()
            self.home_positions[created] = {"x": fx, "y": fy}
            self.render_home()

        def rename_board_ui(self, old):
            new = simpledialog.askstring(
                "名前変更", "新しい名前:", initialvalue=old, parent=self.root)
            if new is None:
                return
            try:
                created = rename_board(self.base, old, new)
            except ValueError as exc:
                messagebox.showerror("名前変更できません", str(exc),
                                     parent=self.root)
                return
            if created != old and old in self.home_positions:
                self.home_positions[created] = self.home_positions.pop(old)
            self.render_home()

        def trash_board_ui(self, name):
            path = os.path.join(self.base, name)
            if not messagebox.askyesno(
                    "ゴミ箱へ削除",
                    "ボード「%s」をゴミ箱へ移しますか？\n"
                    "（中のファイルも一緒にゴミ箱へ入ります。"
                    "ゴミ箱から戻せます）" % name,
                    parent=self.root):
                return
            if recycle_bin_delete([path]):
                self.home_positions.pop(name, None)
                self.render_home()
            else:
                messagebox.showwarning(
                    "削除できませんでした",
                    "ゴミ箱への移動を中止しました。ボードはそのままです。",
                    parent=self.root)

        # ------------------------------------------------------------ board
        def open_board(self, name):
            self.setup_drop()
            self.board_name = name
            self.board_folder = os.path.join(self.base, name)
            data = load_board(self.board_folder)
            self.positions = data.get("positions", {})
            self.notes = data.get("notes", [])
            self.view = "board"
            self.home_frame.pack_forget()
            self.board_frame.pack(side="top", fill="both", expand=True)
            self.root.title("OkibaBoard - " + name)
            self.render_board()
            self.update_board_hint()
            # retry once the board widgets are really on screen, then
            # refresh the hint so it always tells the truth
            self.root.after(120, self.setup_drop)
            self.root.after(200, self.update_board_hint)

        def poll_folder(self):
            try:
                if self.view == "board" and self.board_folder \
                        and not self.drag.get("tag"):
                    names = {e["name"] for e in scan_folder(self.board_folder)}
                    if names != self.known_names:
                        for gone in self.known_names - names:
                            self.positions.pop(gone, None)
                        # saved positions of remaining cards are kept
                        # EXACTLY as-is; only new cards get a free slot
                        # inside render_board().
                        self.render_board()
            except Exception:
                pass
            finally:
                self.root.after(POLL_MS, self.poll_folder)

        def free_position(self):
            used = {(p["x"], p["y"]) for p in self.positions.values()}
            used |= {(n["x"], n["y"]) for n in self.notes}
            idx = 0
            while True:
                x = 24 + (idx % 6) * (CARD_W + 18)
                y = 24 + (idx // 6) * (CARD_H + 18)
                if (x, y) not in used:
                    return x, y
                idx += 1

        def render_board(self):
            c = self.canvas
            for tag in list(self.items):
                c.delete(tag)
            self.items = {}
            if not self.board_folder:
                return
            entries = scan_folder(self.board_folder)
            self.known_names = {e["name"] for e in entries}
            for e in entries:
                pos = self.positions.get(e["name"])
                if pos:
                    x, y = pos["x"], pos["y"]  # saved place: EXACT
                else:
                    x, y = self.free_position()
                    self.positions[e["name"]] = {"x": x, "y": y}
                self.make_card(
                    kind="dir" if e["is_dir"] else "file",
                    name=e["name"], x=x, y=y,
                    icon="📁" if e["is_dir"] else "📄")
            for n in self.notes:
                self.make_card(kind="note", name=n["text"], x=n["x"], y=n["y"],
                               icon="📝", note_id=n["id"])
            self.save_board_data()

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
            self.items[tag] = {"kind": kind, "name": name,
                               "note_id": note_id, "x": x, "y": y}
            c.tag_bind(tag, "<ButtonPress-1>", self.on_press)
            c.tag_bind(tag, "<B1-Motion>", self.on_move)
            c.tag_bind(tag, "<ButtonRelease-1>", self.on_release)
            c.tag_bind(tag, "<Double-Button-1>", self.on_double)
            c.tag_bind(tag, "<Button-3>", self.card_right)

        def card_tag(self):
            for t in self.canvas.gettags("current"):
                if t.startswith("card_"):
                    return t
            return None

        def on_press(self, event):
            tag = self.card_tag()
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
                self.positions[info["name"]] = {"x": info["x"],
                                                "y": info["y"]}
            self.drag = {"tag": None, "dx": 0, "dy": 0}
            self.save_board_data()

        def on_double(self, event):
            tag = self.card_tag()
            if not tag:
                return
            info = self.items[tag]
            if info["kind"] == "note":
                self.edit_note(info)
            elif self.board_folder:
                open_item(os.path.join(self.board_folder, info["name"]))

        # ---- right-click menu (board cards) ----
        def card_right(self, event):
            import tkinter as tk

            tag = self.card_tag()
            if not tag:
                return
            info = self.items[tag]
            menu = tk.Menu(self.root, tearoff=0)
            if info["kind"] == "note":
                menu.add_command(
                    label="編集",
                    command=lambda: self.edit_note(info))
                menu.add_command(
                    label="削除",
                    command=lambda: self.delete_note(info))
            else:
                menu.add_command(
                    label="開く",
                    command=lambda: open_item(
                        os.path.join(self.board_folder, info["name"])))
                menu.add_command(
                    label="名前変更",
                    command=lambda: self.rename_file_ui(info))
                menu.add_command(
                    label="ゴミ箱へ削除",
                    command=lambda: self.trash_file_ui(info))
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        def edit_note(self, info):
            text = simpledialog.askstring(
                "メモを編集", "メモ:", initialvalue=info["name"],
                parent=self.root)
            if text is not None:
                for n in self.notes:
                    if n["id"] == info["note_id"]:
                        n["text"] = text
                self.render_board()

        def delete_note(self, info):
            self.notes = [n for n in self.notes
                          if n["id"] != info["note_id"]]
            self.render_board()

        def rename_file_ui(self, info):
            old = info["name"]
            new = simpledialog.askstring(
                "名前変更", "新しい名前:", initialvalue=old, parent=self.root)
            if new is None:
                return
            new = new.strip()
            if not new or new == old:
                return
            if any(ch in new for ch in _INVALID_CHARS):
                messagebox.showerror(
                    "名前変更できません",
                    "名前に \\ / : * ? \" < > | は使えません",
                    parent=self.root)
                return
            src = os.path.join(self.board_folder, old)
            dest = os.path.join(self.board_folder, new)
            if os.path.exists(dest):
                messagebox.showerror(
                    "名前変更できません", "同じ名前のものがあります",
                    parent=self.root)
                return
            try:
                os.rename(src, dest)
            except OSError as exc:
                messagebox.showerror("名前変更できません", str(exc),
                                     parent=self.root)
                return
            if old in self.positions:
                self.positions[new] = self.positions.pop(old)
            self.render_board()

        def trash_file_ui(self, info):
            path = os.path.join(self.board_folder, info["name"])
            if recycle_bin_delete([path]):
                self.positions.pop(info["name"], None)
                self.render_board()
            else:
                messagebox.showwarning(
                    "削除できませんでした",
                    "ゴミ箱への移動を中止しました。ファイルはそのままです。",
                    parent=self.root)

        def add_note(self):
            if self.view != "board" or not self.board_folder:
                return
            text = simpledialog.askstring("メモ", "メモ:", parent=self.root)
            if not text:
                return
            x, y = self.free_position()
            self.notes.append({"id": uuid.uuid4().hex[:10], "text": text,
                               "x": x, "y": y})
            self.render_board()

    root = tk.Tk()
    app = App(root)
    # If launched with a folder argument inside the boards base, open it.
    for arg in sys.argv[1:]:
        if os.path.isdir(arg):
            parent = os.path.dirname(os.path.abspath(arg))
            if parent == os.path.abspath(app.base):
                app.open_board(os.path.basename(os.path.abspath(arg)))
            break
    root.mainloop()
    return 0


def main():
    if "--self-test" in sys.argv:
        return run_self_test()
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())

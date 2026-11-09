# -*- coding: utf-8 -*-
"""
OkibaBoard v5 - Deskboard-style board app (Python + Tkinter, stdlib only)

Model: MULTIPLE BOARDS, like files on a home screen.
- Launch -> HOME screen (dark, desktop-like) with board icons.
  Boards are REAL folders under ``<SystemDrive>\\OkibaBoards``
  (typically ``C:\\OkibaBoards``; on first run an old
  ``~/Documents/OkibaBoards`` is moved there automatically - if the
  move fails the old folder keeps being used, nothing is lost).
  One button only: "＋ボード" - it ALWAYS asks for a name first, so
  the user freely names every board (the name is the real folder
  name; unusable characters are rejected with an explanation, never
  silently replaced). Double-click a board icon to open it.
  Right-click a board icon: 場所を開く / rename / Recycle Bin.
  Board icons SNAP to a grid (vertical columns, like desktop icons)
  and their cells are saved to ``OkibaBoards/.home.json``.
- BOARD screen: the opened board folder's real files/folders as cards.
  Cards SNAP "ピチッと" to a grid, filling VERTICAL columns first
  (top-to-bottom, then the next column), exactly like real desktop
  icons. Dragging a card and letting go snaps it to the nearest cell;
  if that cell is taken, the two cards SWAP cells. Cells are saved to
  ``.okiba-board.json`` inside the board folder.
  Card size is adjustable in percent with one toolbar button
  "大きさ: 80%" etc. (cycles 25/30/40/50/60/70/80/90/100/125 percent
  of the base 中 size; card size, grid cell and font change together;
  saved per board as "size_percent"; v4's 小/中/大 are migrated).
  Buttons: "← もどる", "＋メモ", "＋ファイル", "大きさ: …%".
  Right-click a file card: 開く / 場所を開く / デスクトップへ移動 /
  rename (renames the real file) / send to Recycle Bin.
  Double-click opens the real file.
  Dragging a file/folder card OUT of the window starts a native OLE
  drag (ole32.DoDragDrop, CF_HDROP) so it can be dropped on the
  Desktop / Explorer with MOVE; the right-click "デスクトップへ移動"
  always works as a non-drag alternative.
  Intake: native Windows drag & drop (WM_DROPFILES via ctypes, no
  external library) OR the guaranteed "＋ファイル" button. Dropped /
  chosen items enter the board folder with the Explorer rule
  (same drive = move, different drive = copy, never overwrite -
  a " (2)" suffix is added instead).
  The folder is polled every ~2 seconds (and re-scanned when the
  window regains focus) so outside changes appear/disappear.
  Look: dark themes only - "テーマ: …" toolbar buttons cycle
  紺/黒グレー/深緑/紫夜/焦げ茶 (app-global, saved in .home.json);
  cards are rounded with a soft shadow, colour-coded by kind
  (folder/video/image/audio/note/file), brighten on hover, and
  image files show a Pillow thumbnail when Pillow is available.

Headless self-test (used by CI):
    OkibaBoard.exe --self-test
    -> exercises base resolution + migration, grid math, board/home
       handling, scanning, save/load and the intake helper without a
       GUI, writes ``self-test-ok.txt``, exits 0.
    (Set OKIBA_BASE to a temp dir to keep the test away from the
     real C:\\ and Documents folders.)
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
BASE_ENV_VAR = "OKIBA_BASE"

# ------------------------------------------------------------------- grid
ORIGIN = 16  # px margin before the first cell, both screens

# Board card sizes are a PERCENT of the base (former "中") size:
# button cycles through these choices (small side is fine-grained
# because the user mainly wants SMALLER cards; 125% is the only
# larger step kept).
PERCENT_CHOICES = [25, 30, 40, 50, 60, 70, 80, 90, 100, 125]
DEFAULT_PERCENT = 100
BASE_CARD_W, BASE_CARD_H = 150, 88    # 100% card size in px
BASE_CELL_W, BASE_CELL_H = 166, 104   # 100% grid cell size in px
BASE_FONT = 10                        # 100% font size in pt
MIN_FONT = 6                          # pt, floor even at 25%
# v4 stored the size as 小/中/大; migrated to percent on load.
# (大 is capped at the new maximum choice, 125%.)
LEGACY_SIZE_PERCENT = {"小": 70, "中": 100, "大": 125}


def size_metrics(percent):
    """Card/cell/font metrics for *percent* (int) of the base size."""
    try:
        p = int(percent)
    except Exception:
        p = DEFAULT_PERCENT
    p = max(10, min(p, 200))
    card_w = max(24, int(round(BASE_CARD_W * p / 100.0)))
    card_h = max(16, int(round(BASE_CARD_H * p / 100.0)))
    return {
        "percent": p,
        "card_w": card_w,
        "card_h": card_h,
        "cell_w": card_w + (BASE_CELL_W - BASE_CARD_W),
        "cell_h": card_h + (BASE_CELL_H - BASE_CARD_H),
        "font": max(MIN_FONT, int(round(BASE_FONT * p / 100.0))),
    }


def next_percent(current):
    """Next size choice after *current* percent (cycles the list)."""
    for choice in PERCENT_CHOICES:
        if choice > current:
            return choice
    return PERCENT_CHOICES[0]

# Home board icons: one fixed grid.
HOME_ICON_W, HOME_ICON_H = 136, 108
HOME_CELL_W, HOME_CELL_H = 152, 124


def cell_to_pos(col, row, cell_w, cell_h):
    """Pixel (x, y) of the top-left corner of grid cell (col, row)."""
    return (ORIGIN + col * cell_w, ORIGIN + row * cell_h)


def pos_to_cell(x, y, cell_w, cell_h):
    """Nearest grid cell for pixel point (x, y); never negative."""
    col = int(round((x - ORIGIN) / float(cell_w)))
    row = int(round((y - ORIGIN) / float(cell_h)))
    return (max(col, 0), max(row, 0))


def rows_per_column(view_height, cell_h):
    """How many cells fit vertically in *view_height* pixels."""
    usable = max(int(view_height) - ORIGIN, cell_h)
    return max(1, usable // cell_h)


def iter_cells(rows_per_col):
    """Yield cells in desktop order: fill a column top-to-bottom,
    then move to the next column on the right."""
    col = 0
    while True:
        for row in range(rows_per_col):
            yield (col, row)
        col += 1


def next_free_cell(occupied, rows_per_col, start=None):
    """First free cell in vertical-column order.

    *occupied* is a set of (col, row) tuples. When *start* is given,
    the search begins at that cell (inclusive) in the same order.
    """
    started = start is None
    for cell in iter_cells(rows_per_col):
        if not started:
            if cell == tuple(start):
                started = True
            else:
                continue
        if cell not in occupied:
            return cell
    raise AssertionError("unreachable")


def move_to_cell(cells, key, target):
    """Move *key* to *target* cell in dict *cells* (key -> (col, row)).

    If another key occupies the target, the two keys SWAP cells.
    If the moving key had no cell yet and the target is taken, the
    occupant keeps its place and the mover does not move (callers
    normally place new keys via next_free_cell instead).
    Returns the occupants-swapped key or None.
    """
    target = tuple(target)
    old = cells.get(key)
    if old is not None:
        old = tuple(old)
    if old == target:
        return None
    other = None
    for k, v in cells.items():
        if k != key and tuple(v) == target:
            other = k
            break
    if other is not None:
        if old is None:
            return None
        cells[other] = old
    cells[key] = target
    return other


def combined_cells(positions, notes):
    """One cell map for files + notes: {("file", name): cell,
    ("note", id): cell}. This is the map drags operate on, so a file
    card and a note card swap with each other exactly like two files.
    """
    cells = {}
    for name, cell in positions.items():
        cells[("file", name)] = tuple(cell)
    for n in notes:
        cells[("note", n["id"])] = (n["col"], n["row"])
    return cells


def apply_cells(positions, notes, cells):
    """Write a combined cell map (see combined_cells) back into the
    *positions* dict and the *notes* list (in place)."""
    for (kind, key), cell in cells.items():
        if kind == "file":
            positions[key] = (int(cell[0]), int(cell[1]))
        else:
            for n in notes:
                if n["id"] == key:
                    n["col"], n["row"] = int(cell[0]), int(cell[1])


def _cell_from_any(value, cell_w, cell_h):
    """Normalize a stored position to a (col, row) cell tuple.

    Accepts the v4 form {"col": c, "row": r} and the legacy v3 pixel
    form {"x": .., "y": ..} (converted with the given cell size).
    Returns None for unusable values.
    """
    if not isinstance(value, dict):
        return None
    if "col" in value and "row" in value:
        try:
            return (max(int(value["col"]), 0), max(int(value["row"]), 0))
        except Exception:
            return None
    if "x" in value and "y" in value:
        try:
            return pos_to_cell(int(value["x"]), int(value["y"]),
                               cell_w, cell_h)
        except Exception:
            return None
    return None


# ------------------------------------------------------- boards base / home
def old_base_dir():
    """Legacy (v3 and earlier) boards location: ~/Documents/OkibaBoards."""
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(docs, BASE_DIR_NAME)


def default_base_dir():
    """Where the boards live now: <SystemDrive>\\OkibaBoards.

    The OKIBA_BASE environment variable overrides everything (used by
    tests/CI so they never touch the real disk root or Documents).
    Off Windows (development only) falls back to ~/OkibaBoards.
    """
    override = os.environ.get(BASE_ENV_VAR)
    if override:
        return override
    if sys.platform == "win32":
        drive = os.environ.get("SystemDrive") or "C:"
        return os.path.join(drive + os.sep, BASE_DIR_NAME)
    return os.path.join(os.path.expanduser("~"), BASE_DIR_NAME)


def migrate_base(old_base, new_base):
    """Return the effective boards base, migrating *old_base* if needed.

    If *new_base* already exists it wins as-is. Otherwise, when the old
    base exists and holds boards, it is moved (shutil.move) to the new
    location. If the move fails, the OLD base is returned so nothing
    is ever lost.
    """
    if os.path.abspath(old_base) == os.path.abspath(new_base):
        return new_base
    if os.path.isdir(new_base):
        return new_base
    if os.path.isdir(old_base) and list_boards(old_base):
        try:
            shutil.move(old_base, new_base)
            return new_base
        except Exception:
            return old_base
    return new_base


def resolve_base():
    """Effective base for the app: migrate old -> new, then use it."""
    return migrate_base(old_base_dir(), default_base_dir())


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


def validate_board_name(name):
    """Return the trimmed board *name*, or raise ValueError.

    Nothing is silently rewritten: a name containing characters that
    cannot be used in a Windows folder name (\\ / : * ? " < > |) or
    ending with a dot is rejected with a message naming the problem,
    so the user can fix the name themselves.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("ボード名が空です")
    bad = sorted({ch for ch in cleaned if ch in _INVALID_CHARS})
    if bad:
        raise ValueError(
            "名前に使えない文字が含まれています: %s\n"
            "フォルダ名には \\ / : * ? \" < > | は使えません"
            % " ".join(bad))
    if cleaned.endswith("."):
        raise ValueError("ボード名の最後に「.」は使えません")
    return cleaned


def create_board(base, name):
    """Create a new board folder under *base*; return its final name.

    The board name IS the real folder name on disk.
    """
    cleaned = validate_board_name(name)
    dest = os.path.join(base, cleaned)
    if os.path.exists(dest):
        raise ValueError("同じ名前のボードがあります")
    os.makedirs(dest)
    return cleaned


def rename_board(base, old, new):
    """Rename board folder *old* to *new* under *base*; return new name."""
    cleaned = validate_board_name(new)
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
        base = resolve_base()
    os.makedirs(base, exist_ok=True)
    if not list_boards(base):
        os.makedirs(os.path.join(base, DEFAULT_BOARD_NAME), exist_ok=True)
    return base


def home_path(base):
    return os.path.join(base, HOME_FILENAME)


def load_home(base):
    """Load home data: {"positions": {name: (col, row)},
    "theme": <theme name or None>}. The theme is app-global."""
    path = home_path(base)
    positions = {}
    theme = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, value in data.get("positions", {}).items():
                cell = _cell_from_any(value, HOME_CELL_W, HOME_CELL_H)
                if cell is not None:
                    positions[name] = cell
            if isinstance(data.get("theme"), str):
                theme = data["theme"]
        except Exception:
            positions = {}
            theme = None
    return {"positions": positions, "theme": theme}


def save_home(base, positions, theme=None):
    """Save home icon cells (dict name -> (col, row)) under *base*.

    When *theme* is None, any theme already stored in the file is
    preserved, so position-only saves never lose the theme choice.
    """
    if theme is None:
        theme = load_home(base).get("theme")
    data = {"positions": {
        name: {"col": int(cell[0]), "row": int(cell[1])}
        for name, cell in positions.items()}}
    if theme is not None:
        data["theme"] = theme
    with open(home_path(base), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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


def save_board(folder, positions, notes, size_percent=DEFAULT_PERCENT):
    """Save card cells, notes and card size into *folder*'s board file.

    positions: dict name -> (col, row). notes: list of dicts with
    id/text/col/row. The size is stored as the integer percent
    "size_percent" (see PERCENT_CHOICES).
    """
    try:
        percent = int(size_percent)
    except Exception:
        percent = DEFAULT_PERCENT
    percent = max(10, min(percent, 200))
    data = {
        "version": 5,
        "size_percent": percent,
        "positions": {
            name: {"col": int(cell[0]), "row": int(cell[1])}
            for name, cell in positions.items()},
        "notes": [
            {"id": n["id"], "text": n["text"],
             "col": int(n["col"]), "row": int(n["row"])}
            for n in notes],
    }
    with open(board_path(folder), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def load_board(folder):
    """Load a board file, normalized to grid cells + percent size.

    Returns {"positions": {name: (col, row)}, "notes": [...],
    "size_percent": int}. A v5 "size_percent" wins; a v4 "size"
    （小/中/大) is migrated via LEGACY_SIZE_PERCENT. Legacy v3 pixel
    positions are converted to cells with the base (100%) cell size.
    """
    result = {"version": 5, "positions": {}, "notes": [],
              "size_percent": DEFAULT_PERCENT}
    path = board_path(folder)
    if not os.path.exists(path):
        return result
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return result
    if data.get("size_percent") is not None:
        try:
            result["size_percent"] = max(
                10, min(int(data["size_percent"]), 200))
        except Exception:
            pass
    elif data.get("size") in LEGACY_SIZE_PERCENT:
        result["size_percent"] = LEGACY_SIZE_PERCENT[data["size"]]
    # cell-stored positions do not depend on the display size; legacy
    # pixel positions are converted with the base (100%) cell metrics
    cell_w, cell_h = BASE_CELL_W, BASE_CELL_H
    for name, value in data.get("positions", {}).items():
        cell = _cell_from_any(value, cell_w, cell_h)
        if cell is not None:
            result["positions"][name] = cell
    for n in data.get("notes", []):
        try:
            cell = _cell_from_any(n, cell_w, cell_h)
            if cell is None:
                continue
            result["notes"].append({
                "id": n.get("id") or uuid.uuid4().hex[:10],
                "text": n.get("text", ""),
                "col": cell[0], "row": cell[1]})
        except Exception:
            continue
    return result


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


def reveal_in_explorer(path):
    """Show *path* in Explorer: file -> selected, folder -> opened.

    Windows only; returns True when the command was launched.
    """
    if sys.platform != "win32" or not path or not os.path.exists(path):
        return False
    try:
        import subprocess

        if os.path.isdir(path):
            subprocess.Popen(["explorer.exe", os.path.abspath(path)])
        else:
            subprocess.Popen('explorer.exe /select,"%s"'
                             % os.path.abspath(path))
        return True
    except Exception:
        return False


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


def desktop_dir():
    """The user's real Desktop folder (where デスクトップへ移動 sends
    items and where an OLE drag-out usually lands)."""
    return os.path.join(os.path.expanduser("~"), "Desktop")


# ------------------------------------------- OLE drag OUT (Windows only)
def _hdrop_payload(paths):
    """Bytes of a CF_HDROP payload (DROPFILES header + file list).

    Pure function (platform independent, unit-tested): a 20-byte
    DROPFILES header (pFiles=20, pt=(0,0), fNC=FALSE, fWide=TRUE)
    followed by the double-null-terminated UTF-16LE path list.
    """
    import struct

    names = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    return header + names.encode("utf-16-le")


def ole_drag_out(paths):
    """Start a native OLE drag of *paths*; return the final drop
    effect (2 = DROPEFFECT_MOVE, 1 = COPY, 0 = none/cancel/error).

    Implements IDataObject (CF_HDROP) + IDropSource in ctypes and
    calls ole32.DoDragDrop. Windows only - returns 0 elsewhere and on
    any failure, so callers can always fall back gracefully. Must be
    called while the left mouse button is physically held down (it
    is, from a drag-motion handler) on a thread with a message loop.
    """
    if sys.platform != "win32" or not paths:
        return 0
    try:
        return _ole_drag_out_win32([os.path.abspath(p) for p in paths])
    except Exception:
        return 0


def _ole_drag_out_win32(paths):
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.windll.ole32
    kernel32 = ctypes.windll.kernel32

    S_OK = 0
    E_NOTIMPL = -2147467263          # 0x80004001
    E_NOINTERFACE = -2147467262      # 0x80004002
    E_INVALIDARG = -2147024809       # 0x80070057
    DV_E_FORMATETC = -2147221404     # 0x80040064
    DRAGDROP_S_DROP = 0x00040100
    DRAGDROP_S_CANCEL = 0x00040101
    DRAGDROP_S_USEDEFAULTCURSORS = 0x00040102
    DROPEFFECT_COPY = 1
    DROPEFFECT_MOVE = 2
    CF_HDROP = 15
    TYMED_HGLOBAL = 0x0001
    DVASPECT_CONTENT = 1
    GMEM_MOVEABLE = 0x0002
    GMEM_ZEROINIT = 0x0040
    MK_LBUTTON = 0x0001
    DATADIR_GET = 1

    ole32.OleInitialize(None)

    def _iid_bytes(text):
        buf = ctypes.create_unicode_buffer(40)
        guid = ctypes.create_string_buffer(16)
        ole32.IIDFromString(text, guid)
        return guid.raw

    IID_IUNKNOWN = _iid_bytes("{00000000-0000-0000-C000-000000000046}")
    IID_IDATAOBJECT = _iid_bytes("{0000010E-0000-0000-C000-000000000046}")
    IID_IDROPSOURCE = _iid_bytes("{00000121-0000-0000-C000-000000000046}")

    class FORMATETC(ctypes.Structure):
        _fields_ = [("cfFormat", wintypes.WORD),
                    ("ptd", ctypes.c_void_p),
                    ("dwAspect", wintypes.DWORD),
                    ("lindex", ctypes.c_long),
                    ("tymed", wintypes.DWORD)]

    class STGMEDIUM(ctypes.Structure):
        _fields_ = [("tymed", wintypes.DWORD),
                    ("hGlobal", ctypes.c_void_p),
                    ("pUnkForRelease", ctypes.c_void_p)]

    HRESULT = ctypes.c_long
    LPTHIS = ctypes.c_void_p
    QI_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS, ctypes.c_void_p,
                              ctypes.c_void_p)
    REF_T = ctypes.WINFUNCTYPE(wintypes.ULONG, LPTHIS)
    GETDATA_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS,
                                   ctypes.POINTER(FORMATETC),
                                   ctypes.POINTER(STGMEDIUM))
    GETHERE_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS,
                                   ctypes.POINTER(FORMATETC),
                                   ctypes.POINTER(STGMEDIUM))
    QUERYGET_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS,
                                    ctypes.POINTER(FORMATETC))
    CANON_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS,
                                 ctypes.POINTER(FORMATETC),
                                 ctypes.POINTER(FORMATETC))
    SETDATA_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS,
                                   ctypes.POINTER(FORMATETC),
                                   ctypes.POINTER(STGMEDIUM),
                                   wintypes.BOOL)
    ENUMFMT_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS, wintypes.DWORD,
                                   ctypes.c_void_p)
    DADVISE_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS,
                                   ctypes.POINTER(FORMATETC),
                                   wintypes.DWORD, ctypes.c_void_p,
                                   ctypes.POINTER(wintypes.DWORD))
    DUNADVISE_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS, wintypes.DWORD)
    ENUMADV_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS, ctypes.c_void_p)
    QCONT_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS, wintypes.BOOL,
                                 wintypes.DWORD)
    FEEDBACK_T = ctypes.WINFUNCTYPE(HRESULT, LPTHIS, wintypes.DWORD)

    class IDataObjectVtbl(ctypes.Structure):
        _fields_ = [("QueryInterface", QI_T), ("AddRef", REF_T),
                    ("Release", REF_T), ("GetData", GETDATA_T),
                    ("GetDataHere", GETHERE_T),
                    ("QueryGetData", QUERYGET_T),
                    ("GetCanonicalFormatEtc", CANON_T),
                    ("SetData", SETDATA_T), ("EnumFormatEtc", ENUMFMT_T),
                    ("DAdvise", DADVISE_T), ("DUnadvise", DUNADVISE_T),
                    ("EnumDAdvise", ENUMADV_T)]

    class IDropSourceVtbl(ctypes.Structure):
        _fields_ = [("QueryInterface", QI_T), ("AddRef", REF_T),
                    ("Release", REF_T),
                    ("QueryContinueDrag", QCONT_T),
                    ("GiveFeedback", FEEDBACK_T)]

    class ComObject(ctypes.Structure):
        _fields_ = [("lpVtbl", ctypes.c_void_p)]

    payload = _hdrop_payload(paths)
    keepalive = []  # vtables, callback objects, COM objects

    def _make_qi(iids, refs):
        def qi(this, riid, ppv):
            if not ppv:
                return E_INVALIDARG
            out = ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))
            if ctypes.string_at(riid, 16) in iids:
                out[0] = this
                refs[0] += 1
                return S_OK
            out[0] = None
            return E_NOINTERFACE
        return QI_T(qi)

    def _make_refs(refs):
        def addref(this):
            refs[0] += 1
            return refs[0]

        def release(this):
            refs[0] -= 1
            return max(refs[0], 0)
        return REF_T(addref), REF_T(release)

    def _set_null(ppv):
        if ppv:
            ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))[0] = None

    # ---------------- IDataObject ----------------
    drefs = [1]

    def _format_ok(fmt):
        return (fmt and fmt.contents.cfFormat == CF_HDROP
                and fmt.contents.dwAspect == DVASPECT_CONTENT
                and (fmt.contents.tymed & TYMED_HGLOBAL))

    def get_data(this, fmt, medium):
        if not _format_ok(fmt):
            return DV_E_FORMATETC
        size = len(payload)
        hglob = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT,
                                     size)
        if not hglob:
            return -2147024882  # E_OUTOFMEMORY 0x8007000E
        locked = kernel32.GlobalLock(hglob)
        if not locked:
            kernel32.GlobalFree(hglob)
            return -2147024882
        ctypes.memmove(locked, payload, size)
        kernel32.GlobalUnlock(hglob)
        medium.contents.tymed = TYMED_HGLOBAL
        medium.contents.hGlobal = hglob
        medium.contents.pUnkForRelease = None
        return S_OK

    def get_data_here(this, fmt, medium):
        return E_NOTIMPL

    def query_get_data(this, fmt):
        return S_OK if _format_ok(fmt) else DV_E_FORMATETC

    def get_canon(this, fmt_in, fmt_out):
        # No canonical form distinct from the input: zero the output
        # FORMATETC and return DATA_S_SAMEFORMATETC (0x00040130).
        if fmt_out:
            ctypes.memset(fmt_out, 0, ctypes.sizeof(FORMATETC))
        return 0x00040130

    def set_data(this, fmt, medium, release):
        return E_NOTIMPL

    def enum_format(this, direction, ppenum):
        _set_null(ppenum)
        return E_NOTIMPL

    def d_advise(this, fmt, advf, sink, connection):
        if connection:
            connection[0] = 0
        return -2147221501  # OLE_E_ADVISENOTSUPPORTED (0x80040003)

    def d_unadvise(this, connection):
        return E_NOTIMPL

    def enum_d_advise(this, ppenum):
        _set_null(ppenum)
        return E_NOTIMPL

    d_vtbl = IDataObjectVtbl()
    d_addref, d_release = _make_refs(drefs)
    d_vtbl.QueryInterface = _make_qi((IID_IUNKNOWN, IID_IDATAOBJECT),
                                     drefs)
    d_vtbl.AddRef = d_addref
    d_vtbl.Release = d_release
    d_vtbl.GetData = GETDATA_T(get_data)
    d_vtbl.GetDataHere = GETHERE_T(get_data_here)
    d_vtbl.QueryGetData = QUERYGET_T(query_get_data)
    d_vtbl.GetCanonicalFormatEtc = CANON_T(get_canon)
    d_vtbl.SetData = SETDATA_T(set_data)
    d_vtbl.EnumFormatEtc = ENUMFMT_T(enum_format)
    d_vtbl.DAdvise = DADVISE_T(d_advise)
    d_vtbl.DUnadvise = DUNADVISE_T(d_unadvise)
    d_vtbl.EnumDAdvise = ENUMADV_T(enum_d_advise)
    data_obj = ComObject()
    data_obj.lpVtbl = ctypes.addressof(d_vtbl)
    keepalive += [d_vtbl, data_obj]

    # ---------------- IDropSource ----------------
    srefs = [1]

    def query_continue(this, escape_pressed, key_state):
        if escape_pressed:
            return DRAGDROP_S_CANCEL
        if not (key_state & MK_LBUTTON):
            return DRAGDROP_S_DROP
        return S_OK

    def give_feedback(this, effect):
        return DRAGDROP_S_USEDEFAULTCURSORS

    s_vtbl = IDropSourceVtbl()
    s_addref, s_release = _make_refs(srefs)
    s_vtbl.QueryInterface = _make_qi((IID_IUNKNOWN, IID_IDROPSOURCE),
                                     srefs)
    s_vtbl.AddRef = s_addref
    s_vtbl.Release = s_release
    s_vtbl.QueryContinueDrag = QCONT_T(query_continue)
    s_vtbl.GiveFeedback = FEEDBACK_T(give_feedback)
    src_obj = ComObject()
    src_obj.lpVtbl = ctypes.addressof(s_vtbl)
    keepalive += [s_vtbl, src_obj]

    ole32.DoDragDrop.restype = HRESULT
    ole32.DoDragDrop.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD)]
    effect = wintypes.DWORD(0)
    hr = ole32.DoDragDrop(ctypes.addressof(data_obj),
                          ctypes.addressof(src_obj),
                          DROPEFFECT_COPY | DROPEFFECT_MOVE,
                          ctypes.byref(effect))
    del keepalive
    if hr in (S_OK, DRAGDROP_S_DROP):
        return int(effect.value)
    return 0


# ------------------------------------------------------------------ self-test
def run_self_test():
    """Exercise base/migration, grid math, boards, save/load, intake."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # ---------- base resolution + migration ----------
            old_base = os.path.join(tmp, "Documents", "OkibaBoards")
            os.makedirs(os.path.join(old_base, "昔のボード"))
            new_base = os.path.join(tmp, "C", "OkibaBoards")

            # env override wins for default_base_dir()
            os.environ[BASE_ENV_VAR] = new_base
            try:
                assert default_base_dir() == new_base
            finally:
                del os.environ[BASE_ENV_VAR]

            # new missing + old has boards -> moved to new
            eff = migrate_base(old_base, new_base)
            assert eff == new_base, eff
            assert not os.path.exists(old_base)
            assert list_boards(new_base) == ["昔のボード"]

            # new exists -> new wins, old left untouched
            old2 = os.path.join(tmp, "old2")
            os.makedirs(os.path.join(old2, "残るボード"))
            eff2 = migrate_base(old2, new_base)
            assert eff2 == new_base
            assert list_boards(old2) == ["残るボード"]

            # move failure -> fall back to old, nothing lost
            blocker = os.path.join(tmp, "blocker")
            with open(blocker, "w", encoding="utf-8") as f:
                f.write("not a folder")
            bad_new = os.path.join(blocker, "OkibaBoards")
            old3 = os.path.join(tmp, "old3")
            os.makedirs(os.path.join(old3, "大事なボード"))
            eff3 = migrate_base(old3, bad_new)
            assert eff3 == old3, eff3
            assert list_boards(old3) == ["大事なボード"]

            # ---------- boards base & home layout ----------
            base = os.path.join(tmp, "boards")
            ensure_base(base)
            assert list_boards(base) == [DEFAULT_BOARD_NAME]

            # free choice of board name (name = real folder name)
            created = create_board(base, "私の作業ボード")
            assert created == "私の作業ボード"
            assert os.path.isdir(os.path.join(base, "私の作業ボード"))
            try:
                create_board(base, "私の作業ボード")
                raise AssertionError("duplicate board must raise")
            except ValueError:
                pass
            # unusable characters must NOT be silently replaced:
            # creation is refused and nothing is made
            for bad_name in ["a/b", "a:b", "a*b?", 'a"b', "a<b>|"]:
                try:
                    create_board(base, bad_name)
                    raise AssertionError("bad name must raise: %r"
                                         % bad_name)
                except ValueError as exc:
                    assert "使えない文字" in str(exc), str(exc)
                assert not os.path.exists(os.path.join(base, bad_name))
            try:
                create_board(base, "   ")
                raise AssertionError("empty name must raise")
            except ValueError:
                pass
            other = create_board(base, "作業メモ")
            assert other == "作業メモ"
            # rename to any free name works and renames the real folder
            renamed = rename_board(base, "私の作業ボード", "素材 置き場")
            assert renamed == "素材 置き場"
            assert os.path.isdir(os.path.join(base, "素材 置き場"))
            assert not os.path.exists(os.path.join(base, "私の作業ボード"))
            try:
                rename_board(base, "素材 置き場", "作業メモ")
                raise AssertionError("rename onto existing must raise")
            except ValueError:
                pass
            # rename with unusable characters is refused, name unchanged
            try:
                rename_board(base, "素材 置き場", "だめ/名前")
                raise AssertionError("bad rename must raise")
            except ValueError as exc:
                assert "使えない文字" in str(exc), str(exc)
            assert os.path.isdir(os.path.join(base, "素材 置き場"))

            save_home(base, {DEFAULT_BOARD_NAME: (2, 1)})
            home = load_home(base)
            assert home["positions"][DEFAULT_BOARD_NAME] == (2, 1)
            assert HOME_FILENAME not in list_boards(base)

            # ---------- themes: definitions valid + dark ----------
            assert THEME_ORDER == list(THEMES.keys()), THEME_ORDER
            for tname, theme in THEMES.items():
                assert set(theme.keys()) == THEME_KEYS, tname
                for key in ("bg_top", "bg_bottom"):
                    rgb = theme[key]
                    assert len(rgb) == 3, (tname, key)
                    assert all(isinstance(v, int) and 0 <= v <= 110
                               for v in rgb), (tname, key, rgb)
                    assert sum(rgb) < 220, (tname, key, rgb)
                for key in THEME_KEYS - {"bg_top", "bg_bottom"}:
                    value = theme[key]
                    assert isinstance(value, str) \
                        and value.startswith("#") \
                        and len(value) == 7, (tname, key, value)
                    int(value[1:], 16)  # must parse as hex colour
            # cycling walks every theme once and wraps to the start
            seen = [DEFAULT_THEME]
            for _ in range(len(THEME_ORDER)):
                seen.append(next_theme(seen[-1]))
            assert seen[1:] == THEME_ORDER[1:] + [THEME_ORDER[0]], seen
            assert valid_theme("存在しないテーマ") == DEFAULT_THEME
            assert valid_theme("深緑") == "深緑"

            # ---------- themes: save / restore via .home.json ------
            save_home(base, {DEFAULT_BOARD_NAME: (2, 1)}, theme="紫夜")
            home2 = load_home(base)
            assert home2["theme"] == "紫夜", home2
            assert home2["positions"][DEFAULT_BOARD_NAME] == (2, 1)
            # a position-only save must preserve the stored theme
            save_home(base, {DEFAULT_BOARD_NAME: (0, 0)})
            home3 = load_home(base)
            assert home3["theme"] == "紫夜", home3
            assert home3["positions"][DEFAULT_BOARD_NAME] == (0, 0)
            save_home(base, {DEFAULT_BOARD_NAME: (2, 1)},
                      theme=DEFAULT_THEME)

            # ---------- grid math ----------
            m = size_metrics(100)  # base （旧「中」) metrics
            # cell -> pos -> cell roundtrip
            for cell in [(0, 0), (1, 0), (0, 3), (4, 2)]:
                x, y = cell_to_pos(cell[0], cell[1],
                                   m["cell_w"], m["cell_h"])
                assert pos_to_cell(x, y, m["cell_w"], m["cell_h"]) == cell
            # a pixel near a cell corner snaps back to that cell
            x, y = cell_to_pos(2, 1, m["cell_w"], m["cell_h"])
            assert pos_to_cell(x + 40, y + 30,
                               m["cell_w"], m["cell_h"]) == (2, 1)
            # a pixel past the half-way point snaps to the next cell
            assert pos_to_cell(x + m["cell_w"] - 10, y,
                               m["cell_w"], m["cell_h"]) == (3, 1)
            # never negative
            assert pos_to_cell(-50, -50, m["cell_w"], m["cell_h"]) == (0, 0)

            # vertical-column auto-fill: columns fill top-to-bottom
            rpc = 3
            occ = set()
            order = []
            for _ in range(5):
                c = next_free_cell(occ, rpc)
                order.append(c)
                occ.add(c)
            assert order == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1)], order
            # starting the search at a given cell
            assert next_free_cell({(0, 0)}, rpc, start=(0, 1)) == (0, 1)
            assert next_free_cell({(0, 0), (0, 1)}, rpc,
                                  start=(0, 1)) == (0, 2)

            # swap at the data level
            cells = {"a.txt": (0, 0), "b.txt": (0, 1), "c.txt": (1, 0)}
            swapped = move_to_cell(cells, "a.txt", (0, 1))
            assert swapped == "b.txt"
            assert cells["a.txt"] == (0, 1) and cells["b.txt"] == (0, 0)
            assert cells["c.txt"] == (1, 0)
            # moving onto a free cell swaps nothing
            assert move_to_cell(cells, "c.txt", (3, 2)) is None
            assert cells["c.txt"] == (3, 2)
            # moving onto its own cell is a no-op
            assert move_to_cell(cells, "c.txt", (3, 2)) is None

            # ---------- board data (scan + save/load + size) ----------
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

            positions = {"alpha.txt": (2, 1)}
            notes = [{"id": "test1", "text": "sample note",
                      "col": 0, "row": 2}]
            save_board(board, positions, notes, size_percent=125)
            names_after = [e["name"] for e in scan_folder(board)]
            assert BOARD_FILENAME not in names_after, names_after

            loaded = load_board(board)
            assert loaded["size_percent"] == 125, loaded["size_percent"]
            assert loaded["positions"]["alpha.txt"] == (2, 1)
            assert loaded["notes"][0]["text"] == "sample note"
            assert (loaded["notes"][0]["col"],
                    loaded["notes"][0]["row"]) == (0, 2)

            # percent round-trips, incl. the smallest choices the
            # user explicitly asked for (25% / 30%)
            for pct in PERCENT_CHOICES:
                save_board(board, {}, [], size_percent=pct)
                assert load_board(board)["size_percent"] == pct, pct
            # default percent when the json has none
            save_board(board, {}, [])
            assert load_board(board)["size_percent"] == DEFAULT_PERCENT

            # ---- percent metrics: smaller percents are smaller,
            # font never below the 6pt floor, cells stay consistent
            seq = [size_metrics(p) for p in PERCENT_CHOICES]
            for smaller, bigger in zip(seq, seq[1:]):
                assert smaller["card_w"] < bigger["card_w"]
                assert smaller["card_h"] < bigger["card_h"]
            assert size_metrics(100)["card_w"] == BASE_CARD_W
            assert size_metrics(100)["card_h"] == BASE_CARD_H
            assert size_metrics(25)["font"] == MIN_FONT == 6
            assert size_metrics(50)["font"] >= MIN_FONT
            assert size_metrics(125)["card_w"] > size_metrics(100)["card_w"]
            # cycling walks the whole choice list and wraps around
            walk = [DEFAULT_PERCENT]
            for _ in range(len(PERCENT_CHOICES)):
                walk.append(next_percent(walk[-1]))
            assert walk[1:-1] == [125, 25, 30, 40, 50, 60, 70, 80, 90], \
                walk
            assert walk[-1] == 100

            # ---- v4 size migration: 小/中/大 -> percent on load
            for legacy_size, expect in [("小", 70), ("中", 100),
                                        ("大", 125)]:
                with open(board_path(board), "w", encoding="utf-8") as f:
                    json.dump({"version": 4, "size": legacy_size,
                               "positions": {}, "notes": []}, f)
                got = load_board(board)["size_percent"]
                assert got == expect, (legacy_size, got)
            # an explicit v5 size_percent beats a stale v4 "size"
            with open(board_path(board), "w", encoding="utf-8") as f:
                json.dump({"version": 5, "size_percent": 40,
                           "size": "大", "positions": {}, "notes": []}, f)
            assert load_board(board)["size_percent"] == 40

            # legacy v3 pixel positions convert to base (100%) cells
            legacy = {"version": 3,
                      "positions": {"alpha.txt": {"x": 350, "y": 120}},
                      "notes": [{"id": "n1", "text": "old", "x": 24,
                                 "y": 24}]}
            with open(board_path(board), "w", encoding="utf-8") as f:
                json.dump(legacy, f, ensure_ascii=False)
            conv = load_board(board)
            assert conv["positions"]["alpha.txt"] == (2, 1), \
                conv["positions"]
            assert (conv["notes"][0]["col"],
                    conv["notes"][0]["row"]) == (0, 0)

            # ---------- drag data flow (what the GUI drag does) ----
            # files and notes share ONE cell map; moving a file card
            # onto a note's cell swaps them, moving to an empty cell
            # just moves, and a save/load round-trip preserves it all
            dpos = {"a.txt": (0, 0), "b.txt": (0, 1)}
            dnotes = [{"id": "n1", "text": "memo", "col": 1, "row": 0}]
            cells = combined_cells(dpos, dnotes)
            swapped = move_to_cell(cells, ("file", "a.txt"), (1, 0))
            assert swapped == ("note", "n1"), swapped
            apply_cells(dpos, dnotes, cells)
            assert dpos["a.txt"] == (1, 0)
            assert (dnotes[0]["col"], dnotes[0]["row"]) == (0, 0)
            assert dpos["b.txt"] == (0, 1)
            cells = combined_cells(dpos, dnotes)
            assert move_to_cell(cells, ("file", "b.txt"), (3, 2)) is None
            apply_cells(dpos, dnotes, cells)
            save_board(board, dpos, dnotes, size_percent=80)
            rt = load_board(board)
            assert rt["positions"] == {"a.txt": (1, 0),
                                       "b.txt": (3, 2)}, rt["positions"]
            assert (rt["notes"][0]["col"], rt["notes"][0]["row"]) == (0, 0)
            assert rt["size_percent"] == 80

            # ---------- OLE drag-out payload (pure part) -------------
            payload = _hdrop_payload([os.path.join(tmp, "x.txt"),
                                      os.path.join(tmp, "y.txt")])
            import struct as _struct
            p_files, _px, _py, _fnc, f_wide = _struct.unpack(
                "<IiiII", payload[:20])
            assert p_files == 20 and f_wide == 1
            listed = payload[20:].decode("utf-16-le").split("\0")
            assert listed[0].endswith("x.txt"), listed
            assert listed[1].endswith("y.txt"), listed
            assert listed[2] == "" and listed[3] == ""
            # the OLE entry point must refuse safely off Windows
            if sys.platform != "win32":
                assert ole_drag_out([os.path.join(tmp, "x.txt")]) == 0

            # ---------- move OUT of a board (desktop fallback) ------
            desk = os.path.join(tmp, "Desktop")
            os.mkdir(desk)
            out_board = os.path.join(tmp, "outboard")
            os.mkdir(out_board)
            out_src = os.path.join(out_board, "dropped.txt")
            with open(out_src, "w", encoding="utf-8") as f:
                f.write("move me out")
            moved_out = place_dropped_items([out_src], desk)
            assert len(moved_out) == 1, moved_out
            assert os.path.exists(os.path.join(desk, "dropped.txt"))
            assert not os.path.exists(out_src), \
                "moving out must remove it from the board folder"
            assert [e["name"] for e in scan_folder(out_board)] == []

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

            # recycle / reveal helpers must refuse safely off Windows
            if sys.platform != "win32":
                assert recycle_bin_delete(
                    [os.path.join(board_dir, "dropped.txt")]) is False
                assert os.path.exists(os.path.join(board_dir, "dropped.txt"))
                assert reveal_in_explorer(
                    os.path.join(board_dir, "dropped.txt")) is False

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
def draw_gradient(canvas, theme=None):
    """Redraw the refined dark background of *canvas* for *theme*.

    A smoother dark gradient, one very soft glow in the top-left
    corner, and only a few faint dots (kept subtle).
    """
    t = THEMES[valid_theme(theme)] if not isinstance(theme, dict) \
        else theme
    top, bottom = t["bg_top"], t["bg_bottom"]
    canvas.delete("bg")
    w = max(canvas.winfo_width(), 2)
    h = max(canvas.winfo_height(), 2)
    steps = 72
    for i in range(steps):
        r = int(top[0] + (bottom[0] - top[0]) * i / steps)
        g = int(top[1] + (bottom[1] - top[1]) * i / steps)
        b = int(top[2] + (bottom[2] - top[2]) * i / steps)
        y0 = int(h * i / steps)
        y1 = int(h * (i + 1) / steps) + 1
        canvas.create_rectangle(0, y0, w, y1,
                                fill="#%02x%02x%02x" % (r, g, b),
                                outline="", tags="bg")
    # soft glow, top-left (flat colour slightly above the gradient)
    glow_r = min(w, h) // 2
    if glow_r > 60:
        canvas.create_oval(-glow_r // 2, -glow_r // 2,
                           glow_r, glow_r // 2,
                           fill=t["glow"], outline="", tags="bg")
    # faint sparse dots
    for x in range(60, w, 140):
        for y in range(60, h, 140):
            canvas.create_oval(x, y, x + 2, y + 2, fill=t["dot"],
                               outline="", tags="bg")
    canvas.tag_lower("bg")


def rounded_rect_points(x, y, x2, y2, r):
    """Polygon points for a rectangle with rounded corners (radius r)."""
    import math

    r = max(0, min(r, (x2 - x) // 2, (y2 - y) // 2))
    pts = []
    for cx, cy, start in ((x2 - r, y + r, -90), (x2 - r, y2 - r, 0),
                          (x + r, y2 - r, 90), (x + r, y + r, 180)):
        for deg in range(start, start + 91, 30):
            rad = math.radians(deg)
            pts += [cx + r * math.cos(rad), cy + r * math.sin(rad)]
    return pts


# file kind by extension -> icon + colour family (board cards)
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv",
              ".m4v", ".mpg", ".mpeg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
              ".tif", ".tiff", ".ico"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"}

KIND_STYLE = {
    # kind: (icon, card fill, outline, brighter outline for hover)
    "dir":   ("📁", "#38300f", "#a8892f", "#d9b64a"),
    "video": ("🎬", "#2a1e40", "#7a5fc0", "#a68ae0"),
    "image": ("🖼", "#173324", "#4f9e63", "#74c489"),
    "audio": ("🎵", "#381a2b", "#c05f8f", "#de8ab2"),
    "note":  ("📝", "#382a16", "#c08b4f", "#dda96e"),
    "file":  ("📄", "#18264a", "#54679f", "#7d92c4"),
}
SHADOW_FILL = "#03050c"

# ------------------------------------------------------------ themes
# All themes are DARK ("明るすぎない") by design. One theme applies to
# the whole app (home + every board) and is stored in .home.json.
# Each theme recolours, as one set: the background gradient + glow +
# dots, the toolbar, the buttons, the labels, the card shadow and
# the generic-file card frame (accent). Kind colours (folder/video/
# image/audio/note) stay recognisable across themes.
THEME_ORDER = ["紺", "黒グレー", "深緑", "紫夜", "焦げ茶"]
DEFAULT_THEME = "紺"
THEME_KEYS = {"bg_top", "bg_bottom", "glow", "dot", "bar", "bar_fg",
              "bar_dim", "btn_bg", "btn_active", "btn_fg", "accent",
              "accent_hover", "shadow"}
THEMES = {
    "紺": {
        "bg_top": (5, 8, 20), "bg_bottom": (13, 21, 48),
        "glow": "#0d1734", "dot": "#1b2749",
        "bar": "#0d1430", "bar_fg": "#aebadd", "bar_dim": "#7e8bb5",
        "btn_bg": "#22315e", "btn_active": "#2e4176",
        "btn_fg": "#ffffff",
        "accent": "#54679f", "accent_hover": "#7d92c4",
        "shadow": "#03050c",
    },
    "黒グレー": {
        "bg_top": (7, 7, 9), "bg_bottom": (24, 24, 28),
        "glow": "#16161c", "dot": "#232329",
        "bar": "#101013", "bar_fg": "#b9bcc4", "bar_dim": "#83868f",
        "btn_bg": "#2a2a31", "btn_active": "#3a3a43",
        "btn_fg": "#ffffff",
        "accent": "#6d7280", "accent_hover": "#9aa0ad",
        "shadow": "#020203",
    },
    "深緑": {
        "bg_top": (4, 13, 9), "bg_bottom": (9, 30, 20),
        "glow": "#0a2015", "dot": "#14301f",
        "bar": "#08170f", "bar_fg": "#a9c6b2", "bar_dim": "#75917e",
        "btn_bg": "#173d28", "btn_active": "#205135",
        "btn_fg": "#ffffff",
        "accent": "#4f8f66", "accent_hover": "#79b48c",
        "shadow": "#020604",
    },
    "紫夜": {
        "bg_top": (9, 6, 18), "bg_bottom": (22, 13, 42),
        "glow": "#180f31", "dot": "#271a47",
        "bar": "#120b24", "bar_fg": "#bcb0d8", "bar_dim": "#8a7cae",
        "btn_bg": "#33205c", "btn_active": "#452c7a",
        "btn_fg": "#ffffff",
        "accent": "#7d63b8", "accent_hover": "#a68ad9",
        "shadow": "#040209",
    },
    "焦げ茶": {
        "bg_top": (15, 8, 7), "bg_bottom": (33, 17, 15),
        "glow": "#241110", "dot": "#331b16",
        "bar": "#1c0e0c", "bar_fg": "#d3bda9", "bar_dim": "#a0806d",
        "btn_bg": "#4a2a1e", "btn_active": "#5f3727",
        "btn_fg": "#ffffff",
        "accent": "#b07a4f", "accent_hover": "#cf9c72",
        "shadow": "#070302",
    },
}


def valid_theme(name):
    """*name* if it is a known theme, else the default theme."""
    return name if name in THEMES else DEFAULT_THEME


def next_theme(name):
    """The theme after *name* in THEME_ORDER (cycles)."""
    name = valid_theme(name)
    idx = THEME_ORDER.index(name)
    return THEME_ORDER[(idx + 1) % len(THEME_ORDER)]


def kind_of_file(name, is_dir):
    """Card kind for a real file/folder name (drives icon + colour)."""
    if is_dir:
        return "dir"
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return "file"


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog

    # Pillow is OPTIONAL (image thumbnails only): without it, image
    # files simply show the usual icon card.
    try:
        from PIL import Image as PILImage
        from PIL import ImageTk as PILImageTk
    except Exception:
        PILImage = PILImageTk = None

    class App:
        def __init__(self, root):
            self.root = root
            root.title("OkibaBoard")
            root.geometry("1120x720")
            root.configure(bg="#070b18")

            self.base = ensure_base()
            self.view = "home"

            # ---- home state: positions = {board name: (col, row)} ----
            _home_data = load_home(self.base)
            self.home_positions = _home_data.get("positions", {})
            # one app-global theme (home + all boards), persisted in
            # .home.json; applied before any widget is built
            self.theme_name = valid_theme(_home_data.get("theme"))
            self._buttons = []       # all toolbar buttons (theming)
            self._bars = []          # all toolbar frames (theming)
            self._frames = []        # home/board root frames (theming)
            self.home_items = {}   # canvas tag -> board name
            self.home_shapes = {}  # canvas tag -> (shape item, outline)
            self.home_drag = {"tag": None, "dx": 0, "dy": 0,
                              "x": 0, "y": 0}
            self.home_rpc = rows_per_column(640, HOME_CELL_H)

            # ---- board state ----
            self.board_folder = None
            self.board_name = ""
            self.board_percent = DEFAULT_PERCENT
            self.positions = {}    # file/folder name -> (col, row)
            self.notes = []        # {id, text, col, row}
            self.items = {}        # canvas tag -> info dict (has x, y px)
            self.card_shapes = {}  # canvas tag -> (shape item, outline)
            self.thumb_cache = {}  # (path, percent) -> PhotoImage
            self.known_names = set()
            self.drag = {"tag": None, "dx": 0, "dy": 0}
            self.ole_active = False   # True while DoDragDrop runs
            self._last_focus_rescan = 0
            self.board_rpc = rows_per_column(
                640, size_metrics(DEFAULT_PERCENT)["cell_h"])

            # ---- native drop ----
            self.drop = NativeDrop(self.on_native_drop)
            self.drop_setup_done = False

            root.configure(bg=self._canvas_bg())
            self._build_home()
            self._build_board()
            self._sync_theme_buttons()
            self.show_home()
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            # coming back to this window re-scans the board folder at
            # once, so files moved in/out with Explorer while the app
            # was in the background are reflected immediately, not
            # only at the next 2s poll
            self.root.bind_all("<FocusIn>", self._on_focus_in)
            self.root.after(60, self.setup_drop)
            self.root.after(POLL_MS, self.poll_folder)

        def _on_focus_in(self, event):
            import time

            now = time.monotonic()
            if now - self._last_focus_rescan < 0.3:
                return  # FocusIn fires in bursts; coalesce them
            self._last_focus_rescan = now
            self.root.after(30, self._rescan_board)

        # ------------------------------------------------------------ helpers
        @property
        def metrics(self):
            return size_metrics(self.board_percent)

        @property
        def theme(self):
            return THEMES[self.theme_name]

        def _canvas_bg(self):
            r, g, b = self.theme["bg_bottom"]
            return "#%02x%02x%02x" % (r, g, b)

        def _save_home(self):
            save_home(self.base, self.home_positions,
                      theme=self.theme_name)

        def _make_button(self, parent, text, command):
            t = self.theme
            btn = tk.Button(
                parent, text=text, command=command,
                bg=t["btn_bg"], fg=t["btn_fg"],
                activebackground=t["btn_active"],
                activeforeground=t["btn_fg"],
                relief="flat", padx=12, pady=6,
                font=("Yu Gothic UI", 10))
            btn.pack(side="left", padx=8, pady=8)
            self._buttons.append(btn)
            return btn

        # ------------------------------------------------------------ themes
        def cycle_theme(self):
            self.theme_name = next_theme(self.theme_name)
            self.apply_theme()
            self._save_home()

        def _sync_theme_buttons(self):
            label = "テーマ: %s" % self.theme_name
            for btn in (self.home_theme_btn, self.board_theme_btn):
                try:
                    btn.config(text=label)
                except Exception:
                    pass

        def apply_theme(self):
            t = self.theme
            bg = self._canvas_bg()
            try:
                self.root.configure(bg=bg)
            except Exception:
                pass
            for frame in self._frames:
                frame.configure(bg=bg)
            for bar in self._bars:
                bar.configure(bg=t["bar"])
            for btn in self._buttons:
                btn.configure(bg=t["btn_bg"], fg=t["btn_fg"],
                              activebackground=t["btn_active"],
                              activeforeground=t["btn_fg"])
            self.home_hint_label.configure(bg=t["bar"],
                                           fg=t["bar_fg"])
            self.home_base_label.configure(bg=t["bar"],
                                           fg=t["bar_dim"])
            self.board_label.configure(bg=t["bar"], fg=t["bar_fg"])
            self.home_canvas.configure(bg=bg)
            self.canvas.configure(bg=bg)
            self._sync_theme_buttons()
            draw_gradient(self.home_canvas, t)
            draw_gradient(self.canvas, t)
            # re-render the visible screen so card frames / board
            # icons pick up the new accent + shadow colours
            if self.view == "home":
                self.render_home()
            else:
                self.render_board()

        def _build_home(self):
            t = self.theme
            frame = tk.Frame(self.root, bg=self._canvas_bg())
            self.home_frame = frame
            self._frames.append(frame)
            bar = tk.Frame(frame, bg=t["bar"])
            bar.pack(side="top", fill="x")
            self._bars.append(bar)
            self._make_button(bar, "＋ボード", self.add_board)
            self.home_theme_btn = self._make_button(
                bar, "テーマ: %s" % self.theme_name, self.cycle_theme)
            self.home_hint_label = tk.Label(
                bar, text="ボードをダブルクリックで開きます",
                bg=t["bar"], fg=t["bar_fg"],
                font=("Yu Gothic UI", 10))
            self.home_hint_label.pack(side="left", padx=12)
            self.home_base_label = tk.Label(
                bar, text="保存場所: %s" % self.base,
                bg=t["bar"], fg=t["bar_dim"],
                font=("Yu Gothic UI", 9))
            self.home_base_label.pack(side="right", padx=12)
            self.home_canvas = tk.Canvas(frame, bg=self._canvas_bg(),
                                         highlightthickness=0)
            self.home_canvas.pack(side="top", fill="both", expand=True)
            self.home_canvas.bind("<Configure>", self._home_configure)
            # widget-level drag bindings: a drag keeps working (and
            # can finish) even when the pointer leaves the icon itself
            self.home_canvas.bind("<B1-Motion>", self.home_move)
            self.home_canvas.bind("<ButtonRelease-1>", self.home_release)

        def _home_configure(self, event):
            draw_gradient(self.home_canvas, self.theme)
            self.home_rpc = rows_per_column(event.height, HOME_CELL_H)

        def _build_board(self):
            t = self.theme
            frame = tk.Frame(self.root, bg=self._canvas_bg())
            self.board_frame = frame
            self._frames.append(frame)
            bar = tk.Frame(frame, bg=t["bar"])
            bar.pack(side="top", fill="x")
            self._bars.append(bar)
            self._make_button(bar, "← もどる", self.show_home)
            self._make_button(bar, "＋メモ", self.add_note)
            self._make_button(bar, "＋ファイル", self.add_files)
            self.size_button = self._make_button(
                bar, "大きさ: %d%%" % self.board_percent,
                self.cycle_percent)
            self.board_theme_btn = self._make_button(
                bar, "テーマ: %s" % self.theme_name, self.cycle_theme)
            self.board_label = tk.Label(
                bar, text="", bg=t["bar"], fg=t["bar_fg"],
                font=("Yu Gothic UI", 10, "bold"))
            self.board_label.pack(side="left", padx=12)
            self.canvas = tk.Canvas(frame, bg=self._canvas_bg(),
                                    highlightthickness=0)
            self.canvas.pack(side="top", fill="both", expand=True)
            self.canvas.bind("<Configure>", self._board_configure)
            # widget-level drag bindings (see home canvas): motion /
            # release keep working when the pointer leaves the card
            # or even the window (a grab is held during drags)
            self.canvas.bind("<B1-Motion>", self.on_move)
            self.canvas.bind("<ButtonRelease-1>", self.on_release)

        def _board_configure(self, event):
            draw_gradient(self.canvas, self.theme)
            self.board_rpc = rows_per_column(event.height,
                                             self.metrics["cell_h"])

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
                self._save_home()
            except Exception:
                pass
            self.root.destroy()

        def save_board_data(self):
            if self.board_folder:
                try:
                    save_board(self.board_folder, self.positions,
                               self.notes,
                               size_percent=self.board_percent)
                except Exception:
                    pass

        def cycle_percent(self):
            self.board_percent = next_percent(self.board_percent)
            self.size_button.config(
                text="大きさ: %d%%" % self.board_percent)
            self.board_rpc = rows_per_column(
                max(self.canvas.winfo_height(), 640),
                self.metrics["cell_h"])
            self.render_board()

        # -------------------------------------------------------- drop setup
        def setup_drop(self):
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
            if self.drop.active or sys.platform != "win32":
                self.drop_setup_done = True

        def on_native_drop(self, paths, x, y, hwnd):
            if self.view != "board" or not self.board_folder:
                return  # drops on the home screen are ignored
            if getattr(self, "root_hwnd", None) is not None \
                    and hwnd == self.root_hwnd:
                x -= self.canvas.winfo_x()
                y -= self.canvas.winfo_y()
            self.intake_files(paths, drop_xy=(max(int(x), 0),
                                              max(int(y), 0)))

        def intake_files(self, paths, drop_xy=None):
            """Shared intake for native drop and the ＋ファイル button."""
            if not self.board_folder:
                return
            try:
                placed = place_dropped_items(list(paths), self.board_folder)
            except Exception as exc:
                self.update_board_hint("追加できませんでした: %s" % exc)
                return
            if not placed:
                self.update_board_hint("追加できるものがありませんでした")
                return
            m = self.metrics
            if drop_xy is not None:
                start = pos_to_cell(drop_xy[0], drop_xy[1],
                                    m["cell_w"], m["cell_h"])
            else:
                start = None
            occupied = self._occupied_cells()
            for p in placed:
                cell = next_free_cell(occupied, self.board_rpc,
                                      start=start)
                self.positions[os.path.basename(p)] = cell
                occupied.add(cell)
                start = cell
            self.render_board()
            self.update_board_hint("%d 件を追加しました" % len(placed))

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

        # ------------------------------------------------------- grid helpers
        def _occupied_cells(self):
            occ = set(self.positions.values())
            occ |= {(n["col"], n["row"]) for n in self.notes}
            return occ

        def _combined_cells(self):
            return combined_cells(self.positions, self.notes)

        def _apply_cells(self, cells):
            apply_cells(self.positions, self.notes, cells)

        # ------------------------------------------------------------ home
        def render_home(self):
            if self.home_drag.get("tag"):
                return  # never re-render under an active home drag
            c = self.home_canvas
            for tag in list(self.home_items):
                c.delete(tag)
            self.home_items = {}
            self.home_shapes = {}
            boards = list_boards(self.base)
            for gone in [n for n in self.home_positions if n not in boards]:
                del self.home_positions[gone]
            occupied = set(self.home_positions.values())
            for name in boards:
                cell = self.home_positions.get(name)
                if cell is None:
                    cell = next_free_cell(occupied, self.home_rpc)
                    self.home_positions[name] = cell
                    occupied.add(cell)
                x, y = cell_to_pos(cell[0], cell[1],
                                   HOME_CELL_W, HOME_CELL_H)
                self.make_board_icon(name, x, y)
            self._save_home()

        def make_board_icon(self, name, x, y):
            c = self.home_canvas
            tag = "board_" + uuid.uuid4().hex[:10]
            icon, fill, _of, _oh = KIND_STYLE["dir"]
            outline = self.theme["accent"]
            # soft shadow, then a rounded folder-coloured body
            c.create_polygon(
                rounded_rect_points(x + 4, y + 5, x + HOME_ICON_W + 4,
                                    y + HOME_ICON_H + 5, 12),
                fill=self.theme["shadow"], outline="",
                tags=(tag, "bicon"))
            shape = c.create_polygon(
                rounded_rect_points(x, y, x + HOME_ICON_W,
                                    y + HOME_ICON_H, 12),
                fill=fill, outline=outline, width=1,
                tags=(tag, "bicon"))
            c.create_text(x + HOME_ICON_W // 2, y + 32, text=icon,
                          font=("Segoe UI Emoji", 26), fill="#f5d97a",
                          tags=(tag, "bicon"))
            label = name if len(name) <= 16 else name[:15] + "…"
            c.create_text(x + HOME_ICON_W // 2, y + HOME_ICON_H - 26,
                          text=label, font=("Yu Gothic UI", 11),
                          fill="#eef1ff", width=HOME_ICON_W - 12,
                          justify="center", tags=(tag, "bicon"))
            self.home_items[tag] = name
            self.home_shapes[tag] = (shape, outline)
            c.tag_bind(tag, "<ButtonPress-1>", self.home_press)
            c.tag_bind(tag, "<B1-Motion>", self.home_move)
            c.tag_bind(tag, "<ButtonRelease-1>", self.home_release)
            c.tag_bind(tag, "<Double-Button-1>", self.home_double)
            c.tag_bind(tag, "<Button-3>", self.home_right)
            c.tag_bind(tag, "<Enter>", self._home_hover_on)
            c.tag_bind(tag, "<Leave>", self._home_hover_off)

        def _home_hover_on(self, event):
            tag = self.home_tag()
            if tag in self.home_shapes:
                shape, _base = self.home_shapes[tag]
                self.home_canvas.itemconfigure(
                    shape, outline=self.theme["accent_hover"],
                    width=2)

        def _home_hover_off(self, event):
            tag = self.home_tag()
            if tag in self.home_shapes:
                shape, base = self.home_shapes[tag]
                self.home_canvas.itemconfigure(shape, outline=base,
                                               width=1)

        def home_tag(self):
            for t in self.home_canvas.gettags("current"):
                if t.startswith("board_"):
                    return t
            return None

        def home_press(self, event):
            tag = self.home_tag()
            if not tag or tag not in self.home_items:
                return
            name = self.home_items[tag]
            cell = self.home_positions[name]
            x, y = cell_to_pos(cell[0], cell[1], HOME_CELL_W, HOME_CELL_H)
            self.home_drag = {"tag": tag, "dx": event.x - x,
                              "dy": event.y - y, "x": x, "y": y}
            self.home_canvas.tag_raise(tag)
            try:
                # keep motion/release events coming to this canvas
                # even if the pointer leaves the icon or the window,
                # so a drag can never get "stuck" half-finished
                self.home_canvas.grab_set()
            except Exception:
                pass

        def home_move(self, event):
            tag = self.home_drag.get("tag")
            if not tag or tag not in self.home_items:
                return
            nx = event.x - self.home_drag["dx"]
            ny = event.y - self.home_drag["dy"]
            self.home_canvas.move(tag, nx - self.home_drag["x"],
                                  ny - self.home_drag["y"])
            self.home_drag["x"], self.home_drag["y"] = nx, ny

        def home_release(self, event=None):
            tag = self.home_drag.get("tag")
            if not tag:
                return
            # clear the drag state FIRST so the widget-level release
            # binding firing right after this becomes a no-op
            drag = self.home_drag
            self.home_drag = {"tag": None, "dx": 0, "dy": 0,
                              "x": 0, "y": 0}
            try:
                self.root.grab_release()
            except Exception:
                pass
            name = self.home_items.get(tag)
            if name is None:
                return
            target = pos_to_cell(drag["x"], drag["y"],
                                 HOME_CELL_W, HOME_CELL_H)
            cells = {n: tuple(c) for n, c in self.home_positions.items()}
            move_to_cell(cells, name, target)
            self.home_positions = cells
            self._save_home()
            self.render_home()

        def home_double(self, event):
            # a double-click is not a drag: drop any drag state the
            # first click left behind before opening the board
            if self.home_drag.get("tag"):
                self.home_release()
            tag = self.home_tag()
            if tag:
                self.open_board(self.home_items[tag])

        def home_right(self, event):
            tag = self.home_tag()
            if not tag:
                return
            name = self.home_items[tag]
            path = os.path.join(self.base, name)
            menu = tk.Menu(self.root, tearoff=0)
            menu.add_command(label="開く",
                             command=lambda: self.open_board(name))
            menu.add_command(label="場所を開く",
                             command=lambda: reveal_in_explorer(path))
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
            # Always ask: the user freely chooses the board's name.
            # The suggested "ボードN" is just an editable draft.
            name = simpledialog.askstring(
                "ボードを作る", "ボードの名前を決めてください:",
                initialvalue="ボード%d" % n, parent=self.root)
            if name is None:
                return  # cancelled -> create nothing
            try:
                created = create_board(self.base, name)
            except ValueError as exc:
                # covers empty names and unusable characters; the
                # message says exactly what is wrong, nothing is made
                messagebox.showerror("ボードを作れません", str(exc),
                                     parent=self.root)
                return
            cell = next_free_cell(set(self.home_positions.values()),
                                  self.home_rpc)
            self.home_positions[created] = cell
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
            self.save_board_data()
            self.board_name = name
            self.board_folder = os.path.join(self.base, name)
            data = load_board(self.board_folder)
            self.positions = {k: tuple(v)
                              for k, v in data.get("positions", {}).items()}
            self.notes = data.get("notes", [])
            self.board_percent = data.get("size_percent",
                                          DEFAULT_PERCENT)
            self.thumb_cache = {}
            self.size_button.config(
                text="大きさ: %d%%" % self.board_percent)
            self.board_rpc = rows_per_column(
                max(self.canvas.winfo_height(), 640),
                self.metrics["cell_h"])
            self.view = "board"
            self.home_frame.pack_forget()
            self.board_frame.pack(side="top", fill="both", expand=True)
            self.root.title("OkibaBoard - " + name)
            self.render_board()
            self.update_board_hint()
            self.root.after(120, self.setup_drop)
            self.root.after(200, self.update_board_hint)

        def poll_folder(self):
            try:
                self._rescan_board()
            except Exception:
                pass
            finally:
                self.root.after(POLL_MS, self.poll_folder)

        def _rescan_board(self):
            """Reflect the board folder's REAL contents on the board.

            Files added outside appear; files moved/removed outside
            (Explorer, an OLE drag-out, デスクトップへ移動) disappear.
            Never runs while a card drag or an OLE drag is in flight,
            so it can never undo or fight a move the user is making.
            """
            if self.view != "board" or not self.board_folder:
                return
            if self.drag.get("tag") or self.ole_active:
                return
            names = {e["name"] for e in scan_folder(self.board_folder)}
            if names != self.known_names:
                for gone in self.known_names - names:
                    self.positions.pop(gone, None)
                self.render_board()

        def render_board(self):
            # a re-render must never fight an in-progress drag or an
            # OLE drag-out (it would recreate the dragged card's items
            # and lose the drag); callers that finish a drag clear the
            # state first and only then render
            if self.drag.get("tag") or self.ole_active:
                return
            c = self.canvas
            for tag in list(self.items):
                c.delete(tag)
            self.items = {}
            self.card_shapes = {}
            if not self.board_folder:
                return
            m = self.metrics
            entries = scan_folder(self.board_folder)
            self.known_names = {e["name"] for e in entries}
            # notes + already-placed files occupy their cells, so a
            # brand-new card never overlaps them
            occupied = self._occupied_cells()
            for e in entries:
                cell = self.positions.get(e["name"])
                if cell is None:
                    cell = next_free_cell(occupied, self.board_rpc)
                    self.positions[e["name"]] = cell
                occupied.add(tuple(cell))
                x, y = cell_to_pos(cell[0], cell[1],
                                   m["cell_w"], m["cell_h"])
                self.make_card(kind=kind_of_file(e["name"],
                                                 e["is_dir"]),
                               name=e["name"], x=x, y=y)
            for n in self.notes:
                x, y = cell_to_pos(n["col"], n["row"],
                                   m["cell_w"], m["cell_h"])
                self.make_card(kind="note", name=n["text"], x=x, y=y,
                               note_id=n["id"])
            self.save_board_data()

        def _thumbnail(self, name):
            """Pillow thumbnail PhotoImage for an image file, or None.

            Any failure (no Pillow, unreadable file, tiny size) falls
            back to the plain icon card. Results are cached per
            (path, percent); PhotoImages are kept referenced here.
            """
            if PILImage is None or not self.board_folder:
                return None
            path = os.path.join(self.board_folder, name)
            key = (path, self.board_percent)
            if key in self.thumb_cache:
                return self.thumb_cache[key]
            photo = None
            try:
                m = self.metrics
                box = (max(8, m["card_w"] - 14),
                       max(8, m["card_h"] - m["font"] - 16))
                with PILImage.open(path) as im:
                    im.thumbnail(box)
                    photo = PILImageTk.PhotoImage(im.convert("RGB"))
            except Exception:
                photo = None
            self.thumb_cache[key] = photo
            return photo

        def make_card(self, kind, name, x, y, note_id=None):
            c = self.canvas
            m = self.metrics
            tag = "card_" + uuid.uuid4().hex[:10]
            icon, fill, outline, hover = KIND_STYLE[kind]
            if kind == "file":
                # generic files take the theme's accent colour, so
                # card frames change together with the theme
                outline = self.theme["accent"]
                hover = self.theme["accent_hover"]
            radius = max(4, min(12, m["card_h"] // 7))
            # soft shadow (offset rounded rect), then the body
            c.create_polygon(
                rounded_rect_points(x + 4, y + 5, x + m["card_w"] + 4,
                                    y + m["card_h"] + 5, radius),
                fill=self.theme["shadow"], outline="",
                tags=(tag, "card"))
            shape = c.create_polygon(
                rounded_rect_points(x, y, x + m["card_w"],
                                    y + m["card_h"], radius),
                fill=fill, outline=outline, width=1,
                tags=(tag, "card"))
            thumb = self._thumbnail(name) if kind == "image" else None
            if thumb is not None:
                c.create_image(x + m["card_w"] // 2,
                               y + 3 + thumb.height() // 2,
                               image=thumb, tags=(tag, "card"))
                label = name
                max_chars = max(4, m["card_w"] // max(m["font"], 6))
                if len(label) > max_chars:
                    label = label[:max_chars - 1] + "…"
                c.create_text(
                    x + m["card_w"] // 2,
                    y + m["card_h"] - m["font"] - 3,
                    text=label, fill="#eef1ff",
                    font=("Yu Gothic UI", m["font"]),
                    width=m["card_w"] - 8, justify="center",
                    tags=(tag, "card"))
            else:
                label = icon + " " + name
                max_chars = max(6, m["card_w"] // 7)
                if len(label) > max_chars:
                    label = label[:max_chars - 1] + "…"
                pad = 8 if self.board_percent >= 50 else 4
                c.create_text(x + pad, y + pad, text=label, anchor="nw",
                              fill="#eef1ff", width=m["card_w"] - pad * 2,
                              font=("Yu Gothic UI", m["font"]),
                              tags=(tag, "card"))
            self.items[tag] = {"kind": kind, "name": name,
                               "note_id": note_id, "x": x, "y": y}
            self.card_shapes[tag] = (shape, outline, hover)
            # every visible part of the card (shadow, body, text,
            # thumbnail) carries the same tag, so the whole card is
            # the drag hit area and all of it moves together
            c.tag_bind(tag, "<ButtonPress-1>", self.on_press)
            c.tag_bind(tag, "<B1-Motion>", self.on_move)
            c.tag_bind(tag, "<ButtonRelease-1>", self.on_release)
            c.tag_bind(tag, "<Double-Button-1>", self.on_double)
            c.tag_bind(tag, "<Button-3>", self.card_right)
            c.tag_bind(tag, "<Enter>", self._card_hover_on)
            c.tag_bind(tag, "<Leave>", self._card_hover_off)

        def _card_hover_on(self, event):
            tag = self.card_tag()
            if tag in self.card_shapes:
                shape, _base, hover = self.card_shapes[tag]
                self.canvas.itemconfigure(shape, outline=hover, width=2)

        def _card_hover_off(self, event):
            tag = self.card_tag()
            if tag in self.card_shapes:
                shape, base, _hover = self.card_shapes[tag]
                self.canvas.itemconfigure(shape, outline=base, width=1)

        def card_tag(self):
            for t in self.canvas.gettags("current"):
                if t.startswith("card_"):
                    return t
            return None

        def on_press(self, event):
            tag = self.card_tag()
            if not tag or tag not in self.items:
                return
            info = self.items[tag]
            self.drag = {"tag": tag, "dx": event.x - info["x"],
                         "dy": event.y - info["y"]}
            self.canvas.tag_raise(tag)
            try:
                # hold a grab for the whole drag: motion/release keep
                # arriving at this canvas even outside the card or
                # the window, so a release can never be missed (in v4
                # a release outside the card left the drag state stuck
                # and the poll guard then blocked re-rendering)
                self.canvas.grab_set()
            except Exception:
                pass

        def on_move(self, event):
            tag = self.drag.get("tag")
            if not tag:
                return
            info = self.items.get(tag)
            if info is None:
                self.drag = {"tag": None, "dx": 0, "dy": 0}
                return
            nx, ny = event.x - self.drag["dx"], event.y - self.drag["dy"]
            self.canvas.move(tag, nx - info["x"], ny - info["y"])
            info["x"], info["y"] = nx, ny
            self._maybe_ole_drag_out(event, info)

        def on_release(self, event=None):
            tag = self.drag.get("tag")
            if not tag:
                return
            info = self.items.get(tag)
            # clear the drag state FIRST: the widget-level release
            # binding fires right after the item-level one and must
            # find nothing left to do; render_board also keys off it
            self.drag = {"tag": None, "dx": 0, "dy": 0}
            try:
                self.root.grab_release()
            except Exception:
                pass
            if info is None:
                return
            m = self.metrics
            target = pos_to_cell(info["x"], info["y"],
                                 m["cell_w"], m["cell_h"])
            if info["kind"] == "note":
                key = ("note", info["note_id"])
            else:
                key = ("file", info["name"])
            cells = self._combined_cells()
            move_to_cell(cells, key, target)  # swaps if occupied
            self._apply_cells(cells)
            # saved position = exactly the cell we just computed;
            # render_board re-draws from it (and persists it)
            self.render_board()

        def _maybe_ole_drag_out(self, event, info):
            """Pointer left the window mid-drag with a file/folder
            card -> hand the drag over to native OLE (DoDragDrop) so
            the item can be dropped on the Desktop / Explorer."""
            if sys.platform != "win32" or self.ole_active:
                return
            if info["kind"] == "note" or not self.board_folder:
                return
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()
            if not (event.x < -16 or event.y < -16
                    or event.x > w + 16 or event.y > h + 16):
                return
            path = os.path.join(self.board_folder, info["name"])
            if not os.path.exists(path):
                return
            # end the internal drag WITHOUT snapping: saved cells are
            # still the pre-drag ones, which is where the card returns
            # if the OLE drag is cancelled or ends in a copy
            self.drag = {"tag": None, "dx": 0, "dy": 0}
            try:
                self.root.grab_release()
            except Exception:
                pass
            self.ole_active = True
            try:
                effect = ole_drag_out([path])
            except Exception:
                effect = 0
            self.ole_active = False
            # the folder contents are the source of truth now: if the
            # item was moved out, its card disappears; if it is still
            # there, the re-render snaps it back to its saved cell
            self._rescan_board()
            self.render_board()
            if effect == 2:  # DROPEFFECT_MOVE
                self.update_board_hint("外へ移動しました")

        def on_double(self, event):
            # a double-click is not a drag: finish any drag state the
            # clicks left behind first (this also re-renders, so the
            # tag lookup below uses the fresh card under the pointer)
            if self.drag.get("tag"):
                self.on_release()
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
                path = os.path.join(self.board_folder, info["name"])
                menu.add_command(
                    label="開く",
                    command=lambda: open_item(path))
                menu.add_command(
                    label="場所を開く",
                    command=lambda: reveal_in_explorer(path))
                menu.add_command(
                    label="デスクトップへ移動",
                    command=lambda: self.move_to_desktop_ui(info))
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

        def move_to_desktop_ui(self, info):
            """Right-click fallback for drag-out (always available):
            move the real file/folder to the Desktop folder; the
            card disappears because the board folder loses the item.
            """
            if not self.board_folder:
                return
            src = os.path.join(self.board_folder, info["name"])
            desk = desktop_dir()
            try:
                os.makedirs(desk, exist_ok=True)
                placed = place_dropped_items([src], desk)
            except Exception as exc:
                messagebox.showwarning(
                    "移動できませんでした", str(exc), parent=self.root)
                return
            if placed:
                self.positions.pop(info["name"], None)
                self.render_board()
                self.update_board_hint("デスクトップへ移動しました")
            else:
                messagebox.showwarning(
                    "移動できませんでした",
                    "すでにデスクトップにあるか、移動できませんでした。",
                    parent=self.root)

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
            cell = next_free_cell(self._occupied_cells(), self.board_rpc)
            self.notes.append({"id": uuid.uuid4().hex[:10], "text": text,
                               "col": cell[0], "row": cell[1]})
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

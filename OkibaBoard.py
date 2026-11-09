# -*- coding: utf-8 -*-
"""
OkibaBoard v4 - Deskboard-style board app (Python + Tkinter, stdlib only)

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
  Card size is adjustable with one toolbar button "大きさ: 小/中/大"
  (card size, grid cell and font change together; saved per board).
  Buttons: "← もどる", "＋メモ", "＋ファイル", "大きさ: …".
  Right-click a file card: 開く / 場所を開く / rename (renames the
  real file) / send to Recycle Bin. Double-click opens the real file.
  Intake: native Windows drag & drop (WM_DROPFILES via ctypes, no
  external library) OR the guaranteed "＋ファイル" button. Dropped /
  chosen items enter the board folder with the Explorer rule
  (same drive = move, different drive = copy, never overwrite -
  a " (2)" suffix is added instead).
  The folder is polled every ~2 seconds so outside changes
  appear/disappear automatically.

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

# Board card sizes: card size, grid cell and font change together.
BOARD_SIZES = {
    "小": {"card_w": 110, "card_h": 64, "cell_w": 126, "cell_h": 80,
           "font": 9},
    "中": {"card_w": 150, "card_h": 88, "cell_w": 166, "cell_h": 104,
           "font": 10},
    "大": {"card_w": 200, "card_h": 116, "cell_w": 216, "cell_h": 132,
           "font": 12},
}
SIZE_ORDER = ["小", "中", "大"]
DEFAULT_SIZE = "中"

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
    """Load home icon cells; returns {"positions": {name: (col, row)}}."""
    path = home_path(base)
    positions = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, value in data.get("positions", {}).items():
                cell = _cell_from_any(value, HOME_CELL_W, HOME_CELL_H)
                if cell is not None:
                    positions[name] = cell
        except Exception:
            positions = {}
    return {"positions": positions}


def save_home(base, positions):
    """Save home icon cells (dict name -> (col, row)) under *base*."""
    data = {"positions": {
        name: {"col": int(cell[0]), "row": int(cell[1])}
        for name, cell in positions.items()}}
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


def save_board(folder, positions, notes, size=DEFAULT_SIZE):
    """Save card cells, notes and card *size* into *folder*'s board file.

    positions: dict name -> (col, row). notes: list of dicts with
    id/text/col/row.
    """
    if size not in BOARD_SIZES:
        size = DEFAULT_SIZE
    data = {
        "version": 4,
        "size": size,
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
    """Load a board file, normalized to v4 cells.

    Returns {"positions": {name: (col, row)}, "notes": [...],
    "size": "小"/"中"/"大"}. Legacy v3 pixel positions are converted
    to cells with the 中 cell size.
    """
    result = {"version": 4, "positions": {}, "notes": [],
              "size": DEFAULT_SIZE}
    path = board_path(folder)
    if not os.path.exists(path):
        return result
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return result
    if data.get("size") in BOARD_SIZES:
        result["size"] = data["size"]
    metrics = BOARD_SIZES[result["size"]]
    for name, value in data.get("positions", {}).items():
        cell = _cell_from_any(value, metrics["cell_w"], metrics["cell_h"])
        if cell is not None:
            result["positions"][name] = cell
    for n in data.get("notes", []):
        try:
            cell = _cell_from_any(n, metrics["cell_w"], metrics["cell_h"])
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

            # ---------- grid math ----------
            m = BOARD_SIZES["中"]
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
            save_board(board, positions, notes, size="大")
            names_after = [e["name"] for e in scan_folder(board)]
            assert BOARD_FILENAME not in names_after, names_after

            loaded = load_board(board)
            assert loaded["size"] == "大", loaded["size"]
            assert loaded["positions"]["alpha.txt"] == (2, 1)
            assert loaded["notes"][0]["text"] == "sample note"
            assert (loaded["notes"][0]["col"],
                    loaded["notes"][0]["row"]) == (0, 2)

            # default size when the json has none
            save_board(board, {}, [])
            assert load_board(board)["size"] == "中"

            # legacy v3 pixel positions convert to 中 cells
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

    class App:
        def __init__(self, root):
            self.root = root
            root.title("OkibaBoard")
            root.geometry("1120x720")
            root.configure(bg="#070b18")

            self.base = ensure_base()
            self.view = "home"

            # ---- home state: positions = {board name: (col, row)} ----
            self.home_positions = load_home(self.base).get("positions", {})
            self.home_items = {}   # canvas tag -> board name
            self.home_drag = {"tag": None, "dx": 0, "dy": 0,
                              "x": 0, "y": 0}
            self.home_rpc = rows_per_column(640, HOME_CELL_H)

            # ---- board state ----
            self.board_folder = None
            self.board_name = ""
            self.board_size = DEFAULT_SIZE
            self.positions = {}    # file/folder name -> (col, row)
            self.notes = []        # {id, text, col, row}
            self.items = {}        # canvas tag -> info dict (has x, y px)
            self.known_names = set()
            self.drag = {"tag": None, "dx": 0, "dy": 0}
            self.board_rpc = rows_per_column(640,
                                             BOARD_SIZES[DEFAULT_SIZE]
                                             ["cell_h"])

            # ---- native drop ----
            self.drop = NativeDrop(self.on_native_drop)
            self.drop_setup_done = False

            self._build_home()
            self._build_board()
            self.show_home()
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.root.after(60, self.setup_drop)
            self.root.after(POLL_MS, self.poll_folder)

        # ------------------------------------------------------------ helpers
        @property
        def metrics(self):
            return BOARD_SIZES[self.board_size]

        def _make_button(self, parent, text, command):
            btn = tk.Button(
                parent, text=text, command=command,
                bg="#22315e", fg="white", activebackground="#2e4176",
                relief="flat", padx=12, pady=6)
            btn.pack(side="left", padx=8, pady=8)
            return btn

        def _build_home(self):
            frame = tk.Frame(self.root, bg="#070b18")
            self.home_frame = frame
            bar = tk.Frame(frame, bg="#0d1430")
            bar.pack(side="top", fill="x")
            self._make_button(bar, "＋ボード", self.add_board)
            tk.Label(
                bar, text="ボードをダブルクリックで開きます",
                bg="#0d1430", fg="#aebadd",
            ).pack(side="left", padx=12)
            tk.Label(
                bar, text="保存場所: %s" % self.base,
                bg="#0d1430", fg="#7e8bb5",
                font=("Yu Gothic UI", 9),
            ).pack(side="right", padx=12)
            self.home_canvas = tk.Canvas(frame, bg="#070b18",
                                         highlightthickness=0)
            self.home_canvas.pack(side="top", fill="both", expand=True)
            self.home_canvas.bind("<Configure>", self._home_configure)

        def _home_configure(self, event):
            draw_gradient(self.home_canvas)
            self.home_rpc = rows_per_column(event.height, HOME_CELL_H)

        def _build_board(self):
            frame = tk.Frame(self.root, bg="#070b18")
            self.board_frame = frame
            bar = tk.Frame(frame, bg="#0d1430")
            bar.pack(side="top", fill="x")
            self._make_button(bar, "← もどる", self.show_home)
            self._make_button(bar, "＋メモ", self.add_note)
            self._make_button(bar, "＋ファイル", self.add_files)
            self.size_button = self._make_button(
                bar, "大きさ: %s" % self.board_size, self.cycle_size)
            self.board_label = tk.Label(bar, text="", bg="#0d1430",
                                        fg="#aebadd")
            self.board_label.pack(side="left", padx=12)
            self.canvas = tk.Canvas(frame, bg="#070b18",
                                    highlightthickness=0)
            self.canvas.pack(side="top", fill="both", expand=True)
            self.canvas.bind("<Configure>", self._board_configure)

        def _board_configure(self, event):
            draw_gradient(self.canvas)
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
                save_home(self.base, self.home_positions)
            except Exception:
                pass
            self.root.destroy()

        def save_board_data(self):
            if self.board_folder:
                try:
                    save_board(self.board_folder, self.positions,
                               self.notes, size=self.board_size)
                except Exception:
                    pass

        def cycle_size(self):
            idx = SIZE_ORDER.index(self.board_size)
            self.board_size = SIZE_ORDER[(idx + 1) % len(SIZE_ORDER)]
            self.size_button.config(text="大きさ: %s" % self.board_size)
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
            cells = {}
            for name, cell in self.positions.items():
                cells[("file", name)] = tuple(cell)
            for n in self.notes:
                cells[("note", n["id"])] = (n["col"], n["row"])
            return cells

        def _apply_cells(self, cells):
            for (kind, key), cell in cells.items():
                if kind == "file":
                    self.positions[key] = tuple(cell)
                else:
                    for n in self.notes:
                        if n["id"] == key:
                            n["col"], n["row"] = int(cell[0]), int(cell[1])

        # ------------------------------------------------------------ home
        def render_home(self):
            c = self.home_canvas
            for tag in list(self.home_items):
                c.delete(tag)
            self.home_items = {}
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
            save_home(self.base, self.home_positions)

        def make_board_icon(self, name, x, y):
            c = self.home_canvas
            tag = "board_" + uuid.uuid4().hex[:10]
            c.create_rectangle(x, y, x + HOME_ICON_W, y + HOME_ICON_H,
                               fill="#1a2447", outline="#46589a", width=2,
                               tags=(tag, "bicon"))
            c.create_text(x + HOME_ICON_W // 2, y + 30, text="📁",
                          font=("Segoe UI Emoji", 26), fill="#eef1ff",
                          tags=(tag, "bicon"))
            label = name if len(name) <= 16 else name[:15] + "…"
            c.create_text(x + HOME_ICON_W // 2, y + HOME_ICON_H - 26,
                          text=label, font=("Yu Gothic UI", 11),
                          fill="#eef1ff", width=HOME_ICON_W - 12,
                          justify="center", tags=(tag, "bicon"))
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
            cell = self.home_positions[name]
            x, y = cell_to_pos(cell[0], cell[1], HOME_CELL_W, HOME_CELL_H)
            self.home_drag = {"tag": tag, "dx": event.x - x,
                              "dy": event.y - y, "x": x, "y": y}
            self.home_canvas.tag_raise(tag)

        def home_move(self, event):
            tag = self.home_drag.get("tag")
            if not tag:
                return
            nx = event.x - self.home_drag["dx"]
            ny = event.y - self.home_drag["dy"]
            self.home_canvas.move(tag, nx - self.home_drag["x"],
                                  ny - self.home_drag["y"])
            self.home_drag["x"], self.home_drag["y"] = nx, ny

        def home_release(self, event):
            tag = self.home_drag.get("tag")
            if not tag:
                return
            name = self.home_items[tag]
            target = pos_to_cell(self.home_drag["x"], self.home_drag["y"],
                                 HOME_CELL_W, HOME_CELL_H)
            cells = {n: tuple(c) for n, c in self.home_positions.items()}
            move_to_cell(cells, name, target)
            self.home_positions = cells
            self.home_drag = {"tag": None, "dx": 0, "dy": 0,
                              "x": 0, "y": 0}
            save_home(self.base, self.home_positions)
            self.render_home()

        def home_double(self, event):
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
            self.board_size = data.get("size", DEFAULT_SIZE)
            self.size_button.config(text="大きさ: %s" % self.board_size)
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
                if self.view == "board" and self.board_folder \
                        and not self.drag.get("tag"):
                    names = {e["name"]
                             for e in scan_folder(self.board_folder)}
                    if names != self.known_names:
                        for gone in self.known_names - names:
                            self.positions.pop(gone, None)
                        self.render_board()
            except Exception:
                pass
            finally:
                self.root.after(POLL_MS, self.poll_folder)

        def render_board(self):
            c = self.canvas
            for tag in list(self.items):
                c.delete(tag)
            self.items = {}
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
                self.make_card(
                    kind="dir" if e["is_dir"] else "file",
                    name=e["name"], x=x, y=y,
                    icon="📁" if e["is_dir"] else "📄")
            for n in self.notes:
                x, y = cell_to_pos(n["col"], n["row"],
                                   m["cell_w"], m["cell_h"])
                self.make_card(kind="note", name=n["text"], x=x, y=y,
                               icon="📝", note_id=n["id"])
            self.save_board_data()

        def make_card(self, kind, name, x, y, icon, note_id=None):
            c = self.canvas
            m = self.metrics
            tag = "card_" + uuid.uuid4().hex[:10]
            fill = "#3a3320" if kind == "note" else "#1a2447"
            outline = "#8a7a3a" if kind == "note" else "#46589a"
            c.create_rectangle(x, y, x + m["card_w"], y + m["card_h"],
                               fill=fill, outline=outline, width=2,
                               tags=(tag, "card"))
            label = icon + " " + name
            max_chars = max(10, m["card_w"] // 8)
            if len(label) > max_chars:
                label = label[:max_chars - 1] + "…"
            c.create_text(x + 10, y + 10, text=label, anchor="nw",
                          fill="#eef1ff", width=m["card_w"] - 20,
                          font=("Yu Gothic UI", m["font"]),
                          tags=(tag, "card"))
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
            self.drag = {"tag": None, "dx": 0, "dy": 0}
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
            self.render_board()  # re-renders snapped + saves

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

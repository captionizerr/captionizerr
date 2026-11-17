"""The only code path that writes next to media files.

It creates `<video stem>.captionizerr.<lang>[.forced].<srt|ass>` and nothing else:
no overwrite, no delete, no other extension, no path outside the video's folder.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

TAG = "captionizerr"
_LANG = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z]{2,4})?$")


class WriteRefused(RuntimeError):
    pass


def target_path(video: Path, lang: str, fmt: str, forced: bool = False) -> Path:
    """`forced` = the translation of a forced track (foreign-language scenes, signs): players
    show `.forced.` subtitles automatically when the audio is in the viewer's language."""
    if not _LANG.match(lang):
        raise WriteRefused(f"invalid language code: {lang!r}")
    if fmt not in ("srt", "ass"):
        raise WriteRefused(f"invalid format: {fmt!r}")
    return video.with_name(f"{video.stem}.{TAG}.{lang}{'.forced' if forced else ''}.{fmt}")


_SIDECAR = re.compile(r"^(?P<stem>.+)\." + TAG + r"\.(?P<lang>[a-z]{2,3}(?:-[A-Za-z]{2,4})?)(?P<forced>\.forced)?\.(?P<fmt>srt|ass)$")


def sidecars(video: Path) -> list[Path]:
    """Our own subtitle files that belong to this video (by exact stem)."""
    video = Path(video)
    if not video.parent.is_dir():
        return []
    out = []
    for p in video.parent.iterdir():
        m = _SIDECAR.match(p.name)
        if m and m.group("stem") == video.stem and p.is_file() and not p.is_symlink():
            out.append(p)
    return sorted(out)


def move_sidecars(old_video: Path, new_video: Path) -> list[tuple[Path, Path]]:
    """After an *arr rename, rename our own sidecars to follow the video. Never overwrites,
    never touches any file that isn't ours, never leaves the new video's folder."""
    old_video, new_video = Path(old_video), Path(new_video)
    moved = []
    for src in sidecars(old_video):
        m = _SIDECAR.match(src.name)
        dest = target_path(new_video, m.group("lang"), m.group("fmt"), bool(m.group("forced")))
        if dest.exists():
            continue
        os.rename(src, dest)
        moved.append((src, dest))
    return moved


def write_next_to(video: Path, lang: str, fmt: str, content: str, forced: bool = False) -> Path:
    video = Path(video)
    if not video.is_file():
        raise WriteRefused(f"video not found: {video}")
    dest = target_path(video, lang, fmt, forced)
    if dest.parent.resolve() != video.parent.resolve():
        raise WriteRefused("destination escapes the video's folder")
    # O_EXCL: fail if anything already exists at that name (file or symlink)
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
    except FileExistsError:
        raise WriteRefused(f"already exists, not overwriting: {dest.name}") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        st = video.stat()
        os.chown(dest, st.st_uid, st.st_gid)
    except (PermissionError, OSError):
        pass
    return dest


def replace_own(path: Path, content: str, history_dir: Path) -> Path:
    """Overwrite one of OUR subtitle files (after an edit). The file must already exist, match
    `*.captionizerr.<lang>.<srt|ass>` and not be a symlink. The previous version is kept in
    `history_dir` (outside the library); the new content is written atomically."""
    import hashlib
    import time
    path = Path(path)
    if not _SIDECAR.match(path.name):
        raise WriteRefused(f"not a Captionizerr file: {path.name}")
    if not path.is_file() or path.is_symlink():
        raise WriteRefused(f"not an existing regular file: {path.name}")
    hd = Path(history_dir) / hashlib.sha1(str(path).encode()).hexdigest()[:16]
    hd.mkdir(parents=True, exist_ok=True)
    (hd / "path.txt").write_text(str(path), encoding="utf-8")
    (hd / (time.strftime("%Y%m%d-%H%M%S-") + f"{time.time_ns() % 10**6:06d}" + path.suffix)).write_bytes(path.read_bytes())
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def follow_rename_history(old: Path, new: Path, history_dir: Path) -> None:
    """Keep the editor's previous versions of a renamed translation (history is keyed by path)."""
    import hashlib
    hd = Path(history_dir)
    src = hd / hashlib.sha1(str(Path(old)).encode()).hexdigest()[:16]
    dst = hd / hashlib.sha1(str(Path(new)).encode()).hexdigest()[:16]
    if src.is_dir() and not dst.exists():
        src.rename(dst)
        (dst / "path.txt").write_text(str(new), encoding="utf-8")


def history(path: Path, history_dir: Path) -> list[Path]:
    import hashlib
    hd = Path(history_dir) / hashlib.sha1(str(Path(path)).encode()).hexdigest()[:16]
    return sorted((p for p in hd.glob("*") if p.name != "path.txt"), reverse=True) if hd.is_dir() else []

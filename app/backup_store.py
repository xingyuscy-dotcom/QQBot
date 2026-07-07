import sqlite3
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from .paths import (
    BACKUPS_DIR,
    COMMANDS_PATH,
    CONFIG_LOCAL_PATH,
    DB_PATH,
    HOT_DB_PATH,
    KNOWLEDGE_DB_PATH,
    LOGS_DIR,
    MEMORIES_DIR,
)


def create_backup() -> dict:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"QQbot_v2_backup_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.zip"
    target = BACKUPS_DIR / name
    tmp_db = BACKUPS_DIR / f".{name}.sqlite3"
    tmp_knowledge_db = BACKUPS_DIR / f".{name}.knowledge.sqlite3"
    tmp_hot_db = BACKUPS_DIR / f".{name}.hot.sqlite3"

    try:
        _copy_sqlite_db(tmp_db)
        _copy_sqlite_db_to(KNOWLEDGE_DB_PATH, tmp_knowledge_db)
        _copy_sqlite_db_to(HOT_DB_PATH, tmp_hot_db)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if tmp_db.exists():
                archive.write(tmp_db, "data/bot.sqlite3")
            if tmp_knowledge_db.exists():
                archive.write(tmp_knowledge_db, "data/knowledge.sqlite3")
            if tmp_hot_db.exists():
                archive.write(tmp_hot_db, "data/hot.sqlite3")
            _write_file(archive, CONFIG_LOCAL_PATH, "config.local.json")
            _write_file(archive, COMMANDS_PATH, "data/commands.json")
            _write_dir(archive, MEMORIES_DIR, "data/memories")
            _write_dir(archive, LOGS_DIR, "logs")
    finally:
        tmp_db.unlink(missing_ok=True)
        tmp_knowledge_db.unlink(missing_ok=True)
        tmp_hot_db.unlink(missing_ok=True)

    return _backup_info(target)


def list_backups() -> list[dict]:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    items = [_backup_info(path) for path in BACKUPS_DIR.glob("*.zip") if path.is_file()]
    return sorted(items, key=lambda item: item["created_at"], reverse=True)


def inspect_backup(name: str) -> dict:
    path = _backup_path(name)
    with zipfile.ZipFile(path) as archive:
        files = [item.filename for item in archive.infolist() if not item.is_dir()]

    return {
        "backup": _backup_info(path),
        "file_count": len(files),
        "contains": {
            "database": "data/bot.sqlite3" in files,
            "knowledge_database": "data/knowledge.sqlite3" in files,
            "hot_database": "data/hot.sqlite3" in files,
            "local_config": "config.local.json" in files,
            "commands": "data/commands.json" in files,
            "memory_files": sum(1 for item in files if item.startswith("data/memories/")),
            "log_files": sum(1 for item in files if item.startswith("logs/")),
        },
        "files": files[:200],
    }


def restore_backup(name: str) -> dict:
    path = _backup_path(name)
    safety_backup = create_backup()

    with zipfile.ZipFile(path) as archive:
        _restore_file(archive, "data/bot.sqlite3", DB_PATH)
        _restore_file(archive, "data/knowledge.sqlite3", KNOWLEDGE_DB_PATH)
        _restore_file(archive, "data/hot.sqlite3", HOT_DB_PATH)
        _restore_file(archive, "config.local.json", CONFIG_LOCAL_PATH)
        _restore_file(archive, "data/commands.json", COMMANDS_PATH)
        _restore_dir(archive, "data/memories/", MEMORIES_DIR)
        _restore_dir(archive, "logs/", LOGS_DIR)

    return {
        "backup": _backup_info(path),
        "safety_backup": safety_backup,
    }


def _backup_path(name: str) -> Path:
    if not name.endswith(".zip") or "/" in name or "\\" in name:
        raise ValueError("备份文件名不合法")

    path = (BACKUPS_DIR / name).resolve()
    backups_root = BACKUPS_DIR.resolve()
    if path.parent != backups_root:
        raise ValueError("备份文件名不合法")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("备份文件不存在")
    return path


def _copy_sqlite_db(target: Path) -> None:
    _copy_sqlite_db_to(DB_PATH, target)


def _copy_sqlite_db_to(source_path: Path, target: Path) -> None:
    if not source_path.exists():
        return
    source = sqlite3.connect(source_path)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _write_file(archive: zipfile.ZipFile, path: Path, arcname: str) -> None:
    if path.exists() and path.is_file():
        archive.write(path, arcname)


def _write_dir(archive: zipfile.ZipFile, root: Path, arc_prefix: str) -> None:
    if not root.exists() or not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file():
            archive.write(path, f"{arc_prefix}/{path.relative_to(root).as_posix()}")


def _restore_file(archive: zipfile.ZipFile, arcname: str, target: Path) -> None:
    if arcname not in archive.namelist():
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(arcname) as source, target.open("wb") as destination:
        shutil.copyfileobj(source, destination)


def _restore_dir(archive: zipfile.ZipFile, arc_prefix: str, target_dir: Path) -> None:
    members = [
        item for item in archive.infolist()
        if not item.is_dir() and item.filename.startswith(arc_prefix)
    ]
    if not members:
        return

    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    for member in members:
        relative = member.filename[len(arc_prefix):]
        target = _safe_target(target_dir, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)


def _safe_target(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    root_path = root.resolve()
    if target != root_path and root_path not in target.parents:
        raise ValueError("备份内包含不安全路径")
    return target


def _backup_info(path: Path) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }

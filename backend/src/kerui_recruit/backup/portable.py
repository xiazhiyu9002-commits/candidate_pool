from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from kerui_recruit.backup.service import _validate_sqlite
from kerui_recruit.backup.snapshot import snapshot_tree, file_hash, CHUNK_SIZE, REBUILD_MARKER

_MAGIC = b"KRBACKUP\x02"
_SALT_SIZE = 16
_KDF_ITERATIONS = 600_000
_DATABASE_NAME = "recruit.sqlite3"


class PortableRestoreError(RuntimeError):
    """Restore failure carrying a stable machine-readable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PortableRestoreReport:
    target_root: str
    files_restored: int
    files_verified: int
    ok: bool


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def is_same_volume(left: Path, right: Path) -> bool:
    """Return True when two paths live on the same storage volume.

    On Windows the drive letter decides; elsewhere the device number of the
    deepest existing ancestor decides (so a not-yet-created target still works).
    """
    left = left.absolute()
    right = right.absolute()
    if os.name == "nt":
        return left.drive.casefold() == right.drive.casefold()
    try:
        return _device(left) == _device(right)
    except OSError:
        return True


def _device(path: Path) -> int:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate.stat().st_dev


class PortableBackupService:
    """Create an encrypted portable backup and restore it to a new directory.

    The backup bundles a consistent SQLite snapshot, a rebuild marker, blobs
    and config into one passphrase-encrypted ``.krbackup`` archive together with
    a SHA-256 manifest. Restore decrypts, extracts into a staging directory,
    verifies every file hash and the SQLite integrity, and only then promotes
    the staging directory into place with a rollback safety net.
    """

    def __init__(self, *, current_root: Path) -> None:
        self.current_root = current_root

    def create(self, target_path: Path, passphrase: str) -> Path:
        target_path = target_path.with_suffix(".krbackup")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = target_path.with_name(f'.{target_path.name}.{uuid4().hex}.tmp')
        try:
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / 'archive.zip'
                self._build_archive(archive)
                salt, nonce = os.urandom(_SALT_SIZE), os.urandom(12)
                header = _MAGIC + salt + nonce
                encryptor = Cipher(algorithms.AES(base64.urlsafe_b64decode(_derive_key(passphrase, salt))), modes.GCM(nonce)).encryptor()
                encryptor.authenticate_additional_data(header)
                with archive.open('rb') as source, temporary.open('wb') as output:
                    output.write(header)
                    while chunk := source.read(CHUNK_SIZE):
                        output.write(encryptor.update(chunk))
                    output.write(encryptor.finalize())
                    output.write(encryptor.tag)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target_path)
        finally:
            temporary.unlink(missing_ok=True)
        return target_path

    def restore(
        self,
        backup_path: Path,
        target_root: Path,
        passphrase: str,
    ) -> PortableRestoreReport:
        target = self._resolve_target(target_root)
        if not backup_path.exists():
            raise PortableRestoreError("E_BACKUP_NOT_FOUND", "备份文件不存在")

        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.staging-{uuid4().hex[:8]}"
        staging.mkdir()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / 'archive.zip'
                self._decrypt(backup_path, archive, passphrase)
                expected = self._extract_and_validate(archive, staging)
            # Legacy archives also contain stale search projections.
            if (staging / 'search').exists():
                shutil.rmtree(staging / 'search')
            (staging / REBUILD_MARKER).write_text('1\n', encoding='ascii')
            self._resolve_target(target)  # recheck before the promotion boundary
            self._promote(staging, target)
        except PortableRestoreError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except Exception as error:
            shutil.rmtree(staging, ignore_errors=True)
            raise PortableRestoreError('E_BACKUP_RESTORE_FAILED', f'恢复失败：{error}') from error

        files_restored = len(expected)
        return PortableRestoreReport(
            target_root=str(target),
            files_restored=files_restored,
            files_verified=files_restored,
            ok=True,
        )

    @staticmethod
    def _decrypt(backup: Path, archive: Path, passphrase: str) -> None:
        with backup.open('rb') as source:
            magic = source.read(len(_MAGIC))
            if magic != _MAGIC:
                # Legacy Fernet tokens are monolithic and necessarily use memory
                # proportional to archive size. New v2 backups never use this path.
                source.seek(0)
                payload = source.read()
                if len(payload) <= _SALT_SIZE:
                    raise PortableRestoreError('E_BACKUP_CORRUPT', '备份文件损坏')
                try:
                    archive.write_bytes(Fernet(_derive_key(passphrase, payload[:_SALT_SIZE])).decrypt(payload[_SALT_SIZE:]))
                except InvalidToken as error:
                    raise PortableRestoreError('E_BACKUP_DECRYPT_FAILED', '备份密码错误或文件损坏') from error
                return
            salt, nonce = source.read(_SALT_SIZE), source.read(12)
            remaining = backup.stat().st_size - len(_MAGIC) - _SALT_SIZE - 12 - 16
            if remaining < 0 or len(nonce) != 12:
                raise PortableRestoreError('E_BACKUP_CORRUPT', '备份文件损坏')
            source.seek(-16, os.SEEK_END)
            tag = source.read(16)
            source.seek(len(_MAGIC) + _SALT_SIZE + 12)
            decryptor = Cipher(algorithms.AES(base64.urlsafe_b64decode(_derive_key(passphrase, salt))), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(_MAGIC + salt + nonce)
            try:
                with archive.open('wb') as output:
                    while remaining:
                        chunk = source.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise InvalidTag
                        remaining -= len(chunk)
                        output.write(decryptor.update(chunk))
                    output.write(decryptor.finalize())
            except InvalidTag as error:
                raise PortableRestoreError('E_BACKUP_DECRYPT_FAILED', '备份密码错误或文件损坏') from error

    def _build_archive(self, output: Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp)
            snapshot_tree(self.current_root, snapshot)
            manifest = {}
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(snapshot.rglob('*')):
                    if path.is_file():
                        relative = path.relative_to(snapshot).as_posix()
                        manifest[relative] = file_hash(path)
                        archive.write(path, relative)
                archive.writestr('manifest.json', json.dumps({'version': 2, 'created': datetime.now(timezone.utc).isoformat(), 'files': manifest}))

    def _extract_and_validate(self, archive: Path, staging: Path) -> dict[str, str]:
        expected = self._read_manifest(archive)
        with zipfile.ZipFile(archive, "r") as source:
            for info in source.infolist():
                if info.is_dir() or info.filename == "manifest.json":
                    continue
                destination = self._safe_join(staging, info.filename)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info) as input_stream, destination.open("wb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, CHUNK_SIZE)

        actual: dict[str, str] = {}
        for path in staging.rglob("*"):
            if path.is_file():
                relative = path.relative_to(staging).as_posix()
                actual[relative] = file_hash(path)

        if set(actual.keys()) != set(expected.keys()):
            missing = sorted(set(expected.keys()) - set(actual.keys()))
            undeclared = sorted(set(actual.keys()) - set(expected.keys()))
            raise PortableRestoreError(
                "E_BACKUP_FILE_MISMATCH",
                "备份文件清单不一致"
                + (f"，缺失：{missing}" if missing else "")
                + (f"，多余：{undeclared}" if undeclared else ""),
            )
        for relative, digest in expected.items():
            if actual.get(relative) != digest:
                raise PortableRestoreError(
                    "E_BACKUP_HASH_MISMATCH", f"文件哈希校验失败：{relative}"
                )

        database = staging / "db" / _DATABASE_NAME
        if not database.is_file():
            raise PortableRestoreError("E_BACKUP_DB_INVALID", "备份缺少人才库数据库")
        try:
            _validate_sqlite(database)
        except ValueError as error:
            raise PortableRestoreError("E_BACKUP_DB_INVALID", str(error)) from error

        return expected

    @staticmethod
    def _read_manifest(archive: Path) -> dict[str, str]:
        with zipfile.ZipFile(archive, "r") as source:
            try:
                manifest_bytes = source.read("manifest.json")
            except KeyError as error:
                raise PortableRestoreError(
                    "E_BACKUP_MANIFEST_MISSING", "备份缺少 manifest.json"
                ) from error
        try:
            payload = json.loads(manifest_bytes.decode("utf-8"))
            files = payload["files"]
            if not isinstance(files, dict) or not files:
                raise ValueError
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise PortableRestoreError(
                "E_BACKUP_MANIFEST_INVALID", "备份 manifest 格式不合法"
            ) from error
        return files

    @staticmethod
    def _safe_join(staging: Path, name: str) -> Path:
        root = staging.resolve()
        destination = (staging / name).resolve()
        try:
            relative = destination.relative_to(root)
        except ValueError as error:
            raise PortableRestoreError(
                "E_BACKUP_PATH_TRAVERSAL", f"备份包含非法路径：{name}"
            ) from error
        if relative.as_posix() in ("", ".", ".."):
            raise PortableRestoreError(
                "E_BACKUP_PATH_TRAVERSAL", f"备份包含非法路径：{name}"
            )
        return destination

    def _resolve_target(self, target_root: Path) -> Path:
        if not str(target_root).strip():
            raise PortableRestoreError("E_BACKUP_INVALID_TARGET", "恢复目标路径为空")
        try:
            target = target_root.expanduser().resolve()
            current = self.current_root.expanduser().resolve()
            if (target == Path(target.anchor) or target == Path.home().resolve()
                    or target.is_relative_to(current) or current.is_relative_to(target)
                    or (target.exists() and (not target.is_dir() or any(target.iterdir())))):
                raise PortableRestoreError('E_BACKUP_INVALID_TARGET', '请选择与当前数据目录无嵌套关系的空目录')
            return target
        except PortableRestoreError:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            raise PortableRestoreError("E_BACKUP_INVALID_TARGET", "恢复目标路径无法解析") from error

    @staticmethod
    def _promote(staging: Path, target: Path) -> None:
        removed_empty_target = False
        try:
            if target.exists():
                # Atomic emptiness check: never rename then recursively delete
                # a directory that acquired unrelated files since validation.
                target.rmdir()
                removed_empty_target = True
            os.replace(staging, target)
        except OSError as error:
            if removed_empty_target and not target.exists():
                target.mkdir(exist_ok=True)
            raise PortableRestoreError(
                "E_BACKUP_PROMOTE_FAILED", f"恢复目录替换失败：{error}"
            ) from error


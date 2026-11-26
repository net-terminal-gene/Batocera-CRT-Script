"""Checked, narrowly scoped handheld override installation; no GUI or shell calls."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

SETTINGS = {'lynx.core': 'handy', 'lynx.emulator': 'libretro',
            'nds.melonds_screen_layout': 'Top Only', 'nds.screens_layout': 'top only'}
CONFIG = 'configs/retroarch/config/'
BORDERS = 'configs/retroarch/overlays/borders/'
BACKUP = 'Batocera-CRT-Script/extra/.scaling_tool_backup/'
STATE = BACKUP + 'installation.json'
LEGACY = BACKUP + 'batocera.conf.keys.backup'
PENDING = BACKUP + 'pending.json'
LIMIT = 8 * 1024 * 1024
RECOVERY_READ_LIMIT = 32 * 1024 * 1024  # Bounded rescue of oversized R1 journals.


class OperationError(RuntimeError):
    pass


def encoded(data): return base64.b64encode(data).decode('ascii')


def decoded(text):
    try: return base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as error: raise OperationError('Invalid saved file data') from error


def packed(value): return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def safe(path):
    for part in (path, *path.parents):
        if part.is_symlink(): raise OperationError('A managed path is redirected: ' + str(path))


def snapshot(path, limit=LIMIT):
    safe(path)
    try: descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError: return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OperationError('A managed target is not a regular file: ' + str(path))
        if info.st_size > limit: raise OperationError('A managed file exceeds the snapshot limit')
        if stat.S_IMODE(info.st_mode) > 0o777: raise OperationError('Unsupported special file permissions')
        stream = os.fdopen(descriptor, 'rb'); descriptor = None
        with stream:
            data = stream.read(limit + 1)
            finished = os.fstat(stream.fileno())
        if len(data) > limit: raise OperationError('A managed file exceeds the snapshot limit')
        safe(path)
        try: current = path.lstat()
        except FileNotFoundError as error: raise OperationError('A managed file changed while being read') from error
        def signature(value):
            return (value.st_dev, value.st_ino, value.st_size, value.st_mode, value.st_nlink,
                    value.st_mtime_ns, value.st_ctime_ns)
        if signature(info) != signature(finished) or signature(info) != signature(current) or len(data) != info.st_size:
            raise OperationError('A managed file changed while being read')
        return {'data': encoded(data), 'mode': stat.S_IMODE(info.st_mode)}
    finally:
        if descriptor is not None: os.close(descriptor)


def record(data, mode=0o644): return {'data': encoded(data), 'mode': mode}


def validate_record(value):
    if value is None: return None
    if not isinstance(value, dict) or set(value) != {'data', 'mode'} or not isinstance(value['data'], str):
        raise OperationError('Invalid saved file record')
    if type(value['mode']) is not int or not 0 <= value['mode'] <= 0o777:
        raise OperationError('Invalid saved file permissions')
    data = decoded(value['data'])
    if len(data) > LIMIT: raise OperationError('Saved file exceeds the snapshot limit')
    return data


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try: os.fsync(fd)
    finally: os.close(fd)


def make_parents(path):
    safe(path)
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent); parent = parent.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        sync_directory(directory); sync_directory(directory.parent)


def publish(path, value):
    """Publish one verified preimage or postimage, synchronizing its directory."""
    data = validate_record(value)
    safe(path)
    if value is None:
        path.unlink(missing_ok=True)
        if path.parent.exists(): sync_directory(path.parent)
        return
    make_parents(path.parent)
    safe(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.scaling-', delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush(); os.fchmod(output.fileno(), value['mode']); os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)
    if snapshot(path) != value: raise OperationError('File verification failed: ' + str(path))


def key_lines(data):
    return {key: [encoded(line) for line in data.splitlines(keepends=True)
                  if line.startswith((key + '=').encode())] for key in SETTINGS}


def replace_keys(data, values):
    prefixes = tuple((key + '=').encode() for key in SETTINGS)
    remaining = b''.join(line for line in data.splitlines(keepends=True) if not line.startswith(prefixes))
    saved = [decoded(line) for key in SETTINGS for line in values[key]]
    additions = b''.join(line + (b'\n' if index < len(saved) - 1 and not line.endswith((b'\n', b'\r')) else b'')
                         for index, line in enumerate(saved))
    if remaining and additions and not remaining.endswith((b'\n', b'\r')): remaining += b'\n'
    return remaining + additions


def without_border(data):
    """Scaling-only configs keep the bundled settings but never enable a border overlay."""
    if b'input_overlay = ' not in data: return data
    result = []
    for line in data.splitlines(keepends=True):
        key = line.split(b'=', 1)[0].strip()
        if key in (b'input_overlay_enable', b'input_overlay_enable_autopreferred'):
            ending = b'\r\n' if line.endswith(b'\r\n') else b'\n' if line.endswith(b'\n') else b''
            line = key + b' = "false"' + ending
        result.append(line)
    return b''.join(result)


@dataclass
class Plan:
    action: str
    changes: list[dict]
    checks: dict
    scaling: int = 0
    borders: int = 0
    overwritten: int = 0
    legacy: bool = False
    skipped: int = 0
    has_originals: bool = True
    removed: int = 0
    replaces: str | None = None  # 'borders', 'scaling' or 'legacy': the install this operation replaces.


class Engine:
    def __init__(self, system=Path('/userdata/system')):
        self.system = Path(system)
        self.extra = self.system / 'Batocera-CRT-Script/extra'

    def path(self, name):
        if not isinstance(name, str): raise OperationError('Invalid managed path')
        parts = Path(name).parts
        if not parts or Path(name).is_absolute() or Path(name).as_posix() != name or any(p in ('.', '..') for p in parts) or '\\' in name:
            raise OperationError('Invalid managed path')
        if name not in ('batocera.conf', STATE, LEGACY, PENDING) and not (
                name.startswith(CONFIG) and len(parts) > 3 or name.startswith(BORDERS) and len(parts) > 4):
            raise OperationError('File is outside the handheld override scope')
        path = self.system / name
        safe(path)
        return path

    def payload(self, include_borders=True):
        result = {}
        sources = [(self.extra / 'config', CONFIG)]
        if include_borders: sources.append((self.extra / 'overlays/borders', BORDERS))
        for source, destination in sources:
            safe(source)
            if not source.is_dir(): raise OperationError('Required handheld assets are missing')
            for path in sorted(source.rglob('*')):
                safe(path)
                if path.is_dir(): continue
                value = snapshot(path)
                if value is None: raise OperationError('A handheld asset disappeared')
                data = decoded(value['data'])
                if destination == CONFIG and not include_borders: data = without_border(data)
                result[destination + path.relative_to(source).as_posix()] = record(data)
        if not any(n.startswith(CONFIG) for n in result) or include_borders and not any(n.startswith(BORDERS) for n in result):
            raise OperationError('Required handheld assets are empty')
        return result

    def validate_record(self, value):
        return validate_record(value)

    def read_json(self, name):
        value = snapshot(self.path(name), RECOVERY_READ_LIMIT if name == PENDING else LIMIT)
        if value is None: return None
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result: raise OperationError('Duplicate key in saved operation state')
                result[key] = item
            return result
        try: result = json.loads(decoded(value['data']), object_pairs_hook=unique)
        except (ValueError, TypeError) as error: raise OperationError('Cannot read the saved operation state') from error
        if not isinstance(result, dict): raise OperationError('Saved operation state is not an object')
        return result

    def legacy_keys(self, data):
        result = {}
        for line in data.splitlines():
            if not line or line.startswith(b'#'): continue
            fields = line.split(b'|', 2)
            if len(fields) != 3: raise OperationError('Invalid original settings backup')
            try: key = fields[0].decode('ascii')
            except UnicodeError as error: raise OperationError('Invalid settings backup key') from error
            if key not in SETTINGS or key in result or fields[1] not in (b'present', b'absent'):
                raise OperationError('Invalid original settings backup')
            result[key] = [encoded(fields[0] + b'=' + fields[2] + b'\n')] if fields[1] == b'present' else []
        if set(result) != set(SETTINGS): raise OperationError('Original settings backup is incomplete')
        return result

    def installation(self):
        state = self.read_json(STATE)
        if state is None: return None
        if type(state.get('version')) is not int or state['version'] != 1 or not isinstance(state.get('files'), dict):
            raise OperationError('Unsupported installation backup')
        if 'conf_existed' in state and type(state['conf_existed']) is not bool:
            raise OperationError('Invalid original configuration existence record')
        keys = state.get('keys')
        if not isinstance(keys, dict) or set(keys) != set(SETTINGS): raise OperationError('Invalid original settings')
        for key, lines in keys.items():
            if not isinstance(lines, list): raise OperationError('Invalid original settings')
            for line in lines:
                raw = decoded(line)
                if not isinstance(line, str) or not raw.startswith((key + '=').encode()) or raw.splitlines(keepends=True) != [raw]:
                    raise OperationError('Invalid original setting line')
        for name, entry in state['files'].items():
            self.path(name)
            if not name.startswith((CONFIG, BORDERS)) or not isinstance(entry, dict) or set(entry) != {'before', 'installed'}:
                raise OperationError('Invalid file ownership backup')
            if entry['installed'] is None: raise OperationError('Missing installed file ownership record')
            self.validate_record(entry['before']); self.validate_record(entry['installed'])
        return state

    def status(self):
        """'borders', 'scaling', 'legacy' for an older install, or None when nothing is installed."""
        state = self.installation()
        if state is not None:
            return 'borders' if any(name.startswith(BORDERS) for name in state['files']) else 'scaling'
        return 'legacy' if snapshot(self.path(LEGACY)) is not None else None

    def plan(self, action):
        if action not in ('install_all', 'install_scaling', 'uninstall'): raise OperationError('Unknown operation')
        if snapshot(self.path(PENDING)) is not None: raise OperationError('An interrupted operation needs recovery first')
        state = self.installation()
        payload = ({name: entry['installed'] for name, entry in state['files'].items()}
                   if action == 'uninstall' and state else self.payload(action != 'install_scaling'))
        conf = snapshot(self.path('batocera.conf'))
        data = decoded(conf['data']) if conf else b''
        legacy = snapshot(self.path(LEGACY))
        from_legacy = bool(legacy and state is None)
        has_originals = state is not None or legacy is not None
        if state is not None: replaces = 'borders' if any(n.startswith(BORDERS) for n in state['files']) else 'scaling'
        else: replaces = 'legacy' if legacy is not None else None
        original_keys = state['keys'] if state else self.legacy_keys(decoded(legacy['data'])) if legacy else key_lines(data)
        checks = {name: snapshot(self.path(name)) for name in ('batocera.conf', STATE, LEGACY, PENDING)}
        changes, skipped, overwritten, removed = [], 0, 0, 0
        def change(name, after):
            before = checks.setdefault(name, snapshot(self.path(name)))
            if before != after: changes.append(dict(path=name, before=before, after=after))
        if action != 'uninstall':
            state = state or dict(version=1, keys=original_keys, files={}, conf_existed=conf is not None)
            # Replacing an install that had borders with scaling-only restores the saved border originals.
            for name in [n for n in state['files'] if n.startswith(BORDERS) and n not in payload]:
                entry = state['files'].pop(name)
                current = checks.setdefault(name, snapshot(self.path(name)))
                if current != entry['installed']:
                    raise OperationError('An installed file has been edited; it was preserved: ' + name)
                change(name, entry['before'])
                removed += 1
            for name, value in payload.items():
                current = checks.setdefault(name, snapshot(self.path(name)))
                previous = state['files'].get(name)
                if previous and current != previous['installed']:
                    raise OperationError('An installed file has been edited; it was preserved: ' + name)
                if previous is None:
                    # A legacy backup establishes ownership only of matching bundled files.
                    before = None if from_legacy and current and current['data'] == value['data'] else current
                    previous = state['files'][name] = dict(before=before, installed=value)
                previous['installed'] = value
                overwritten += current is not None and current != value
                change(name, value)
            desired = {key: [encoded((key + '=' + value + '\n').encode())] for key, value in SETTINGS.items()}
            change('batocera.conf', record(replace_keys(data, desired), conf['mode'] if conf else 0o644))
            change(STATE, record(packed(state), 0o600))
            if legacy is None:
                lines = [b'# Batocera-CRT-Script Scaling Tool - batocera.conf key backup\n']
                for key in SETTINGS:
                    values = original_keys[key]
                    lines.append(key.encode() + (b'|present|' + decoded(values[0]).split(b'=', 1)[1].rstrip(b'\r\n')
                                                if values else b'|absent|') + b'\n')
                change(LEGACY, record(b''.join(lines), 0o600))
        else:
            owned = state['files'] if state else {name: dict(before=None, installed=value) for name, value in payload.items()}
            for name, value in owned.items():
                current = checks.setdefault(name, snapshot(self.path(name)))
                if state and current != value['installed']:
                    raise OperationError('An installed file has been edited; it was preserved: ' + name)
                if not state and current and current['data'] != value['installed']['data']:
                    skipped += 1; continue
                change(name, value['before'])
            if not state and not legacy:
                # Match literal complete keys and values; preserve user settings and comments.
                prefixes = tuple((key + '=' + value).encode() for key, value in SETTINGS.items())
                restored = b''.join(line for line in data.splitlines(keepends=True)
                                    if line.rstrip(b'\r\n') not in prefixes)
            else: restored = replace_keys(data, original_keys)
            if state and not state.get('conf_existed', True) and not restored: change('batocera.conf', None)
            elif conf is not None or restored: change('batocera.conf', record(restored, conf['mode'] if conf else 0o644))
            change(STATE, None); change(LEGACY, None)
        plan = Plan(action, changes, checks,
                    sum(n.startswith(CONFIG) for n in payload), sum(n.startswith(BORDERS) for n in payload),
                    overwritten, from_legacy, skipped, has_originals, removed,
                    replaces if action != 'uninstall' else None)
        self._prepare(plan)
        return plan

    def _journal(self):
        journal = self.read_json(PENDING)
        if journal is None: return None
        if type(journal.get('version')) is not int or journal['version'] != 1 or journal.get('phase') not in ('applying', 'complete'):
            raise OperationError('Unsupported recovery journal')
        if not isinstance(journal.get('changes'), list) or not isinstance(journal.get('created_dirs'), list):
            raise OperationError('Invalid recovery journal')
        names = []
        for entry in journal['changes']:
            if not isinstance(entry, dict) or set(entry) != {'path', 'before', 'after'}: raise OperationError('Invalid recovery target')
            name = entry['path']; self.path(name)
            if name == PENDING or name in names: raise OperationError('Invalid recovery target')
            names.append(name)
            self.validate_record(entry['before']); self.validate_record(entry['after'])
        allowed_dirs = {str(parent.relative_to(self.system)) for name in names
                        for parent in self.path(name).parents if parent != self.system and self.system in parent.parents}
        directories = journal['created_dirs']
        if any(not isinstance(name, str) or name not in allowed_dirs for name in directories):
            raise OperationError('Invalid recovery directory')
        if len(set(directories)) != len(directories): raise OperationError('Duplicate recovery directory')
        return journal

    def pending(self):
        journal = self._journal()
        if journal is None: return None
        return dict(phase=journal['phase'], files=len(journal['changes']),
                    digest=hashlib.sha256(decoded(snapshot(self.path(PENDING), RECOVERY_READ_LIMIT)['data'])).hexdigest())

    def _rollback(self, journal):
        # Validate every owner before restoring even one file.
        for entry in journal['changes']:
            if snapshot(self.path(entry['path'])) not in (entry['before'], entry['after']):
                raise OperationError('Recovery stopped because a managed file changed outside this operation')
        for entry in reversed(journal['changes']): publish(self.path(entry['path']), entry['before'])
        for name in sorted(journal['created_dirs'], key=lambda n: len(Path(n).parts), reverse=True):
            path = self.system / name; safe(path)
            try: path.rmdir()
            except (FileNotFoundError, OSError): pass  # Never remove a nonempty directory.

    def recover(self, digest, progress=lambda count, total: None):
        current = self.pending()
        if current is None or current['digest'] != digest: raise OperationError('Recovery changed after review; review it again')
        journal = self._journal()
        if journal['phase'] == 'complete':
            if any(snapshot(self.path(e['path'])) != e['after'] for e in journal['changes']):
                raise OperationError('Completed operation no longer matches its journal')
        else: self._rollback(journal)
        self.path(PENDING).unlink(); sync_directory(self.path(PENDING).parent)
        progress(1, 1)
        return 'Recovered the interrupted operation.' if journal['phase'] != 'complete' else 'Verified the completed operation.'

    def _prepare(self, plan):
        for entry in plan.changes:
            self.path(entry['path'])
            self.validate_record(entry['before']); self.validate_record(entry['after'])
        created = {str(parent.relative_to(self.system)) for entry in plan.changes
                   for parent in self.path(entry['path']).parents
                   if parent != self.system and self.system in parent.parents and not parent.exists()}
        journal = dict(version=1, phase='applying', action=plan.action, changes=plan.changes,
                       created_dirs=sorted(created))
        data = packed(journal)
        if len(data) > LIMIT:
            raise OperationError('The combined recovery journal exceeds 8 MiB; no files were changed')
        return journal

    def apply(self, plan, progress=lambda count, total: None):
        for name, expected in plan.checks.items():
            if snapshot(self.path(name)) != expected: raise OperationError('Files changed after review; review the operation again')
        journal = self._prepare(plan)
        publish(self.path(PENDING), record(packed(journal), 0o600))
        try:
            for index, entry in enumerate(plan.changes, 1):
                if snapshot(self.path(entry['path'])) != entry['before']:
                    raise OperationError('A managed file changed during application')
                publish(self.path(entry['path']), entry['after'])
                progress(index, len(plan.changes))
            for entry in plan.changes:
                if snapshot(self.path(entry['path'])) != entry['after']: raise OperationError('Final file verification failed')
        except Exception as error:
            try: self._rollback(journal)
            except Exception as recovery:
                raise OperationError('Application stopped; recovery is required. ' + str(recovery)) from error
            self.path(PENDING).unlink(); sync_directory(self.path(PENDING).parent)
            raise OperationError('Application failed; the previous files were restored. ' + str(error)) from error
        # After all outputs verify, a failure to publish/clean the completion
        # receipt leaves recovery information intact. Never begin a rollback
        # while an on-disk receipt may already say the operation is complete.
        journal['phase'] = 'complete'
        publish(self.path(PENDING), record(packed(journal), 0o600))
        self.path(PENDING).unlink(); sync_directory(self.path(PENDING).parent)
        if plan.action == 'uninstall': return 'Uninstall completed and verified.'
        return 'Replacement completed and verified.' if plan.replaces else 'Installation completed and verified.'

    @contextmanager
    def lock(self):
        import fcntl
        path = self.extra / '.scaling_tool_backup/operation.lock'
        safe(path); make_parents(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise OperationError('Unsafe overlays/scaling lock')
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error: raise OperationError('Another overlays/scaling session is active') from error
            yield
        finally: os.close(fd)

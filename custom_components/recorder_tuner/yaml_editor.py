"""Edit `recorder: exclude: entities:` in configuration.yaml without touching the rest.

Only the top-level ``recorder:`` block is parsed and re-emitted (with ruamel.yaml
round-trip, so comments, quotes and ``!secret`` / ``!include`` tags survive).
Every other line of configuration.yaml is copied back byte-for-byte.

If ``recorder:`` points at another file via ``!include``, that file is edited instead.

This module has no Home Assistant imports so it can be unit-tested on its own.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

CONFIG_FILE = "configuration.yaml"
BACKUP_SUFFIX = ".recorder_tuner.bak"
KEEP_BACKUPS = 5

_TOP_KEY_RE = re.compile(r"^([A-Za-z_][\w]*)\s*:(?:\s|$)")


class RecorderYamlError(Exception):
    """Raised when the recorder config cannot be safely edited."""


@dataclass
class RecorderExcludes:
    """What configuration.yaml currently excludes."""

    file: str
    mode: str  # "inline", "include", or "absent"
    entities: list[str] = field(default_factory=list)
    entity_globs: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    has_include_filter: bool = False


@dataclass
class EditResult:
    """Outcome of a write."""

    file: str
    backup: str | None
    original_text: str
    added: list[str]
    removed: list[str]
    added_domains: list[str] = field(default_factory=list)
    removed_domains: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers


def _yaml(indent: tuple[int, int, int] | None = None) -> YAML:
    y = YAML()  # round-trip
    y.preserve_quotes = True
    y.width = 4096
    mapping, sequence, offset = indent or (2, 4, 2)
    y.indent(mapping=mapping, sequence=sequence, offset=offset)
    return y


def _guess_indent(text: str) -> tuple[int, int, int]:
    """Guess (mapping, sequence, offset) indentation from existing YAML text."""
    mapping = None
    seq_offset = None
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        lead = len(line) - len(stripped)
        if mapping is None and lead > 0 and not stripped.startswith("- "):
            mapping = lead
    # Find a "key:" line followed directly by a "- item" line to learn the dash offset.
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for prev, cur in zip(lines, lines[1:]):
        if prev.rstrip().endswith(":") and cur.lstrip().startswith("- "):
            seq_offset = (len(cur) - len(cur.lstrip(" "))) - (len(prev) - len(prev.lstrip(" ")))
            break
    mapping = mapping or 2
    if seq_offset is None:
        seq_offset = 2
    seq_offset = max(seq_offset, 0)
    return mapping, seq_offset + 2, seq_offset


def _find_recorder_block(lines: list[str]) -> tuple[int, int] | None:
    """Return [start, end) line indexes of the top-level recorder block."""
    starts = [i for i, ln in enumerate(lines) if (m := _TOP_KEY_RE.match(ln)) and m.group(1) == "recorder"]
    if not starts:
        return None
    if len(starts) > 1:
        raise RecorderYamlError(
            "configuration.yaml has more than one top-level 'recorder:' key; fix that first"
        )
    start = starts[0]
    end = start + 1
    while end < len(lines):
        ln = lines[end]
        if ln.strip() == "" or ln[0] in " \t#":
            end += 1
            continue
        break
    # Blank lines and column-0 comments at the tail belong to whatever follows.
    while end > start + 1 and (lines[end - 1].strip() == "" or lines[end - 1].startswith("#")):
        end -= 1
    return start, end


def _tag_of(node: Any) -> str | None:
    tag = getattr(node, "tag", None)
    if tag is None:
        return None
    value = getattr(tag, "value", tag)
    return str(value) if value else None


def _as_list(node: Any) -> list[str]:
    if node is None:
        return []
    if isinstance(node, str):
        return [node]
    if isinstance(node, (list, tuple)):
        return [str(x) for x in node]
    return []


def _dump(y: YAML, data: Any) -> str:
    buf = io.StringIO()
    y.dump(data, buf)
    out = buf.getvalue()
    return out if out.endswith("\n") else out + "\n"


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _backup(path: Path) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.{stamp}{BACKUP_SUFFIX}")
    shutil.copy2(path, dest)
    olds = sorted(path.parent.glob(f"{path.name}.*{BACKUP_SUFFIX}"))
    for old in olds[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
    return str(dest)


# --------------------------------------------------------------------------- locate


@dataclass
class _Located:
    path: Path              # file that holds the recorder mapping
    text: str               # full text of that file
    mode: str               # inline / include / absent
    block: tuple[int, int] | None  # line range inside configuration.yaml (inline mode)
    recorder: Any           # parsed recorder value (mapping or None)
    yaml: YAML
    root: Any               # parsed doc that gets dumped back


def _locate(config_dir: str) -> _Located:
    cfg = Path(config_dir) / CONFIG_FILE
    text = cfg.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    block = _find_recorder_block(lines)
    if block is None:
        y = _yaml(_guess_indent(text))
        return _Located(cfg, text, "absent", None, None, y, None)

    block_text = "".join(lines[block[0]:block[1]])
    y = _yaml(_guess_indent(block_text if block[1] - block[0] > 2 else text))
    root = y.load(block_text)
    value = root.get("recorder") if isinstance(root, dict) else None
    tag = _tag_of(value)

    if tag and tag.startswith("!include"):
        if tag != "!include":
            raise RecorderYamlError(f"recorder uses {tag}; only a plain !include file is supported")
        inc = (cfg.parent / str(value.value)).resolve()
        if not inc.is_file():
            raise RecorderYamlError(f"recorder include file not found: {inc}")
        inc_text = inc.read_text(encoding="utf-8")
        y2 = _yaml(_guess_indent(inc_text))
        inc_root = y2.load(inc_text) if inc_text.strip() else None
        if inc_root is not None and not isinstance(inc_root, dict):
            raise RecorderYamlError(f"{inc.name} is not a YAML mapping")
        return _Located(inc, inc_text, "include", None, inc_root, y2, inc_root)

    if tag:
        raise RecorderYamlError(f"recorder uses an unsupported tag ({tag})")
    if value is not None and not isinstance(value, dict):
        raise RecorderYamlError("recorder: is not a mapping")
    return _Located(cfg, text, "inline", block, value, y, root)


# --------------------------------------------------------------------------- public API


def read_excludes(config_dir: str) -> RecorderExcludes:
    """Return the current recorder exclude/include settings from YAML."""
    loc = _locate(config_dir)
    res = RecorderExcludes(file=str(loc.path), mode=loc.mode)
    rec = loc.recorder
    if not isinstance(rec, dict):
        return res
    exclude = rec.get("exclude")
    if isinstance(exclude, dict):
        res.entities = _as_list(exclude.get("entities"))
        res.entity_globs = _as_list(exclude.get("entity_globs"))
        res.domains = _as_list(exclude.get("domains"))
    res.has_include_filter = isinstance(rec.get("include"), dict) and bool(rec.get("include"))
    return res


def _edit_list(exclude: Any, key: str, add: set[str], remove: set[str]) -> tuple[list[str], list[str]]:
    """Add/remove values in exclude[key] (a YAML list), keeping order and comments."""
    seq = exclude.get(key)
    if seq is None:
        seq = CommentedSeq()
    elif isinstance(seq, str):
        seq = CommentedSeq([seq])
    elif not isinstance(seq, list):
        raise RecorderYamlError(f"recorder.exclude.{key} is not a list")

    removed = [str(v) for v in seq if str(v) in remove]
    for idx in range(len(seq) - 1, -1, -1):
        if str(seq[idx]) in remove:
            del seq[idx]
    present = {str(v) for v in seq}
    added = sorted(add - present)
    for val in added:
        seq.append(val)

    if len(seq):
        exclude[key] = seq
    elif key in exclude:
        del exclude[key]
    return added, removed


def _clean(values: list[str] | tuple[str, ...] | None) -> set[str]:
    return {v.strip() for v in (values or []) if v and v.strip()}


def apply_changes(
    config_dir: str,
    add: list[str],
    remove: list[str],
    add_domains: list[str] | None = None,
    remove_domains: list[str] | None = None,
) -> EditResult:
    """Add/remove entity IDs and domains in recorder.exclude and write the file."""
    add_set = _clean(add)
    remove_set = _clean(remove) - add_set
    add_dom = _clean(add_domains)
    remove_dom = _clean(remove_domains) - add_dom

    loc = _locate(config_dir)
    rec = loc.recorder
    if rec is None:
        rec = CommentedMap()

    exclude = rec.get("exclude")
    if exclude is None:
        exclude = CommentedMap()
    elif not isinstance(exclude, dict):
        raise RecorderYamlError("recorder.exclude is not a mapping")

    added, removed = _edit_list(exclude, "entities", add_set, remove_set)
    added_d, removed_d = _edit_list(exclude, "domains", add_dom, remove_dom)

    if not (added or removed or added_d or removed_d):
        return EditResult(str(loc.path), None, loc.text, [], [])

    if len(exclude):
        rec["exclude"] = exclude
    elif "exclude" in rec:
        del rec["exclude"]

    y = loc.yaml
    if loc.mode == "include":
        new_text = _dump(y, rec) if len(rec) else "{}\n"
    else:
        if loc.mode == "inline":
            loc.root["recorder"] = rec if len(rec) else None
            new_block = _dump(y, loc.root)
        else:
            new_block = _dump(y, CommentedMap(recorder=rec))
        if loc.mode == "inline":
            lines = loc.text.splitlines(keepends=True)
            start, end = loc.block  # type: ignore[misc]
            # Keep the original line ending style of the block's last line.
            new_text = "".join(lines[:start]) + new_block + "".join(lines[end:])
        else:  # absent -> append
            sep = "" if loc.text.endswith("\n") or not loc.text else "\n"
            new_text = loc.text + sep + "\n" + new_block

    backup = _backup(loc.path)
    _atomic_write(loc.path, new_text)
    return EditResult(str(loc.path), backup, loc.text, added, removed, added_d, removed_d)


def restore(path: str, text: str) -> None:
    """Put a file back to the exact text it had before apply_changes."""
    _atomic_write(Path(path), text)

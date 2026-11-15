import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from common.config import get_path
from common.utils import read_json, write_json


def _parse_locator_file(path: Path) -> Tuple[List[str], Dict[str, List[Dict[str, str]]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    order: List[str] = []
    groups: Dict[str, List[Dict[str, str]]] = {}
    pending_comments: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            pending_comments.append(stripped.lstrip("#").strip())
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$', stripped)
        if not m:
            pending_comments = []
            continue
        name, expr = m.group(1), m.group(2).strip()
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append({"expression": expr, "comment": " ".join(pending_comments).strip()})
        pending_comments = []
    return order, groups


def load_locator_groups(config: Dict[str, Any], platform: str) -> Tuple[List[str], Dict[str, List[Dict[str, str]]]]:
    path = get_path(config, "locators_dir") / f"{platform}_locators.txt"
    return _parse_locator_file(path)


def _write_locator_file(path: Path, groups: Dict[str, List[Dict[str, str]]], order: List[str]) -> None:
    out: List[str] = []
    for name in order:
        entries = groups.get(name, [])
        if not entries:
            continue
        comment = (entries[0].get("comment") or "").strip()
        if comment:
            out.append(f"# {comment}")
        for entry in entries:
            out.append(f"{name} = {entry['expression']}")
        out.append("")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _read_changed(config: Dict[str, Any]) -> List[Dict[str, str]]:
    path = get_path(config, "changed_locators_file")
    data = read_json(path, [])
    if not isinstance(data, list):
        return []
    cleaned = []
    for item in data:
        if isinstance(item, dict):
            broke = str(item.get("broke_locator", "")).strip()
            suggested = str(item.get("suggested_locator", "")).strip()
            if broke and suggested:
                cleaned.append({"broke_locator": broke, "suggested_locator": suggested})
    return cleaned


def _write_changed(config: Dict[str, Any], entries: List[Dict[str, str]]) -> None:
    write_json(get_path(config, "changed_locators_file"), entries)


def _is_invalid_suggested_locator(suggested: str) -> bool:
    s = suggested.strip()
    if not s:
        return True
    if s.startswith("#"):
        return True
    # reject raw id selectors wrapped as plain # usage suggestion cases
    lowered = s.lower()
    if "locator('#" in lowered or "find_element('#" in lowered:
        # allow if it's not just an id selector? spec says "do not suggest locator in changed_locators.json if it starts with #"
        # interpret strictly for values beginning with # and common bad form
        pass
    return False


def record_changed_locator(config: Dict[str, Any], broke_locator: str, suggested_locator: str) -> None:
    broke = (broke_locator or "").strip()
    suggested = (suggested_locator or "").strip()
    if not broke or not suggested:
        return
    if suggested.startswith("#"):
        return
    # reject if it starts with # inside common page.locator('#...') only when selector itself starts with # as primary bad id? 
    # spec example: "suggested_locator": "page.locator('#radix-_r_8o_')" -> do not suggest
    if re.search(r"page\.locator\(\s*['\"]#", suggested):
        return
    if re.search(r"locator\(\s*['\"]#", suggested) and suggested.count("locator(") == 1 and suggested.strip().startswith("page.locator("):
        return
    changed = _read_changed(config)
    for item in changed:
        if item["broke_locator"] == broke:
            item["suggested_locator"] = suggested
            _write_changed(config, changed)
            return
    changed.append({"broke_locator": broke, "suggested_locator": suggested})
    _write_changed(config, changed)


def apply_auto_correct_if_enabled(config: Dict[str, Any]) -> None:
    changed = _read_changed(config)
    locators_dir = get_path(config, "locators_dir")
    locator_files = sorted(locators_dir.glob("*_locators.txt"))
    if not changed:
        _write_changed(config, [])
        return

    auto_correct = bool(config.get("auto_correct_locator", False))
    if auto_correct:
        remaining: List[Dict[str, str]] = []
        for item in changed:
            broke = item["broke_locator"].strip()
            suggested = item["suggested_locator"].strip()
            if suggested.startswith("#") or re.search(r"page\.locator\(\s*['\"]#", suggested):
                remaining.append(item)
                continue
            applied_or_found = False
            for lf in locator_files:
                order, groups = _parse_locator_file(lf)
                file_changed = False
                for name in order:
                    entries = groups.get(name, [])
                    exprs = [e["expression"].strip() for e in entries]
                    if broke in exprs:
                        applied_or_found = True
                        if suggested not in exprs:
                            idx = exprs.index(broke)
                            entries.insert(idx + 1, {"expression": suggested, "comment": entries[idx].get("comment", "")})
                            groups[name] = entries
                            file_changed = True
                        break
                if file_changed:
                    _write_locator_file(lf, groups, order)
            if not applied_or_found:
                remaining.append(item)
        _write_changed(config, remaining)
        return

    # auto_correct off: remove entry if suggested already present with broke in same group
    remaining = []
    for item in changed:
        broke = item["broke_locator"].strip()
        suggested = item["suggested_locator"].strip()
        remove = False
        for lf in locator_files:
            _, groups = _parse_locator_file(lf)
            for entries in groups.values():
                exprs = [e["expression"].strip() for e in entries]
                if broke in exprs and suggested in exprs:
                    remove = True
                    break
            if remove:
                break
        if not remove:
            remaining.append(item)
    _write_changed(config, remaining)

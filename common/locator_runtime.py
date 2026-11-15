from typing import Any, Callable, Dict, List, Optional, Tuple

from common.locators import load_locator_groups
from common.vision import vision_recover


def _eval_expression(expr: str, context: Dict[str, Any]) -> Any:
    safe_builtins = {
        "__import__": __import__,
        "len": len,
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "list": list,
        "dict": dict,
        "tuple": tuple,
        "set": set,
        "enumerate": enumerate,
        "range": range,
        "map": map,
        "filter": filter,
        "print": print,
        "isinstance": isinstance,
        "getattr": getattr,
    }
    return eval(expr, {"__builtins__": safe_builtins}, dict(context))


def get_locator_candidates(config: Dict[str, Any], platform: str, locator_name: str, context: Dict[str, Any]) -> List[Tuple[str, Any]]:
    _, groups = load_locator_groups(config, platform)
    entries = groups.get(locator_name, [])
    candidates: List[Tuple[str, Any]] = []
    for entry in entries:
        expr = entry["expression"]
        try:
            obj = _eval_expression(expr, context)
            candidates.append((expr, obj))
        except Exception:
            continue
    return candidates


def _check_playwright(obj: Any) -> bool:
    try:
        if hasattr(obj, "count"):
            try:
                c = obj.count()
                if c == 0:
                    return False
            except Exception:
                pass
        target = obj.first if hasattr(obj, "first") else obj
        if hasattr(target, "wait_for"):
            try:
                target.wait_for(state="attached", timeout=1000)
                return True
            except Exception:
                pass
        if hasattr(obj, "count"):
            try:
                return obj.count() > 0
            except Exception:
                return False
        return target is not None
    except Exception:
        return False


def resolve_locator(config: Dict[str, Any], platform: str, locator_name: str, context: Dict[str, Any]) -> Tuple[Optional[str], Optional[Any], List[str]]:
    candidates = get_locator_candidates(config, platform, locator_name, context)
    exprs = [e for e, _ in candidates]
    if not candidates:
        return None, None, exprs

    for expr, obj in candidates:
        if _check_playwright(obj):
            return expr, obj, exprs

    return candidates[0][0], candidates[0][1], exprs


def _pw_target(obj: Any) -> Any:
    return obj.first if hasattr(obj, "first") else obj


def _apply_vision_fallback(config: Dict[str, Any], platform: str, locator_name: str, failed_exprs: List[str], context: Dict[str, Any], intent: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    page = context.get("page")
    if page is None:
        return {"action": "none", "reason": "No page in context"}
    return vision_recover(config, platform, locator_name, failed_exprs, page, intent, extra=extra)


def _try_vision_action(result: Dict[str, Any], context: Dict[str, Any], value: Optional[str] = None) -> Any:
    action = (result.get("action") or "none").lower()
    coords = result.get("coordinates") or {}
    page = context.get("page")
    if page is None:
        return None
    try:
        if action == "click" and isinstance(coords, dict) and "x" in coords and "y" in coords:
            page.mouse.click(float(coords["x"]), float(coords["y"]))
            return True
        if action == "type" and isinstance(coords, dict) and "x" in coords and "y" in coords:
            page.mouse.click(float(coords["x"]), float(coords["y"]))
            page.keyboard.insert_text(value if value is not None else str(result.get("value", "")))
            return True
        if action == "scroll":
            if isinstance(coords, dict) and "x" in coords and "y" in coords:
                page.mouse.wheel(0, float(coords.get("y", 300)))
            else:
                page.mouse.wheel(0, 400)
            return True
    except Exception:
        return None
    return None


def run_with_locator(config: Dict[str, Any], platform: str, locator_name: str, context: Dict[str, Any], intent: str, action_fn: Callable[[Any], Any], allow_vision: bool = True, vision_value: Optional[str] = None) -> Any:
    candidates = get_locator_candidates(config, platform, locator_name, context)
    exprs = [e for e, _ in candidates]
    _, selected_obj, _ = resolve_locator(config, platform, locator_name, context)
    ordered: List[Tuple[str, Any]] = []
    if selected_obj is not None:
        for e, o in candidates:
            if o is selected_obj:
                ordered.append((e, o))
                break
    for e, o in candidates:
        if not any(id(o) == id(xo) and e == xe for xe, xo in ordered):
            ordered.append((e, o))
    if not ordered:
        ordered = candidates

    errors = []
    for e, o in ordered:
        try:
            return action_fn(o)
        except Exception as ex:
            errors.append(f"{e} :: {ex}")

    if allow_vision:
        vision_result = _apply_vision_fallback(config, platform, locator_name, exprs or [e for e, _ in ordered], context, intent, extra={"errors": errors[-5:]})
        acted = _try_vision_action(vision_result, context, value=vision_value)
        if acted is not None:
            return acted
    raise RuntimeError(f"All locators failed for {platform}:{locator_name}. Vision attempted for current run.")


def try_with_locator(config: Dict[str, Any], platform: str, locator_name: str, context: Dict[str, Any], intent: str, action_fn: Callable[[Any], Any], allow_vision: bool = True, vision_value: Optional[str] = None) -> Tuple[bool, Any]:
    try:
        result = run_with_locator(config, platform, locator_name, context, intent, action_fn, allow_vision=allow_vision, vision_value=vision_value)
        return True, result
    except Exception as e:
        return False, e

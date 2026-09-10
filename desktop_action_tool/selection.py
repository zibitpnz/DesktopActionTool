"""Exact window and control selectors, independent of Windows APIs."""
from .action_runtime import ActionError


def filter_windows(windows, title=None, process_name=None):
    return [window for window in windows
            if (title is None or window["title"] == title)
            and (process_name is None or window["process_name"].casefold() == process_name.casefold())]


def require_unique(candidates, kind):
    if len(candidates) == 1:
        return candidates[0]
    error = ActionError(kind + ("_NOT_FOUND" if not candidates else "_AMBIGUOUS"),
                        "no matching " + kind.lower() if not candidates else "multiple matching " + kind.lower() + "s",
                        "refine the selector and repeat the command")
    error.candidates = candidates
    raise error


def filter_controls(metadata, name=None, automation_id=None, ready_only=False):
    controls = [control for control in metadata["controls"]
                if (name is None or control["name"] == name)
                and (automation_id is None or control["automation_id"] == automation_id)
                and (not ready_only or (control["is_enabled"] and not control["is_offscreen"]
                     and control.get("clipped_window_rect") is not None))]
    return {**metadata, "controls": controls, "returned_count": len(controls),
            "name": name, "automation_id": automation_id}


def compact_control_description(control: dict[str, object]) -> str:
    return (
        f"id={control.get('id')}, name={control.get('name')!r}, "
        f"automation_id={control.get('automation_id')!r}, "
        f"control_type={control.get('control_type')!r}"
    )


def select_uia_control(
    controls_metadata: dict[str, object],
    control_id: int | None,
    automation_id: str | None,
    name: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    controls = controls_metadata.get("controls")
    if not isinstance(controls, list):
        raise ValueError("UIA controls metadata is invalid")
    if controls_metadata.get("truncated") and control_id is None:
        raise ActionError("CONTROL_SEARCH_TRUNCATED", "cannot prove selector uniqueness in a truncated control list",
                          "narrow --uia-control-types or increase --controls-limit")

    selector: dict[str, object]
    matches: list[dict[str, object]]
    if control_id is not None:
        selector = {"type": "control_id", "value": control_id}
        matches = [
            control
            for control in controls
            if isinstance(control, dict) and int(control.get("id", -1)) == control_id
        ]
    elif automation_id is not None:
        selector = {"type": "automation_id", "value": automation_id}
        matches = [
            control
            for control in controls
            if isinstance(control, dict)
            and str(control.get("automation_id", "")) == automation_id
        ]
    elif name is not None:
        selector = {"type": "name", "value": name}
        matches = [
            control
            for control in controls
            if isinstance(control, dict) and str(control.get("name", "")) == name
        ]
    else:
        raise ValueError("UIA highlight selector was not provided")

    if not matches:
        raise ValueError(
            "UIA control highlight found no matches for "
            f"{selector['type']}={selector['value']!r}; try --uia-list-controls first"
        )
    if len(matches) > 1:
        candidates = "; ".join(compact_control_description(control) for control in matches[:8])
        extra = "" if len(matches) <= 8 else f"; ... and {len(matches) - 8} more"
        raise ValueError(
            "UIA control highlight selector is ambiguous; use "
            f"--uia-highlight-control-id or --uia-highlight-automation-id. Candidates: "
            f"{candidates}{extra}"
        )
    selected = matches[0]
    if selected.get("is_offscreen") or not selected.get("is_enabled") or selected.get("clipped_window_rect") is None:
        raise ActionError("CONTROL_NOT_READY", "selected control is not visible and enabled")
    return selected, selector

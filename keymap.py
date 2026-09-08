"""Supported hotkey names and deterministic modifier ordering."""
MODIFIERS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
KEYS = {"enter": 0x0D, "tab": 0x09, "escape": 0x1B, "space": 0x20,
        "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "home": 0x24,
        "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "left": 0x25,
        "up": 0x26, "right": 0x27, "down": 0x28}
KEYS.update({chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)})
KEYS.update({str(number): ord(str(number)) for number in range(10)})
KEYS.update({"f" + str(number): 0x6F + number for number in range(1, 25)})
EXTENDED = {"win", "delete", "insert", "home", "end", "pageup", "pagedown", "left", "up", "right", "down"}


def parse_hotkey(value):
    parts = [part.strip().casefold() for part in value.split("+")]
    if len(set(parts)) != len(parts) or any(not part for part in parts):
        raise ValueError("hotkey contains an empty or repeated key")
    if parts[-1] not in KEYS or any(part not in MODIFIERS for part in parts[:-1]):
        raise ValueError("hotkey requires supported modifiers followed by one key; see README")
    names = [name for name in MODIFIERS if name in parts[:-1]] + [parts[-1]]
    return [{"name": name, "vk": MODIFIERS[name] if name in MODIFIERS else KEYS[name],
             "extended": name in EXTENDED} for name in names]

"""Read-only startup gate during an explicit update or incomplete recovery."""
import json
import os
from pathlib import Path
import sys


def startup_allowed(root=None, *, mcp=False):
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    marker = root / '.tools/updater/active.json'
    try:
        # lexists also treats a broken link as requiring attention.
        if not os.path.lexists(marker):
            return True
        if marker.is_file() and not marker.is_symlink() and marker.stat().st_size <= 4096:
            record = json.loads(marker.read_text(encoding='utf-8'))
            token = os.environ.get('DESKTOPACTION_MAINTENANCE_TOKEN')
            if token and len(token) == 32 and token == record.get('token'):
                return True  # Only inherited by the updater's own diagnostic children.
    except (OSError, ValueError, TypeError):
        pass  # Corrupt/inaccessible state must not allow partially replaced code to run.
    result = {'ok': False, 'error_code': 'MAINTENANCE_REQUIRED',
              'error': 'An update or recovery is in progress. Wait for the updater; if it stopped, run update.bat -Status and -Rollback.'}
    print(json.dumps(result), file=sys.stderr if mcp else sys.stdout)
    return False

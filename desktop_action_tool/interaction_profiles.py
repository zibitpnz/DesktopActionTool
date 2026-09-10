"""Session-scoped interaction policy shared by CLI and MCP subprocesses."""
from .action_runtime import ActionError

PROFILES = ('human', 'uia_visual', 'background')


def state_profile(state):
    """Version 2 prevents older executors from silently ignoring a profile."""
    if state is None:
        return None
    profile = state.get('profile')
    version = state.get('version', 1)
    if ((version == 1 and profile is not None)
            or (version == 2 and profile not in PROFILES)
            or version not in (1, 2)):
        raise ActionError('INDICATOR_STATE_INVALID', 'invalid session profile or state version',
                          'inspect the session state; do not bypass its policy')
    return profile


def violation(profile, reason):
    raise ActionError('PROFILE_VIOLATION', f'{profile}: {reason}',
                      'keep the selected profile; if the task requires another mode, ask the user before ending this session and starting another')


def prepare(args, session):
    """Apply the policy before argument validation, without performing input."""
    requested = getattr(args, 'profile', None)
    if requested is not None and not args.session_start:
        raise ActionError('PROFILE_REQUIRES_SESSION_START', '--profile is only valid with --session-start',
                          'start a bound session with the desired profile')
    args.profile_session_state = session
    args.interaction_profile = requested if args.session_start else state_profile(session)
    profile = args.interaction_profile
    if profile is None:
        return
    if profile not in PROFILES:
        violation(profile, 'unknown profile')
    if profile == 'background':
        # User keystrokes in another application must not cancel background work.
        args.no_abort_key = True
        args.activity_frame = False
    elif profile in ('human', 'uia_visual'):
        args.activity_frame = True

    pointer = any(getattr(args, k) is not None for k in
                  ('mouse_move', 'mouse_move_relative', 'click', 'double_click', 'drag_to', 'scroll_ticks'))
    keyboard = any(getattr(args, k) for k in
                   ('stdin', 'select_all', 'press_enter', 'press_delete', 'press_backspace')) or any(
                       getattr(args, k) is not None for k in ('text', 'text_file', 'hotkey'))
    window_change = args.focus_only or args.minimize_window or args.resize_window is not None or args.set_window_rect is not None

    if profile in ('background', 'uia_visual') and (pointer or keyboard):
        violation(profile, 'simulated mouse and keyboard input is disabled; use direct UIA actions')
    if profile == 'background' and window_change:
        violation(profile, 'focusing, moving, resizing and minimizing windows is disabled')
    if profile == 'human':
        if args.uia_action:
            violation(profile, 'direct UIA actions are disabled; UIA may be used to read controls')
        if args.scroll_jitter:
            violation(profile, 'scroll jitter uses instant cursor shifts; omit it in human mode')
        moves = any(getattr(args, k) is not None for k in ('mouse_move', 'mouse_move_relative', 'drag_to'))
        if moves:
            if getattr(args, 'profile_requested_smooth', None) is False:
                violation(profile, 'instant cursor movement is disabled')
            if args.min_mouse_move_duration_ms <= 0:
                violation(profile, 'smooth movement requires a positive minimum duration')
            args.smooth_move = True
        if (args.text is not None or args.text_file is not None or args.stdin) and args.min_delay_ms <= 0:
            violation(profile, 'typing requires a positive delay between characters')
    elif args.uia_action:
        follow = profile == 'uia_visual'
        explicit = getattr(args, 'profile_requested_cursor', None)
        if explicit is not None and explicit != follow:
            violation(profile, 'cursor_follow contradicts the session profile')
        args.uia_cursor_follow = follow


def check_binding(args, state):
    """Recheck the persisted policy inside the session lock before dispatch."""
    expected = getattr(args, 'interaction_profile', None)
    actual = state_profile(state) if state and state.get('persistent') else None
    if expected != actual:
        raise ActionError('PROFILE_CHANGED', 'session profile changed before dispatch',
                          'inspect session_status; do not retry under a different profile automatically')


def check_input(profile, kind):
    """Independent native-input boundary; UIA pointer guards cannot replace it."""
    if profile == 'background' or profile == 'uia_visual' and kind != 'move':
        violation(profile, f'native {kind} input is disabled')


def describe(profile, args=None):
    return {'profile': profile,
            'enforced': profile is not None,
            'shared_input': profile != 'background',
            'escape_cancels': profile != 'background' and not getattr(args, 'no_abort_key', False)}

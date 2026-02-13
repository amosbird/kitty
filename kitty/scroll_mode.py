#!/usr/bin/env python3

import re
from enum import Enum, auto
from typing import TYPE_CHECKING, List, Optional, Tuple

from .fast_data_types import (
    GLFW_FKEY_BACKSPACE,
    GLFW_FKEY_DOWN,
    GLFW_FKEY_ENTER,
    GLFW_FKEY_ESCAPE,
    GLFW_FKEY_KP_ENTER,
    GLFW_FKEY_LEFT,
    GLFW_FKEY_PAGE_DOWN,
    GLFW_FKEY_PAGE_UP,
    GLFW_FKEY_RIGHT,
    GLFW_FKEY_UP,
    GLFW_MOD_ALT,
    GLFW_MOD_CONTROL,
    GLFW_MOD_SHIFT,
    GLFW_MOUSE_BUTTON_LEFT,
    GLFW_PRESS,
    GLFW_REPEAT,
    KeyEvent,
    SCROLL_FULL,
    SCROLL_LINE,
    SCROLL_PAGE,
    get_boss,
    get_options,
)
from .marks import marker_from_regex

if TYPE_CHECKING:
    from .tabs import TabManager
    from .window import Window


class ScrollModeState(Enum):
    NAVIGATE = auto()
    SEARCH = auto()
    SELECT = auto()


def _get_tab_manager(window: 'Window') -> Optional['TabManager']:
    boss = get_boss()
    for tm in boss.os_window_map.values():
        for tab in tm.tabs:
            for w in tab:
                if w.id == window.id:
                    return tm
    return None


# Prompt patterns for detecting shell prompts via regex {{{
# Used as fallback when OSC 133 shell integration is not available.
# Matches common prompt-ending characters and special prompt symbols.
_PROMPT_PATTERN = re.compile(
    r'❯'                         # starship, pure, spaceship
    r'|➜'                        # robbyrussell (oh-my-zsh default)
    r'|⟩'                        # some minimal prompts
    r'|λ'                        # haskell-style prompts
    r'|:\)[\s\x00]*$'           # smiley prompt: "hostname :) "
    r'|[\$#%>][\s\x00]*$'      # line ending with $ # % > (common prompt endings)
    r'|>>>\s'                    # python REPL
    r'|In\s*\[\d+\]'            # IPython/Jupyter
)
# }}}


class ScrollMode:
    """Native scroll mode for navigating the scrollback buffer.

    Provides tmux copy-mode style navigation with vim keybindings,
    incremental search, visual selection (char/line/block), prompt
    jumping, and a powerline-style status bar in the tab bar area.
    """

    def __init__(self) -> None:
        self.active = False
        self.state = ScrollModeState.NAVIGATE
        self._window: Optional['Window'] = None
        self._tab_manager: Optional['TabManager'] = None
        self._is_alt_screen = False
        # Cursor position: absolute line from top of buffer (0 = oldest history line)
        self._cursor_abs = 0
        self._cursor_x = 0
        # Search state
        self._search_query = ''
        self._search_backwards = True
        # Selection state
        self._sel_start_abs = 0
        self._sel_start_x = 0
        self._sel_mode: Optional[str] = None  # 'char', 'line', 'block'
        self._sel_prev_mode: Optional[str] = None
        # Mouse drag state
        self._drag_active = False
        self._drag_started = False
        self._drag_press_y = 0
        self._drag_press_x = 0

    # Properties {{{

    @property
    def _total_lines(self) -> int:
        if self._window is None:
            return 0
        screen = self._window.screen
        if self._is_alt_screen:
            return screen.lines
        return screen.historybuf.count + screen.lines

    def _viewport_top(self) -> int:
        """Return the absolute line index at the top of the visible viewport."""
        if self._window is None:
            return 0
        screen = self._window.screen
        if self._is_alt_screen:
            return 0
        return screen.historybuf.count - screen.scrolled_by

    # }}}

    # Entry/exit {{{

    def enter(self, window: 'Window', silent: bool = False) -> None:
        """Activate scroll mode for the given window.

        If silent=True, silently abort when tab bar is not visible (for
        mouse auto-enter).  Otherwise show an error message.
        """
        self._tab_manager = _get_tab_manager(window)
        # Scroll mode requires the tab bar for its status display
        if self._tab_manager is None or self._tab_manager.tab_bar_hidden or not self._tab_manager.tab_bar_should_be_visible:
            if not silent:
                get_boss().show_error('Scroll mode unavailable', 'Scroll mode requires the tab bar to be visible (set tab_bar_min_tabs 1).')
            self._tab_manager = None
            return
        self._window = window
        self.active = True
        self.state = ScrollModeState.NAVIGATE
        self._search_query = ''
        self._sel_mode = None
        screen = window.screen
        self._is_alt_screen = not screen.is_main_linebuf()
        # Pause child output: buffer raw bytes instead of parsing them,
        # so the scrollback content stays stable while browsing.
        screen.set_scroll_pause(True)
        # Clear any normal (non-scroll-mode) selection that was in progress
        screen.clear_selection()
        if self._is_alt_screen:
            # Alt screen (vim/less): cursor at application cursor position
            self._cursor_abs = screen.cursor.y
            self._cursor_x = screen.cursor.x
        else:
            # Main screen: cursor at the terminal cursor's line
            self._cursor_abs = screen.historybuf.count + screen.cursor.y
            self._cursor_x = screen.cursor.x
        self._sync_cursor()
        if self._tab_manager is not None:
            self._tab_manager.mark_tab_bar_dirty()

    def exit(self) -> None:
        """Deactivate scroll mode and restore normal terminal state."""
        if self._window is not None:
            screen = self._window.screen
            screen.set_scroll_cursor(0, 0, 0)
            screen.set_scroll_selection(0, 0, 0, 0, 0)
            self._clear_search_marker()
            # Flush buffered output, then unpause
            screen.flush_scroll_pending()
            screen.set_scroll_pause(False)
            if not self._is_alt_screen:
                screen.scroll(SCROLL_FULL, False)
            self._is_alt_screen = False
        if self._tab_manager is not None:
            self._tab_manager.update_tab_bar_data()
            self._tab_manager.mark_tab_bar_dirty()
        self.active = False
        self._sel_mode = None
        self._window = None
        self._tab_manager = None
        # Wake up IO loop so it resumes reading from child PTY
        get_boss().child_monitor.wakeup()

    def enter_search(self, window: 'Window') -> None:
        """Enter scroll mode directly in search state."""
        self.enter(window)
        if self.active:
            self._search_backwards = True
            self._search_query = ''
            self.state = ScrollModeState.SEARCH
            self._mark_dirty()

    def enter_prompt_jump(self, window: 'Window') -> None:
        """Enter scroll mode and jump to previous prompt.

        If no prompt is found above the terminal cursor, scroll mode
        is not activated.  Not available on alternate screen.
        """
        screen = window.screen
        if not screen.is_main_linebuf():
            return  # no prompt history on alt screen
        real_cursor_abs = screen.historybuf.count + screen.cursor.y
        found_line = -1
        self._window = window  # temporarily set for _get_line_text
        for abs_line in range(real_cursor_abs - 1, -1, -1):
            try:
                text = self._get_line_text(abs_line)
                if _PROMPT_PATTERN.search(text):
                    found_line = abs_line
                    break
            except (IndexError, Exception):
                continue
        self._window = None
        if found_line < 0:
            return
        self.enter(window)
        if self.active:
            self._move_cursor_to(found_line, 0)

    # }}}

    # Cursor and viewport management {{{

    def _mark_dirty(self) -> None:
        """Request tab bar redraw to reflect updated state."""
        if self._tab_manager is not None:
            self._tab_manager.mark_tab_bar_dirty()

    def _sync_cursor(self) -> None:
        """Push cursor position and selection bounds to the C rendering layer."""
        if self._window is None:
            return
        screen = self._window.screen
        vy = self._cursor_abs - self._viewport_top()
        vy = max(0, min(vy, screen.lines - 1))
        # Determine cursor width (2 for wide chars)
        cw = 1
        try:
            text = self._get_line_text(self._cursor_abs)
            ci = self._cell_to_char_idx(text, self._cursor_x)
            if ci < len(text):
                cw = self._char_width(text[ci])
        except (IndexError, Exception):
            pass
        screen.set_scroll_cursor(self._cursor_x, vy, 1, cw)
        self._sync_selection()

    def _sync_selection(self) -> None:
        """Push selection highlight bounds to the C rendering layer."""
        if self._window is None:
            return
        screen = self._window.screen
        if self._sel_mode is None or self.state != ScrollModeState.SELECT:
            screen.set_scroll_selection(0, 0, 0, 0, 0)
            return

        vt = self._viewport_top()
        num_lines = screen.lines

        # Normalize: start is always the earlier position
        if self._sel_start_abs < self._cursor_abs or (
            self._sel_start_abs == self._cursor_abs and self._sel_start_x <= self._cursor_x
        ):
            start_abs, start_x = self._sel_start_abs, self._sel_start_x
            end_abs, end_x = self._cursor_abs, self._cursor_x
        else:
            start_abs, start_x = self._cursor_abs, self._cursor_x
            end_abs, end_x = self._sel_start_abs, self._sel_start_x

        start_vy = start_abs - vt
        end_vy = end_abs - vt

        # Entirely off-screen
        if end_vy < 0 or start_vy >= num_lines:
            screen.set_scroll_selection(0, 0, 0, 0, 0)
            return

        # Clamp to visible viewport
        if start_vy < 0:
            start_vy, start_x = 0, 0
        if end_vy >= num_lines:
            end_vy, end_x = num_lines - 1, screen.columns - 1

        sel_type = 2 if self._sel_mode == 'line' else (3 if self._sel_mode == 'block' else 1)
        if sel_type == 2:  # line: full width
            start_x, end_x = 0, screen.columns - 1
        elif sel_type == 3:  # block: pass raw coords, C handles min/max
            start_x = self._sel_start_x
            end_x = self._cursor_x
        elif sel_type == 1:  # char: extend end_x to cover full wide char
            try:
                end_line = self._get_line_text(end_abs)
                ci = self._cell_to_char_idx(end_line, end_x)
                if ci < len(end_line) and self._char_width(end_line[ci]) == 2:
                    end_x += 1
            except (IndexError, Exception):
                pass
        screen.set_scroll_selection(sel_type, start_x, start_vy, end_x, end_vy)

    def _ensure_cursor_visible(self) -> None:
        """Scroll the viewport so the cursor remains on screen."""
        if self._window is None:
            return
        screen = self._window.screen
        num_lines = screen.lines
        total = self._total_lines
        self._cursor_abs = max(0, min(self._cursor_abs, total - 1))
        self._cursor_x = max(0, min(self._cursor_x, screen.columns - 1))

        if self._is_alt_screen:
            # Alt screen: no scrolling, viewport is fixed
            return

        vt = self._viewport_top()
        vb = vt + num_lines - 1
        if self._cursor_abs < vt:
            screen.scroll(vt - self._cursor_abs, True)
        elif self._cursor_abs > vb:
            screen.scroll(self._cursor_abs - vb, False)

    def _move_cursor(self, dy: int, dx: int = 0) -> None:
        """Move cursor by a relative offset and update display."""
        old_x = self._cursor_x
        self._cursor_abs += dy
        self._cursor_x += dx
        self._ensure_cursor_visible()
        # Snap to character boundary (avoid landing on 2nd cell of wide char)
        snapped = self._snap_cell_x(self._cursor_abs, self._cursor_x)
        if dx > 0 and snapped <= old_x:
            # Moving right landed on 2nd cell of wide char at old position;
            # advance past it by adding the character's full width.
            try:
                text = self._get_line_text(self._cursor_abs)
                ci = self._cell_to_char_idx(text, old_x)
                if ci < len(text):
                    snapped = old_x + self._char_width(text[ci])
            except (IndexError, Exception):
                snapped = old_x + 1
        self._cursor_x = snapped
        self._sync_cursor()
        self._mark_dirty()

    def _move_cursor_to(self, abs_line: int, x: int = 0) -> None:
        """Move cursor to an absolute position and update display."""
        self._cursor_abs = abs_line
        self._cursor_x = x
        self._ensure_cursor_visible()
        self._cursor_x = self._snap_cell_x(self._cursor_abs, self._cursor_x)
        self._sync_cursor()
        self._mark_dirty()

    # }}}

    # Tab bar / powerline status display {{{

    def _update_tab_bar(self) -> None:
        """Render powerline-style status in the tab bar area.

        Layout: [MODE] [search query] [match count] ... [row:col] [line/total]
        Uses gruvbox color palette with vim-style mode indicators.
        """
        if self._tab_manager is None:
            return
        tb = self._tab_manager.tab_bar
        if not tb.laid_out_once:
            return
        s = tb.screen
        s.cursor.x = 0
        s.erase_in_line(2, False)

        def _rgb(r: int, g: int, b: int) -> int:
            return ((r << 16 | g << 8 | b) << 8) | 2

        # Gruvbox palette
        bg1 = _rgb(0x3c, 0x38, 0x36)
        bg2 = _rgb(0x50, 0x49, 0x45)
        fg1 = _rgb(0xeb, 0xdb, 0xb2)
        fg2 = _rgb(0xd5, 0xc4, 0xa1)
        yellow = _rgb(0xfa, 0xbd, 0x2f)
        red = _rgb(0xfb, 0x49, 0x34)

        # Mode badge
        s.cursor.bold = True
        s.cursor.italic = False
        if self.state == ScrollModeState.SELECT:
            mode_bg = _rgb(0xfe, 0x80, 0x19)  # orange
            mode_fg = _rgb(0x28, 0x28, 0x28)
            if self._sel_mode == 'line':
                mode_text = ' V-LINE '
            elif self._sel_mode == 'block':
                mode_text = ' VBLOCK '
            else:
                mode_text = ' VISUAL '
        elif self.state == ScrollModeState.SEARCH:
            mode_bg = _rgb(0xb8, 0xbb, 0x26)  # green
            mode_fg = _rgb(0x28, 0x28, 0x28)
            mode_text = ' SEARCH '
        else:
            mode_bg = _rgb(0x83, 0xa5, 0x98)  # blue-green
            mode_fg = _rgb(0x28, 0x28, 0x28)
            mode_text = ' NORMAL '

        s.cursor.fg = mode_fg
        s.cursor.bg = mode_bg
        s.draw(mode_text)

        # Search segments (if active or has query)
        has_search = bool(self._search_query) or self.state == ScrollModeState.SEARCH
        if has_search:
            # FontAwesome arrow icons for search direction
            direction = '\uf0d8' if self._search_backwards else '\uf0d7'
            cursor_bar = '|' if self.state == ScrollModeState.SEARCH else ''
            matches = self._find_all_matches() if self._search_query else []
            match_total = len(matches)

            # Query segment
            s.cursor.fg = mode_bg
            s.cursor.bg = bg1
            s.cursor.bold = False
            s.draw('\ue0b0')  # powerline right arrow
            s.cursor.fg = fg1
            s.cursor.bg = bg1
            s.draw(f' {direction} {self._search_query}{cursor_bar} ')

            # Match count segment
            s.cursor.fg = bg1
            s.cursor.bg = bg2
            s.draw('\ue0b0')
            s.cursor.fg = yellow if match_total > 0 else red
            s.cursor.bg = bg2
            s.cursor.bold = True
            if match_total > 0:
                match_idx = self._current_match_index(matches) + 1
                s.draw(f' {match_idx}/{match_total} ')
            elif self._search_query:
                s.draw(' 0/0 ')
            else:
                s.draw('   ')

            # Close segment
            s.cursor.fg = bg2
            s.cursor.bg = 0
            s.cursor.bold = False
            s.draw('\ue0b0')
        else:
            s.cursor.fg = mode_bg
            s.cursor.bg = 0
            s.cursor.bold = False
            s.draw('\ue0b0')

        # Right-aligned position info
        s.cursor.fg = fg2
        s.cursor.bg = 0
        s.cursor.bold = False

        total = self._total_lines
        right_text = f' {self._cursor_abs + 1}:{self._cursor_x + 1} '
        right_pos = f' {self._cursor_abs + 1}/{total} '
        right_total = 1 + len(right_text) + 1 + len(right_pos)

        if s.cursor.x < s.columns - right_total:
            s.draw(' ' * (s.columns - s.cursor.x - right_total))

        # row:col segment
        s.cursor.fg = bg2
        s.cursor.bg = 0
        s.draw('\ue0b2')  # powerline left arrow
        s.cursor.fg = fg2
        s.cursor.bg = bg2
        s.draw(right_text)

        # line/total segment
        s.cursor.fg = bg1
        s.cursor.bg = bg2
        s.draw('\ue0b2')
        s.cursor.fg = fg1
        s.cursor.bg = bg1
        s.cursor.bold = True
        s.draw(right_pos)

    # }}}

    # Search {{{

    def _apply_search_marker(self) -> None:
        """Highlight all search matches using the marker system."""
        if self._window is None or not self._search_query:
            return
        try:
            marker = marker_from_regex(re.escape(self._search_query), 1, flags=re.IGNORECASE)
            self._window.screen.set_marker(marker)
        except re.error:
            pass

    def _clear_search_marker(self) -> None:
        if self._window is None:
            return
        self._window.screen.set_marker()

    def _find_all_matches(self) -> List[Tuple[int, int]]:
        """Return all (abs_line, column) positions matching the search query."""
        if self._window is None or not self._search_query:
            return []
        query_lower = self._search_query.lower()
        total = self._total_lines
        matches: List[Tuple[int, int]] = []
        for abs_line in range(total):
            try:
                line_text = self._get_line_text(abs_line).lower()
                start = 0
                while True:
                    col = line_text.find(query_lower, start)
                    if col < 0:
                        break
                    matches.append((abs_line, col))
                    start = col + 1
            except (IndexError, Exception):
                continue
        return matches

    def _current_match_index(self, matches: List[Tuple[int, int]]) -> int:
        """Return the index of the match at or nearest after the cursor."""
        for i, (line, col) in enumerate(matches):
            if line == self._cursor_abs and col == self._cursor_x:
                return i
        for i, (line, col) in enumerate(matches):
            if line > self._cursor_abs or (line == self._cursor_abs and col >= self._cursor_x):
                return i
        return 0

    def _jump_to_nearest_match(self) -> None:
        """Jump to nearest match; stay put if cursor is already on one."""
        if self._window is None or not self._search_query:
            return
        query_lower = self._search_query.lower()
        try:
            line_text = self._get_line_text(self._cursor_abs)
            col = line_text.lower().find(query_lower, self._cursor_x)
            if col == self._cursor_x:
                return  # already on a match
            if col >= 0:
                self._cursor_x = col
                self._sync_cursor()
                return
        except (IndexError, Exception):
            pass
        self._jump_to_match(self._search_backwards)

    def _jump_to_match(self, backwards: bool) -> None:
        """Jump to the next or previous search match, with wrapping."""
        if self._window is None or not self._search_query:
            return
        query_lower = self._search_query.lower()
        total = self._total_lines
        cur = self._cursor_abs
        cur_x = self._cursor_x

        if backwards:
            # Search current line before cursor
            try:
                line_text = self._get_line_text(cur)
                col = line_text.lower().rfind(query_lower, 0, cur_x)
                if col >= 0:
                    self._cursor_x = col
                    self._sync_cursor()
                    self._mark_dirty()
                    return
            except (IndexError, Exception):
                pass
            # Search upward
            for abs_line in range(cur - 1, -1, -1):
                try:
                    line_text = self._get_line_text(abs_line)
                    col = line_text.lower().rfind(query_lower)
                    if col >= 0:
                        self._move_cursor_to(abs_line, col)
                        return
                except (IndexError, Exception):
                    continue
            # Wrap from bottom
            for abs_line in range(total - 1, cur - 1, -1):
                try:
                    line_text = self._get_line_text(abs_line)
                    col = line_text.lower().rfind(query_lower)
                    if col >= 0:
                        self._move_cursor_to(abs_line, col)
                        return
                except (IndexError, Exception):
                    continue
        else:
            # Search current line after cursor
            try:
                line_text = self._get_line_text(cur)
                col = line_text.lower().find(query_lower, cur_x + 1)
                if col >= 0:
                    self._cursor_x = col
                    self._sync_cursor()
                    self._mark_dirty()
                    return
            except (IndexError, Exception):
                pass
            # Search downward
            for abs_line in range(cur + 1, total):
                try:
                    line_text = self._get_line_text(abs_line)
                    col = line_text.lower().find(query_lower)
                    if col >= 0:
                        self._move_cursor_to(abs_line, col)
                        return
                except (IndexError, Exception):
                    continue
            # Wrap from top
            for abs_line in range(0, cur + 1):
                try:
                    line_text = self._get_line_text(abs_line)
                    col = line_text.lower().find(query_lower)
                    if col >= 0:
                        self._move_cursor_to(abs_line, col)
                        return
                except (IndexError, Exception):
                    continue

    # }}}

    # Prompt jumping {{{

    def _jump_to_prompt(self, backwards: bool) -> None:
        """Jump cursor to the nearest shell prompt line, with wrapping."""
        if self._window is None:
            return
        total = self._total_lines
        cur = self._cursor_abs
        if backwards:
            for abs_line in range(cur - 1, -1, -1):
                try:
                    text = self._get_line_text(abs_line)
                    if _PROMPT_PATTERN.search(text):
                        self._move_cursor_to(abs_line, 0)
                        return
                except (IndexError, Exception):
                    continue
            # Wrap from bottom
            for abs_line in range(total - 1, cur - 1, -1):
                try:
                    text = self._get_line_text(abs_line)
                    if _PROMPT_PATTERN.search(text):
                        self._move_cursor_to(abs_line, 0)
                        return
                except (IndexError, Exception):
                    continue
        else:
            for abs_line in range(cur + 1, total):
                try:
                    text = self._get_line_text(abs_line)
                    if _PROMPT_PATTERN.search(text):
                        self._move_cursor_to(abs_line, 0)
                        return
                except (IndexError, Exception):
                    continue
            # Wrap from top
            for abs_line in range(0, cur + 1):
                try:
                    text = self._get_line_text(abs_line)
                    if _PROMPT_PATTERN.search(text):
                        self._move_cursor_to(abs_line, 0)
                        return
                except (IndexError, Exception):
                    continue

    # }}}

    # Mouse handling {{{

    def handle_mouse(self, window: 'Window', button: int, repeat_count: int) -> bool:
        """Handle mouse events dispatched from C.

        repeat_count semantics from dispatch_mouse_event:
          1 = press, 2 = double-press, 3 = triple-press,
          0 = drag (move while button held), -1 = release
        Returns True if the event was consumed.
        """
        if button != GLFW_MOUSE_BUTTON_LEFT:
            return self.active  # consume but ignore non-left in scroll mode

        pos = window.current_mouse_position()
        if pos is None:
            return self.active
        cell_x, cell_y = pos['cell_x'], pos['cell_y']

        # --- Not in scroll mode: auto-enter on double/triple click/drag ---
        if not self.active:
            if not get_options().scroll_mode_mouse:
                return False
            if repeat_count == 1:
                # Store press cell position for potential drag auto-enter
                self._drag_press_y = cell_y
                self._drag_press_x = cell_x
                return False  # let normal handling proceed
            if repeat_count == 2:
                self.enter(window, silent=True)
                if self.active:
                    self._mouse_click(cell_x, cell_y)
                    self._select_word_at_cursor()
                return True
            if repeat_count == 3:
                self.enter(window, silent=True)
                if self.active:
                    self._mouse_click(cell_x, cell_y)
                    self._start_selection('line')
                return True
            if repeat_count == 0:
                # Drag in normal mode: auto-enter scroll mode with char selection
                self.enter(window, silent=True)
                if self.active:
                    screen = window.screen
                    # Recompute anchor using current viewport (after pixel offset reset)
                    if self._is_alt_screen:
                        anchor_abs = self._drag_press_y
                    else:
                        anchor_abs = screen.historybuf.count - screen.scrolled_by + self._drag_press_y
                    anchor_x = self._drag_press_x
                    self._move_cursor_to(anchor_abs, anchor_x)
                    self._start_selection('char')
                    # Enable drag continuation inside scroll mode
                    self._drag_active = True
                    self._drag_started = True
                    # Recompute current position with fresh viewport
                    self._mouse_move(cell_x, cell_y)
                return True
            return False

        # --- In scroll mode ---
        if repeat_count == 1:
            # Single press: exit visual, move cursor, start potential drag
            if self.state == ScrollModeState.SELECT:
                self._sel_mode = None
                self.state = ScrollModeState.NAVIGATE
            self._mouse_click(cell_x, cell_y)
            self._drag_active = True
            self._drag_started = False
            return True

        if repeat_count == 2:
            # Double press: select word
            self._mouse_click(cell_x, cell_y)
            self._select_word_at_cursor()
            self._drag_active = False
            return True

        if repeat_count == 3:
            # Triple press: select line
            self._mouse_click(cell_x, cell_y)
            self._start_selection('line')
            self._drag_active = False
            return True

        if repeat_count == 0:
            # Drag: extend selection following mouse
            if not getattr(self, '_drag_active', False):
                return True
            if not getattr(self, '_drag_started', False):
                # First drag movement: start char selection from original click pos
                self._start_selection('char')
                self._drag_started = True
            # Move cursor to new position (extends selection)
            self._mouse_move(cell_x, cell_y)
            return True

        # Release or other: consume in scroll mode
        return True

    def _mouse_click(self, cell_x: int, cell_y: int) -> None:
        """Move cursor to the clicked cell position."""
        if self._window is None:
            return
        screen = self._window.screen
        abs_line = self._viewport_top() + cell_y
        total = self._total_lines
        abs_line = max(0, min(abs_line, total - 1))
        cell_x = max(0, min(cell_x, screen.columns - 1))
        cell_x = self._snap_cell_x(abs_line, cell_x)
        self._move_cursor_to(abs_line, cell_x)

    def _mouse_move(self, cell_x: int, cell_y: int) -> None:
        """Move cursor to cell during drag (selection extension).

        When the mouse is at the top or bottom edge, scroll the viewport
        to allow selecting beyond the current view.
        """
        if self._window is None:
            return
        screen = self._window.screen
        num_lines = screen.lines
        # Auto-scroll when dragging at viewport edges (main screen only)
        if not self._is_alt_screen:
            if cell_y <= 0 and screen.scrolled_by < screen.historybuf.count:
                screen.scroll(1, True)
            elif cell_y >= num_lines - 1 and screen.scrolled_by > 0:
                screen.scroll(1, False)
        abs_line = self._viewport_top() + cell_y
        total = self._total_lines
        abs_line = max(0, min(abs_line, total - 1))
        cell_x = max(0, min(cell_x, screen.columns - 1))
        cell_x = self._snap_cell_x(abs_line, cell_x)
        self._cursor_abs = abs_line
        self._cursor_x = cell_x
        self._ensure_cursor_visible()
        self._sync_cursor()
        self._mark_dirty()

    def _select_word_at_cursor(self) -> None:
        """Select the word under the cursor position (vim-style class grouping)."""
        if self._window is None:
            return
        cols = self._window.screen.columns
        try:
            cells = self._line_cells(self._cursor_abs, cols)
        except (IndexError, Exception):
            return
        if not cells:
            return
        # Find cursor position in cells list
        pos = 0
        for i, (ch, cx) in enumerate(cells):
            if cx >= self._cursor_x:
                pos = i
                break
        else:
            return
        cls = self._char_class(cells[pos][0])
        if cls == 0:
            return  # on whitespace, don't select
        # Expand to cover all same-class chars
        start = pos
        while start > 0 and self._char_class(cells[start - 1][0]) == cls:
            start -= 1
        end = pos
        while end + 1 < len(cells) and self._char_class(cells[end + 1][0]) == cls:
            end += 1
        # Set selection using cell positions
        self._sel_start_abs = self._cursor_abs
        self._sel_start_x = cells[start][1]
        self._cursor_x = cells[end][1]
        self._sel_mode = 'char'
        self.state = ScrollModeState.SELECT
        self._sync_cursor()
        self._mark_dirty()

    # }}}

    # Key handling {{{

    def handle_key(self, ev: KeyEvent) -> bool:
        """Main key dispatch. Returns True if the key was consumed."""
        if ev.action not in (GLFW_PRESS, GLFW_REPEAT):
            return True
        if self._window is None:
            return False
        if self.state == ScrollModeState.NAVIGATE:
            return self._handle_navigate(ev)
        elif self.state == ScrollModeState.SEARCH:
            return self._handle_search(ev)
        elif self.state == ScrollModeState.SELECT:
            return self._handle_select(ev)
        return False

    def _handle_navigate(self, ev: KeyEvent) -> bool:
        assert self._window is not None
        key = ev.key
        mods = ev.mods

        # Exit
        if (key == ord('q') and not mods) or (key == GLFW_FKEY_ESCAPE and not mods):
            self.exit()
            return True

        # Search: / or Alt+s = backwards, ? = forwards
        if (key == ord('/') and not mods) or (key == ord('s') and mods == GLFW_MOD_ALT):
            self._search_backwards = True
            self._search_query = ''
            self.state = ScrollModeState.SEARCH
            self._mark_dirty()
            return True
        if (key == ord('/') and mods == GLFW_MOD_SHIFT) or ev.text == '?':
            self._search_backwards = False
            self._search_query = ''
            self.state = ScrollModeState.SEARCH
            self._mark_dirty()
            return True

        # n/N: jump to next/prev search match
        if key == ord('n') and not mods:
            if self._search_query:
                self._jump_to_match(self._search_backwards)
            return True
        if key == ord('n') and mods == GLFW_MOD_SHIFT:
            if self._search_query:
                self._jump_to_match(not self._search_backwards)
            return True

        # Alt+u/Alt+n: jump to previous/next prompt
        if key == ord('u') and mods == GLFW_MOD_ALT:
            self._jump_to_prompt(backwards=True)
            return True
        if key == ord('n') and mods == GLFW_MOD_ALT:
            self._jump_to_prompt(backwards=False)
            return True

        # v/V/Ctrl+V: enter selection mode
        if key == ord('v') and not mods:
            self._start_selection('char')
            return True
        if key == ord('v') and mods == GLFW_MOD_SHIFT:
            self._start_selection('line')
            return True
        if key == ord('v') and mods == GLFW_MOD_CONTROL:
            self._start_selection('block')
            return True

        return self._handle_movement(ev)

    def _start_selection(self, mode: str) -> None:
        """Begin visual selection of the given mode at current cursor position."""
        self._sel_start_abs = self._cursor_abs
        self._sel_start_x = 0 if mode == 'line' else self._cursor_x
        self._sel_mode = mode
        self.state = ScrollModeState.SELECT
        self._sync_cursor()
        self._mark_dirty()

    def _handle_movement(self, ev: KeyEvent) -> bool:
        """Shared cursor movement handler for NAVIGATE and SELECT modes."""
        assert self._window is not None
        key = ev.key
        mods = ev.mods
        num_lines = self._window.screen.lines

        # Basic movement: h/j/k/l and arrow keys
        if (key == ord('j') and not mods) or key == GLFW_FKEY_DOWN:
            self._move_cursor(1)
            return True
        if (key == ord('k') and not mods) or key == GLFW_FKEY_UP:
            self._move_cursor(-1)
            return True
        if (key == ord('h') and not mods) or key == GLFW_FKEY_LEFT:
            self._move_cursor(0, -1)
            return True
        if (key == ord('l') and not mods) or key == GLFW_FKEY_RIGHT:
            self._move_cursor(0, 1)
            return True

        # Half-page and full-page scrolling
        if key == ord('d') and mods in (0, GLFW_MOD_CONTROL):
            self._move_cursor(max(1, num_lines // 2))
            return True
        if key == ord('u') and mods in (0, GLFW_MOD_CONTROL):
            self._move_cursor(-max(1, num_lines // 2))
            return True
        if (key == ord('f') and mods == GLFW_MOD_CONTROL) or key == GLFW_FKEY_PAGE_DOWN:
            self._move_cursor(num_lines)
            return True
        if (key == ord('b') and mods == GLFW_MOD_CONTROL) or key == GLFW_FKEY_PAGE_UP:
            self._move_cursor(-num_lines)
            return True

        # Jump to top/bottom: g/G
        if key == ord('g') and not mods:
            self._move_cursor_to(0, 0)
            return True
        if key == ord('g') and mods == GLFW_MOD_SHIFT:
            self._move_cursor_to(self._total_lines - 1, 0)
            return True

        # Line start/end: 0/$
        if key == ord('0') and not mods:
            self._cursor_x = 0
            self._sync_cursor()
            self._mark_dirty()
            return True
        if ev.text == '$':
            self._cursor_x = self._window.screen.columns - 1
            self._sync_cursor()
            self._mark_dirty()
            return True

        # Word movement: w/e/b
        if key == ord('w') and not mods:
            self._word_move_forward(to_end=False)
            return True
        if key == ord('e') and not mods:
            self._word_move_forward(to_end=True)
            return True
        if key == ord('b') and not mods:
            self._word_move_backward()
            return True

        return True  # consume unknown keys

    def _handle_search(self, ev: KeyEvent) -> bool:
        """Handle key input during incremental search mode."""
        assert self._window is not None
        key = ev.key
        mods = ev.mods

        # Escape: cancel search
        if key == GLFW_FKEY_ESCAPE and not mods:
            self._clear_search_marker()
            self._search_query = ''
            self.state = ScrollModeState.NAVIGATE
            self._mark_dirty()
            return True

        # Enter: accept search, return to navigate
        if key in (GLFW_FKEY_ENTER, GLFW_FKEY_KP_ENTER) and not mods:
            self.state = ScrollModeState.NAVIGATE
            self._mark_dirty()
            return True

        # Backspace: delete last character
        if key == GLFW_FKEY_BACKSPACE and not mods:
            if self._search_query:
                self._search_query = self._search_query[:-1]
                if self._search_query:
                    self._apply_search_marker()
                else:
                    self._clear_search_marker()
            self._mark_dirty()
            return True

        # Ctrl+u: clear search query
        if key == ord('u') and mods == GLFW_MOD_CONTROL:
            self._search_query = ''
            self._clear_search_marker()
            self._mark_dirty()
            return True

        # Printable character: append to query
        if not mods or mods == GLFW_MOD_SHIFT:
            text = ev.text
            if text and len(text) == 1 and text.isprintable():
                self._search_query += text
                self._apply_search_marker()
                self._jump_to_nearest_match()
                self._mark_dirty()
                return True

        return True

    def _handle_select(self, ev: KeyEvent) -> bool:
        """Handle key input during visual selection mode."""
        assert self._window is not None
        key = ev.key
        mods = ev.mods

        # Escape: yank selection and exit scroll mode
        if key == GLFW_FKEY_ESCAPE and not mods:
            self._yank_selection()
            return True

        # q: exit scroll mode entirely
        if key == ord('q') and not mods:
            self.exit()
            return True

        # y: yank and exit, Y: yank and stay
        if key == ord('y') and not mods:
            self._yank_selection()
            return True
        if key == ord('y') and mods == GLFW_MOD_SHIFT:
            self._yank_selection(stay=True)
            return True

        # o: swap anchor and cursor
        if key == ord('o') and not mods:
            self._sel_start_abs, self._cursor_abs = self._cursor_abs, self._sel_start_abs
            self._sel_start_x, self._cursor_x = self._cursor_x, self._sel_start_x
            self._ensure_cursor_visible()
            self._sync_cursor()
            self._mark_dirty()
            return True

        # v/V/Ctrl+V: toggle between selection modes
        if key == ord('v') and not mods:
            if self._sel_mode == 'char':
                self._sel_mode = None
                self.state = ScrollModeState.NAVIGATE
            else:
                self._sel_mode = 'char'
            self._sync_cursor()
            self._mark_dirty()
            return True
        if key == ord('v') and mods == GLFW_MOD_SHIFT:
            if self._sel_mode == 'line':
                self._sel_mode = self._sel_prev_mode or 'char'
            else:
                self._sel_prev_mode = self._sel_mode
                self._sel_mode = 'line'
            self._sync_cursor()
            self._mark_dirty()
            return True
        if key == ord('v') and mods == GLFW_MOD_CONTROL:
            if self._sel_mode == 'block':
                self._sel_mode = self._sel_prev_mode or 'char'
            else:
                self._sel_prev_mode = self._sel_mode
                self._sel_mode = 'block'
            self._sync_cursor()
            self._mark_dirty()
            return True

        # n/N: search match jumping works in select mode too
        if key == ord('n') and not mods:
            if self._search_query:
                self._jump_to_match(self._search_backwards)
            return True
        if key == ord('n') and mods == GLFW_MOD_SHIFT:
            if self._search_query:
                self._jump_to_match(not self._search_backwards)
            return True

        # Search: / or Alt+s = backwards, ? = forwards — clears selection
        if (key == ord('/') and not mods) or (key == ord('s') and mods == GLFW_MOD_ALT):
            self._sel_mode = None
            self._search_backwards = True
            self._search_query = ''
            self.state = ScrollModeState.SEARCH
            self._sync_cursor()
            self._mark_dirty()
            return True
        if (key == ord('/') and mods == GLFW_MOD_SHIFT) or ev.text == '?':
            self._sel_mode = None
            self._search_backwards = False
            self._search_query = ''
            self.state = ScrollModeState.SEARCH
            self._sync_cursor()
            self._mark_dirty()
            return True

        return self._handle_movement(ev)

    # }}}

    # Word movement {{{

    def _char_class(self, ch: str) -> int:
        """Classify a character for word motion.

        Uses kitty's select_by_word_characters option: alphanumeric chars
        plus those in the option are word chars (class 1), whitespace is
        class 0, everything else is a separator (class 2).
        """
        if ch.isspace():
            return 0
        if ch.isalnum() or ch in self._word_chars:
            return 1
        return 2

    @property
    def _word_chars(self) -> str:
        from .fast_data_types import get_options
        return get_options().select_by_word_characters

    def _line_cells(self, abs_line: int, cols: int) -> list[tuple[str, int]]:
        """Return list of (char, cell_x) pairs for a line, respecting wide chars."""
        text = self._get_line_text(abs_line)
        result: list[tuple[str, int]] = []
        cell = 0
        for ch in text:
            if cell >= cols:
                break
            result.append((ch, cell))
            cell += self._char_width(ch)
        return result

    def _word_move_forward(self, to_end: bool) -> None:
        """Move cursor to next word end (e) or next word start (w), vim-style.

        Vim word motion classifies chars as word/punct/space:
        - w: skip rest of current word-or-punct group, skip spaces, land on
             start of next word-or-punct group. Wraps to next line.
        - e: advance one, skip spaces, then skip through the word-or-punct
             group, land on its last char. Wraps to next line.
        """
        if self._window is None:
            return
        total = self._total_lines
        cols = self._window.screen.columns
        line_abs = self._cursor_abs
        try:
            cells = self._line_cells(line_abs, cols)
        except (IndexError, Exception):
            return

        # Find position in cells list from cursor_x
        pos = 0
        for i, (ch, cx) in enumerate(cells):
            if cx >= self._cursor_x:
                pos = i
                break
        else:
            pos = len(cells)

        def next_line() -> tuple[int, list[tuple[str, int]]] | None:
            nonlocal line_abs, cells
            line_abs += 1
            if line_abs >= total:
                return None
            try:
                cells = self._line_cells(line_abs, cols)
            except (IndexError, Exception):
                return None
            return (line_abs, cells)

        if to_end:
            # e: advance 1, skip spaces, then skip through same-class group
            pos += 1
            while True:
                # Advance past end of line
                if pos >= len(cells):
                    if next_line() is None:
                        return
                    pos = 0
                    continue
                # Skip spaces
                while pos < len(cells) and self._char_class(cells[pos][0]) == 0:
                    pos += 1
                if pos >= len(cells):
                    continue
                # Now on a non-space char; find end of this class group
                cls = self._char_class(cells[pos][0])
                while pos + 1 < len(cells) and self._char_class(cells[pos + 1][0]) == cls:
                    pos += 1
                break
        else:
            # w: skip rest of current class group, skip spaces, land on next
            while True:
                if pos >= len(cells):
                    if next_line() is None:
                        return
                    pos = 0
                    # If new line starts with content, land here
                    if cells and self._char_class(cells[0][0]) != 0:
                        break
                    continue
                # Skip current class group
                cls = self._char_class(cells[pos][0])
                if cls != 0:
                    while pos < len(cells) and self._char_class(cells[pos][0]) == cls:
                        pos += 1
                # Skip spaces
                while pos < len(cells) and self._char_class(cells[pos][0]) == 0:
                    pos += 1
                if pos < len(cells):
                    break
                # Wrapped to next line
                if next_line() is None:
                    return
                pos = 0
                if cells and self._char_class(cells[0][0]) != 0:
                    break
                continue

        if pos < len(cells):
            self._move_cursor_to(line_abs, cells[pos][1])

    def _word_move_backward(self) -> None:
        """Move cursor to previous word start (b), vim-style.

        Go back one, skip spaces, then skip backward through the same-class
        group, landing on the first char of that group.
        """
        if self._window is None:
            return
        cols = self._window.screen.columns
        line_abs = self._cursor_abs
        try:
            cells = self._line_cells(line_abs, cols)
        except (IndexError, Exception):
            return

        pos = 0
        for i, (ch, cx) in enumerate(cells):
            if cx >= self._cursor_x:
                pos = i
                break
        else:
            pos = len(cells) - 1 if cells else 0

        def prev_line() -> tuple[int, list[tuple[str, int]]] | None:
            nonlocal line_abs, cells
            line_abs -= 1
            if line_abs < 0:
                return None
            try:
                cells = self._line_cells(line_abs, cols)
            except (IndexError, Exception):
                return None
            return (line_abs, cells)

        pos -= 1
        while True:
            if pos < 0:
                if prev_line() is None:
                    return
                pos = len(cells) - 1
                if pos < 0:
                    continue
            # Skip spaces backward
            while pos >= 0 and self._char_class(cells[pos][0]) == 0:
                pos -= 1
            if pos < 0:
                continue
            # Now on a non-space char; go back through same class
            cls = self._char_class(cells[pos][0])
            while pos > 0 and self._char_class(cells[pos - 1][0]) == cls:
                pos -= 1
            break

        if pos >= 0 and pos < len(cells):
            self._move_cursor_to(line_abs, cells[pos][1])

    # }}}

    # Text extraction and clipboard {{{

    def _get_line_text(self, abs_line: int) -> str:
        """Get the text content of an absolute line (0 = oldest history line)."""
        if self._window is None:
            return ''
        screen = self._window.screen
        if self._is_alt_screen:
            # Alt screen: abs_line is a direct linebuf index, no history
            return str(screen.linebuf.line(abs_line))
        h_count = screen.historybuf.count
        if abs_line < h_count:
            # historybuf: index 0 = newest, count-1 = oldest
            return str(screen.historybuf.line(h_count - 1 - abs_line))
        else:
            return str(screen.linebuf.line(abs_line - h_count))

    @staticmethod
    def _cell_to_char_idx(text: str, cell_x: int) -> int:
        """Convert a cell (column) position to a Python string index.

        Wide characters (CJK etc.) occupy 2 cells but are 1 Python char.
        This walks through the string accumulating cell widths until the
        target cell is reached, returning the corresponding char index.
        """
        import unicodedata
        cell = 0
        for idx, ch in enumerate(text):
            if cell >= cell_x:
                return idx
            w = 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
            cell += w
        return len(text)

    @staticmethod
    def _char_width(ch: str) -> int:
        """Return the cell width of a character (2 for wide/fullwidth, else 1)."""
        import unicodedata
        return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1

    def _snap_cell_x(self, abs_line: int, cell_x: int) -> int:
        """Snap cell_x to the start cell of the character at that position.

        If cell_x falls on the second cell of a wide character, move it
        back to the first cell so cursor and selection boundaries align
        to character boundaries.
        """
        try:
            text = self._get_line_text(abs_line)
        except (IndexError, Exception):
            return cell_x
        cell = 0
        for ch in text:
            w = self._char_width(ch)
            if cell_x < cell + w:
                return cell
            cell += w
        return cell_x
        return len(text)

    def _is_line_continued(self, abs_line: int) -> bool:
        """Check if abs_line is a continuation of the previous line (wrapped).

        Returns True if abs_line is a soft-wrapped continuation, meaning
        the previous line ended by wrapping rather than a real newline.
        """
        if self._window is None or abs_line <= 0:
            return False
        screen = self._window.screen
        if self._is_alt_screen:
            return bool(screen.linebuf.is_continued(abs_line))
        h_count = screen.historybuf.count
        if abs_line < h_count:
            return bool(screen.historybuf.is_continued(h_count - 1 - abs_line))
        else:
            lb_idx = abs_line - h_count
            if lb_idx == 0:
                # First linebuf line: check if history ends with wrap
                if h_count > 0:
                    return bool(screen.historybuf.is_continued(0))
                return False
            return bool(screen.linebuf.is_continued(lb_idx))

    def _get_selected_text(self) -> str:
        """Extract the text within the current visual selection.

        Soft-wrapped lines are joined without newlines; only real line
        breaks produce newline characters in the result.
        """
        if self._sel_mode is None:
            return ''
        if self._sel_start_abs <= self._cursor_abs:
            start_abs, end_abs = self._sel_start_abs, self._cursor_abs
        else:
            start_abs, end_abs = self._cursor_abs, self._sel_start_abs

        if self._sel_mode == 'block':
            x_left = min(self._sel_start_x, self._cursor_x)
            x_right = max(self._sel_start_x, self._cursor_x)
            parts: list[str] = []
            for i in range(start_abs, end_abs + 1):
                try:
                    line = self._get_line_text(i)
                    ci_left = self._cell_to_char_idx(line, x_left)
                    ci_right = self._cell_to_char_idx(line, x_right + 1)
                    parts.append(line[ci_left:ci_right].rstrip())
                except (IndexError, Exception):
                    continue
            return '\n'.join(parts)

        # Normalize x direction for char/line selection
        if self._sel_start_abs < self._cursor_abs or (
            self._sel_start_abs == self._cursor_abs and self._sel_start_x <= self._cursor_x
        ):
            start_x, end_x = self._sel_start_x, self._cursor_x
        else:
            start_x, end_x = self._cursor_x, self._sel_start_x

        parts = []
        for i in range(start_abs, end_abs + 1):
            try:
                line = self._get_line_text(i)
                if self._sel_mode == 'char':
                    if i == start_abs and i == end_abs:
                        ci_s = self._cell_to_char_idx(line, start_x)
                        ci_e = self._cell_to_char_idx(line, end_x + 1)
                        line = line[ci_s:ci_e]
                    elif i == start_abs:
                        ci_s = self._cell_to_char_idx(line, start_x)
                        line = line[ci_s:]
                    elif i == end_abs:
                        ci_e = self._cell_to_char_idx(line, end_x + 1)
                        line = line[:ci_e]
                line = line.rstrip()
                # Join with previous if this line is a soft-wrap continuation
                if parts and self._is_line_continued(i):
                    parts[-1] += line
                else:
                    parts.append(line)
            except (IndexError, Exception):
                continue
        return '\n'.join(parts)

    def _yank_selection(self, stay: bool = False) -> None:
        """Copy selection to clipboard. If stay is False, exit scroll mode."""
        text = self._get_selected_text()
        if text:
            from .clipboard import set_clipboard_string
            set_clipboard_string(text)
        if stay:
            self._sel_mode = None
            self.state = ScrollModeState.NAVIGATE
            if self._window:
                self._window.screen.set_scroll_selection(0, 0, 0, 0, 0)
            self._sync_cursor()
            self._mark_dirty()
        else:
            self.exit()

    # }}}

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
        self._drag_press_abs = 0
        self._drag_press_x = 0

    # Properties {{{

    @property
    def _total_lines(self) -> int:
        if self._window is None:
            return 0
        screen = self._window.screen
        return screen.historybuf.count + screen.lines

    def _viewport_top(self) -> int:
        """Return the absolute line index at the top of the visible viewport."""
        if self._window is None:
            return 0
        screen = self._window.screen
        return screen.historybuf.count - screen.scrolled_by

    # }}}

    # Entry/exit {{{

    def enter(self, window: 'Window') -> None:
        """Activate scroll mode for the given window."""
        self._window = window
        self.active = True
        self.state = ScrollModeState.NAVIGATE
        self._search_query = ''
        self._sel_mode = None
        self._tab_manager = _get_tab_manager(window)
        screen = window.screen
        # If running in alternate screen (e.g. vim), switch to main buffer
        # which holds the scrollback history. Restored on exit.
        self._is_alt_screen = not screen.is_main_linebuf()
        if self._is_alt_screen:
            screen.toggle_alt_screen()
        # Pause child output: buffer raw bytes instead of parsing them,
        # so the scrollback content stays stable while browsing.
        screen.set_scroll_pause(True)
        # Clear any normal (non-scroll-mode) selection that was in progress
        screen.clear_selection()
        # Place cursor at bottom of visible content
        self._cursor_abs = screen.historybuf.count + screen.lines - 1 - screen.scrolled_by
        self._cursor_x = 0
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
            # Flush buffered output, then unpause and scroll to bottom
            screen.flush_scroll_pending()
            screen.set_scroll_pause(False)
            screen.scroll(SCROLL_FULL, False)
            if self._is_alt_screen:
                screen.toggle_alt_screen()
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
        is not activated.
        """
        screen = window.screen
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
        screen.set_scroll_cursor(self._cursor_x, vy, 1)
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
        screen.set_scroll_selection(sel_type, start_x, start_vy, end_x, end_vy)

    def _ensure_cursor_visible(self) -> None:
        """Scroll the viewport so the cursor remains on screen."""
        if self._window is None:
            return
        screen = self._window.screen
        h = screen.historybuf.count
        num_lines = screen.lines
        total = h + num_lines
        self._cursor_abs = max(0, min(self._cursor_abs, total - 1))
        self._cursor_x = max(0, min(self._cursor_x, screen.columns - 1))

        vt = self._viewport_top()
        vb = vt + num_lines - 1
        if self._cursor_abs < vt:
            screen.scroll(vt - self._cursor_abs, True)
        elif self._cursor_abs > vb:
            screen.scroll(self._cursor_abs - vb, False)

    def _move_cursor(self, dy: int, dx: int = 0) -> None:
        """Move cursor by a relative offset and update display."""
        self._cursor_abs += dy
        self._cursor_x += dx
        self._ensure_cursor_visible()
        self._sync_cursor()
        self._mark_dirty()

    def _move_cursor_to(self, abs_line: int, x: int = 0) -> None:
        """Move cursor to an absolute position and update display."""
        self._cursor_abs = abs_line
        self._cursor_x = x
        self._ensure_cursor_visible()
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
                # Store press position for potential drag auto-enter
                screen = window.screen
                self._drag_press_abs = screen.historybuf.count - screen.scrolled_by + cell_y
                self._drag_press_x = cell_x
                return False  # let normal handling proceed
            if repeat_count == 2:
                self.enter(window)
                if self.active:
                    self._mouse_click(cell_x, cell_y)
                    self._select_word_at_cursor()
                return True
            if repeat_count == 3:
                self.enter(window)
                if self.active:
                    self._mouse_click(cell_x, cell_y)
                    self._start_selection('line')
                return True
            if repeat_count == 0:
                # Drag in normal mode: auto-enter scroll mode with char selection
                self.enter(window)
                if self.active:
                    # Set anchor at original press position
                    anchor_abs = getattr(self, '_drag_press_abs', self._cursor_abs)
                    anchor_x = getattr(self, '_drag_press_x', 0)
                    self._move_cursor_to(anchor_abs, anchor_x)
                    self._start_selection('char')
                    # Move to current drag position
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
        self._move_cursor_to(abs_line, cell_x)

    def _mouse_move(self, cell_x: int, cell_y: int) -> None:
        """Move cursor to cell during drag (selection extension)."""
        if self._window is None:
            return
        screen = self._window.screen
        abs_line = self._viewport_top() + cell_y
        total = self._total_lines
        abs_line = max(0, min(abs_line, total - 1))
        cell_x = max(0, min(cell_x, screen.columns - 1))
        self._cursor_abs = abs_line
        self._cursor_x = cell_x
        self._ensure_cursor_visible()
        self._sync_cursor()
        self._mark_dirty()

    def _select_word_at_cursor(self) -> None:
        """Select the word under the cursor position."""
        if self._window is None:
            return
        try:
            text = self._get_line_text(self._cursor_abs)
        except (IndexError, Exception):
            return
        cols = self._window.screen.columns
        x = self._cursor_x
        if x >= len(text) or x >= cols:
            return

        def _is_word_char(ch: str) -> bool:
            return ch.isalnum() or ch == '_'

        if not _is_word_char(text[x]):
            return
        # Find word boundaries
        start = x
        while start > 0 and _is_word_char(text[start - 1]):
            start -= 1
        end = x
        while end + 1 < len(text) and end + 1 < cols and _is_word_char(text[end + 1]):
            end += 1
        # Set selection: anchor at word start, cursor at word end
        self._sel_start_abs = self._cursor_abs
        self._sel_start_x = start
        self._cursor_x = end
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

        # Escape: cancel selection
        if key == GLFW_FKEY_ESCAPE and not mods:
            self._sel_mode = None
            self.state = ScrollModeState.NAVIGATE
            if self._window:
                self._window.screen.set_scroll_selection(0, 0, 0, 0, 0)
            self._sync_cursor()
            self._mark_dirty()
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

        return self._handle_movement(ev)

    # }}}

    # Word movement {{{

    def _word_move_forward(self, to_end: bool) -> None:
        """Move cursor to next word start (w) or word end (e)."""
        if self._window is None:
            return
        total = self._total_lines
        cols = self._window.screen.columns
        line_abs = self._cursor_abs
        x = self._cursor_x
        try:
            text = self._get_line_text(line_abs)
        except (IndexError, Exception):
            return

        def _is_word_char(ch: str) -> bool:
            return ch.isalnum() or ch == '_'

        if to_end:
            # e: skip current char, skip non-word, stop at end of word
            x += 1
            while True:
                if x >= len(text) or x >= cols:
                    line_abs += 1
                    if line_abs >= total:
                        return
                    x = 0
                    try:
                        text = self._get_line_text(line_abs)
                    except (IndexError, Exception):
                        return
                while x < len(text) and x < cols and not _is_word_char(text[x]):
                    x += 1
                if x >= len(text) or x >= cols:
                    continue
                while x + 1 < len(text) and x + 1 < cols and _is_word_char(text[x + 1]):
                    x += 1
                break
        else:
            # w: skip rest of current word, skip non-word, stop at next word start
            while True:
                while x < len(text) and x < cols and _is_word_char(text[x]):
                    x += 1
                while x < len(text) and x < cols and not _is_word_char(text[x]):
                    x += 1
                if x < len(text) and x < cols:
                    break
                line_abs += 1
                if line_abs >= total:
                    return
                x = 0
                try:
                    text = self._get_line_text(line_abs)
                except (IndexError, Exception):
                    return

        self._move_cursor_to(line_abs, x)

    def _word_move_backward(self) -> None:
        """Move cursor to previous word start (b)."""
        if self._window is None:
            return
        line_abs = self._cursor_abs
        x = self._cursor_x
        try:
            text = self._get_line_text(line_abs)
        except (IndexError, Exception):
            return

        def _is_word_char(ch: str) -> bool:
            return ch.isalnum() or ch == '_'

        x -= 1
        while True:
            if x < 0:
                line_abs -= 1
                if line_abs < 0:
                    return
                try:
                    text = self._get_line_text(line_abs)
                except (IndexError, Exception):
                    return
                x = len(text) - 1
                if x < 0:
                    continue
            while x >= 0 and not _is_word_char(text[x]):
                x -= 1
            if x < 0:
                continue
            while x > 0 and _is_word_char(text[x - 1]):
                x -= 1
            break

        self._move_cursor_to(line_abs, max(0, x))

    # }}}

    # Text extraction and clipboard {{{

    def _get_line_text(self, abs_line: int) -> str:
        """Get the text content of an absolute line (0 = oldest history line)."""
        if self._window is None:
            return ''
        screen = self._window.screen
        h_count = screen.historybuf.count
        if abs_line < h_count:
            # historybuf: index 0 = newest, count-1 = oldest
            return str(screen.historybuf.line(h_count - 1 - abs_line))
        else:
            return str(screen.linebuf.line(abs_line - h_count))

    def _get_selected_text(self) -> str:
        """Extract the text within the current visual selection."""
        if self._sel_mode is None:
            return ''
        if self._sel_start_abs <= self._cursor_abs:
            start_abs, end_abs = self._sel_start_abs, self._cursor_abs
        else:
            start_abs, end_abs = self._cursor_abs, self._sel_start_abs

        if self._sel_mode == 'block':
            x_left = min(self._sel_start_x, self._cursor_x)
            x_right = max(self._sel_start_x, self._cursor_x)
            lines = []
            for i in range(start_abs, end_abs + 1):
                try:
                    line = self._get_line_text(i)
                    lines.append(line[x_left:x_right + 1].rstrip())
                except (IndexError, Exception):
                    continue
            return '\n'.join(lines)

        # Normalize x direction for char selection
        if self._sel_start_abs < self._cursor_abs or (
            self._sel_start_abs == self._cursor_abs and self._sel_start_x <= self._cursor_x
        ):
            start_x, end_x = self._sel_start_x, self._cursor_x
        else:
            start_x, end_x = self._cursor_x, self._sel_start_x

        lines = []
        for i in range(start_abs, end_abs + 1):
            try:
                line = self._get_line_text(i)
                if self._sel_mode == 'char':
                    if i == start_abs and i == end_abs:
                        line = line[start_x:end_x + 1]
                    elif i == start_abs:
                        line = line[start_x:]
                    elif i == end_abs:
                        line = line[:end_x + 1]
                lines.append(line.rstrip())
            except (IndexError, Exception):
                continue
        return '\n'.join(lines)

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

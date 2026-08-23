#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Amos Bird <amosbird@gmail.com>

from unittest.mock import patch

from kitty.fast_data_types import GLFW_FKEY_ESCAPE, GLFW_PRESS, KeyEvent, MOVE, Screen, send_mouse_event

from . import BaseTest, parse_bytes


class TestScrollMode(BaseTest):

    def test_entry_and_exit(self):
        from kitty.scroll_mode import ScrollMode

        class TabManager:
            tab_bar_hidden = False
            tab_bar_should_be_visible = True

            def mark_tab_bar_dirty(self):
                pass

            def update_tab_bar_data(self):
                pass

        class ChildMonitor:
            def wakeup(self):
                pass

        class Boss:
            child_monitor = ChildMonitor()

        class Window:
            id = 1

            def __init__(self, screen: Screen):
                self.screen = screen

        screen = self.create_screen()
        window = Window(screen)
        mode = ScrollMode()
        with patch('kitty.scroll_mode._get_tab_manager', return_value=TabManager()), patch('kitty.scroll_mode.get_boss', return_value=Boss()):
            mode.enter(window)
            self.assertTrue(mode.active)
            self.assertFalse(screen.cursor_visible)
            mode.exit()
            self.assertFalse(mode.active)
            self.assertTrue(screen.cursor_visible)

    def test_entry_starts_at_terminal_cursor(self):
        from kitty.scroll_mode import ScrollMode

        class Window:
            id = 1

            def __init__(self, screen: Screen):
                self.screen = screen

        screen = self.create_screen(cols=10, lines=4)
        parse_bytes(screen, b'first\r\nabc')
        mode = ScrollMode()
        with patch('kitty.scroll_mode._get_tab_manager', return_value=None):
            mode.enter(Window(screen))
        self.assertEqual((mode._cursor_abs, mode._cursor_x), (screen.historybuf.count + screen.cursor.y, screen.cursor.x))

    def test_alt_screen_remains_visible_while_active(self):
        from kitty.scroll_mode import ScrollMode

        class Window:
            id = 1

            def __init__(self, screen: Screen):
                self.screen = screen

        screen = self.create_screen(cols=10, lines=4)
        parse_bytes(screen, b'main\x1b[?1049halt')
        screen.cursor_position(2, 4)
        mode = ScrollMode()
        with patch('kitty.scroll_mode._get_tab_manager', return_value=None):
            mode.enter(Window(screen))
        self.assertFalse(screen.is_main_linebuf())
        self.assertEqual(str(screen.linebuf.line(0)), 'alt')
        self.assertEqual(mode._total_lines, screen.lines)
        self.assertEqual((mode._cursor_abs, mode._cursor_x), (1, 3))

    def test_alt_screen_is_restored_after_exit(self):
        from kitty.scroll_mode import ScrollMode

        class Window:
            id = 1

            def __init__(self, screen: Screen):
                self.screen = screen

        class ChildMonitor:
            def wakeup(self):
                pass

        class Boss:
            child_monitor = ChildMonitor()

        screen = self.create_screen(cols=10, lines=4)
        parse_bytes(screen, b'main\x1b[?1049halt')
        screen.cursor_position(2, 4)
        mode = ScrollMode()
        with patch('kitty.scroll_mode._get_tab_manager', return_value=None), patch('kitty.scroll_mode.get_boss', return_value=Boss()):
            mode.enter(Window(screen))
            self.assertFalse(screen.is_main_linebuf())
            mode.exit()
        self.assertFalse(screen.is_main_linebuf())
        self.assertEqual(str(screen.linebuf.line(0)), 'alt')
        self.assertEqual((screen.cursor.y, screen.cursor.x), (1, 3))

    def test_escape_yanks_selection_and_exits(self):
        from kitty.scroll_mode import ScrollMode, ScrollModeState

        class Window:
            def __init__(self, screen: Screen):
                self.screen = screen

        screen = self.create_screen()
        parse_bytes(screen, b'hello')
        mode = ScrollMode()
        mode._window = Window(screen)
        mode.active = True
        mode.state = ScrollModeState.SELECT
        mode._sel_mode = 'char'
        mode._sel_start_abs = mode._cursor_abs = 0
        mode._sel_start_x = 0
        mode._cursor_x = 4
        event = KeyEvent(GLFW_FKEY_ESCAPE, action=GLFW_PRESS)

        with patch('kitty.clipboard.set_clipboard_string') as set_clipboard, patch.object(mode, 'exit') as exit_mode:
            self.assertTrue(mode.handle_key(event))
            set_clipboard.assert_called_once_with('hello')
            exit_mode.assert_called_once_with()

    def test_selection_joins_soft_wrapped_lines(self):
        from kitty.scroll_mode import ScrollMode

        class Window:
            def __init__(self, screen: Screen):
                self.screen = screen

        screen = self.create_screen(cols=5, lines=4)
        parse_bytes(screen, b'abcdefgh\r\nij')
        mode = ScrollMode()
        mode._window = Window(screen)
        mode._sel_mode = 'char'
        mode._sel_start_abs = 0
        mode._sel_start_x = 0
        mode._cursor_abs = 2
        mode._cursor_x = 1
        self.assertEqual(mode._get_selected_text(), 'abcdefgh\nij')

        screen.reset()
        parse_bytes(screen, b'1234 5\r\nij')
        mode._cursor_abs = 2
        self.assertEqual(mode._get_selected_text(), '1234 5\nij')

        screen = self.create_screen(cols=5, lines=2, scrollback=4)
        parse_bytes(screen, b'abcdefghijklm')
        mode._window = Window(screen)
        mode._cursor_abs = 2
        mode._cursor_x = 2
        self.assertEqual(mode._get_selected_text(), 'abcdefghijklm')

    def test_exit_flushes_output_before_resuming_io(self):
        from kitty.scroll_mode import ScrollMode

        calls = []

        class Screen:
            def set_scroll_cursor(self, *args):
                pass

            def set_scroll_selection(self, *args):
                pass

            def set_marker(self, *args):
                pass

            def flush_scroll_pending(self):
                calls.append('flush')

            def set_scroll_pause(self, pause):
                calls.append(('pause', pause))

            def scroll(self, *args):
                pass

        class Window:
            screen = Screen()

        class ChildMonitor:
            def wakeup(self):
                pass

        class Boss:
            child_monitor = ChildMonitor()

        mode = ScrollMode()
        mode._window = Window()
        mode.active = True
        with patch('kitty.scroll_mode.get_boss', return_value=Boss()):
            mode.exit()
        self.assertEqual(calls, ['flush', ('pause', False)])

    def test_scroll_logic_does_not_hide_unexpected_errors(self):
        from kitty.scroll_mode import ScrollMode

        mode = ScrollMode()
        mode._window = object()
        mode._search_query = 'needle'
        mode._total_lines_override = 1
        with patch.object(type(mode), '_total_lines', new_callable=lambda: property(lambda self: 1)), patch.object(
            mode, '_get_line_text', side_effect=RuntimeError('broken line access')
        ):
            with self.assertRaisesRegex(RuntimeError, 'broken line access'):
                mode._find_all_matches()

    def test_navigation_search_and_mouse_drag(self):
        from kitty.scroll_mode import ScrollMode, ScrollModeState

        class Window:
            id = 1

            def __init__(self, screen: Screen):
                self.screen = screen

            def current_mouse_position(self):
                return {'cell_x': 3, 'cell_y': 1}

        screen = self.create_screen(cols=8, lines=3)
        parse_bytes(screen, b'one\r\ntwo')
        mode = ScrollMode()
        mode._window = Window(screen)
        mode.active = True
        mode._cursor_abs = 0
        mode._move_cursor(1, 2)
        self.assertEqual((mode._cursor_abs, mode._cursor_x), (1, 2))

        mode._search_query = 'two'
        self.assertEqual(mode._find_all_matches(), [(1, 0)])

        mode._drag_active = True
        mode._cursor_abs = mode._cursor_x = 0
        self.assertTrue(mode.handle_mouse(mode._window, 0, 0))
        self.assertEqual(mode.state, ScrollModeState.SELECT)
        self.assertEqual((mode._cursor_abs, mode._cursor_x), (1, 3))

    def test_word_boundaries_use_configured_characters(self):
        from kitty.scroll_mode import ScrollMode

        class Window:
            def __init__(self, screen: Screen):
                self.screen = screen

        screen = self.create_screen(cols=20, lines=2, options={'select_by_word_characters': '/:'})
        parse_bytes(screen, b'foo/bar:baz next')
        mode = ScrollMode()
        mode._window = Window(screen)
        mode._cursor_abs = 0
        mode._cursor_x = 4
        mode._select_word_at_cursor()
        self.assertEqual((mode._sel_start_x, mode._cursor_x), (0, 10))

        mode._sel_mode = None
        mode._cursor_x = 0
        mode._word_move_forward(to_end=False)
        self.assertEqual(mode._cursor_x, 12)
        mode._word_move_backward()
        self.assertEqual(mode._cursor_x, 0)

    def test_app_mouse_tracking_takes_precedence(self):
        screen = self.create_screen(options={'scroll_mode_mouse': True})
        parse_bytes(screen, b'\x1b[?1003h')
        self.assertTrue(send_mouse_event(screen, 0, 0, 0, MOVE, 0, 0, 0))
        self.assertEqual(screen.callbacks.wtcbuf, b'\x1b[MC!!')

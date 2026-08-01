#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Amos Bird <amosbird@gmail.com>

from unittest.mock import patch

from kitty.fast_data_types import MOVE, Screen, send_mouse_event

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
            mode.exit()
            self.assertFalse(mode.active)

    def test_app_mouse_tracking_takes_precedence(self):
        screen = self.create_screen(options={'scroll_mode_mouse': True})
        parse_bytes(screen, b'\x1b[?1003h')
        self.assertTrue(send_mouse_event(screen, 0, 0, 0, MOVE, 0, 0, 0))
        self.assertEqual(screen.callbacks.wtcbuf, b'\x1b[MC!!')

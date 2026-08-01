#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Amos Bird <amosbird@gmail.com>

from types import SimpleNamespace
from unittest.mock import patch

from kitty.boss import Boss

from . import BaseTest


class TestBoss(BaseTest):

    def test_keyboard_grab_released_only_after_last_os_window_closes(self) -> None:
        class TabManager:
            def destroy(self) -> None:
                pass

        boss = Boss.__new__(Boss)
        boss.os_window_map = {1: TabManager(), 2: TabManager()}
        boss.os_window_death_actions = {}
        boss.window_id_map = {}
        boss.cached_values = {}

        grab_calls = []

        def grab_keyboard(action):
            grab_calls.append(action)
            return action is None

        with (
            patch('kitty.boss.get_options', return_value=SimpleNamespace(remember_window_position=False)),
            patch('kitty.boss.grab_keyboard', side_effect=grab_keyboard),
        ):
            boss.on_os_window_closed(1, 0, 0, 80, 24, False)
            self.assertEqual(grab_calls, [])
            boss.on_os_window_closed(2, 0, 0, 80, 24, False)
            self.assertEqual(grab_calls, [None, False])

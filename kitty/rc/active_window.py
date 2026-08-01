#!/usr/bin/env python
# License: GPLv3 Copyright: 2024, Amos Bird

from typing import TYPE_CHECKING

from .base import ArgsType, Boss, PayloadGetType, PayloadType, RCOptions, RemoteCommand, ResponseType, Window

if TYPE_CHECKING:
    from kitty.cli_stub import ActiveWindowRCOptions as CLIOptions


class ActiveWindow(RemoteCommand):

    short_desc = 'Return the active window in the current tab'
    desc = 'Prints out the id of the active window.'

    def message_to_kitty(self, global_opts: RCOptions, opts: 'CLIOptions', args: ArgsType) -> PayloadType:
        return {}

    def response_from_kitty(self, boss: Boss, window: Window | None, payload_get: PayloadGetType) -> ResponseType:
        tab = boss.tab_for_id(window.tab_id) if window else boss.active_tab
        return str(tab.active_window.id) if tab and tab.active_window else None


active_window = ActiveWindow()

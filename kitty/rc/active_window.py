#!/usr/bin/env python

from typing import Optional

from .base import (
    ArgsType,
    Boss,
    PayloadGetType,
    PayloadType,
    RCOptions,
    RemoteCommand,
    ResponseType,
    Window,
)


class ActiveWindow(RemoteCommand):
    short_desc = "Return the active window in the current tab"
    desc = "Prints out the id of the active window."

    def message_to_kitty(
        self, global_opts: RCOptions, opts: "CLIOptions", args: ArgsType
    ) -> PayloadType:
        return {}

    def response_from_kitty(
        self, boss: Boss, window: Optional[Window], payload_get: PayloadGetType
    ) -> ResponseType:
        tab = boss.active_tab
        if window:
            tab = boss.tab_for_id(window.tab_id)
        if tab and tab.active_window:
            return str(tab.active_window.id)
        return None


active_window = ActiveWindow()

#pragma once

#include "internal.h"
#include "dbus_glfw.h"
#include <xkbcommon/xkbcommon.h>

typedef struct {
    bool ok, inited, name_owner_changed;
    const char *input_ctx_path;
    char im_unique_name[256];
} _GLFWFcitx5Data;

typedef struct {
    xkb_keycode_t keycode;
    xkb_keysym_t keysym;
    GLFWid window_id;
    GLFWkeyevent glfw_ev;
    char __embedded_text[64];
} _GLFWFcitx5KeyEvent;

void glfw_connect_to_fcitx5(_GLFWFcitx5Data *fcitx5);
void glfw_fcitx5_terminate(_GLFWFcitx5Data *fcitx5);
void glfw_fcitx5_set_focused(_GLFWFcitx5Data *fcitx5, bool focused);
void glfw_fcitx5_dispatch(_GLFWFcitx5Data *fcitx5);
bool fcitx5_process_key(const _GLFWFcitx5KeyEvent *ev, _GLFWFcitx5Data *fcitx5);
void glfw_fcitx5_set_cursor_geometry(_GLFWFcitx5Data *fcitx5, int x, int y, int w, int h);

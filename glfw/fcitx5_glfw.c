#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "internal.h"
#include "fcitx5_glfw.h"

#define debug debug_input

static const char FCITX5_SERVICE[]  = "org.fcitx.Fcitx5";
static const char FCITX5_IM_PATH[] = "/org/freedesktop/portal/inputmethod";
static const char FCITX5_IM_INTERFACE[] = "org.fcitx.Fcitx.InputMethod1";
static const char FCITX5_IC_INTERFACE[] = "org.fcitx.Fcitx.InputContext1";
static const char NAME_OWNER_CHANGED_RULE[] = "type='signal',interface='org.freedesktop.DBus',member='NameOwnerChanged'";


static void
send_text(const char *text, GLFWIMEState ime_state) {
    _GLFWwindow *w = _glfwFocusedWindow();
    if (w && w->callbacks.keyboard) {
        GLFWkeyevent fake_ev = {.action = GLFW_PRESS};
        fake_ev.text = text;
        fake_ev.ime_state = ime_state;
        w->callbacks.keyboard((GLFWwindow*) w, &fake_ev);
    }
}

static void
handle_fcitx5_forward_key(DBusMessage *msg) {
    uint32_t keysym, state;
    dbus_bool_t is_release;
    DBusMessageIter iter;
    dbus_message_iter_init(msg, &iter);

    if (dbus_message_iter_get_arg_type(&iter) != DBUS_TYPE_UINT32) return;
    dbus_message_iter_get_basic(&iter, &keysym);
    dbus_message_iter_next(&iter);

    if (dbus_message_iter_get_arg_type(&iter) != DBUS_TYPE_UINT32) return;
    dbus_message_iter_get_basic(&iter, &state);
    dbus_message_iter_next(&iter);

    if (dbus_message_iter_get_arg_type(&iter) != DBUS_TYPE_BOOLEAN) return;
    dbus_message_iter_get_basic(&iter, &is_release);

    unsigned int mods = 0;
#define M(g, i) if(state & (i)) mods |= GLFW_MOD_##g
    M(SHIFT, 1 << 0);
    M(CAPS_LOCK, 1 << 1);
    M(CONTROL, 1 << 2);
    M(ALT, 1 << 3);
    M(NUM_LOCK, 1 << 4);
    M(SUPER, 1 << 6);
#undef M

    debug("FCITX5: ForwardKey: keysym=%x state=%x is_release=%d mods=%x\n", keysym, state, is_release, mods);
    glfw_xkb_forwarded_key_from_ime(keysym, mods);
}

static const char*
get_preedit_text_from_message(DBusMessage *msg) {
    /* UpdateFormattedPreedit signature: a(si)i
     * We extract the concatenated text from the array of (string, format_flag) structs.
     */
    static char buf[4096];
    buf[0] = 0;
    size_t pos = 0;
    DBusMessageIter iter, array_iter, struct_iter;
    dbus_message_iter_init(msg, &iter);

    if (dbus_message_iter_get_arg_type(&iter) != DBUS_TYPE_ARRAY) return NULL;
    dbus_message_iter_recurse(&iter, &array_iter);

    while (dbus_message_iter_get_arg_type(&array_iter) == DBUS_TYPE_STRUCT) {
        dbus_message_iter_recurse(&array_iter, &struct_iter);
        if (dbus_message_iter_get_arg_type(&struct_iter) == DBUS_TYPE_STRING) {
            const char *text = NULL;
            dbus_message_iter_get_basic(&struct_iter, &text);
            if (text) {
                size_t len = strlen(text);
                if (pos + len < sizeof(buf) - 1) {
                    memcpy(buf + pos, text, len);
                    pos += len;
                }
            }
        }
        dbus_message_iter_next(&array_iter);
    }
    buf[pos] = 0;
    return buf;
}

// Signal handler for org.fcitx.Fcitx.InputContext1 signals
static DBusHandlerResult
fcitx5_message_handler(DBusConnection *conn UNUSED, DBusMessage *msg, void *user_data) {
    _GLFWFcitx5Data *fcitx5 = (_GLFWFcitx5Data*)user_data;

    switch(glfw_dbus_match_signal(msg, FCITX5_IC_INTERFACE,
                "CommitString", "UpdateFormattedPreedit", "ForwardKey", "CurrentIM", NULL)) {
        case 0: {
            const char *text = NULL;
            DBusMessageIter iter;
            dbus_message_iter_init(msg, &iter);
            if (dbus_message_iter_get_arg_type(&iter) == DBUS_TYPE_STRING)
                dbus_message_iter_get_basic(&iter, &text);
            debug("FCITX5: CommitString: '%s'\n", text ? text : "(nil)");
            send_text(text, GLFW_IME_COMMIT_TEXT);
            break;
        }
        case 1: {
            const char *text = get_preedit_text_from_message(msg);
            debug("FCITX5: UpdateFormattedPreedit: '%s'\n", text ? text : "(nil)");
            send_text(text ? text : "", GLFW_IME_PREEDIT_CHANGED);
            break;
        }
        case 2:
            handle_fcitx5_forward_key(msg);
            break;
        case 3: {
            // CurrentIM signal: (name: s, unique_name: s, language_code: s)
            const char *name = NULL, *unique_name = NULL, *lang = NULL;
            DBusMessageIter iter;
            dbus_message_iter_init(msg, &iter);
            if (dbus_message_iter_get_arg_type(&iter) == DBUS_TYPE_STRING) {
                dbus_message_iter_get_basic(&iter, &name);
                dbus_message_iter_next(&iter);
            }
            if (dbus_message_iter_get_arg_type(&iter) == DBUS_TYPE_STRING) {
                dbus_message_iter_get_basic(&iter, &unique_name);
                dbus_message_iter_next(&iter);
            }
            if (dbus_message_iter_get_arg_type(&iter) == DBUS_TYPE_STRING) {
                dbus_message_iter_get_basic(&iter, &lang);
            }
            debug("FCITX5: CurrentIM: name='%s' unique_name='%s' lang='%s'\n",
                  name ? name : "", unique_name ? unique_name : "", lang ? lang : "");
            if (unique_name && unique_name[0]) {
                strncpy(fcitx5->im_unique_name, unique_name, sizeof(fcitx5->im_unique_name) - 1);
                fcitx5->im_unique_name[sizeof(fcitx5->im_unique_name) - 1] = 0;
            }
            send_text(unique_name ? unique_name : "", GLFW_IME_INPUT_METHOD_CHANGED);
            break;
        }
    }
    return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;
}

static DBusHandlerResult
fcitx5_on_owner_change(DBusConnection *conn UNUSED, DBusMessage *msg, void *user_data) {
    if (dbus_message_is_signal(msg, "org.freedesktop.DBus", "NameOwnerChanged")) {
        const char *name, *old_owner, *new_owner;
        if (!dbus_message_get_args(msg, NULL,
                DBUS_TYPE_STRING, &name, DBUS_TYPE_STRING, &old_owner,
                DBUS_TYPE_STRING, &new_owner, DBUS_TYPE_INVALID))
            return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;
        if (strcmp(name, FCITX5_SERVICE) != 0)
            return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;
        _GLFWFcitx5Data *fcitx5 = (_GLFWFcitx5Data*)user_data;
        fcitx5->name_owner_changed = true;
        debug("FCITX5: NameOwnerChanged: old='%s' new='%s'\n", old_owner, new_owner);
        return DBUS_HANDLER_RESULT_HANDLED;
    }
    return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;
}

static void
input_context_created(DBusMessage *msg, const DBusError *err, void *data) {
    _GLFWFcitx5Data *fcitx5 = (_GLFWFcitx5Data*)data;

    if (err) {
        _glfwInputError(GLFW_PLATFORM_ERROR, "FCITX5: Failed to create input context: %s: %s",
                        err->name ? err->name : "", err->message ? err->message : "");
        return;
    }

    /* Reply signature: (o, ay) — object path + UUID byte array.
     * We need the object path. */
    DBusMessageIter iter;
    dbus_message_iter_init(msg, &iter);
    if (dbus_message_iter_get_arg_type(&iter) != DBUS_TYPE_OBJECT_PATH) {
        _glfwInputError(GLFW_PLATFORM_ERROR, "FCITX5: CreateInputContext reply has unexpected type");
        return;
    }
    const char *path = NULL;
    dbus_message_iter_get_basic(&iter, &path);
    if (!path || !path[0]) {
        _glfwInputError(GLFW_PLATFORM_ERROR, "FCITX5: CreateInputContext returned empty path");
        return;
    }

    free((void*)fcitx5->input_ctx_path);
    fcitx5->input_ctx_path = _glfw_strdup(path);
    if (!fcitx5->input_ctx_path) return;

    DBusConnection *conn = glfw_dbus_session_bus();
    if (!conn) return;

    // Subscribe to signals on our IC interface
    char rule[512];
    snprintf(rule, sizeof(rule),
             "type='signal',interface='%s',path='%s'",
             FCITX5_IC_INTERFACE, fcitx5->input_ctx_path);
    dbus_bus_add_match(conn, rule, NULL);

    DBusObjectPathVTable vtable = {.message_function = fcitx5_message_handler};
    dbus_connection_try_register_object_path(conn, fcitx5->input_ctx_path, &vtable, fcitx5, NULL);

    // SetCapability: Preedit (1<<1) | FormattedPreedit (1<<4) | GetIMInfoOnFocus (1<<23)
    uint64_t caps = (1ULL << 1) | (1ULL << 4) | (1ULL << 23);
    glfw_dbus_call_method_no_reply(conn, FCITX5_SERVICE, fcitx5->input_ctx_path, FCITX5_IC_INTERFACE,
                                   "SetCapability", DBUS_TYPE_UINT64, &caps, DBUS_TYPE_INVALID);

    fcitx5->ok = true;
    glfw_fcitx5_set_focused(fcitx5, _glfwFocusedWindow() != NULL);
    glfw_fcitx5_set_cursor_geometry(fcitx5, 0, 0, 0, 0);
    debug("FCITX5: Connected to fcitx5 daemon, IC path: %s\n", fcitx5->input_ctx_path);
}

static bool
setup_connection(_GLFWFcitx5Data *fcitx5) {
    fcitx5->ok = false;
    DBusConnection *conn = glfw_dbus_session_bus();
    if (!conn) return false;

    // Register NameOwnerChanged filter before the async CreateInputContext call,
    // so daemon restarts during the pending period are not missed.
    dbus_bus_add_match(conn, NAME_OWNER_CHANGED_RULE, NULL);
    dbus_connection_add_filter(conn, fcitx5_on_owner_change, fcitx5, NULL);

    // Build CreateInputContext message with a(ss) argument
    RAII_MSG(msg, dbus_message_new_method_call(FCITX5_SERVICE, FCITX5_IM_PATH,
                                                FCITX5_IM_INTERFACE, "CreateInputContext"));
    if (!msg) return false;

    DBusMessageIter iter, array_iter, struct_iter;
    dbus_message_iter_init_append(msg, &iter);
    dbus_message_iter_open_container(&iter, DBUS_TYPE_ARRAY, "(ss)", &array_iter);

    // program = "kitty"
    dbus_message_iter_open_container(&array_iter, DBUS_TYPE_STRUCT, NULL, &struct_iter);
    const char *key1 = "program", *val1 = "kitty";
    dbus_message_iter_append_basic(&struct_iter, DBUS_TYPE_STRING, &key1);
    dbus_message_iter_append_basic(&struct_iter, DBUS_TYPE_STRING, &val1);
    dbus_message_iter_close_container(&array_iter, &struct_iter);

    // display = "x11:" or "wayland:" based on env
    const char *display_str = "";
    static char display_buf[128];
    const char *wl = getenv("WAYLAND_DISPLAY");
    const char *xd = getenv("DISPLAY");
    if (wl && wl[0]) {
        snprintf(display_buf, sizeof(display_buf), "wayland:%s", wl);
        display_str = display_buf;
    } else if (xd && xd[0]) {
        snprintf(display_buf, sizeof(display_buf), "x11:%s", xd);
        display_str = display_buf;
    }
    dbus_message_iter_open_container(&array_iter, DBUS_TYPE_STRUCT, NULL, &struct_iter);
    const char *key2 = "display", *val2 = display_str;
    dbus_message_iter_append_basic(&struct_iter, DBUS_TYPE_STRING, &key2);
    dbus_message_iter_append_basic(&struct_iter, DBUS_TYPE_STRING, &val2);
    dbus_message_iter_close_container(&array_iter, &struct_iter);

    dbus_message_iter_close_container(&iter, &array_iter);

    if (!call_method_with_msg(conn, msg, DBUS_TIMEOUT_USE_DEFAULT, input_context_created, fcitx5, false)) {
        return false;
    }
    return true;
}

static bool
check_connection(_GLFWFcitx5Data *fcitx5) {
    if (!fcitx5->inited) return false;
    if (!fcitx5->name_owner_changed) return fcitx5->ok;
    // fcitx5 daemon restarted — clean up old state and reconnect
    debug("FCITX5: Daemon restart detected, reconnecting...\n");
    fcitx5->name_owner_changed = false;
    DBusConnection *conn = glfw_dbus_session_bus();
    if (conn) {
        dbus_bus_remove_match(conn, NAME_OWNER_CHANGED_RULE, NULL);
        dbus_connection_remove_filter(conn, fcitx5_on_owner_change, fcitx5);
        if (fcitx5->input_ctx_path) {
            char rule[512];
            snprintf(rule, sizeof(rule), "type='signal',interface='%s',path='%s'",
                     FCITX5_IC_INTERFACE, fcitx5->input_ctx_path);
            dbus_bus_remove_match(conn, rule, NULL);
            dbus_connection_unregister_object_path(conn, fcitx5->input_ctx_path);
        }
    }
    free((void*)fcitx5->input_ctx_path);
    fcitx5->input_ctx_path = NULL;
    fcitx5->ok = false;
    // Clear any stuck preedit overlay (daemon may have died mid-composition)
    send_text("", GLFW_IME_PREEDIT_CHANGED);
    setup_connection(fcitx5);
    return fcitx5->ok;
}

void
glfw_connect_to_fcitx5(_GLFWFcitx5Data *fcitx5) {
    if (fcitx5->inited) return;
    const char *im_module = getenv("GLFW_IM_MODULE");
    if (!im_module || !im_module[0]) {
        // Auto-detect: check if fcitx5 is running by checking GTK_IM_MODULE or similar
        const char *gtk_im = getenv("GTK_IM_MODULE");
        const char *qt_im = getenv("QT_IM_MODULE");
        bool is_fcitx = false;
        if (gtk_im && (strcmp(gtk_im, "fcitx") == 0 || strcmp(gtk_im, "fcitx5") == 0)) is_fcitx = true;
        if (qt_im && (strcmp(qt_im, "fcitx") == 0 || strcmp(qt_im, "fcitx5") == 0)) is_fcitx = true;
        if (!is_fcitx) return;
    } else {
        if (strcmp(im_module, "fcitx") != 0 && strcmp(im_module, "fcitx5") != 0) return;
    }
    fcitx5->inited = true;
    fcitx5->name_owner_changed = false;
    memset(fcitx5->im_unique_name, 0, sizeof(fcitx5->im_unique_name));
    setup_connection(fcitx5);
}

void
glfw_fcitx5_terminate(_GLFWFcitx5Data *fcitx5) {
    if (!fcitx5->inited) return;
    DBusConnection *conn = glfw_dbus_session_bus();
    if (conn) {
        dbus_bus_remove_match(conn, NAME_OWNER_CHANGED_RULE, NULL);
        dbus_connection_remove_filter(conn, fcitx5_on_owner_change, fcitx5);
        if (fcitx5->input_ctx_path) {
            if (fcitx5->ok) {
                glfw_dbus_call_method_no_reply(conn, FCITX5_SERVICE, fcitx5->input_ctx_path,
                                               FCITX5_IC_INTERFACE, "DestroyIC", DBUS_TYPE_INVALID);
            }
            char rule[512];
            snprintf(rule, sizeof(rule), "type='signal',interface='%s',path='%s'",
                     FCITX5_IC_INTERFACE, fcitx5->input_ctx_path);
            dbus_bus_remove_match(conn, rule, NULL);
            dbus_connection_unregister_object_path(conn, fcitx5->input_ctx_path);
        }
    }
    free((void*)fcitx5->input_ctx_path);
    fcitx5->input_ctx_path = NULL;
    fcitx5->ok = false;
}

void
glfw_fcitx5_set_focused(_GLFWFcitx5Data *fcitx5, bool focused) {
    if (!check_connection(fcitx5)) return;
    DBusConnection *conn = glfw_dbus_session_bus();
    if (!conn) return;
    glfw_dbus_call_method_no_reply(conn, FCITX5_SERVICE, fcitx5->input_ctx_path,
                                   FCITX5_IC_INTERFACE, focused ? "FocusIn" : "FocusOut",
                                   DBUS_TYPE_INVALID);
}

void
glfw_fcitx5_set_cursor_geometry(_GLFWFcitx5Data *fcitx5, int x, int y, int w, int h) {
    if (!check_connection(fcitx5)) return;
    DBusConnection *conn = glfw_dbus_session_bus();
    if (!conn) return;
    glfw_dbus_call_method_no_reply(conn, FCITX5_SERVICE, fcitx5->input_ctx_path,
                                   FCITX5_IC_INTERFACE, "SetCursorRect",
                                   DBUS_TYPE_INT32, &x, DBUS_TYPE_INT32, &y,
                                   DBUS_TYPE_INT32, &w, DBUS_TYPE_INT32, &h,
                                   DBUS_TYPE_INVALID);
}

void
glfw_fcitx5_dispatch(_GLFWFcitx5Data *fcitx5 UNUSED) {
    // fcitx5 uses the session bus, which is dispatched by glfw_dbus_session_bus_dispatch()
    // No additional dispatch needed.
}

static void
key_event_processed(DBusMessage *msg, const DBusError *err, void *data) {
    dbus_bool_t handled = FALSE;
    _GLFWFcitx5KeyEvent *ev = (_GLFWFcitx5KeyEvent*)data;
    ev->glfw_ev.text = ev->__embedded_text;
    bool failed = false;

    if (err) {
        _glfwInputError(GLFW_PLATFORM_ERROR, "FCITX5: Failed to process key: %s: %s",
                        err->name ? err->name : "", err->message ? err->message : "");
        failed = true;
    } else {
        glfw_dbus_get_args(msg, "FCITX5: Failed to get key result", DBUS_TYPE_BOOLEAN, &handled, DBUS_TYPE_INVALID);
        debug("FCITX5: ProcessKeyEvent result: handled=%d\n", handled);
    }
    glfw_xkb_key_from_ime((_GLFWIBUSKeyEvent*)ev, handled ? true : false, failed);
    free(ev);
}

bool
fcitx5_process_key(const _GLFWFcitx5KeyEvent *ev_, _GLFWFcitx5Data *fcitx5) {
    if (!check_connection(fcitx5)) return false;
    DBusConnection *conn = glfw_dbus_session_bus();
    if (!conn) return false;

    _GLFWFcitx5KeyEvent *ev = calloc(1, sizeof(_GLFWFcitx5KeyEvent));
    if (!ev) return false;
    memcpy(ev, ev_, sizeof(_GLFWFcitx5KeyEvent));
    if (ev->glfw_ev.text) strncpy(ev->__embedded_text, ev->glfw_ev.text, sizeof(ev->__embedded_text) - 1);
    ev->glfw_ev.text = NULL;

    // ProcessKeyEvent signature: uuubu
    // (keyval, keycode, state, isRelease, time)
    uint32_t keyval = ev->keysym;
    // fcitx5's native D-Bus frontend passes keycode directly to Key() without
    // adjustment, unlike the IBus frontend which adds 8. ev->keycode is the
    // evdev scancode (X11 keycode - 8), so we must add 8 back to get the
    // XKB keycode that fcitx5 expects.
    uint32_t keycode = ev->keycode + 8;
    uint32_t state = ev->glfw_ev.mods;

    // Convert GLFW mods to X11 modifier mask
    uint32_t xstate = 0;
    if (state & GLFW_MOD_SHIFT) xstate |= (1 << 0);
    if (state & GLFW_MOD_CAPS_LOCK) xstate |= (1 << 1);
    if (state & GLFW_MOD_CONTROL) xstate |= (1 << 2);
    if (state & GLFW_MOD_ALT) xstate |= (1 << 3);
    if (state & GLFW_MOD_NUM_LOCK) xstate |= (1 << 4);
    if (state & GLFW_MOD_SUPER) xstate |= (1 << 6);

    dbus_bool_t is_release = (ev->glfw_ev.action == GLFW_RELEASE) ? TRUE : FALSE;
    uint32_t timestamp = 0;

    if (!glfw_dbus_call_method_with_reply(
            conn, FCITX5_SERVICE, fcitx5->input_ctx_path, FCITX5_IC_INTERFACE, "ProcessKeyEvent",
            3000, key_event_processed, ev,
            DBUS_TYPE_UINT32, &keyval, DBUS_TYPE_UINT32, &keycode, DBUS_TYPE_UINT32, &xstate,
            DBUS_TYPE_BOOLEAN, &is_release, DBUS_TYPE_UINT32, &timestamp,
            DBUS_TYPE_INVALID)) {
        free(ev);
        return false;
    }
    return true;
}

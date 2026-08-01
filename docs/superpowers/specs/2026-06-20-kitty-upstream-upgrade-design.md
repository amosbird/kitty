# Kitty upstream upgrade and local patch-stack redesign

**Date:** 2026-06-20

## Goal

Rebuild the local kitty patch stack on the latest `upstream/master`, keeping only locally useful behavior and producing a small, reviewable commit series. The resulting branch must build and pass the project test suite before it replaces local `master`. Pushing the rewritten history is explicitly out of scope.

## Safety and history

- Preserve the current branch at `backup/pre-upstream-0.48-upgrade`.
- Remove the uncommitted scroll experiments rather than porting them.
- Remove `.mesh/` completely; do not add a repository ignore rule for it.
- Build the replacement history from `upstream/master`, not by mechanically rebasing all old commits.
- Do not force-push `origin/master`; leave the rewritten local branch for manual review.

## Patch selection

### Keep and port

1. Build only the Linux amd64 static kitten in `build_static_binaries`.
2. Allow Escape to quit the hints kitten and TUI hold screen.
3. Keep the machine-specific `Eisu_toggle` to keypad-equal mapping as an isolated workaround.
4. Keep the `active_window` remote-control command.
5. Keep `@cmdline` launch substitution.
6. Keep native scroll mode, including mouse integration and its follow-up mouse-tracking fix.
7. Keep URL-hint Unicode punctuation and balanced-bracket handling.
8. Keep the requirement to release an X11 keyboard grab when the final grabbed single-instance window closes, but replace the old incorrect `action is not None` condition.
9. Keep native Fcitx5 support and the `on_input_method_changed` watcher interface, while fixing forwarded key-release handling.

### Drop

1. The old fake-key IME-position update, because current upstream updates IME position through its normal IME path.
2. The old X11 touchpad valuator/hotplug fix, because current upstream has broader valuator initialization and reset handling.
3. All uncommitted momentum-direction and pending-scroll-pixel experiments.
4. `KITTY_SCROLL_LOG` file logging and all related code.
5. `.mesh/mesh.db` and the entire `.mesh/` directory.

## Target commit series

Each item should remain independently buildable where practical. Generated files belong in the same commit as the source definition that generates them.

1. `build: only build linux amd64 static kitten`
2. `hints: allow escape to quit`
3. `keyboard: map Eisu_toggle to keypad equal`
4. `remote-control: add active_window`
5. `launch: add @cmdline substitution`
6. `scroll-mode: add native scrollback navigation`
7. `hints: improve URL punctuation and bracket handling`
8. `x11: release keyboard grab after the final grabbed window closes`
9. `fcitx5: add native input method support`

The exact order may change if current upstream dependencies require it, but unrelated features must not be combined.

## Migration method

1. Create a temporary integration branch at current `upstream/master`.
2. Reconstruct each selected patch from its original commit, comparing it with current upstream before applying it.
3. Prefer current upstream APIs and structure over preserving old implementation details.
4. For each feature:
   - add or update a focused test first when behavior changes or a bug is fixed;
   - run the test and confirm the expected failure;
   - implement only the required behavior;
   - run the focused test and a build;
   - commit the coherent feature.
5. After all patches pass verification, repoint local `master` to the integration result.

## Feature-specific design

### Native scroll mode

Port the user-visible behavior rather than blindly copying the old 1,600-line implementation. Reconcile it with current `Window`, `Screen`, mouse dispatch, key-sequence, shader, and option APIs. Preserve the configured actions:

- `enter_scroll_mode`
- `enter_scroll_search`
- `enter_scroll_prompt`
- `scroll_mode_mouse`

Add focused tests for at least entry/exit, cursor movement over scrollback, and the rule that application mouse tracking disables `scroll_mode_mouse` interception. Avoid changing normal scroll behavior outside scroll mode.

### X11 keyboard grab

The old patch tied release to `os_window_death_actions`, which does not identify ownership of the keyboard grab. Track enough state to release only when the window/lifecycle that acquired the grab is finished, and ensure the final close cannot leave the single-instance process holding a global X11 grab. Keep the X11 ungrab request flushed where needed. Add a regression test around the Python lifecycle/state boundary; isolate native X11 calls behind the smallest existing testable seam.

### Native Fcitx5

Integrate with current upstream IME lifecycle and avoid duplicating behavior now provided by upstream. Preserve:

- native Fcitx5 D-Bus input context creation;
- preedit and commit handling;
- focus and cursor geometry updates;
- daemon restart recovery;
- current input-method notification through `on_input_method_changed`.

Correct `ForwardKey` so its `is_release` value reaches the generated GLFW event instead of every forwarded event becoming `GLFW_PRESS`. Add a focused test or the smallest runnable native self-check proving press and release remain distinct.

### URL hints

Retain only behavior not already upstream:

- truncate at non-ASCII Unicode punctuation;
- strip selected trailing ASCII punctuation, including `;` and `:`;
- remove unmatched trailing closing brackets;
- retain balanced brackets in valid URLs.

Port and extend the existing Go tests before implementation.

### Small compatibility patches

Keep `active_window`, `@cmdline`, Escape handling, and the Eisu workaround separate. Adapt their registration and typing to current upstream. Avoid adding generalized configuration or abstraction not required by the existing behavior.

## Verification

Minimum final verification:

```bash
./dev.sh build
./test.py
git diff --check
git status --short --branch
```

Also run focused tests after each behavioral patch. Verify that:

- no source reference to `KITTY_SCROLL_LOG` remains;
- `.mesh/` does not exist;
- the old IME-position and hotplug patches are absent from the rebuilt series;
- local `master` is based on the recorded latest `upstream/master`;
- `backup/pre-upstream-0.48-upgrade` still points to the old history;
- no remote ref was changed.

## Rollback

If the rebuilt branch is unusable, reset local `master` to `backup/pre-upstream-0.48-upgrade`. Because no force-push is performed, `origin/master` remains another copy of the old state until the result is manually accepted.

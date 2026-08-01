# Kitty upstream upgrade implementation plan

**Design:** `docs/superpowers/specs/2026-06-20-kitty-upstream-upgrade-design.md`

## 1. Prepare integration history

- Verify the worktree is clean and the backup branch exists.
- Create `upgrade/upstream-0.48` at current `upstream/master`.
- Cherry-pick the approved design commit so the rebuilt history documents itself.
- Record old commit IDs as source material only.

Verification:

```bash
git status --short
git merge-base --is-ancestor upstream/master HEAD
```

## 2. Port small independent patches

Port and commit separately:

1. Linux amd64 static kitten build restriction.
2. Escape handling in hints and hold.
3. `Eisu_toggle` keypad-equal workaround.
4. `active_window` remote command.
5. `@cmdline` launch substitution.

For each patch, compare current upstream APIs first, add a focused test where an existing harness can express the behavior, build, and commit.

## 3. Port native scroll mode

- First port or add focused tests for option parsing and core scroll-mode state behavior.
- Confirm tests fail before production code is added.
- Adapt the old implementation to current `Screen`, `Window`, `Boss`, mouse dispatch, generated options, and shader APIs.
- Preserve mouse-tracking exclusion from the follow-up fix.
- Regenerate config code using `gen/config.py`.
- Run focused tests and `./dev.sh build`.
- Commit all scroll-mode code and generated files together.

## 4. Port URL hint behavior

- Port the old test cases to current `kittens/hints/marks_test.go` first.
- Run `./test.py HintMarking` and confirm the new cases fail.
- Implement only missing Unicode punctuation and bracket behavior.
- Re-run the focused test and commit.

## 5. Rewrite X11 keyboard-grab release

- Identify the current owner/lifecycle state for `--grab-keyboard` and single-instance windows.
- Add a focused regression test for final-window release without relying on `os_window_death_actions`.
- Confirm failure.
- Implement minimal explicit ownership/state tracking and flush X11 ungrab.
- Run focused tests and build, then commit.

## 6. Port native Fcitx5 support

- Compare old Fcitx5 integration with current upstream IBus and IME lifecycle.
- Add a test/self-check proving forwarded press and release produce distinct GLFW actions.
- Confirm failure before implementation.
- Port D-Bus context, preedit/commit, focus, cursor geometry, restart recovery, and watcher notification.
- Pass `is_release` through forwarded-key handling.
- Build and run focused tests, then commit.

## 7. Final cleanup and verification

- Confirm no old scroll experiments or logging were reintroduced.
- Confirm `.mesh/` is absent.
- Run:

```bash
./dev.sh build
./test.py
git diff --check
git status --short --branch
```

- Review the new commits against `upstream/master`.
- Move local `master` to the verified integration branch.
- Do not push or force-push.
- Keep `backup/pre-upstream-0.48-upgrade` unchanged.

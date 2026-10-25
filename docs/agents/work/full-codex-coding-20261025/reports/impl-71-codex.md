# #71 combined implementation report

## Fixed candidate

- Worker: `/opt/qqbot/QQBotForge-codex-71-worker`
- Branch: `codex/full-codex-71-worker`
- Base: `e891f58` (`feat/ninja3-71-complete`), tree `4b7aaac3d99c0d676edc410510da3092e600f6ad`
- Implementation commit: `495cf9ace32b7ed8390b32378750b84ef5de7077`
- Implementation tree: `f7eddd2b36af583965d0a02527842708233e7c88`
- The implementation commit is the candidate to review; this report is an additional evidence commit on top of it.

The existing #72/#73/#74/#75/#77 work and the integrated #79/#101 sender evidence were preserved. No controller checkout, merge, deployment, production write, QQ session, game session, or real password operation was used.

## Implemented scope

- #76: added a single-account verification protocol that logs in with the sealed intent's new password, checks the same phone/area identity, and never submits a second change. Unknown outcomes remain paused when verification is inconclusive. Automatic verification is bounded; an authenticated owner can request the second bounded check with `忍3核验 <id>` or the backend `verify` action. Verified success uses `remote=verified_success` and goes through the existing local-only commit/recovery path. Task ownership, cancellation, retry, restart, and receipt rules remain token-bound.
- #78: added `ninja3_emergency` persistent state, fail-closed synchronous gates, owner-only QQ/private/backend entry points, per-account task delegation, task reuse, restart preservation, explicit resume, running-task exit protection, notification recording, and exit semantics that preserve unresolved task pauses. Member queries and menus are suppressed while the mode is active; non-owner emergency commands are silent.
- Durable local password commits now invalidate Ninja3 query revisions, closing the stale-credential window when a task completes during query preparation.
- Documentation and glossary entries describe verification and emergency-mode semantics. Focused real-plugin tests cover protocol identity checks, unknown verification success/failure/bounds, emergency sequencing, persistence failure, restart, exit, menu wiring, and Bot isolation.

## Validation

Successful focused run after the final implementation commit:

```text
/tmp/qqbot-75-venv/bin/python -m pytest -q \
  tests/password_protocol \
  tests/bot_modules/test_ninja3_password_tasks.py \
  tests/bot_modules/test_ninja3_password_batches.py \
  tests/bot_modules/test_ninja3_emergency.py \
  tests/bot_modules/test_ninja3_menus.py
253 passed in 51.32s
```

Additional checks after the implementation commit:

- Ruff check: passed for all changed Ninja3 and test files.
- Ruff format check: 12 files already formatted.
- Explicit mypy: passed for `password_protocol.py`, `password_tasks.py`, `password_batches.py`, `emergency.py`, `service.py`, `main.py`, and `storage.py`.
- `git diff --check e891f58..HEAD`: passed.
- Final worker tree was clean on `codex/full-codex-71-worker` before adding this report.

## Broader test limits

The repository-wide `pytest -q` collection is blocked in this checkout because the fixed CI test assets are not staged locally: `tests/test_api_modules_runtime.py` imports the absent `tests/helpers.py`. The branch's CI asset-staging boundary run produced 56 passes and 2 pre-existing failures where the checked-in pinned asset tries to overwrite `tests/conftest.py`.

The full `tests/bot_modules` run reached 756 passing tests. Its remaining failures are pre-existing base-tree contract/expectation mismatches outside this implementation: two Ninja3 extension-storage contract assertions (`project`/`atomic` are already present and the v2 schema is already accepted), and the older #73/#74 join/menu assertions that still expect the pre-#74 plain directory text instead of the merged shared menu format. The updated emergency menu assertion passes separately. No failure was hidden or changed to make the focused scope green.

The game account-center identity contract remains the existing #75 controlled fake-transport assumption (`userinfo.data.phone` and `.area`); no real platform response was inspected. Real QQ subscription/permission and real game behavior remain for the independent #80 review/acceptance.

## Handoff

Review the implementation delta from `e891f58` through `495cf9a` as one candidate. Start independent review from the fixed base and retain the focused 253-test command above. Check the emergency state and task rows together across a fresh process, especially an active task ID after restart, a failed emergency persistence write, and owner exit with unresolved unknown/local-pending tasks. Final merge, CI staging, production data, real QQ, real game login, and deployment remain outside this worker task.

# #71 combined implementation report

## Repair update after independent review

The independent review fixed the previous candidate at `d2960f11ef5d3ea793dd08a89606c0f6eb51329c` (tree `5297db4c251a1d5d21355c202ada757ab75beafa`) and identified six blocking race and isolation defects. The repair candidate is:

- Repair base: `d2960f11ef5d3ea793dd08a89606c0f6eb51329c`, tree `5297db4c251a1d5d21355c202ada757ab75beafa`
- Repair commit: `6c43a55e66b9e2abe9a5c0b3af1d3474a218a9ae`
- Repair tree: `4d597d3787eae41b7b6e12bfe119d8cf56657055`

The repaired code normalizes an interrupted `verifying` row to paused/unknown while retaining attempts, guards verifier result writes against cancellation and executor state, detaches a canceled verifier so unload cannot wait forever, fails the emergency synchronous gate closed for every SQLite read/lock error, re-reads emergency state so another process's exit is observed, and routes all owner batch/selection/confirmation/status replies privately while emergency mode is active. New regressions cover each finding.

Old review red evidence: the fixed candidate left `verifying` active across restart; a verifier that completed after cancellation could write `verified_success` and local credentials; shutdown awaited a non-cooperative verifier; locked emergency reads could allow the cached normal path; a second EmergencyMode instance kept seeing active after another instance exited; and batch/selection/status group responses bypassed the private emergency route.

New repair green evidence:

```text
/tmp/qqbot-75-venv/bin/python -m pytest -q \
  tests/password_protocol \
  tests/bot_modules/test_ninja3_password_tasks.py \
  tests/bot_modules/test_ninja3_password_batches.py \
  tests/bot_modules/test_ninja3_emergency.py \
  tests/bot_modules/test_ninja3_menus.py
259 passed in 52.49s
```

Ruff check/format, explicit mypy for the seven Ninja3 source files, and `git diff --check d2960f1..6c43a55` passed. The final report commit is added after this repair commit, so the repair SHA/tree above remains the exact source candidate for independent review.

## Final cross-process exit repair

The last review found one P1 in the previous repair: `EmergencyMode.exit()` only checked process-local runners. The final repair base was `adf9375e3fec400925cfb71f39d3a1ae2b6bb40d` (tree `e310cd0a2b77b0cb86a06c9e389df3dfb2204a75`), and the final source repair is:

- Final repair commit: `2d962a51571c8f91c96ae14f9693dd0e8edc5824`
- Final repair tree: `a45821d08d29e09c2c32d3cd8073a7b3d1005209`

Exit now checks persisted emergency account handoffs and the corresponding `ninja3_password_tasks` rows. It rejects missing/selected/prepared/preparing/submitting/verifying child tasks even when another process owns them, while allowing paused unknown and local-only recovery tasks. The runner publishes `status=running` before child selection, closing the task-less handoff window. A real two-connection regression holds the fake change request in flight, rejects peer exit, releases it, and then permits exit.

Final red/green evidence: the prior candidate's cross-process scenario could write `state=exited` during the other process's remote child task; the final focused real-component suite is **260 passed in 53.38s**, including the new two-connection exit regression. Ruff check/format, explicit mypy, and `git diff --check e891f58..HEAD` pass. No real QQ, game, password, merge, deploy, or production operation was performed.

## Final runner-ownership and privacy repair

The latest review found two P1s in the preceding candidate: a peer EmergencyMode runner could observe another process's submitting child, mark it complete/unknown, and enable exit; and emergency-mode authorization/stale-menu paths could still reply publicly. The final repair base was `63c683918d3c2996dee88eb7f5c6b02bcfe5549c` (tree `06b83153f9b8ac7ee5369773fe10fc7f9b4730f6`), and the source repair is:

- Final repair commit: `13e2ee1a23225c02bad5f78877660f8b9364c2a1`
- Final repair tree: `b56ce389f75b4cde933df9785357693523c7ad6b`

Emergency state now persists a runner token. A peer runner cannot claim or mutate the active owner’s row; restart takeover is allowed only after durable child tasks are no longer in selected/active execution states. The runner publishes its running handoff before child selection. Authorization in an active emergency is silent for non-owners and private for the owner; stale menu callbacks follow the same rule.

Final red/green evidence: the prior candidate's peer runner could clear a submitting task and exit; its group authorization and stale callback could expose public replies. The final focused suite is **261 passed in 53.27s**, including the blocked fake-protocol two-connection runner regression and stale-menu/authorization privacy regression. Ruff check/format, explicit mypy, and diff checks pass. No real QQ, game, password, merge, deploy, or production operation was performed.

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

# Group lifecycle module subscription verification

Source baseline: `39f9edb7080ba42583cdedf156af03c43812e905` (upstream main).
Source under test: `c84a301` (`feat/module-member-events`).
Test baseline: `04d9017` (upstream ci-assets). This test commit adds the independent
module consumer and transport integration suite, and initializes the real
HookManager in the existing manual production-scale host.

Tests use temporary config/storage, fake Bot identities and platform/storage
boundaries. No live QQ connection or account-sharing consumer/data is involved.
The real Application, WS dispatch/worker queue, Webhook handler, ModuleManager
setup/disable/reload, and plugin loader/dispatch are used.

## Red / green and checks

- Before: `test_real_module_observes_webhook` fails on upstream main because
  `ModuleContext` has no `subscribe_group_lifecycle`; real module enable fails.
- After: 31 new tests pass. Four lifecycle kinds traverse both WS and Webhook.
- Python 3.13 (matching CI): `ruff check .`, `mypy core web main.py`, `pytest -q`
  pass; full suite is 472 passed, versus baseline 441 passed.
- Also exercised on Python 3.11 during development.
- Fixed-snapshot Standards and Spec reviews completed. Findings about cancelled
  subscription retention, WS worker exhaustion and cancellation-resistant Bot
  isolation were fixed and checked with behavior tests.

Coverage includes native/missing/invalid fields, repeated/late/same-time events,
multiple Bots/subscribers/groups, neighboring business dispatch, plugin filters
and disable compatibility, exceptions, timeouts, callback cancellation,
unsubscribe/unload/reload/setup failure, background completion boundaries,
waiter cancellation, bounded history, cancellation resistance and 300 waiting
same-group messages while another group remains able to dispatch.

## Reproduce exact combination

Export the full source commit above into a temporary directory, overlay this
commit's `tests/` directory (not main's tests), install source requirements plus
pytest/pytest-asyncio/pytest-cov/mypy/ruff/types-PyYAML, then run:

```sh
ruff check .
mypy core web main.py
pytest -q
```

Use the complete source tree: image-hosting tests import runtime `modules/`.
The code PR contains no test assets. Upstream CI currently overlays upstream
`ci-assets/tests`; it will not exercise these new tests until maintainers accept
the separate test commit/patch. The local combined result is distinct from the
code PR's existing CI coverage.

## Existing manual stress failure

The command below was run on baseline and changed combinations:

```sh
python -m tests.stress run --config tests/stress/configs/ci_scenarios.yaml --suite production_scale
```

Both fail in the existing `_print_store_stats` because `_ScaleHarness` has no
`_group_users_cache` after upstream runtime refactoring. The traffic phases run,
but this is not a passing stress result or a reliable performance comparison.
That unrelated baseline defect is not fixed by this feature. The test host's
new HookManager initialization only supplies the actual new mixin dependency.

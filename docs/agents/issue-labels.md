# Issue labels

Map the role keys used by the development controller (`ai-coding`) and live
acceptance (`live-verify`) to this repository's GitHub labels. Triage roles
match [triage-labels.md](triage-labels.md).

## Triage

| Role key | GitHub label |
| --- | --- |
| `triage.needs-triage` | `needs-triage` |
| `triage.needs-info` | `needs-info` |
| `triage.ready-for-agent` | `ready-for-agent` |
| `triage.ready-for-human` | `ready-for-human` |
| `triage.wontfix` | `wontfix` |

## Lifecycle

An issue carries one lifecycle label at a time.

| Role key | GitHub label | Set when | Set by |
| --- | --- | --- | --- |
| `workflow.in-progress` | `workflow:implementing` | A worker starts the ticket (remove `ready-for-agent`) | controller |
| `workflow.in-review` | `workflow:validating` | A PR candidate is under independent review | controller |
| `workflow.changes-requested` | `workflow:fixing` | Review or live acceptance requires a code change | controller / live acceptance |
| `workflow.blocked` | `workflow:blocked` | Missing permission, capability, decision, dependency or environment | controller / live acceptance |
| `workflow.ready-for-acceptance` | `awaiting-live` | Released after final review and CI; kept through merge and deploy | controller |
| `workflow.live-verified` | `live-verified` | Live acceptance passed; stays on the closed issue | live acceptance |

After live acceptance:

- Passed, including limits accepted by contract: `awaiting-live` → `live-verified`, then close as authorized.
- Passed, but an unrelated new problem was found: the original becomes `live-verified`; the new issue starts at `needs-triage` and links back.
- The ticket's own requirement fails live: `awaiting-live` → `workflow:fixing`; the issue stays open and returns to the controller.
- Blocked by environment or platform: `awaiting-live` → `workflow:blocked`; return to `awaiting-live` once unblocked.

`urgent`, `blocking`, `bug`, `enhancement` and the other topic labels are
independent of the lifecycle. Agents switch only the labels above; creating
new labels needs the owner's confirmation.

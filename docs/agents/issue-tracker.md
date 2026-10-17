# Issue tracker: GitHub

Issues and specs live in GitHub Issues for `xmxxc/QQBotForge`.
Use the `gh` CLI from the repository root. Outside the clone,
specify `--repo xmxxc/QQBotForge` where supported.

## Conventions

- Create: `gh issue create --title "..." --body-file <file>`
- Read with discussion: `gh issue view <number> --comments`
- Read labels and metadata:
  `gh issue view <number> --json number,title,body,labels,comments`
- List: `gh issue list --state open`; apply label filters as needed.
- Comment: `gh issue comment <number> --body-file <file>`
- Add or remove labels:
  `gh issue edit <number> --add-label "..."` or `--remove-label "..."`
- Close: `gh issue close <number> --comment "..."`

For multiline content, write the exact text to a temporary file and
pass it with `--body-file`.

When a skill says "publish to the issue tracker", create a GitHub issue.
When it says "fetch the relevant ticket", read the issue and its comments.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Wayfinding operations

- Map: one issue labelled `wayfinder:map`, containing Notes,
  Decisions-so-far, and Fog.
- Child ticket: a GitHub sub-issue linked to the map. If sub-issues
  are unavailable, use a task list in the map and put `Part of #<map>`
  at the top of each child.
- Ticket type: `wayfinder:research`, `wayfinder:prototype`,
  `wayfinder:grilling`, or `wayfinder:task`.
- Blocking: use native GitHub issue dependencies. If unavailable,
  record `Blocked by: #<n>, #<n>` at the top of the child.
  A ticket is unblocked when every blocker is closed.
- Frontier: select the first open, unblocked, unassigned child
  in map order.
- Claim: assign the ticket to the driving developer before work.
- Resolve: comment with the answer, close the child, then append
  a summary and link to the map's Decisions-so-far.

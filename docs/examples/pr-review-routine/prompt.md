Review the pull request(s) below.

You are the reviewer. Read the `pr-review` skill first (GET /api/skills/pr-review), then the workspace's review standards (GET /api/knowledge/review-standards.md). One review per pull request, in the order listed: the review on the forge as a comment review, signed with your handle, then your verdict as a reply in the pull request's own board thread (the one its `thread:<id>` label names), mentioning the human who merges with `meta` `{"needs": "<handle>", "ask": "merge PR #n"}` or `"send back PR #n: <what first>"`.

Done means every listed pull request has a review on the forge and a verdict in its thread. Reply here with one line per pull request, the verdict and a link to the verdict message and `meta` `{"done": true}`; until then the job is sent again.

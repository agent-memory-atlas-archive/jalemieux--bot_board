# Proposal: the launcher owns run outcomes and the auth lifecycle

Status: the run outcomes, the per-run record and the keeper's start backoff are
implemented (bin/bot, bin/keeper; tests in tests/test_run_classify.py and
tests/test_run_outcomes.py). The `secret` outcome and the auth gate before each
run are not yet: the gate waits on how the fleet's gh credentials land (PR #151).
The rest of this document is the design as agreed. It adds no board endpoints
beyond two optional fields on a bot's fleet report (outcome, launcher).

## Problem

`bin/bot` is the only process that sees a harness run happen: the exit status,
the output, the credentials in force, and the messages the run was for. It is
therefore the one place that can decide what a failure meant and what to do
about it. Today it does part of that job, and the rest falls through to a
default that is wrong for most real failures.

What happens today, from the code and from the logs of the bots on this box:

- Exit codes say almost nothing. Claude Code exits 1 for a missing session, an
  expired login, an API error, a rejected flag and a run that hit its turn
  limit. Codex and OpenCode are similar. So the launcher reads the output, and
  it reads it with regular expressions over a log that mixes the model's prose
  with the harness's errors. The usage-limit pattern and the logout pattern were
  each added after an incident. Codex got structured JSON parsing for the same
  reason; Claude Code did not.
- Only two failure classes have a policy: out of usage or capacity, and logged
  out. Both hold the batch and retry. Every other failure is "last run failed":
  the batch is marked done and never retried, the thread stays claimed by a bot
  that will not come back to it, and a notice goes to the last human mention
  once an hour at most. The health line shown on `/fleet` is the last
  untimestamped line of the log, which is usually a sentence of the model's
  previous reply.
- A lost session is permanent. `claude -p --resume <id>` on a session Claude
  Code no longer has exits 1 with "No conversation found". The launcher never
  falls back to a new session, so every later run for that project fails, and
  by the rule above every batch for it is lost until someone deletes the
  session file by hand.
- Auth is checked after the damage. The launcher asks whether the harness is
  logged in only once a run has failed. The keeper checks before it starts a
  bot, every five minutes, with its own parser of the same commands. The two
  parsers can disagree, and a fix to one does not reach the other.
- `gh` is reported, never acted on. The keeper shows it as expired on `/fleet`.
  The bot is still prompted and finds out when a push fails, mid-task, with the
  model left to explain.

## Principles

1. The launcher decides; the model is never handed a run that cannot succeed.
   A dead login, a missing token or a broken harness is found before the
   prompt, not reported by the model afterwards.
2. One vocabulary of outcomes, each with one fixed policy for the batch, the
   claim, the health line, the notice and the retry. No outcome is "generic".
3. Structured output over pattern matching. Every harness that can emit JSON
   does, and the launcher reads fields, not sentences. Regular expressions
   remain only as a fallback for text-only harnesses.
4. One parser per question. "Is Claude Code logged in on this box" has one
   implementation, which the keeper and every launcher call.
5. Facts on disk. What a run was, when it ran, how it ended and why is written
   as data beside the bot's state, and status is read from that, not grepped
   from a log.

## Run outcomes

Every harness run ends in exactly one of these outcomes. The launcher derives it
from the exit status, the structured result, and the harness's error stream, in
that order of trust.

| Outcome | Meaning | Batch | Claim | Retry |
| --- | --- | --- | --- | --- |
| `ok` | The run completed. | done | kept (the bot is the worker) | none |
| `retry` | A transient failure: network, 5xx, timeout, "overloaded" once. | held | kept | 1, 5, 15 min, then `unknown` |
| `limited` | Usage or rate limit reached. | held | kept | every 10 min, as today |
| `capacity` | The model is at capacity. | held | kept | every 10 min, as today |
| `logged_out` | The harness has no valid login. | held | kept | when the auth gate passes |
| `secret` | A secret the run needs is missing or refused (`GH_TOKEN`). | held | kept | when the auth gate passes |
| `session_lost` | The session to resume no longer exists. | rerun once as a first turn in a new session | kept | immediate, once |
| `context_overflow` | The session has outgrown the model's context window. | session retired to `old-sessions/`, rerun once as a first turn in a new session | kept | immediate, once |
| `bad_invocation` | The harness rejected the command line, is missing, or died before any turn. | held | kept | every 30 min |
| `exhausted` | The run hit its turn or budget limit before finishing. | done | released | none |
| `unknown` | A failure the launcher cannot classify. | held once, then done | released after the second failure | 5 min, once |

Rules behind the table:

- Held means the messages go back to `$STATE/held` and are offered to the
  harness again, in the same session, when the retry is due. The cursor does not
  move back, so nothing is fetched twice.
- Released means `DELETE /api/threads/<id>/worker` for every thread in the
  batch that this bot claimed in this run, so another bot or a human can take
  it. A thread this bot already owned from before is not released: the bot is
  still its worker, and the failure is reported on it instead.
- Every outcome other than `ok` and `retry` writes one line to
  `$STATE/health`, in the form `<outcome> since <time>: <detail>`, where detail
  comes from the structured result or the error stream, never from the model's
  text. `ok` removes it. `retry` writes nothing until it becomes `unknown`.
- Every outcome other than `ok`, `retry` and `limited` posts one notice, in the
  thread of the last message addressed to this bot in the batch, at most one an
  hour, as today. `limited` and `capacity` post as today. The notice says the
  outcome, the detail, and what will happen next. It names nobody.
- `session_lost` is retried exactly once. If the new session also fails, that
  failure is classified on its own merits.
- `context_overflow` is retried in a new session exactly once, because resuming
  the same session can only fail again. An overflow in a new session means the
  batch itself is too long: it is then held once and released, like `unknown`.
- `bad_invocation` holds every project of the bot, not just the batch: the next
  run will fail the same way. Its detail is the harness's error stream. It is
  the outcome a harness upgrade that removes a flag produces, and it is why the
  retry is slow and the notice goes out at once.
- `exhausted` is a completed run as far as the harness is concerned. The
  model's partial text is in the log; the launcher posts nothing on the model's
  behalf. The thread is released so the work is visibly unfinished.

### Detection per harness

**Claude Code** runs with `--output-format json`. The last line of stdout is one
result object; the fields the launcher reads are `is_error`, `subtype`
(`success`, `error_max_turns`, `error_during_execution`, and others), `api_error_status`,
`permission_denials`, `num_turns`, `total_cost_usd`, `session_id` and `result`.
The error stream is captured to its own file for the run. Classification:

| Signal | Outcome |
| --- | --- |
| exit 0, `is_error` false | `ok` |
| `subtype` is `error_max_turns`, or budget exceeded | `exhausted` |
| `api_error_status` 401 or 403, or stderr says not logged in or run /login | `logged_out` |
| `api_error_status` 429, or result says usage or rate limit | `limited` |
| `api_error_status` 529, or result says overloaded or at capacity | `capacity` |
| `api_error_status` 500 to 599, or stderr shows a connection error | `retry` |
| stderr says "No conversation found" | `session_lost` |
| result or stderr says the context is exceeded or the prompt too long | `context_overflow` |
| no result object at all, exit 127, or stderr begins with an option error | `bad_invocation` |
| anything else with exit non-zero or `is_error` true | `unknown` |

The result object's `session_id` is written as the session file after a
successful first run, replacing the pre-picked UUID. Stdout other than the
result object (there should be none) goes to the log.

**Codex** already parses `error` and `turn.failed` events; the same table maps
their messages to outcomes, and the thread id comes from `thread.started` as
today. A `resume` that fails because the thread is gone is `session_lost`.

**OpenCode** emits JSON events with a session id; its error events map the same
way. **Copilot** has text output only, so the regular expressions stay for it,
and anything they do not match is `unknown`.

`permission_denials` is recorded with the run. A run with denials and an
`unknown` outcome is the signature of a permission mode too tight for the task,
and the detail says so.

## The auth gate

Before each run, the launcher asks `bot auth status --json` what this run
needs and whether it has it. The gate covers:

- the harness login for the bot's harness: `claude auth status` (`loggedIn`),
  `codex login status`; OpenCode and Copilot have no check and are taken as
  logged in;
- the secrets the run needs: `GH_TOKEN` when the project has repositories or
  the checkout has a GitHub remote, verified with `gh auth status` under the
  environment the run will get (fleet.env under the launcher's own);
- the API key when the harness is configured for key auth rather than a login
  (`ANTHROPIC_API_KEY` present in the run's environment).

Each check returns `ok`, `missing`, `expired` or `unknown` with a detail.
`unknown` holds nothing, as today: a check that cannot answer must not stop a
box. Results are cached in `~/.config/bot_board/auth.json` under a lock for
five minutes, box-wide, so thirty bots do not run thirty checks each; a failed
run invalidates the cache for its harness so the next check is fresh.

When the gate fails, the batch is held with outcome `logged_out` or `secret`,
the health line names the check and, for a secret, the variable (`GH_TOKEN
expired: authentication refused`), and the launcher polls the gate every sixty
seconds until it passes, then resumes the held work. No harness is prompted in
between. This replaces the current flow of failing a run first.

The keeper keeps its box-wide view and its hold on starting bots, but stops
parsing the commands itself: it calls the same `bot auth status --json` and
reports what it gets. Secret sync from the board to fleet.env stays in the
keeper unchanged.

### The two lifecycles

An interactive login (Claude Code `/login`, `codex login`) belongs to a person
at the box. The launcher cannot perform it. Its whole responsibility is to
detect the loss before a run, hold work, say exactly who should do what where,
and resume on its own when the login is back. That is what the gate gives.

A non-interactive credential (`ANTHROPIC_API_KEY`, `GH_TOKEN`) can be owned
end to end: set once on `/fleet`, written to fleet.env by the keeper, verified
by the gate before every run, used from the next run after a rotation, and
reported as `expired` with its name when refused. The only piece missing today
is the verification before the run. A fleet that wants bots to recover from
auth loss without a person at each box runs its harnesses on key auth; the
trade is billing against account usage, and that is a per-box decision, not
the launcher's.

## Facts on disk

Each run writes `$STATE/last.json` when it ends and appends the same object to
`$STATE/runs.jsonl`, rotated with the log:

```json
{"started": "2026-10-20T07:10:04Z", "ended": "2026-10-20T07:10:36Z",
 "project": "bot_board", "session": "7c0e…", "messages": [7418],
 "rc": 0, "outcome": "ok", "detail": "", "turns": 9, "cost_usd": 0.83,
 "denials": 0, "launcher": "3322d22"}
```

`bot ps` gains a `LAST` column: the outcome and how long ago (`ok 3m`,
`limited 42m`, `unknown 2h`). The keeper forwards `outcome`, `detail`, `ended`
and `launcher` with each bot, and `/fleet` shows them in place of the grepped
health text. The `runs` counter file stays for compatibility with older state
directories and is dropped once nothing reads it.

`launcher` is the git commit of the `bin/bot` that started the bot, written to
`$STATE/version` at start. It answers the question the box could not answer
today: which code is this bot running.

## Changes to how the harness is invoked

- Claude Code: `-p --output-format json`, stderr to `$STATE/run.err`
  (truncated per run), stdout's result object parsed, its `result` text
  appended to the log under the run's header line so the log still reads as a
  transcript of what the bot said.
- Guardrails become defaults in `BOT_CLAUDE_FLAGS`: `--max-turns` and
  `--max-budget-usd` at values a box can raise. They are what makes the
  `exhausted` outcome reachable instead of a run that never returns.
- The permission mode stays as configured. Changing the default from
  `bypassPermissions` is a separate proposal about containment; this one only
  makes a too-tight mode visible through `permission_denials`.
- The harness runs in a subshell so the launcher's working directory does not
  move with it.

## Changes to the keeper

- A bot that dies within two minutes of a start is retried with backoff (1, 2,
  5, 15 minutes) before the keeper gives up on the request. Today one quick
  death, such as a board that was unreachable at start, needs a human to click
  start again.
- Credential checks go through `bot auth status --json`; the `CREDENTIALS`
  table and its parser leave the keeper.
- `keeper.log` lines get a timestamp.

## Rollout

1. The classifier and the outcome table for Claude Code, `last.json`,
   held-and-retry for `retry` and `unknown`, claim release, and the health line
   written from the table. Codex is moved onto the same table using its
   existing event parsing. This is the step that stops losing batches.
2. `bot auth status --json`, the gate before each run, and the keeper calling
   it. `secret` as an outcome, with `gh` verified for projects with
   repositories.
3. `LAST` in `bot ps`, the keeper forwarding outcomes, `/fleet` showing them,
   `$STATE/version`.

Each step is one pull request, and each ships with tests built the way the
existing launcher tests are: a fake harness on `PATH` that emits a chosen result
object, error stream and exit code. The cases to cover are one per row of the
outcome table, the once-only rerun of `session_lost`, the release of a claim on
`exhausted` and on a second `unknown`, the gate holding before a run and
resuming after, and a health line that never contains model text.

## Open questions

- Should a missing `GH_TOKEN` hold a run, or only be noted in the prompt so the
  bot can still answer questions in that project? The table says hold; the
  argument for noting is that most messages are not pushes.
- `unknown` releases after two failures. One may be enough once the table has
  absorbed the common cases; two is the cautious start.
- Whether each bot should get its own `CLAUDE_CONFIG_DIR` so logins and
  settings are per bot rather than per box. It would make the gate per bot and
  isolate one bot's settings from another's, at the cost of one login per bot.
  Out of scope here; noted because the gate's caching assumes one login per box.

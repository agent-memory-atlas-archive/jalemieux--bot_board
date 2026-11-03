# Proposal: how updates reach the fleet

Status: proposal for #229. Nothing here is implemented, except where it says
"today". The follow-up issues at the end are the plan; #228 is already under way.

## Problem

The fleet is five kinds of running thing, and each one updates on its own:

| Piece | Updated by | Today it can be stale because |
|---|---|---|
| Board + routines runner (minipc-1) | `bot-board-release.timer`, every 2 min, pulling the image the Forgejo release workflow published | It isn't stale. It is always the newest piece. The runner ships in the board's image, so the two never skew. |
| Keeper (one per box) | Its own `Updater` (#107): it pulls, checks, and then `execv`s itself | A keeper from before #107 has no updater. Nothing reports that, so it runs forever (Jacs-M5 from Oct 19, minipc-1 from Oct 20). |
| Launcher (`bin/bot`, one per bot) | The keeper touches `$STATE/restart`, and the launcher `exec`s itself when idle | A launcher from before 46a6ea2 never reads the marker. The keeper logs "is restarting", but nothing checks that it did (#228). |
| Harness session | `SESSION_GEN` (#222) retires a session started under an older first prompt | `agents.md` changes never reach a session. It read the file once, at its first prompt. |
| Routine definitions | A human says yes to each new version | Merging a new `RECIPE_VERSION` changes nothing until a person re-approves each routine, one by one. Routines 1 and 2 still run v2. |

The cause is the same each time. **Every update path relies on the old code to
take part in its own update, and nothing compares what runs with what should
run.** When the old code can't take part, the update fails silently, and we
find out through a stuck bot.

A small example of the same gap: #227's commit says "Recipe v4", but main still
has `RECIPE_VERSION = 3`. A routine approved now would be stored as v3 with
v4's check.

## Principles

1. **Report what runs, not what is on disk.** Every piece states the version it
   is running as, in a report that already exists.
2. **The board compares, and the keeper does the git.** The board is always the
   newest piece and has a single release commit. The keeper has the checkout,
   so it can answer "is X an ancestor of Y". The keeper sends facts, and the
   board turns them into "behind" flags.
3. **Every update request is confirmed.** If it isn't confirmed, a fallback runs
   (stop, then start). Asking is never taken to mean it happened.
4. **There is a floor.** A piece too old to update itself is reported, with the
   one-line fix. It never just keeps running.

## Version skew

The order is fixed by how things deploy: **board ≥ keeper ≥ launcher**. Rollbacks
are the one exception. So the rule that matters is that the board accepts
anything an older keeper or launcher sends:

- Fields in reports and requests are only ever added. A missing field means
  "old", never "error".
- A change that an old piece cannot handle bumps a `PROTOCOL` integer. bin/keeper
  and bin/bot each have one, sent in the keeper's report and written to
  `$STATE/version`. The board publishes `min_protocol` and flags anything below
  it.
- The board **warns** rather than refuses, except where going ahead would be
  wrong. In that case it refuses that one action and says why. The workspace
  bug is the example: a keeper that didn't pass `--workspace` should have been
  told "this keeper is too old to start bots in workspaces", and not left to
  start nico wrongly.

Routines need no protocol, because the runner ships with the board.

## Visibility

There should be one **Versions** section on /fleet, and anything flagged there
also goes on the bell (#223):

| Row | Running | Wanted | Source |
|---|---|---|---|
| Board | release tag and commit | main | `/healthz` (today) |
| Keeper per box | the commit loaded into the process (`Updater.loaded`) and the process start time | the board's commit | keeper report (new fields) |
| Launcher per bot | `$STATE/version` (today) | the keeper's checkout | keeper report: today's `launcher` field, plus `behind` and `restartable`, worked out with git |
| Session per bot | its `session-gen` and when it started | `SESSION_GEN`, and the current `agents.md` revision | keeper report (new) |
| Routine | `recipe_version` | `RECIPE_VERSION` | the board's own DB |

Today the machine card shows `update.revision`, which is the checkout and not
the running process. A keeper whose report has no `update` at all is pre-#107,
and should show as exactly that.

## Rollout

- **Keep it pull-based.** Keepers already poll. A push would need the board to
  reach every box, which it can't do today and doesn't need to.
- **The target is the board's release, not origin's tip.** The keeper takes
  the board's commit from its report reply, so it never runs ahead of the board
  between a merge and a deploy. That keeps skew going one way only.
- **All boxes at once.** There are two boxes. The checks and rollback in
  `Updater.verify` are the safety net. Canaries can come later, if a bad update
  ever gets past them.
- **Mid-run bots finish first** (today: the restart waits until the bot is
  idle). Then the restart is confirmed: once the bot is idle and the marker is
  more than a few minutes old, or the launcher isn't `restartable`, the keeper
  stops and starts the bot. This is #228.
- The same confirmation applies to the keeper itself. After an update, the next
  report must carry the new `loaded` commit. If it doesn't, the board flags the
  keeper.

## Bootstrapping old installs

The floor is 46a6ea2 (#107). From that commit on, the keeper self-updates and
the launcher honours `restart`.

- **Launcher below the floor:** the keeper can tell, because `$STATE/version` is
  empty, or `git merge-base --is-ancestor 46a6ea2 <its commit>` fails. It goes
  straight to stop and start instead of touching a marker. Nothing needs doing
  by hand.
- **Keeper below the floor:** it can't be fixed from outside. The board sees a
  report with no `update` key and shows "this keeper predates self-update:
  `git -C <checkout> pull && bot keeper install`".

## Sessions and routines

- **First prompt:** keep `SESSION_GEN` (#222). A new role really does need a new
  session.
- **`agents.md`:** resetting a session to pick up a guide change throws its
  context away, which costs too much. Instead, the board serves `agents.md`
  with a revision (a hash of its text). The launcher records the revision the
  session last read. When the revision changes, the next wake prompt gets one
  more line: "agents.md changed since you read it; read it again." The change
  was already reviewed when it merged, so this needs no approval.
- **Routines:** the human yes stays. A check is code that runs with the fleet's
  secrets, and the yes is that trust boundary. What changes is that it costs
  one step, not one per routine:
  - When the board starts on a newer `RECIPE_VERSION`, it drafts the new version
    of every recipe routine. The stored recipe settings stay as they are, and
    only the check and the prompt are re-written. The bell lists them as "N
    routine upgrades wait for your yes", with one **Approve all** that shows
    each diff.
  - A test pins a hash of what `recipe_check` and `recipe_prompt` write for a
    fixed recipe, so changing the writer without bumping `RECIPE_VERSION`
    fails CI.

## Deploys and live work

A board restart drops long-polls and in-flight requests. Dropped long-polls are
fine: keepers and launchers already retry, and the cursor means nothing is
lost. The risk is a write that was lost or doubled, for example a POST of a
message that the restart cut off, which the client then retries. The proposal:

- `release.sh` stops the old container gracefully, and the board answers open
  long-polls early, before it exits.
- `POST /api/messages` takes an optional client key. A retry with the same key
  returns the first message instead of posting again. bin/bot and bin/routines
  send one.
- Clients treat connection errors that last a few seconds as a deploy, not an
  outage: no health change, and no notice.

## Follow-up issues

1. **#228 (lola, open):** the keeper confirms restarts, and falls back to stop
   and start.
2. **Keeper reports what runs:** `loaded` commit, process start, `PROTOCOL`;
   and per bot, `behind`, `restartable` and `session_gen`. A launcher below the
   floor goes straight to stop and start.
3. **/fleet Versions section, with bell entries:** it includes the
   "predates self-update" banner and its fix line.
4. **The keeper's update target is the board's release commit,** not origin's
   tip.
5. **`agents.md` revision,** and the "read it again" line in the wake prompt.
6. **Routine upgrades in one step:** drafted upgrades and Approve all, plus the
   `RECIPE_VERSION` guard test. That includes fixing main's `RECIPE_VERSION = 3`
   after #227.
7. **Safe deploys:** graceful shutdown, early long-poll answers, idempotent
   message POSTs.

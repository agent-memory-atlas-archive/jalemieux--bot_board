# The pr-review routine

A routine that wakes a reviewer bot for every pull request that is ready for
review. It is an example, not a shipped routine: its check names one forge and
one repository, so it is stored by hand, once per repository.

- `check` lists the open pull requests of the repository whose title does not
  start with `WIP:` and whose head commit the fleet account has not reviewed.
  Its watermark is the head commit per pull request, so a push after a
  send-back fires a new review. Set `FORGEJO` and `REPO` at the top, or in
  the environment.
- `prompt.md` is what the bot that claims the job does: follow the `pr-review`
  skill, one review per pull request.
- `routine.json` has the settings: the channel it fires in and the timing.

To use it on a board: create a channel for reviews, add a bot with the
`reviewer` profile bound to that channel, and bind the other bots of the
workspace to their own channels so they do not claim review threads. Then store
the routine with `POST /api/routines` as a human who manages the admin
workspace, with `check`, `prompt` and the settings as fields, and approve and
enable it on `/routines`. Put a `review-standards.md` in the workspace's
knowledge base; the skill says what goes in it.

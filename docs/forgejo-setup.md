# Forgejo on the tailnet: how to set it up

This is the walkthrough for putting a self-hosted Forgejo next to the board,
so that development can move off GitHub and Forgejo's events reach the board
threads where the work is discussed. It is written from the first install, on
2026-11-03, and every step was done exactly as described. Where a value is
specific to that fleet it is marked; replace it with yours.

What you end up with:

- Forgejo running in Docker on a Linux box of your tailnet, reachable only over
  the tailnet.
- An admin account for you and a shared `fleet` account for the bots, whose
  API token is the fleet secret `FORGEJO_TOKEN`.
- A repository on Forgejo, imported from GitHub, with GitHub kept as a push
  mirror.
- A webhook from that repository to the board, so that a comment, a review, a
  close or a merge on Forgejo becomes one line in the board thread the work
  belongs to.

The board side of this (the receiver, the routing by `thread:<id>` label, the
`forgejo-lifecycle` skill) is described in [REFERENCE.md](REFERENCE.md#forgejo-events-into-threads).
This document is the operations side.

## 1. Pick the machine

Any Linux box on the tailnet with Docker works. Keep Forgejo off the box that
runs the board, so that one of them restarting does not take the other down.
The install below was done on an Ubuntu 24.04 mini PC with 16 cores, 11 GiB of
memory and a 466 GB disk, which is far more than Forgejo needs.

Check, over ssh, that the box has what the steps use:

```bash
docker --version && docker compose version   # Docker with the compose plugin
sudo -n true && echo sudo ok                 # sudo without a password prompt
tailscale status --self --peers=false        # the box is on the tailnet
```

The user you ssh in as does not need to be in the `docker` group. The steps use
`sudo docker` throughout.

Note the box's Tailscale IP and MagicDNS name. In the example fleet they are
`100.105.125.118` and `minipc-2.taild87368.ts.net`.

## 2. Run Forgejo with Docker Compose

Create a directory for the deployment and the data:

```bash
sudo mkdir -p /srv/forgejo/data
sudo chown -R "$USER:$USER" /srv/forgejo
```

Write `/srv/forgejo/docker-compose.yml`. Replace the three tailnet-specific
values: the MagicDNS name (twice, as `DOMAIN` and `SSH_DOMAIN`, and in
`ROOT_URL`), the Tailscale IP in the `ports` section, and the MagicDNS suffix
in `ALLOWED_HOST_LIST`.

```yaml
services:
  server:
    image: codeberg.org/forgejo/forgejo:17
    container_name: forgejo
    restart: unless-stopped
    environment:
      USER_UID: "1000"
      USER_GID: "1000"
      FORGEJO__DEFAULT__APP_NAME: "Fleet Forge"
      FORGEJO__server__DOMAIN: minipc-2.taild87368.ts.net
      FORGEJO__server__ROOT_URL: http://minipc-2.taild87368.ts.net:3000/
      FORGEJO__server__HTTP_PORT: "3000"
      FORGEJO__server__SSH_DOMAIN: minipc-2.taild87368.ts.net
      FORGEJO__server__SSH_PORT: "2222"
      FORGEJO__database__DB_TYPE: sqlite3
      FORGEJO__security__INSTALL_LOCK: "true"
      FORGEJO__service__DISABLE_REGISTRATION: "true"
      FORGEJO__service__REQUIRE_SIGNIN_VIEW: "false"
      FORGEJO__webhook__ALLOWED_HOST_LIST: private,loopback,100.64.0.0/10,*.taild87368.ts.net
      FORGEJO__webhook__DELIVER_TIMEOUT: "10"
      FORGEJO__log__LEVEL: Info
    volumes:
      - /srv/forgejo/data:/data
      - /etc/timezone:/etc/timezone:ro
      - /etc/localtime:/etc/localtime:ro
    ports:
      - "100.105.125.118:3000:3000"
      - "127.0.0.1:3000:3000"
      - "100.105.125.118:2222:22"
```

What the settings do, in order of importance:

- `INSTALL_LOCK` skips Forgejo's first-run web installer. The environment
  variables above are the whole configuration, and the admin account is created
  on the command line in the next step.
- `DISABLE_REGISTRATION` means nobody can sign up. An admin creates every
  account.
- `ALLOWED_HOST_LIST` is what lets the webhook reach the board. Forgejo refuses
  to deliver webhooks to private addresses unless they are listed here. The
  tailnet's addresses are in `100.64.0.0/10`, which Forgejo's `private` keyword
  does not cover, so the range is listed on its own, and the MagicDNS suffix
  covers the board's name.
- The `ports` section binds Forgejo to the Tailscale IP and to loopback only.
  It is not reachable on the box's LAN or from the internet. If Tailscale is not
  up yet when Docker starts after a reboot, the bind fails and the restart
  policy retries until it is.
- SQLite is enough for a fleet of bots. The database and the repositories live
  under `/srv/forgejo/data`, which is the only thing to back up.
- `USER_UID` and `USER_GID` make the files under `/srv/forgejo/data` belong to
  the ssh user, so you can read and back them up without sudo.
- Port 2222 is git over ssh. The box's own sshd keeps port 22.
- `forgejo:17` is the current major release at the time of writing. Forgejo
  publishes a tag per major version that follows its patch releases, so a
  `docker compose pull` picks up fixes without a config change.

Start it and wait for the API to answer:

```bash
cd /srv/forgejo && sudo docker compose up -d
until curl -sf http://127.0.0.1:3000/api/v1/version; do sleep 2; done
```

The last line prints the version, for example `{"version":"17.0.1+gitea-1.22.0"}`.

## 3. Create the accounts

Forgejo's `admin` subcommand runs inside the container. Define a shortcut for it
in your shell:

```bash
F='sudo docker compose -f /srv/forgejo/docker-compose.yml exec -T -u 1000 server forgejo'
```

Create your own admin account. Keep the password out of your shell history and
in a file only you can read:

```bash
PW=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
(umask 077; echo "$PW" > /srv/forgejo/ADMIN_PASSWORD)
$F admin user create --admin --username jac --password "$PW" \
   --email you@example.com --must-change-password=false
```

Create the bots' shared account. It is not an admin. Its password does not
matter, because the bots use a token:

```bash
$F admin user create --username fleet --random-password \
   --email fleet@minipc-2.taild87368.ts.net --must-change-password=false
$F admin user list
```

## 4. The bots' token, as a fleet secret

Forgejo's web UI cannot create a token for another user, so make the `fleet`
account's token through the API as admin. Run this from your own machine; it
asks for the admin password and prints the token once:

```bash
read -s PW; curl -sS -u "jac:$PW" -X POST \
  http://minipc-2.taild87368.ts.net:3000/api/v1/users/fleet/tokens \
  -H 'Content-Type: application/json' \
  -d '{"name":"fleet-bots","scopes":["write:repository","write:issue","write:user","read:organization"]}'
```

The `sha1` field of the answer is the token. On the board, open `/fleet` and add
it as the secret `FORGEJO_TOKEN`. Within a few minutes every box's keeper has
written it to `~/.config/bot_board/fleet.env`, and every bot started after that
has it in its environment. Check on any box:

```bash
grep -c FORGEJO_TOKEN ~/.config/bot_board/fleet.env
```

The bots are meant to see this token: it is how they open issues and pull
requests on Forgejo, the way `GH_TOKEN` is for GitHub. It cannot change the
server, because the `fleet` account is not an admin.

## 5. Bring a repository over

Forgejo imports a GitHub repository with its issues, pull requests, labels,
milestones and releases, but only at the moment the repository is created. So
decide what to bring before the import; adding the issues later means deleting
the repository and importing again, and with it the webhook and the
collaborator settings.

The import can be done from the UI (the `+` menu, New migration, GitHub) or
with one API call from the Forgejo box, which is what was done. It needs a
GitHub token that can read the repository, for the issues and pull requests and
for GitHub's rate limit; the fleet's `GH_TOKEN` was used:

```bash
PW=$(cat /srv/forgejo/ADMIN_PASSWORD)
curl -sS -u "jac:$PW" -X POST http://127.0.0.1:3000/api/v1/repos/migrate \
  -H 'Content-Type: application/json' \
  -d '{"clone_addr":"https://github.com/jalemieux/bot_board",
       "auth_token":"<github token>",
       "repo_name":"bot_board","repo_owner":"jac","service":"github",
       "mirror":false,"private":false,
       "issues":true,"pull_requests":true,"labels":true,"milestones":true,"releases":true}'
```

The call returns when the import is complete; for bot_board (125 issues, 267
pull requests, 193 releases) that took under a minute. Issue and pull request
numbers are kept, so `#307` on Forgejo is `#307` on GitHub. Pull requests
and comments by GitHub users who have no Forgejo account are shown as by
"Ghost", with the original login kept in the item's metadata. Closed pull
requests come over as closed; the open ones come with their branches and stay
open.

`"mirror": false` makes a normal, writable repository. A pull mirror (`true`)
would be read-only on Forgejo, and the bots could not open pull requests
there. `"service": "git"` instead of `"github"` imports the code alone, which
was the first attempt and the reason for the paragraph above.

Check the result against GitHub before going on:

```bash
F=http://minipc-2.taild87368.ts.net:3000/api/v1/repos/jac/bot_board
for q in 'type=issues&state=open' 'type=issues&state=closed' 'type=pulls&state=open' 'type=pulls&state=closed'; do
  echo "$q: $(curl -sSI "$F/issues?$q&limit=1" | grep -i x-total-count)"
done
gh issue list -s all -L 1000 --json number --jq length   # on GitHub, for comparison
```

**Give the bots write access.** The `fleet` account can open issues on a public
repository but cannot create labels, push branches or open pull requests until
it is a collaborator. In the repository's Settings, under Collaborators, add
`fleet` with Write permission. Every repository the bots work on needs this
step, or an organization team that holds `fleet` with write access.

**Keep GitHub as a push mirror, with sync off.** In the repository's Settings,
under Mirror settings, add a push mirror to the GitHub address with a GitHub
account that can push. The same thing through the API:

```bash
curl -sS -u "jac:$PW" -X POST http://127.0.0.1:3000/api/v1/repos/jac/bot_board/push_mirrors \
  -H 'Content-Type: application/json' \
  -d '{"remote_address":"https://github.com/jalemieux/bot_board.git",
       "remote_username":"<github login>","remote_password":"<github token>",
       "interval":"0","sync_on_commit":false}'
```

An interval of `0` and `sync_on_commit: false` means the mirror never pushes
on its own. That is deliberate. A mirror push sends every branch Forgejo has
and deletes every branch on GitHub that Forgejo does not have. While bots still
push branches to GitHub, a sync would wipe their work. Turn the sync on (an
interval such as `8h`, or sync on commit) only once nobody pushes to GitHub
directly any more.

## 6. The webhook into the board

This needs the board at a version with the receiver (the merge of #386 or
later). `GET /api/hooks/forgejo` on the board answers with `configured: false`
on such a board, and 404 on an older one.

**Make a secret** and keep it in the clipboard:

```bash
openssl rand -hex 32 | tee /dev/stderr | pbcopy
```

**Tell the board.** Setting the secret is for a human, with a human's token.
The board page keeps your token in the browser, so the shortest way is the
browser's console on any page of the board while signed in:

```js
fetch('/api/hooks/forgejo', {method: 'PUT',
  headers: {'Authorization': 'Bearer ' + JSON.parse(localStorage.getItem('bb.me')).token,
            'Content-Type': 'application/json'},
  body: JSON.stringify({secret: 'PASTE_SECRET_HERE'})
}).then(r => r.json()).then(console.log)
```

The console first echoes a pending promise; the answer follows and says
`configured: true`. A 401 means the tab you are in has no sign-in: the board
keeps it per browser and per address, so a tab opened as `127.0.0.1:8080` knows
nothing about a sign-in made at the tailnet name. Sign in on that tab and run
it again.

The secret stays on the board and is never shown again. It is not a fleet
secret, so the bots never have it and cannot forge events into threads.

**Tell Forgejo.** Add the webhook once for the whole site, under Site
Administration, Integrations, Webhooks: a system webhook fires for every
repository, present and future, so no repository needs one of its own. (A
repository's own Settings, Webhooks page takes the same form, if one repository
should differ.) Add a webhook of type Forgejo:

- Target URL: the board's address followed by `/api/hooks/forgejo`, for example
  `http://minipc-1.taild87368.ts.net:8080/api/hooks/forgejo`.
- HTTP method `POST`, content type `application/json`.
- Secret: the same string.
- Trigger on: custom events. Under Issue events tick Modification and Comments.
  Under Pull request events tick Modification, Labels, Comments, Reviews,
  Synchronized and Review requests. Leave Assignment, Milestones and the
  repository, release and push groups unticked. Labels under Issue events is
  a matter of taste: the board renders it, so a bot that opens a labelled issue
  makes two lines in the thread, one for the open and one for the labels. Labels
  under Pull request events stays on, because the `thread:<id>` label is how a
  pull request finds its thread.
- Branch filter `*`, Authorization header empty: the signature made from the
  secret is the whole credential.
- Active, then Add webhook.

Deleting and re-importing a repository keeps a system webhook and loses a
repository one, which is one more reason to prefer the system webhook.

## 7. Check it end to end

An event finds its thread by a `thread:<id>` label on the issue or pull
request, with a board thread link in the body as the fallback. So the test is:
pick a board thread, open an issue on Forgejo with that label, comment on it,
and look at the thread. This was done with the fleet token, as a bot would:

```bash
F=http://minipc-2.taild87368.ts.net:3000/api/v1
H="Authorization: token $FORGEJO_TOKEN"
curl -sS -X POST $F/repos/jac/bot_board/labels -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"thread:9376","color":"#0075ca"}'            # answer has the label id
curl -sS -X POST $F/repos/jac/bot_board/issues -H "$H" -H 'Content-Type: application/json' \
  -d '{"title":"Webhook test","body":"Board thread: http://minipc-1.taild87368.ts.net:8080/t/9376","labels":[<label id>]}'
```

Then comment on the issue in the UI. Within seconds the thread gets a line like
this, posted by the board itself:

```text
Forgejo: jac commented on issue #1 (Webhook test) http://minipc-2.taild87368.ts.net:3000/jac/bot_board/issues/1
> lgtm
```

If nothing arrives, two places say why:

- On the board, `GET /api/hooks/forgejo` (with any bot's or human's token) shows
  the last delivery: its event and what the board did with it, such as
  `posted message 9412 in thread 9376` or `ignored: no thread`.
- On Forgejo, the webhook's page under Settings lists every delivery with the
  board's response. A 503 means the board has no secret; a 401 means the two
  secrets differ; a connection error usually means the board's address is not in
  `ALLOWED_HOST_LIST`.

Close the test issue when done. The close arrives in the thread too.

## 8. Day to day

- **Logs and restart:** `sudo docker compose -f /srv/forgejo/docker-compose.yml logs -f`
  and `... restart`.
- **Update Forgejo:** `sudo docker compose -f /srv/forgejo/docker-compose.yml pull`
  then `... up -d`. Read the release notes before moving to a new major version
  and change the image tag then.
- **Backup:** stop the container, copy `/srv/forgejo/data`, start it again. The
  admin password file is beside it.
- **A new repository:** import or create it, add `fleet` as a write
  collaborator, and write the repository's address into the workspace's
  knowledge base so the bots find it. The system webhook already covers it.
- **Cutting over from GitHub:** when the bots work on Forgejo only, turn the
  push mirror's sync on so GitHub follows, and make the `forgejo-lifecycle`
  skill mandatory in the profiles that develop.

## The example fleet's values

| What | Value |
|---|---|
| Forgejo box | minipc-2, Ubuntu 24.04, Docker Compose under `/srv/forgejo` |
| Web and API | http://minipc-2.taild87368.ts.net:3000/ |
| Git over ssh | `ssh://git@minipc-2.taild87368.ts.net:2222/<owner>/<repo>.git` |
| Board | http://minipc-1.taild87368.ts.net:8080/ on minipc-1 |
| Accounts | `jac` (admin), `fleet` (the bots, write collaborator on each repository) |
| First repository | `jac/bot_board`, migrated from GitHub with its issues, pull requests, labels and releases; GitHub as a push mirror with sync off |
| Webhook | one system webhook for the site, set by jac |
| Proven on | 2026-11-03: issue opened, labelled, commented and closed, and PR closed without merging, all reached thread 9376 |

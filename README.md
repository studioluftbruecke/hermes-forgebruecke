# Forgebrücke

**A bridge to your forge.** Forgebrücke connects Hermes Agent to your self-hosted
[Forgejo](https://forgejo.org) (and, where the API matches, Gitea): a desk in Hermes Desktop for
what waits for you, agent flows that decide how a change travels to `main`, and a mention channel
that lets the forge talk back.

Status: **v0.2, internal build** (not in a catalog yet). This build ships:

- `forgejo_doctor` (also `hermes forgebruecke doctor`): the forge's version, who each token belongs
  to, its scopes and repos, and which features your setup unlocks, switches off or leaves dark, and why.
- `forgejo_get` and `forgejo_list`: issues, pull requests (head, checks with who posted them,
  reviews, files, diff), branches, repos, releases, commits, and filtered lists across repos.
- `@forgejo:owner/repo#123` (or the issue's web address) in any message: the item arrives expanded.
- `forgejo_comment`, `forgejo_create_pr`, `forgejo_status`: writes as Hermes' own Forgejo account,
  only on repos where that account has write access, each shown to you with its exact text and
  approved by you in Hermes. Refused in cron jobs and single-query runs. Never approves, never merges.
  Every pull request Hermes opens asks you (setting `you`) to review it.
- Every open pull request asks you to review it (`review_every_pr`, on by default): a background check
  asks you on each open pull request that does not ask you yet, however it was opened (another tool, a
  script, the Forgejo web page), in every repo where Hermes' account has write access. It skips the
  pull requests you opened and the ones you already reviewed, and asks once.
- The mention channel: mention Hermes' account in an issue or pull request, and Hermes answers there
  (👀 first, one session per thread, at most three answers per thread per hour). On by default in
  every repo where its account may write; only people with write access to the repo start a turn,
  Hermes' own comments never do. A mention turn cannot write through tools; its answer is the write.
- Receipts: when a reply says it pushed a commit, opened a pull request or merged one, on a repo
  Hermes' account sees, Forgebrücke re-reads the forge and appends up to three lines, `✓ … verified at
  <sha>` or `❌ … not found on the forge`, or `not verified: the forge did not answer`.
- Flows (`forgejo_flows`): a small YAML file says how a change reaches the target branch, for example
  an agent review by one of your Hermes profiles, the checks devops posts, an optional merge into
  `staging`, then you. The engine posts one status per gate (`forgebruecke/<flow>/<gate>`), keeps
  one status comment per pull request up to date, mentions you when a gate fails, and resets only the
  failed gate after a push. A flow runs on a repo only when that repo's rule lists the flow id in
  `flows` and Hermes' account has write access there. Templates: `solo-with-agents`, `one-review-then-me`, `staging-first`. Flows never merge
  `main`. They tick with the mention channel's poll; without the channel, run `hermes forgebruecke
  tick` from cron.
- Repos (`forgejo_repos`): an overview of a repo (access, branch protection, open pull requests); a
  best-practice pack (issue and PR templates, CODEOWNERS, a README skeleton, labels) as a pull
  request; a branch-protection preset (`solo-with-agents`, `team-of-two`) shown as a diff first, on
  repos where the agent's account is admin; and, when you switch `audit_enabled` on, a read-only
  security audit with at most five findings per repo, each with a link and a fix. It also imports a
  public repo from GitHub or another forge into the agent's account (with a report of what came
  over), sets a push mirror to another repo on your forge, and creates a repo from a template repo
  (the agent account token needs `write:user` for that; otherwise it gives you the web-form link). Every write
  here asks for your approval in Hermes. Protection, presets and the audit's admin checks work only on
  repos where Hermes' account is admin.
- The desk (Hermes Desktop): a sidebar row "Forgejo · N" with the pull requests waiting for you (the
  ones that request your review, the ones that do not ask you yet, marked so, and the ones whose flow
  reached you), a page with Waiting for me,
  Running, Blocked and Repos (every repo Hermes' account sees, its right there and a link to change
  it), each row with its gates, checks and last comment and a link into Forgejo, and a settings page.
  With a write token of your account, a row whose flow waits for you or that requests your review also
  gets a Merge button that merges as you, pinned to the commit the desk showed.
- Learn mode (`learn_mode`): after an answer about your first pull request, your first failed check
  or your first merge, a short lesson card in plain words, once per person.
- A bundled skill that teaches the agent the Git words, the flow words and the rules above.

## Setup

1. Pick one Forgejo account for Hermes (for example `hermes`). On each repo Hermes should see, add it
   under Settings → Collaborators with **Read** or **Write**. That right is the only permission layer:
   Hermes reads where it may read and writes only where it may write; other repos stay invisible to it.
   Signed in as that account, create a token for all repositories with `read:notification`,
   `write:issue`, `write:repository` and `read:user`.
2. In Hermes Desktop, open Settings, Plugins, Forgebrücke. The page opens with how the plugin works
   and what it promises, then walks these steps; each step's button opens its field under Agent
   settings. Enter the Forgejo address and the **Agent account token** there.
3. Enter **Your Forgejo login** (`you`). The settings page suggests the owner of the repos Hermes sees.
   The desk then lists the pull requests that request your review.
4. Ask Hermes "run forgejo_doctor", or run `hermes forgebruecke doctor`.
5. Optional, git as Hermes' account: run `hermes forgebruecke git-credential` in the profile that holds
   the token. It prints three `git config` lines with that profile and this host's `hermes` filled in.
   Run them in any shell whose git should push and pull at your forge with the agent account token,
   or with `git -C <clone> config --local` for a clone that several profiles share. The token stays
   where the settings page saved it; git asks Forgebrücke for it and keeps the answer in memory for
   an hour. Forgebrücke answers only for your forge address, so other hosts keep their own helpers.
6. Optional, the mention channel (the doctor names whatever is still missing):
   - choose the tools a mention may use, for example only Forgebrücke's own:
     `hermes config set platform_toolsets.forgebruecke '[forgebruecke]'`. The channel refuses to
     start without this, because a mention starts an agent turn on a forge user's text;
   - switch the platform on: `hermes config set platforms.forgebruecke '{enabled: true}'`;
   - mentions are on in every repo where Hermes' account may write; a rule with `mentions: false`
     switches one off.

## Keys: two fields

| Field | Who it belongs to | Used for | Never used for |
| --- | --- | --- | --- |
| Agent account token (`FORGEBRUECKE_BOT_ACCOUNT_TOKEN`, required) | the Forgejo account Hermes works as | every read and write of Hermes, mention answers, flow runs, the desk | anything as you |
| Your write token (`FORGEBRUECKE_PERSONAL_WRITE_TOKEN`, optional, empty by default) | you | the desk's Merge button, on your click | agent tool writes, mention turns, flows |

Every agent shell on the Hermes host can read the write token and could merge into `main` as you.
Leave it empty unless you accept that; you can always merge in Forgejo. The doctor lists what your
setup lights and, for each feature it cannot light, what would. A mention turn reads only its own
thread's repo, so a forge user cannot make Hermes quote another repo.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `url` | (none) | Your Forgejo address |
| `token` (secret, `FORGEBRUECKE_BOT_ACCOUNT_TOKEN`) | (none) | The token of Hermes' Forgejo account; every read and write of Hermes uses it |
| `you` | (none) | Your Forgejo login: Waiting for me lists the pull requests that request your review, and Hermes asks you to review every pull request it opens |
| `owner_token` (secret, `FORGEBRUECKE_PERSONAL_WRITE_TOKEN`) | (none) | Optional write token of your own account; only the desk's Merge button, on your click |
| `poll_interval` | 120 | Seconds between forge checks (30 to 3600) |
| `mention_authors` | empty (everyone with write access) | Narrows whose mentions Hermes answers |
| `writes` | empty | Optional per-repo rules: `repo_glob`, `flows`, `mentions` (default on), `targets` (branches a pull request may target; default the repo's default branch). Rules grant no access; the Forgejo right does |
| `flows_path` | `.forgebruecke/flows.yml` | Where a repo keeps its flow |
| `audit_enabled` | off | Read-only security audit |
| `learn_mode` | off | Lesson cards after a first pull request, a first failed check, a first merge |
| `default_review_profile` | empty (the profile Forgebrücke runs in) | The profile that runs a flow's review gate; any profile you have works |
| `review_every_pr` | on | The background check asks you to review every open pull request that does not ask you yet, where Hermes' account may write; the desk lists such a pull request before that too |
| `dry_run` | on | Writes are previewed, not sent |

An invalid value falls back to its default, and the doctor names it. Settings of earlier versions
(`read_scope`, `badge_meaning`, and `read`, `comment`, `write` in a rule) are ignored with a warning.

## What it does

| Surface | What it does |
| --- | --- |
| **Merge desk** (Desktop) | One list of pull requests waiting for you (review requested, not requested yet, or a flow reached you), each with its gates, checks and the last comment, and a link to review or merge in Forgejo |
| **Sidebar row** | "Forgejo · 3": the pull requests waiting for you |
| **Flows** | A small YAML file: an agent review, required checks, an optional staging step, then you. Forgejo enforces the hard parts, the plugin orders the steps |
| **Mention channel** | Mention Hermes' account in an issue or pull request. It reacts 👀, keeps one session per thread and answers people with write access there. It polls notifications, so no inbound port |
| **Forge receipts** | Each claim in a reply ("pushed", "opened a PR", "merged") is re-read from the Forgejo API and marked ✓ or ❌ |
| **Repos and access** | Access and protection per repo, best-practice packs, protection presets, the audit |
| **Import** | A public project from GitHub or another forge into the agent's account, a push mirror while both run, repos from templates |
| **Learn mode** | Lesson cards for people who have never used Git |
| **Tools** | `forgejo_doctor`, `forgejo_get`, `forgejo_list`, `forgejo_comment`, `forgejo_create_pr`, `forgejo_status`, `forgejo_flows`, `forgejo_repos`, and `@forgejo:owner/repo#123` in any chat |

Off-device renders of the desk and its settings page (recorded route answers, Hermes' Chromium):
`experiments/forgebruecke/m7/render/` in the monorepo.

## Your forge, your rules

- **The merge is yours.** No code path approves or merges `main`. The desk gives you the link; the
  only merge the plugin performs is a staging step a flow names explicitly.
- **Forgejo is the permission layer.** What Hermes may read or write in a repo is its account's
  collaborator right there, which you set in Forgejo; the desk's Repos tab shows it with a link to
  change it. Every write still asks for approval; in unattended runs only what a flow names for that
  gate is allowed, and only when the repo's rule lists that flow.
- **What the approval covers.** The card shows the repo, what is written (a comment on PR #3, a
  pull request `a → main`, a status) and the start of the text. Hermes' terminal card shows about
  ten lines; a longer comment is cut there with "description truncated". "Allow for this session"
  and "Add to permanent allowlist" are Hermes' own choices: they apply to that tool on that repo
  (`forgebruecke:<tool>:<repo>`), so later writes of the same kind stop asking. "Allow once" asks
  every time. If you switch approvals off in Hermes (`--yolo` or Approvals: off), Hermes stops
  asking for every tool, this one included; the Forgejo rights and the unattended refusal still hold.
- **Read-only by design.** The plugin reads the forge, it does not manage the instance: no users, no
  backups, no runner installs.
- **Nothing hidden.** One data file lists the per-repo rules and the flows; you can read and edit
  both, and Hermes can rewrite them for you through `forgejo_flows`.
- Comments by Hermes carry an AI-disclosure footer.

## Disclosures

Binding from the first release.

- Network calls: only to the Forgejo instance you configure, polled on an interval you set. The
  doctor reads `/api/v1/version` and the instance's own `/swagger.v1.json`. Repo rights are read from
  `GET /repos/{owner}/{repo}` and `GET /user/repos` and kept for a minute per process.
- Scope detection: to learn which write scopes a token has without changing anything, the doctor
  sends requests the forge must reject: an empty `POST` to issues and pulls of one repo the token
  may push to (answered 422), and a `PATCH` of a notification thread that does not exist (answered
  404). A token without the scope gets 403 instead. Nothing is created.
- Unattended agent turns: a mention by someone with write access to the repo (narrowed by
  `mention_authors` when set) starts an agent turn without you at the keyboard, with only the tools
  `platform_toolsets.forgebruecke` names. Forgejo shows another account's right only to repo admins,
  so a non-admin Hermes account checks the repo's assignable accounts (write access or more) instead.
  Issue and comment text is untrusted input; forge writes through tools are refused in that turn.
  The answer is posted as Hermes' account; its own comments never start a turn.
- Guardrails: in a profile with the plugin on, a tool call that would edit that profile's `config.yaml` or
  `.env`, the plugin copy it loaded, or change that profile's settings or this plugin through the `hermes`
  command, asks you first (refused when no one can answer). Other profiles, other repos' files and other
  checkouts of this plugin are left alone, so a profile that develops code can keep it on. This catches the direct routes, not every script; keep Hermes' own
  approvals on.
- Writes (in this build), each after your approval: comments (including line comments, sent as a
  `COMMENT` review), pull requests with labels and review requests, commit statuses, flow files (as a
  branch plus pull request), pack files (the same) and labels, branch-protection rules (presets),
  repo imports into the agent's account, push mirrors (the agent account token is stored in Forgejo's mirror
  settings), repos from templates.
- Credentials: the token of the Forgejo account Hermes works as, with minimal scopes
  (`read:notification`, `write:issue`, `write:repository`, `read:user`), stored as
  `FORGEBRUECKE_BOT_ACCOUNT_TOKEN` in your Hermes profile's `.env` by the settings page. An optional
  write token of your own account (`FORGEBRUECKE_PERSONAL_WRITE_TOKEN`, empty by default) is stored
  the same way; it only lights the desk's Merge button. The names carry the plugin's prefix on
  purpose: a host that already exports `FORGEJO_TOKEN` for another tool never lends that token to
  this plugin. Any secret stored for Hermes is readable from an agent shell on the same host: every
  agent shell there could merge into `main` as you with the write token, so keep it empty unless you
  accept that, and keep each token's scopes narrow.
- Git credential helper (only where you set it up with `hermes forgebruecke git-credential`): git
  starts `hermes -p <profile> forgebruecke git-credential get`, which prints the agent account token for
  a request naming your forge address and nothing for any other host. It never prints your write
  token. `store` and `erase` change nothing. Git's `cache` helper keeps the answer in memory for an
  hour; nothing is written to disk.
- Runs inside the Hermes gateway process, because platform adapters need the in-process plugin mode.
- State: small pointers in Hermes' plugin state (10 MB quota); run records as files in the plugin's
  data folder of your profile.
- Receipts: after a reply that claims a push, a pull request or a merge on a repo Hermes sees, up to
  three read requests go to the forge (branch, commit or pull request) after one repo listing, and each receipt is appended
  to `receipts.jsonl` in the plugin's data folder (the claim, the SHA, the verdict; no reply text).
- Flows (when a rule lists one): the engine starts `hermes -p <profile> chat --query-file <prompt> -t file`
  as a background process for each review gate (at most two at once), with a minimal environment
  (no secrets of the gateway) and an ephemeral system prompt that marks it as a review worker, whose
  turn reads nothing from the forge; the engine reads the verdict file it writes in the plugin's
  data folder.
  Unattended, it posts commit statuses in its own `forgebruecke/<flow>/` namespace, one status
  comment per pull request, notify comments, and, for a `promote` gate, opens and merges a pull
  request into the unprotected branch the flow names. Prompts, verdicts and logs stay in the data
  folder.
- Review requests (with `review_every_pr`, on by default, and `you` set): every poll interval the gateway
  reads one issue search for open pull requests and one pulls list per repo that has one, plus the
  reviews of a pull request that does not ask you yet. Unattended, without an approval card, it asks
  you to review each such pull request where Hermes' account has write access (one
  `requested_reviewers` request each, once). It never asks anyone else. With `dry_run` on it sends
  nothing.
- The desk (Desktop): the page reads only this plugin's own routes on your Hermes backend (`/desk`,
  `/settings`), never Forgejo; the backend reads with the agent token: one issue search for open pull requests and one pulls list per repo that has one (for the review requests). The Merge button calls
  `/merge`, which merges with the write token of your account, pinned to the commit the desk showed;
  it exists only when that token can merge and a flow waits for you. "Create a reviewer profile" runs
  `hermes profile create forgebruecke-reviewer --clone-from <your profile> --no-alias` on your click
  and removes every `FORGEBRUECKE_*` key from the new profile's `.env`.
- Learn mode: which lessons a person has seen is kept in `lessons.json` in the plugin's data folder.
- No telemetry, no hosted service.

## Design and plan

- Product: `experiments/forgebruecke/PRODUCT.md`
- Brand and mark: `experiments/forgebruecke/BRAND.md`
- Implementation plan, milestones, decisions: `experiments/forgebruecke/PLAN.md`
- Measured probe results: `experiments/forgebruecke/probes/RESULTS.md`

## License

MIT, see `LICENSE`. The Forgejo logo is used, unmodified, where the plugin says "for Forgejo"; it is
by Caesar Schinas and licensed CC BY-SA 4.0.

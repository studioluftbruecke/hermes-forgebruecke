# Forgebrücke

**A bridge to your forge.** Forgebrücke connects Hermes Agent to your self-hosted
[Forgejo](https://forgejo.org). Gitea works where its API matches. Hermes works there as its own
account, you approve what it writes, and the merge stays yours.

## What you get

- **Desk** in Hermes Desktop, or `hermes forgebruecke desk` in a terminal. The pull requests that
  wait for you, each with its state in one sentence.
- **Chat.** Ask about issues, pull requests, checks and repos. `@forgejo:owner/repo#123` pulls one
  into a message.
- **Writes.** Comments, pull requests and checks, each shown to you first.
- **Receipts.** "Pushed", "opened" or "merged" in a reply is checked against Forgejo and marked ✓ or ❌.
- **Flows.** A small file per repo: agent review, checks, optional staging, then you.
- **Mentions.** Mention Hermes' account in a thread and it answers there.
- **Repos.** Access overview, starter pack, protection presets, audit, import, mirrors, templates.
- **Learn mode.** Short Git lessons at your first pull request, failed check and merge.

## Install

Forgebrücke needs Hermes 0.23.0 or newer. On the machine that runs Hermes:

```bash
hermes plugins install <repository URL>#plugins/forgebruecke
hermes plugins enable forgebruecke
```

Then start a new chat.

## Setup

1. **A Forgejo account for Hermes.** Create a user, for example `hermes`. Add it to each repo under
   Settings → Collaborators. Signed in as that user, create a token for all repositories with
   `read:notification`, `write:issue`, `write:repository`, `read:user`.
2. **Address and token.** In Hermes Desktop open Settings → Plugins → Forgebrücke. Enter your
   Forgejo address and Hermes' token.
3. **You.** Click "Use @your-name" on the Forgebrücke page, so the desk knows whose reviews to list.
4. **Let Hermes write.** A new setup only previews writes. Switch on "Let Hermes write".

The Forgebrücke page names the next step. `hermes forgebruecke doctor` prints the same check in a
terminal.

Optional:

- **Merge from the desk.** Add your write token, a token of your own account with
  `write:repository` and `read:user`. Any agent shell on the Hermes host can read it and could merge
  as you.
- **Git as Hermes' account.** `hermes forgebruecke git-credential` prints the three `git config` lines.
- **Mentions.** Run these two commands. The doctor names what is still missing.

  ```bash
  hermes -p <profile> config set platform_toolsets.forgebruecke '[forgebruecke]'
  hermes -p <profile> config set platforms.forgebruecke '{enabled: true}'
  ```

## What you can ask

- "What waits for me on Forgejo?"
- "Summarise @forgejo:owner/repo#12 and tell me what to check before I merge."
- "Set up a flow for owner/repo: one agent review, then me."

## Your rules

- **The merge is yours.** Nothing approves or merges `main`. A flow can merge only into an
  unprotected branch you named, such as `staging`.
- **Forgejo decides where.** Hermes reads and writes only where its account has access.
- **You approve each write.** "Allow for this session" and "always" apply per tool and repo. With
  Hermes' approvals off, writes go out unasked.
- **Unattended runs do not write** (cron jobs, single-query runs, mention turns), except a flow you
  wrote and review requests to you.
- **A new commit or a changed flow is checked again.** A push or a higher `version` in the
  flow file runs every gate again. After a failed or edited gate, that gate and the later ones run.
- **One flow per repo.** A rule that lists several flows runs only the first one.

## If something is wrong

- **Start with the doctor.** `hermes forgebruecke doctor` names the next step in its last line, and why
  a feature is not available.
- **A write changed nothing.** A new setup only previews. Switch on "Let Hermes write".
- **A write was refused.** The refusal names the missing access and links the repo's Collaborators page.
- **The desk lists no reviews.** Check that "You" is set (setup step 3).

## Settings

"You" and the four switches are on the Forgebrücke page. Everything else is in Hermes' settings.

| Setting | Default | What it does |
| --- | --- | --- |
| `url` | | Your Forgejo address |
| `token` (`FORGEBRUECKE_BOT_ACCOUNT_TOKEN`) | | Hermes' token |
| `you` | | Your Forgejo user name |
| `owner_token` (`FORGEBRUECKE_PERSONAL_WRITE_TOKEN`) | | Your write token, for the desk's Merge button |
| `dry_run` | on | Preview writes, send none |
| `review_every_pr` | on | Ask you to review every open pull request |
| `learn_mode` | off | Lesson cards |
| `audit_enabled` | off | Allow the read-only audit |
| `writes` | empty | One rule per repo: `repo_glob`, `flows`, `mentions`, `targets` |
| `flows_path` | `.forgebruecke/flows.yml` | Where a repo keeps its flow |
| `default_review_profile` | this profile | Profile that runs a flow's review |
| `mention_authors` | everyone with write access | Narrows whose mentions get an answer |
| `poll_interval` | 120 | Seconds between polls of Forgejo (30 to 3600) |

## Disclosures

- **Network.** Only your Forgejo, polled on your interval. The doctor also reads its
  `/swagger.v1.json`.
- **Scope detection.** The doctor learns a token's write scopes from requests that Forgejo must
  reject. They are an empty `POST` to issues, pulls and `/user/repos`, and a `PATCH` of a missing
  notification. Nothing is created.
- **Credentials.** Two tokens in your profile's `.env`, written by Hermes' settings. Any agent shell
  on that host can read them. The plugin never reads `FORGEJO_TOKEN` or another tool's token.
- **Writes after your approval.**
  - Comments with an AI-disclosure footer, and line comments as a `COMMENT` review
  - Pull requests with labels and review requests
  - Checks on a commit
  - Flow and pack files, as a branch plus a pull request
  - Labels and protection presets
  - Imports into Hermes' account
  - Push mirrors. Forgejo stores Hermes' token in the mirror's settings
  - Repos from templates
- **Writes without a card.** Review requests to you (`review_every_pr`). A flow you wrote posts
  checks named `forgebruecke/<flow>/…`, one status comment per pull request and notify comments. Its
  `promote` gate opens a pull request and merges it into the branch the flow names. None of them
  with `dry_run` on.
- **Unattended agent turns.** A mention by someone with write access starts a turn without you. The
  turn gets only the tools `platform_toolsets.forgebruecke` names, reads only its thread's repo and
  cannot write through tools. Its answer is posted as Hermes' account. Issue and comment text is
  untrusted input.
- **Background processes.** A flow's review gate starts `hermes -p <profile> chat --query-file …`,
  at most two at once. Each gets a minimal environment and no access to Forgejo, and writes a
  verdict file for the engine.
- **The desk.** The page calls only this plugin's routes on your Hermes backend. `/merge` merges with
  your write token, pinned to the commit the desk showed. `/setting` saves `you` and the four
  switches. "Create a reviewer profile" runs `hermes profile create` and removes every
  `FORGEBRUECKE_*` key from the new profile. "Ask Hermes" opens a chat with a draft and sends nothing.
  A process that can call these routes can do the same, so keep Hermes' approvals on.
- **Guard.** A tool call that would edit the profile's settings or secrets, this plugin's files or
  its data folder asks you first. The guard catches direct routes, not every script.
- **Git credential helper** (only where you set it up). It prints Hermes' token for your Forgejo
  address, nothing for another host, and never your write token.
- **Process and state.** The plugin runs inside the Hermes gateway. State, run records,
  `receipts.jsonl`, `desk-actions.jsonl` and `lessons.json` live in your profile's plugin data folder.
- **`@forgejo:` in any message expands**, including text you paste.
- No telemetry, no hosted service.

## License

MIT, see `LICENSE`. The Forgejo logo, used unmodified, is by Caesar Schinas and licensed CC BY-SA 4.0.

# Changelog

## 0.0.24

- Merge shows on every row under Waiting for me that Forgejo lets your account merge into its target branch (your
  repo permission and the branch's protection rule), also when nobody asked you to review it. It is the row's main
  button then. A draft, a pull request Forgejo cannot merge yet, and a flow row whose merge waits for someone else
  still have none. The desk asks Forgejo once per repo and branch every 5 minutes, with your write token.
- On a row that asks for your review, "Review in Forgejo" stays the main button, with the green Merge right above it.
  On every other row you may merge, Merge is the main button.
- A flow row asks for the merge right on the target branch Forgejo names now, so a pull request retargeted since the
  last flows check shows the right Merge button, and the merge dialog names the new branch.
- A merged pull request leaves Waiting for me on the click, and the sidebar number follows. A pull request with a
  flow also leaves at once when it was merged in Forgejo, without waiting for the next flows check.
- The desk read after a merge no longer checks your write token's scopes again, so it answers faster.
- Every Merge button is green: on the row and "Merge as me" in the dialog. It takes the theme's green, darkened so its
  white text stays readable in light and dark.
- Ask Hermes has one more item at the end, "Ask something else…". It drafts only the pull request's reference, so you
  type your own question. Nothing is sent.
- A row's actions stack in one narrow column at its right (Merge first), so the title and the checks get the width.
- Waiting for me has a row of repository buttons at the top once more than one repository waits: All, then one per
  repository with its count. A click shows only that repository's pull requests. The buttons keep their places, and
  the choice holds through a refresh and a tab switch. When a repository's last pull request leaves, its button goes
  and the list shows All again.
- The Repos tab shows the repos as a list or as a grid of cards (3 per row in a 1280 px window, 4 from 1600 px). The
  switch sits above the repos, and Desktop keeps your choice per profile.
- Each repo shows its open pull request count. A click on it expands the repo: its default branch's latest commit
  with its checks, then each open pull request with title, author, age, checks, Merge (the same rule and green as
  Waiting for me), Open in Forgejo and Ask Hermes. The pull requests are read when you expand, not with the page.
  A merge there updates the count, the list and Waiting for me on the click.
- Each repo also has "<n> waiting for you", which opens Waiting for me filtered to that repo, and "Ask Hermes about
  this repo", which drafts "What changed here this week?" with the repo's reference. Nothing is sent.
- In the grid, every card of a row has the same height, and the open pull request lines sit on one level, also when
  one card carries a badge such as "flow demo" or "read only".
- The desk loads about three times faster. It read Forgejo one answer after another; it now keeps 4 reads in flight.
  With 16 pull requests waiting on a real Forgejo, the desk took 10.4 s and now takes 3.7 s (median of 5 reads each),
  with the same 46 requests. Expanding a repo in the Repos tab reads its checks the same way. The flows tick still
  reads one at a time, so a rate limit still stops it at the first refused request. When one desk read fails (a rate
  limit, say), the desk starts no further reads; only the up to 3 already under way finish.

## 0.0.23

- "Create a reviewer profile" removes Forgebrücke's two tokens from the new profile with
  `hermes -p forgebruecke-reviewer config unset`, so Hermes edits its own `.env`; the plugin no longer reads or
  rewrites that file. When a removal fails, the settings page shows the commands still to run, one per line.

## 0.0.22

- The sidebar row says Forgebrücke, with the number of pull requests waiting for you: "Forgebrücke · 3" (was
  "Forgejo · 3"). Desktop takes the number from the desk's counts, so the row shows the new name with an older
  Hermes too.
- The chat tab that Ask Hermes opens is called Forgebrücke (was Forgejo).
- A Sidebar section at the end of the settings page: Show in the sidebar, and Position (lower numbers sit higher;
  Kanban is 50). Both are kept on this Desktop. The row now sits at 55, below Kanban with the other Studio
  Luftbrücke plugins (was 56). A hidden row's desk opens from the command palette: Open Forgebrücke.

## 0.0.21

- Forgejo is checked every 30 seconds by default (was 120).
- `/desk` and `/settings` say which version answers.
- The desk's note about late flows shows only when a row of a flow is on the desk.
- The settings page says when mentions and review requests stopped, and what to do.
- With Security audit on, the settings page lists repos whose default branch has no protection rule.
- A token saved after this profile's plugin host started is reported, with a button that reloads it
  (`/reload-tokens`).
- A parking area under Waiting for me. Park sets a pull request aside with a note only you see; parked rows
  leave the count and the sidebar number, and come back on their own when a draft is marked ready, on new
  commits (unless you untick that), or when someone else comments, with a Back badge, the reason, your note
  and a "What changed" link. Unpark and Undo bring a row back at once. Routes `/park` and `/unpark`.
- Comment as you: a comment icon on a waiting row posts your words with your write token, as you, and reads
  them back. It needs `write:issue` on your write token (now in the recommended scopes); a token without it
  keeps Merge. Route `/comment`. The settings guard asks before a shell call to `/comment`, `/park` or
  `/unpark`.
- The desk as text and `hermes forgebruecke desk` show parked rows: `6 waiting (1 parked)`.
- Tagline "A bridge to your Forgejo." No text says that every write asks: writes follow Hermes' approval
  setting (with approvals off, they go out unasked). The write token's risk is his sentence, the same on the
  settings page and in the manifest: it is also exposed to Hermes, ultimately giving Hermes full access to
  your account.
- Desk and settings use the full width of the pane.
- Rows read `#12 Title`; the line under the title no longer repeats the number.
- The page is called Forgebrücke (was Forgejo desk), the palette says "Open Forgebrücke", and the headers draw
  the decided mark with its wings, in one colour.
- The rules on the settings page show their text under each headline, in Konstantin's words: "Forgejo decides
  what Hermes may do" (with the write-token warning) and "You approve the important part".
- When the Desktop half and Hermes run different versions, the desk and the settings page say so, with the
  update command.
- A waiting row without a Merge button says why in its tooltip (a draft, or Forgejo cannot merge it yet).
- The mention channel stops with "Restart the gateway to finish the update" when an update only half
  arrived, instead of failing every poll; it also reads a ledger built by an older copy.

## 0.0.20

- Docs only: a shorter README with install, first use and uninstall, `CONTRIBUTING.md`, and
  screenshots and a banner in `docs/`. The code is the same as 0.0.19.

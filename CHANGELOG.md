# Changelog

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

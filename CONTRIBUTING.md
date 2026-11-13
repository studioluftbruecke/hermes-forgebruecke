# Contributing to Forgebrücke

Thanks for helping. This file says how to report a problem, how to run the tests, and how a pull
request lands.

## Report a bug or propose a feature

Open a GitHub issue on this repo.

- **Bug:** what you asked Hermes or clicked, what happened, what you expected, your Hermes version
  (`hermes --version`), the plugin version (`hermes plugins list`) and your Forgejo version. For a
  token or setup problem, add the output of `hermes forgebruecke doctor`. Remove host names, repo
  names and tokens first.
- **Feature:** the job you want to get done, and why the current plugin does not do it.

Pick **Bug report** or **Feature request**. A plain issue is fine for anything else. A security
problem never goes into a public issue: use **Security → Report a vulnerability** on this repo.

## Run the tests

From a checkout of this repo, with Hermes installed:

```bash
python -m pytest tests -q -p no:cacheprovider --import-mode=importlib
hermes plugins validate .
```

`pytest` 8 is enough. The tests need nothing else and never call a real Forgejo: they run against
an in-memory fake forge (`tests/fb_fakes*.py`). `hermes plugins validate` is the catalog's own gate:
manifest, declared capabilities, security scan and the Desktop lint.

## How pull requests land

Forgebrücke is developed in a private workbench, and this repo receives release commits only. So a
pull request here is never merged on GitHub. When we accept it, we port the change by hand into the
workbench with a `Co-authored-by:` line that credits you, and it ships in the next version. Then we
close your pull request with a link to that release. "Closed" means "landed", not "rejected".

## Code style

- **Python:** standard library and Hermes only, handlers importable without Hermes. Writes to
  Forgejo go through the approval and unattended checks, never around them.
- **JavaScript (`desktop/plugin.js`):** plain ESM, no build step, imports only `@hermes/plugin-sdk`,
  `react` and `react/jsx-runtime`, no `eval`.
- **Copy:** short sentences, one next action per state. The README tests check sentence length.

## Licence and conduct

Forgebrücke is MIT licensed. By contributing, you agree that your contribution is licensed under
MIT too.

We follow the [Contributor Covenant 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).

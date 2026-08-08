# Contributing

Issues and pull requests are welcome. Read this page first, because this
project refuses some changes that other projects accept.

## The safety model is not open to change

[SAFETY.md](SAFETY.md) records what spacefinder can do to your files. It moves
an item to the Trash. That is the complete list.

A pull request that adds any of these will be closed:

- code that deletes a file
- code that runs a shell command, in any form
- a command-line option that reads a rules file from a path

These limits are the reason the tool is safe to run. They replace a list of
forbidden paths, because no such list is complete. The review that produced
SAFETY.md wrote a rules file that reached `~/Library/Keychains` and `~/.ssh`
while it passed every guard.

CI enforces these limits. The `no-dangerous-capability` job reads the source
and fails the build.

## Before you open a pull request

- Run `python3 tests.py`. All tests must pass.
- Add a test for the behaviour that you change. A test that fails before your
  change, and passes after it, is the best kind.
- Keep the code in the Python standard library. spacefinder has no
  dependencies, and that is deliberate.
- Write documentation in ASD-STE100 (Simplified Technical English), like the
  rest of the documentation here.

## What happens to your pull request

Workflows do not start automatically for a pull request from a fork. A
maintainer approves the run first. This is not a comment on you. A pull request
can change the test file, and the test file runs on a machine, so somebody
looks at the change before it runs.

## A new rule

A new rule in `rules.json` is the easiest useful contribution. A rule needs:

- a `why` field that says what the files are, and what makes them safe to
  remove
- the correct `safety` level. Use `safe` only when the system makes the data
  again without help
- an `action` of `trash` for files, or `command` for a command that the user
  runs

Treat `rules.json` as code. It decides which files the tool moves.

## Report a security problem

Do not open a public issue for a problem that lets spacefinder move or destroy
a file that the rules do not name. Use the private report function on GitHub:
**Security → Report a vulnerability** on the repository page.

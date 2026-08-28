# CLAUDE.md

spacefinder finds the files that use disk space on a Mac, and moves some of
them to the Trash. It uses the Python standard library only.

## Before you change behaviour

[SAFETY.md](SAFETY.md) records what this tool may do to a file, and
[CONTRIBUTING.md](CONTRIBUTING.md) lists the changes this project refuses. The
tool moves an item to the Trash. It does not delete files and it does not run
shell commands. CI fails the build on either one.

## The writing standard

`README.md`, `SAFETY.md`, `CONTRIBUTING.md` and `GLOSSARY.md` are written to a
controlled vocabulary, defined in [GLOSSARY.md](GLOSSARY.md). One term, one
meaning. At most 30 words in a sentence and 6 sentences in a paragraph. Active
voice, present tense, and name the actor.

Read the glossary before you write or edit one of those documents. Do not
restate its rules here; this is a pointer, and two copies drift.

`python3 tools/vocab-check/check.py` checks the mechanical part. The rule that
pays, one term with one meaning, is the one no check reaches.

`tools/vocab-check/config.json` names the documents in scope. `notYetConverted`
holds the documents that predate the standard. That list may only shrink: a new
document joins the standard, it does not join the list.

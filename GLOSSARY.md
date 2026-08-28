# Glossary

The controlled vocabulary for `README.md`, `SAFETY.md` and `CONTRIBUTING.md`.
Every term below has one meaning. These documents use these words only in these
meanings, and they use no synonym for a term that appears here.

This file is the authority for those three documents. It does not describe how
the source code names things. Where it is stricter than the source,
[Words we do not use](#words-we-do-not-use) says so.

**How to read an entry.** Bold is the term. Italic is the part of speech. A
**Not:** line gives the words we reject for that meaning, and why. Rejected
words are in `code font`, because this file has to name what it forbids. The
check reads a word in code font as a mention, and the same word in prose as a
use.

---

## What spacefinder measures

**Allocated bytes** *(noun)* — The space that a file uses on the disk. These
bytes become available when you remove the file. spacefinder measures this
number.
**Not:** `size` on its own, which does not say which of the two numbers it
means.

**Apparent size** *(noun)* — The length of a file as the file system reports
it. A sparse file and an APFS-compressed file both use fewer allocated bytes
than their apparent size.

---

## The disk

**Volume** *(noun)* — One APFS file system. A Mac holds several. Preboot,
Recovery and the boot volume are examples.

**Container** *(noun)* — The APFS pool that holds the volumes. Every volume in
one container draws on the same free space, so a scan of one volume cannot
account for the whole disk.

**Snapshot** *(noun)* — An APFS record that holds the blocks of a file after
you delete the file. Time Machine makes local snapshots. This is the usual
reason why a large deletion returns no free space.

**Purgeable** *(adjective)* — Of space that macOS reports as free on demand.
The container releases it when the disk fills. A file scan cannot find this
space, so the accounting section names it separately.

---

## Finding the files

**Scan** *(noun)* — One examination of one set of directories, from the command
to the report.
**Not:** `run`, `pass`.

**Examine** *(verb)* — To read the metadata of the files in one directory.

A **scan** and the verb **examine** are not synonyms, and this pair is
deliberate. The scan is the whole act. To examine is what spacefinder does to
one directory inside it.
**Not:** `probe`, `crawl`, `walk` for the verb. The source calls the internal
routine a walk, and these documents do not.

**Rule** *(noun)* — One entry in `rules.json`. A rule says where to look, what
counts as a match, and what to do about it.

**Generic rule** *(noun)* — A rule with the `generic` field set. A specific rule
wins against it when both select one path.
**Not:** `general` rule. The field is spelled `generic`, and one letter of drift
is harder to see than a different word.

**Match** *(noun)* — One path that a rule selects.
**Not:** `hit`, `result`.

**Item** *(noun)* — The file or the directory that spacefinder moves to the
Trash. Every match is a candidate; an item is what moves.
**Not:** `target`, which also means the file that a symlink points to.

**Safety level** *(noun)* — One of `safe`, `caution` and `manual`. The level
says how much the tool may act on its own.

**Depth** *(noun)* — The number of path components below a root. The guard on a
move measures depth.
**Not:** `level`, which is the safety level.

---

## The scan machinery

**Worker** *(noun)* — One thread that examines directories.

**Block** *(verb)* — A directory blocks when its system call does not return. A
permission question with no answer does this, and so does a file provider mount
that does not respond. No error arrives, and the thread cannot be cancelled.
**Not:** `hang`, `stall`, `wedge`, `stuck`, `unresponsive`.

**Replace** *(verb)* — spacefinder replaces a worker that is blocked. It starts
a new worker, and the blocked worker never returns. The queue then continues.

**Abandon** *(verb)* — spacefinder abandons the scan when too many directories
block. Many blocked directories mean a missing permission and not one bad mount,
and more requests in that condition can cost your terminal a permission it
holds.

**Exit** *(verb)* — Of the program, to end. `--version` shows the version and
exits.

---

## Words we do not use

Each row gives a word to avoid, the reason, and what to write instead.

| Do not write | Why | Write |
| --- | --- | --- |
| **just**, **simply**, **obviously**, **of course** | They tell the reader that their difficulty is their own fault. | delete the word |
| **stop** | Four meanings in these documents: a directory that will not return, a worker that is replaced, a scan that is given up, and a program that ends. One sentence in SAFETY.md used two of them eight words apart. | **block**, **replace**, **abandon** or **exit** — say which |
| **hang**, **stall**, **wedge**, **stuck**, **unresponsive** | Five words for one state. | **blocked** |
| **probe** | A third word for examine, and it reaches the reader in the accounting section. | **examine** |
| **hit** | Another word for a path that a rule selects. | **match** |
| **target** for the item that moves | **target** also means the file that a symlink points to. | **item** |
| **result** for one match | A rule produces matches. The report holds the results of the scan. | **match** |
| **level** for path depth | **level** is the safety level. | **depth** |
| **general** for a rule | The field is spelled `generic`. | **generic** |
| **delete** for what this tool does | spacefinder moves an item to the Trash and deletes nothing. Say what it does. | **move to the Trash** |

The last five rows are conditional: they depend on the sense, so the check
cannot match them without parts of speech. Follow those by hand.

---

## Style rules

These documents follow the *spirit* of ASD-STE100, not its approved word list.
We define our own controlled vocabulary, which is this file. From the
specification we take the rules that earn their keep for a reader who may not be
reading in a first language.

1. **One term, one meaning.** Use the words above, in the meanings above. Never
   introduce a synonym for a defined term.
2. **Short sentences.** At most 30 words. One idea in each.
3. **Short paragraphs.** At most 6 sentences.
4. **Active voice**, and the present tense.
5. **Say who acts.** Write "spacefinder skips the folder", not "the folder is
   skipped".
6. **One instruction per sentence.**
7. **Use vertical lists** for anything with more than two parts.
8. **Keep the articles.** Write "the worker examines the directory", not
   "worker examines directory".
9. **At most three nouns together.** Break up a longer cluster.
10. **Give the mechanism, not only the rule.** A reader who does not know why a
    rule exists will work around it. Mechanism is where we spend our words.

**What is checked, and what is not.** `tools/vocab-check/check.py` enforces
rules 2 and 3, and the rows above that can be matched without parts of speech.
Everything else is followed by hand.

Rule 9 is **not** checked, because finding a noun cluster needs a parser we do
not have. Rule 1 is not checked either, and it is the most important one. No
check knows whether a sentence is true.

---

## Out of scope

This standard governs the three documents named at the top of this file.

Source comments, commit messages and issue replies are out of scope. They argue
and they hedge, and the standard would take that away.

The text in `rules.json` reaches the reader, so its `why` and `note` fields
should follow the vocabulary. The check does not read them, because they are
not prose paragraphs.

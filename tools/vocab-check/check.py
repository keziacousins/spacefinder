#!/usr/bin/env python3
"""Check documentation against this project's controlled vocabulary.

VENDORED FILE. It is checked in on purpose, so the check runs for anyone who
clones the repository. It needs Python 3 and nothing else. Installing the
`controlled-vocabulary` plugin is how these files are authored, not how they
are run.

Naming a denied word. A glossary has to name the words it rules out. Inline
code becomes a placeholder before anything is counted, so a denied word in code
font is a MENTION and passes, and the same word in plain prose is a USE and
fails. Write the deny table, and any "Not:" line, in code font.

What this enforces, and what it cannot. The standard has three mechanical
parts: sentence length, paragraph length, and the denied words that match
without parts of speech. This file asserts those. It does not reach "one term,
one meaning", which is the most important rule, and it does not reach noun
clusters, which need a parser. No check knows whether a sentence is true.

Usage:
    check.py [--config PATH] [--root PATH]
    check.py --stats            # measure, do not judge: use before setting limits
    check.py --self-test        # exercise the checker on known-bad text
"""

import argparse
import json
import os
import re
import sys
from glob import glob

# The vendored copy carries this so `vendor.py` can tell an old copy from one
# the consumer has edited. Bump it whenever this file changes, and record the
# new hash with `tools/vendor.py record` — a test fails if you forget.
__version__ = "1.0.0"

# --- the prose pipeline -----------------------------------------------------
#
# Markdown reduced to the prose a reader reads, as paragraphs. Code, tables,
# headings and link targets carry no prose obligation and are dropped.
#
# Every line-anchored strip uses `[ \t]*` and never `\s*`. `\s` matches a
# newline, so `^\s*>` swallows the blank line ABOVE a blockquote and welds it
# onto the paragraph before it, which reads as a 7-sentence violation in a
# 4-sentence entry. A checker that invents violations gets switched off, and a
# switched-off check enforces nothing.

FENCED_CODE = re.compile(r"```[\s\S]*?```")
INLINE_CODE = re.compile(r"`[^`\n]*`")
TABLE_ROW = re.compile(r"^[ \t]*\|.*$", re.M)
HEADING = re.compile(r"^[ \t]*#{1,6} .*$", re.M)
HORIZONTAL_RULE = re.compile(r"^[ \t]*[-*_]{3,}[ \t]*$", re.M)
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
BLOCKQUOTE_MARKER = re.compile(r"^[ \t]*>[ \t]?", re.M)
LIST_MARKER = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+", re.M)
EMPHASIS = re.compile(r"\*\*|__|(?<!\w)[*_](?!\w)")
PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
WHITESPACE = re.compile(r"\s+")

# Sentence-final punctuation followed by a capital or the end of the paragraph.
# Deliberately conservative: a false split invents a violation. Documents
# written to this standard avoid "e.g." and "i.e." for the same reason.
#
# A project whose house style begins a sentence with a lowercase name — a
# product written `spacefinder`, a command, an identifier — must list those
# words in `sentenceStarts`. Without that the splitter joins the sentence to
# the one before it and reports the pair as one long sentence, which is a
# violation the document does not contain.
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")


def sentence_splitter(extra_starts=()):
    if not extra_starts:
        return SENTENCE_BREAK
    alternatives = "|".join(re.escape(w) for w in extra_starts)
    return re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(]|(?:%s)\b)" % alternatives)

HAS_LETTER_OR_DIGIT = re.compile(r"[a-zA-Z0-9]")


def prose(markdown):
    """Markdown -> the paragraphs a reader actually reads."""
    text = FENCED_CODE.sub("", markdown)
    text = INLINE_CODE.sub("X", text)  # a placeholder word, so counts stay honest
    text = TABLE_ROW.sub("", text)
    text = HEADING.sub("", text)
    text = HORIZONTAL_RULE.sub("", text)
    text = LINK.sub(r"\1", text)  # links -> their text
    text = BLOCKQUOTE_MARKER.sub("", text)  # markers go, the prose stays
    # A list item is its own unit, not a continuation of the sentence that
    # introduces it. The standard asks for a vertical list wherever there are
    # more than two parts, so counting a list and its lead-in as one long
    # sentence would penalise the rule the standard asks for.
    text = LIST_MARKER.sub("\n\n", text)
    text = EMPHASIS.sub("", text)
    paragraphs = (WHITESPACE.sub(" ", p).strip() for p in PARAGRAPH_BREAK.split(text))
    return [p for p in paragraphs if p]


def sentences(paragraph, splitter=None):
    return [s.strip() for s in (splitter or SENTENCE_BREAK).split(paragraph) if s.strip()]


def words(sentence):
    return len([w for w in WHITESPACE.split(sentence) if HAS_LETTER_OR_DIGIT.search(w)])


def shorten(s, limit=90):
    return s if len(s) <= limit else s[: limit - 3] + "..."


# --- configuration ----------------------------------------------------------

DEFAULT_CONFIG_NAME = "config.json"
DENY_SECTION_DEFAULT = "Words we do not use"


class ConfigError(Exception):
    pass


def find_root(start):
    """The repository root, so globs mean the same thing from any directory."""
    here = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return os.path.abspath(start)
        here = parent


def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ConfigError("no config at %s" % path)
    except ValueError as e:
        raise ConfigError("config at %s is not valid JSON: %s" % (path, e))

    for key in ("docs", "glossary"):
        if key not in raw:
            raise ConfigError('config is missing "%s"' % key)

    cfg = {
        "docs": raw["docs"],
        "glossary": raw["glossary"],
        "maxWordsPerSentence": raw.get("maxWordsPerSentence", 30),
        "maxSentencesPerParagraph": raw.get("maxSentencesPerParagraph", 6),
        "notYetConverted": raw.get("notYetConverted", []),
        "expectDocs": raw.get("expectDocs"),
        "sentenceStarts": raw.get("sentenceStarts", []),
        "denySection": raw.get("denySection", DENY_SECTION_DEFAULT),
        "denied": raw.get("denied", []),
    }
    for i, d in enumerate(cfg["denied"]):
        for key in ("term", "pattern", "instead"):
            if key not in d:
                raise ConfigError('denied[%d] is missing "%s"' % (i, key))
        try:
            d["compiled"] = re.compile(d["pattern"], re.I)
        except re.error as e:
            raise ConfigError("denied[%d] pattern does not compile: %s" % (i, e))
    return cfg


# --- gathering documents ----------------------------------------------------


def gather(cfg, root):
    exempt = set(cfg["notYetConverted"])
    found = {}
    for pattern in cfg["docs"]:
        for path in glob(os.path.join(root, pattern), recursive=True):
            if os.path.isfile(path):
                found[os.path.relpath(path, root)] = path
    docs = []
    for rel in sorted(found):
        if os.path.basename(rel) in exempt or rel in exempt:
            continue
        with open(found[rel], "r", encoding="utf-8") as f:
            docs.append((rel, f.read()))
    return docs, found


# --- the glossary is the source of truth for denied words -------------------

BOLD = re.compile(r"\*\*([^*]+)\*\*")


def glossary_denied_terms(text, section):
    """The bolded words in the first column of the deny table.

    The table is written for a reader, so most of its rows are conditional
    ("**live** for a stream", "**task**") and cannot be matched without parts
    of speech. Those rows are followed by hand. This function exists to catch
    the drift the other way: a pattern in the config that no longer names
    anything the glossary rules out.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^#{1,6}\s+%s\s*$" % re.escape(section), line.strip()):
            start = i + 1
            break
    if start is None:
        return None
    terms = set()
    for line in lines[start:]:
        if re.match(r"^#{1,6}\s+", line.strip()):
            break
        if not line.lstrip().startswith("|"):
            continue
        first_cell = line.strip().strip("|").split("|")[0]
        for match in BOLD.findall(first_cell):
            terms.add(match.strip().lower())
    return terms


# --- the checks -------------------------------------------------------------


def check(cfg, root):
    """Returns (failures, notes). A failure is a line the operator must act on."""
    failures = []
    notes = []
    docs, found = gather(cfg, root)

    # Non-exercise is not evidence. Every assertion below is "this list is
    # empty", which is also what a passing broken checker reports.
    #
    # Two ways to have nothing to check, and they are not the same. A glob that
    # matches no file on disk is broken and fails. A glob that matches files
    # that are all exempt is the state a repository is in on the day it adopts
    # the standard, before the first document is converted. That one passes,
    # and says so on every run.
    if not docs:
        if found:
            notes.append(
                "all %d document(s) are on notYetConverted, so nothing is "
                "checked yet — convert one to start enforcing the standard"
                % len(found)
            )
            return failures, notes
        failures.append(
            "no documents matched %s (relative to %s)" % (cfg["docs"], root)
        )
        return failures, notes
    if cfg["expectDocs"] is not None:
        expected = sorted(cfg["expectDocs"])
        actual = sorted(os.path.basename(rel) for rel, _ in docs)
        if expected != actual:
            failures.append(
                "expected to check %s, checked %s — update expectDocs when a document is added"
                % (expected, actual)
            )
    for exempt in cfg["notYetConverted"]:
        if not any(os.path.basename(rel) == exempt or rel == exempt for rel in found):
            failures.append(
                'notYetConverted names "%s", which is not on disk — the list may only shrink'
                % exempt
            )

    # The deny list must still describe the glossary.
    glossary_path = os.path.join(root, cfg["glossary"])
    if not os.path.isfile(glossary_path):
        failures.append("no glossary at %s" % cfg["glossary"])
    elif cfg["denied"]:
        with open(glossary_path, "r", encoding="utf-8") as f:
            terms = glossary_denied_terms(f.read(), cfg["denySection"])
        if terms is None:
            failures.append(
                '%s has no "%s" section — the deny list has no source of truth'
                % (cfg["glossary"], cfg["denySection"])
            )
        else:
            for d in cfg["denied"]:
                if d["term"].lower() not in terms:
                    failures.append(
                        'denied word "%s" is not in %s § %s — the config and the glossary have drifted'
                        % (d["term"], cfg["glossary"], cfg["denySection"])
                    )
            unchecked = len(terms) - len(
                set(d["term"].lower() for d in cfg["denied"]) & terms
            )
            if unchecked:
                notes.append(
                    "%d of %d glossary denials need parts of speech and are followed by hand"
                    % (unchecked, len(terms))
                )

    max_words = cfg["maxWordsPerSentence"]
    max_sentences = cfg["maxSentencesPerParagraph"]
    splitter = sentence_splitter(cfg["sentenceStarts"])
    for rel, text in docs:
        paragraphs = prose(text)
        for p in paragraphs:
            sents = sentences(p, splitter)
            if len(sents) > max_sentences:
                failures.append(
                    '%s: %d sentences — "%s"' % (rel, len(sents), shorten(p))
                )
            for s in sents:
                n = words(s)
                if n > max_words:
                    failures.append('%s: %d words — "%s"' % (rel, n, shorten(s)))
            for d in cfg["denied"]:
                hit = d["compiled"].search(p)
                if hit:
                    failures.append(
                        "%s: %s — %s" % (rel, hit.group(0), d["instead"])
                    )
    return failures, notes


def stats(cfg, root):
    docs, _ = gather(cfg, root)
    if not docs:
        print("no documents matched %s" % cfg["docs"])
        return 2
    splitter = sentence_splitter(cfg["sentenceStarts"])
    total = 0
    over_25 = 0
    print("%-28s %8s %8s %8s" % ("document", "sentences", "longest", "over 25"))
    for rel, text in docs:
        sents = [s for p in prose(text) for s in sentences(p, splitter)]
        lengths = [words(s) for s in sents]
        total += len(sents)
        this_over = len([n for n in lengths if n > 25])
        over_25 += this_over
        print(
            "%-28s %8d %8d %8d"
            % (rel, len(sents), max(lengths) if lengths else 0, this_over)
        )
    print("%-28s %8d %8s %8d" % ("TOTAL", total, "", over_25))
    print(
        "\nSet the limit from this. A ceiling with slack fires on sprawl; a limit\n"
        "set at the longest sentence you happen to have written fires on every\n"
        "subordinate clause, and a check that nags gets switched off."
    )
    return 0


# --- the checker's own tests ------------------------------------------------
#
# The checks above assert emptiness, which is what a passing broken checker
# also reports. These drive known-bad text through the same functions.


def self_test():
    failures = []

    def ok(claim, condition):
        if not condition:
            failures.append(claim)

    ok("splits three sentences", len(sentences("One. Two three. Four.")) == 3)
    ok("counts five words", words("the token enters the step") == 5)
    ok("does not split on a version number", len(sentences("Version 1.2 is fine.")) == 1)

    long_sentence = ("word " * 31) + "."
    ok("catches a long sentence", any(words(s) > 30 for s in sentences(long_sentence)))

    wordy = "A. " * 7
    ok("catches a long paragraph", len(sentences(prose(wordy)[0])) > 6)

    ok("drops fenced code", prose("```\nsome code\n```\n\ntext here.") == ["text here."])
    ok("drops table rows", prose("| a | b |\n| - | - |\n\ntext here.") == ["text here."])
    ok("drops headings", prose("## A heading\n\ntext here.") == ["text here."])
    ok("keeps inline code as one word", words(prose("the `admit` function")[0]) == 3)

    # The `^\s*` bug this guards: `\s` matches a newline, so a line-anchored
    # strip can eat the blank line above the quote and weld the two together.
    ok(
        "keeps a blockquote out of the paragraph above it",
        prose("First. Second.\n\n> A quote.") == ["First. Second.", "A quote."],
    )
    ok(
        "keeps a list item out of its lead-in",
        prose("Lead in:\n\n- one\n- two") == ["Lead in:", "one", "two"],
    )
    ok(
        "keeps a numbered list item out of its lead-in",
        prose("Lead in:\n\n1. one\n2. two") == ["Lead in:", "one", "two"],
    )

    # A glossary must be able to name what it forbids: code font is a mention,
    # plain prose is a use. Without this, the deny table's own rows and every
    # "Not:" line report violations, which is the fastest way to a switched-off
    # check.
    ok("a denied word in code font is a mention", prose("Not: `item`.") == ["Not: X."])
    ok("a denied word in prose survives to be caught", "item" in prose("Not: item.")[0])

    # A house style that begins a sentence with a lowercase product name.
    ok(
        "does not split before a lowercase word by default",
        len(sentences("An answer. spacefinder waits.")) == 1,
    )
    ok(
        "splits before a listed lowercase sentence start",
        len(sentences("An answer. spacefinder waits.", sentence_splitter(["spacefinder"]))) == 2,
    )

    table = (
        "## Words we do not use\n\n"
        "| Do not write | Why | Write |\n| --- | --- | --- |\n"
        "| **just**, **simply** | blame | delete it |\n"
        "| **live** for a stream | two meanings | the real-time tail |\n\n"
        "## Next section\n\n| **ignored** | x | y |\n"
    )
    terms = glossary_denied_terms(table, "Words we do not use")
    ok("reads the deny table", terms == {"just", "simply", "live"})
    ok(
        "reports a missing deny section",
        glossary_denied_terms("## Other\n", "Words we do not use") is None,
    )

    for f in failures:
        print("self-test FAILED: %s" % f)
    if not failures:
        print("self-test: 19 checks passed")
    return 1 if failures else 0


# --- entry point ------------------------------------------------------------


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.path.join(here, DEFAULT_CONFIG_NAME))
    parser.add_argument("--root", default=None)
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--self-test", dest="self_test", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print("vocab-check: %s" % e, file=sys.stderr)
        return 2

    root = os.path.abspath(args.root) if args.root else find_root(os.path.dirname(os.path.abspath(args.config)))

    if args.stats:
        return stats(cfg, root)

    failures, notes = check(cfg, root)
    for note in notes:
        print("vocab-check: %s" % note)
    if failures:
        print("\n%d violation(s) of the writing standard:\n" % len(failures))
        for f in failures:
            print("  %s" % f)
        print("\nThe standard is %s." % cfg["glossary"])
        return 1
    print("vocab-check: the writing standard holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())

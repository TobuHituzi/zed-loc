#!/usr/bin/env python3
"""replace_ast.py -- AST-aware string replacement for the zed-loc fork.

Drop-in alternative to replace.py. replace.py rewrites every occurrence of a
quoted literal with a regular expression; that broke the build repeatedly
because the same literal often doubles as a programmatic identifier in the
same file (key-context names, proc-macro ident comparisons, SQL phrase
searches, serde attribute values, serialized discriminants, ...).

This tool parses each Rust file with tree-sitter and decides PER OCCURRENCE
whether a string literal may be translated. Literals in identifier positions
are left untouched; display text is translated. See INCIDENTS.md for the
incident catalog this classification is built against, and
ast-tools/REPORT.md for the rule/verification mapping.

Usage (mirrors replace.py):
    python3 replace_ast.py <dict.json | lang-dir> [--root DIR]
                           [--dry-run] [--report PATH]

    <dict.json | lang-dir>  Dictionary JSON (same format as replace.py), or a
                            directory containing exactly one *.json dictionary
                            (e.g. "ja" -> ja/ja-JP.json). Default: zh.json.
    --root DIR              Directory the dictionary's file paths are relative
                            to (default: current directory, like replace.py).
    --dry-run               Do not write files; print per-file counts.
    --report PATH           Write a JSON report of skipped occurrences
                            (file, line, literal, rule) and pair notes.

Run it with the ast-tools venv interpreter:
    ~/projects/zed-refresh/ast-tools/venv/bin/python replace_ast.py ja/ja-JP.json
(tree_sitter is imported from that venv; a fallback path lookup is built in.)

Classification summary (per occurrence, first matching rule wins):
  byte-string        byte string literals are never display text
  attribute          inside #[...] (except clap display attributes:
                     help / about / doc / after_help / long_about values)
  macro-sql          inside sql!(...) -- never translated
  macro-format-string  first string literal of a message macro
                     (format!/println!/.../panic!/bail!/anyhow!/log macros)
                     -> TRANSLATED; other literals in a macro body are
                     skipped as macro-body; macro_rules! bodies: macro-def
  const-value        const/static values, via the identifier heuristic
                     (see const_is_identifier); sentence-like values are
                     display text and are translated
  match-pattern      literal in a match-arm pattern
  comparison         literal compared with == / != (soft skip, see below)
  deny-arg:<name>    argument of a search/registration/lookup method or
                     function (add, key_context, is_ident, find, rfind,
                     contains, get, starts_with, ends_with, split, replace,
                     bind, register, set_key_context, KeyContext::parse)
  keybinding-context Some("...") context argument of KeyBinding::new
  deny-receiver:<m>  literal used as the receiver of find/contains/len/...
                     (the needle whose length/offsets are computed)
  index-key          literal used as an index key

File-level consistency (per file and literal value):
  * If any occurrence is hard-skipped (or const-skipped), ALL occurrences of
    that literal in the file are skipped. A literal that is an identifier
    anywhere in the file is an identifier everywhere in the file; this is
    what keeps e.g. "Diagnostics" (registered via .key_context and also
    returned via .into()) consistent.
  * Otherwise, if a literal is both compared with ==/!= somewhere and used
    as plain display/creation text elsewhere in the same file, the pair is
    self-consistent (both sides live in this file) and ALL occurrences are
    translated -- the "Recommended" pattern from INCIDENTS.md.
  * A literal that only ever appears as a ==/!= operand is skipped.
"""

import argparse
import glob
import json
import os
import re
import sys


def _bootstrap_tree_sitter():
    try:
        import tree_sitter_rust  # noqa: F401
        from tree_sitter import Language, Parser  # noqa: F401
        return
    except ImportError:
        pass
    # Fall back to the project venv: <project>/ast-tools/venv/lib/python*/site-packages
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for base in (script_dir, os.getcwd()):
        for cand in glob.glob(
            os.path.join(base, "..", "ast-tools", "venv", "lib", "python*", "site-packages")
        ) + glob.glob(
            os.path.join(base, "ast-tools", "venv", "lib", "python*", "site-packages")
        ):
            if cand not in sys.path:
                sys.path.insert(0, os.path.normpath(cand))
    import tree_sitter_rust  # noqa: F401
    from tree_sitter import Language, Parser  # noqa: F401


_bootstrap_tree_sitter()
import tree_sitter_rust
from tree_sitter import Language, Parser

# ---------------------------------------------------------------------------
# Rule tables
# ---------------------------------------------------------------------------

#: Methods/functions whose string argument is a key, needle, context name or
#: otherwise matched against data that must not change language.
DENY_METHODS = {
    "add",            # key_context.add("Editor") -- context registration
    "key_context",    # .key_context("Editor")
    "is_ident",       # proc-macro ident comparison
    "find", "rfind",  # sqlez: find("rename column")
    "contains", "get", "starts_with", "ends_with",
    "split", "replace",
    "strip_prefix", "strip_suffix",          # google_ai: strip_prefix("models/")
    "trim_start_matches", "trim_end_matches",
    "bind",           # SQL parameter binding of serialized values
    "register", "set_key_context",
}

#: Methods for which the literal as *receiver* is a needle/template whose
#: bytes or length drive program logic ("rename column".len()).
RECEIVER_DENY_METHODS = {
    "find", "rfind", "contains", "starts_with", "ends_with",
    "split", "replace", "len", "is_empty",
    "strip_prefix", "strip_suffix", "trim_start_matches", "trim_end_matches",
}

#: Macros whose first string literal is a user-facing format string.
MESSAGE_MACROS = {
    "format", "println", "eprintln", "print", "eprint",
    "write", "writeln", "panic", "bail", "anyhow", "ensure", "format_err",
    # log / tracing style message macros (same shape as println!)
    "log", "info", "warn", "error", "debug", "trace",
}

#: Attribute keys whose value is display text even inside #[...].
DISPLAY_ATTRS = {"help", "about", "doc", "after_help", "long_about"}

#: Const/static names that mark the value as identifier/protocol material.
CONST_NAME_HINTS = ("PREFIX", "SUFFIX", "KEY", "HEADER", "TOKEN")

WRAPPER_NODES = {"reference_expression", "parenthesized_expression"}
LITERAL_NODES = {"string_literal", "raw_string_literal"}

_PARSER = Parser(Language(tree_sitter_rust.language()))


# ---------------------------------------------------------------------------
# Literal decoding / encoding
# ---------------------------------------------------------------------------

def decode_escapes(s):
    """Decode the body of a normal (non-raw) Rust string literal."""
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= len(s):
            break
        e = s[i]
        i += 1
        if e == "n":
            out.append("\n")
        elif e == "t":
            out.append("\t")
        elif e == "r":
            out.append("\r")
        elif e == "0":
            out.append("\0")
        elif e == "\\":
            out.append("\\")
        elif e == '"':
            out.append('"')
        elif e == "'":
            out.append("'")
        elif e == "\n":  # line continuation: skip following whitespace
            while i < len(s) and s[i] in " \t\r\n":
                i += 1
        elif e == "x":
            out.append(chr(int(s[i:i + 2], 16)))
            i += 2
        elif e == "u":
            j = s.index("}", i)
            out.append(chr(int(s[i + 1:j], 16)))
            i = j + 1
        else:
            out.append(e)
    return "".join(out)


def decode_literal(raw):
    """Return (decoded_value, kind); kind in str/raw/byte/rawbyte."""
    m = re.match(r"(b?r)(#+)\"", raw)
    if m:
        hashes = m.group(2)
        kind = "rawbyte" if m.group(1) == "br" else "raw"
        inner = raw[len(m.group(1)) + len(hashes) + 1: -(len(hashes) + 1)]
        return inner, kind
    if raw.startswith('b"'):
        return decode_escapes(raw[2:-1]), "byte"
    if raw.startswith('"'):
        return decode_escapes(raw[1:-1]), "str"
    return raw, "str"


def escape_str(s):
    out = []
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u{%x}" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def emit_literal(translation, kind):
    """Render a translation as a Rust literal of the given kind."""
    if kind == "raw":
        h = 0
        while ('"' + "#" * h) in translation:
            h += 1
        return "r" + "#" * h + '"' + translation + '"' + "#" * h
    return '"' + escape_str(translation) + '"'


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------

def node_text(src, node):
    return src[node.start_byte:node.end_byte].decode("utf-8")


def ancestors_of(node):
    out = []
    p = node.parent
    while p is not None:
        out.append(p)
        p = p.parent
    return out


def iter_literals(root):
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in LITERAL_NODES:
            yield n
        stack.extend(reversed(n.children))


def unwrap(node):
    """Step through reference/parens wrappers toward the meaningful parent."""
    p = node.parent
    while p is not None and p.type in WRAPPER_NODES:
        node = p
        p = p.parent
    return node, p


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _same_node(a, b):
    """Node identity by span (tree-sitter wrappers are recreated per access)."""
    return (
        a.type == b.type
        and a.start_byte == b.start_byte
        and a.end_byte == b.end_byte
    )


def _attribute_is_display(src, attr_item, literal):
    """True if the literal is the value of a clap display key inside #[...]."""
    leaves = []

    def collect(n):
        if _same_node(n, literal):
            leaves.append(n)
            return
        if not n.children:
            leaves.append(n)
            return
        for c in n.children:
            collect(c)

    collect(attr_item)
    for i, leaf in enumerate(leaves):
        if _same_node(leaf, literal):
            if i >= 2:
                prev_ident, prev_sym = leaves[i - 2], leaves[i - 1]
                if (
                    prev_ident.type in ("identifier", "scoped_identifier")
                    and node_text(src, prev_ident).split("::")[-1] in DISPLAY_ATTRS
                    and node_text(src, prev_sym) == "="
                ):
                    return True
            return False
    return False


def _macro_name(src, macro_node):
    m = macro_node.child_by_field_name("macro")
    if m is None:
        return ""
    return node_text(src, m).split("::")[-1]


def _effective_macro(src, node):
    """Find the macro invocation governing a literal inside token trees.

    tree-sitter does not parse macro invocations nested inside another
    macro's token tree (e.g. format! inside vec![...] is opaque tokens), so
    walk up through the token_tree layers and look for an inner `name!`
    pattern immediately before the token tree that contains the literal.
    Plain groups (parens/braces) are stepped over.

    Returns (macro_name, token_tree_node) or (None, None).
    """
    layers = []  # token_tree nodes, innermost first
    p = node.parent
    macro_node = None
    while p is not None:
        if p.type == "token_tree":
            layers.append(p)
        elif p.type == "macro_invocation":
            macro_node = p
            break
        elif p.type == "attribute_item":
            return None, None
        p = p.parent
    if macro_node is None:
        return None, None
    if not layers:
        return _macro_name(src, macro_node), None
    for tt in layers:
        parent = tt.parent
        if parent is not None and parent.type == "macro_invocation":
            return _macro_name(src, parent), tt
        if parent is not None and parent.type == "token_tree":
            sibs = parent.children
            idx = next(
                (j for j, s in enumerate(sibs) if _same_node(s, tt)), None
            )
            if idx is not None and idx >= 2 and sibs[idx - 1].type == "!":
                name_node = sibs[idx - 2]
                if name_node.type == "identifier":
                    return node_text(src, name_node), tt
                if name_node.type == "scoped_identifier":
                    return node_text(src, name_node).split("::")[-1], tt
            continue  # plain group: look one layer outward
    return _macro_name(src, macro_node), layers[-1]


def _call_info(src, call):
    """Return (kind, name, full_text) for a call_expression's function."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return None, None, ""
    text = node_text(src, fn)
    if fn.type == "field_expression":
        field = fn.child_by_field_name("field")
        return "method", (node_text(src, field) if field else ""), text
    if fn.type == "scoped_identifier":
        return "function", text.split("::")[-1], text
    if fn.type == "identifier":
        return "function", text, text
    return "other", text, text


def classify(src, node, kind):
    """Classify one literal occurrence.

    Returns (cls, rule): cls in {"hard", "soft", "const", "plain"}.
    "const" is resolved later by the const heuristic.
    """
    if kind in ("byte", "rawbyte"):
        return "hard", "byte-string"

    anc = ancestors_of(node)
    anc_types = [a.type for a in anc]

    # -- attributes ---------------------------------------------------------
    for a in anc:
        if a.type == "attribute_item":
            if _attribute_is_display(src, a, node):
                return "plain", "attribute-display"
            return "hard", "attribute"
        if a.type in ("macro_invocation", "macro_definition"):
            break  # macros are handled below; attributes cannot nest inside

    # -- macros -------------------------------------------------------------
    for a in anc:
        if a.type == "macro_definition":
            return "hard", "macro-def"
    macro_name, macro_tt = _effective_macro(src, node)
    if macro_name is not None:
        if macro_name == "sql":
            return "hard", "macro-sql"
        if macro_name in MESSAGE_MACROS and macro_tt is not None:
            lits = sorted(iter_literals(macro_tt), key=lambda n: n.start_byte)
            if lits and lits[0].start_byte == node.start_byte:
                return "plain", "macro-format-string"
        return "hard", "macro-body"

    # -- const / static values ----------------------------------------------
    for a in anc:
        if a.type in ("const_item", "static_item"):
            return "const", "const-value"

    # -- match patterns -------------------------------------------------------
    if "match_pattern" in anc_types:
        return "hard", "match-pattern"

    # -- == / != operands -----------------------------------------------------
    inner, parent = unwrap(node)
    if parent is not None and parent.type == "binary_expression":
        op = parent.child_by_field_name("operator")
        if op is not None and node_text(src, op) in ("==", "!="):
            return "soft", "comparison"

    # -- receiver of a deny method ("rename column".len()) --------------------
    if node.parent is not None and node.parent.type == "field_expression":
        fe = node.parent
        fn_node = (
            fe.parent.child_by_field_name("function")
            if fe.parent is not None and fe.parent.type == "call_expression"
            else None
        )
        if fn_node is not None and _same_node(fn_node, fe):
            field = fe.child_by_field_name("field")
            if field is not None and node_text(src, field) in RECEIVER_DENY_METHODS:
                return "hard", "deny-receiver:" + node_text(src, field)

    # -- argument of a deny call ----------------------------------------------
    cur = node
    for _ in range(3):
        inner, parent = unwrap(cur)
        if parent is None or parent.type != "arguments":
            break
        call = parent.parent
        if call is None or call.type != "call_expression":
            break
        ckind, cname, ctext = _call_info(src, call)
        if ckind == "method" and cname in DENY_METHODS:
            return "hard", "deny-arg:" + cname
        if ckind == "function":
            if cname == "parse" and "KeyContext" in ctext:
                return "hard", "deny-arg:KeyContext::parse"
            if cname == "key_context":
                return "hard", "deny-arg:key_context"
            if "KeyBinding" in ctext and cname == "new":
                return "hard", "keybinding-context"
        # Only keep walking outward through transparent wrapper calls.
        if cname in ("Some", "Ok", "Err"):
            cur = call
            continue
        break

    # -- index keys -------------------------------------------------------------
    inner, parent = unwrap(node)
    if parent is not None and parent.type == "index_expression":
        idx = parent.child_by_field_name("index")
        if idx is not None and (
            _same_node(idx, inner) or _same_node(idx, node)
        ):
            return "hard", "index-key"

    # -- let patterns -----------------------------------------------------------
    if node.parent is not None and node.parent.type == "let_condition":
        return "hard", "let-pattern"

    return "plain", None


_IDENT_LIKE = re.compile(r"^\S+$")


def const_is_identifier(value, name):
    """Heuristic: is this const/static value identifier material?

    Skip (identifier-like) when the value is a single whitespace-free token,
    when the const name advertises a protocol role (PREFIX/SUFFIX/KEY/
    HEADER/TOKEN -- e.g. CO_AUTHOR_PREFIX), or when the value is a prefix
    ending in ':'. Otherwise the value is sentence-like display text
    (e.g. pane.rs CONFLICT_MESSAGE) and gets translated.
    """
    if _IDENT_LIKE.match(value):
        return True
    if any(h in name for h in CONST_NAME_HINTS):
        return True
    if value.rstrip().endswith(":"):
        return True
    return False


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_file(path, replacements):
    """Apply replacements (dict original->translation) to one .rs file.

    Returns (new_content_bytes, stats) or (None, stats) if unchanged.
    """
    with open(path, "rb") as f:
        src = f.read()
    tree = _PARSER.parse(src)

    # Collect occurrences whose decoded value has a translation.
    occurrences = []  # (node, value, kind, translation)
    for node in iter_literals(tree.root_node):
        raw = node_text(src, node)
        value, kind = decode_literal(raw)
        # Dictionary keys are the decoded values; fall back to the raw inner
        # text for keys stored undecoded.
        translation = replacements.get(value)
        if translation is None and kind == "str":
            translation = replacements.get(raw[1:-1])  # undecoded body
        elif translation is None and kind == "byte":
            translation = replacements.get(raw[2:-1])
        if not translation:
            continue
        occurrences.append((node, value, kind, translation))

    if not occurrences:
        return None, {"translated": 0, "skipped": [], "notes": []}

    # Classify every occurrence.
    classified = []  # (node, value, kind, translation, cls, rule)
    for node, value, kind, translation in occurrences:
        cls, rule = classify(src, node, kind)
        if cls == "const":
            # Find the owning const/static item to get its name.
            name = ""
            for a in ancestors_of(node):
                if a.type in ("const_item", "static_item"):
                    name_node = a.child_by_field_name("name")
                    if name_node is not None:
                        name = node_text(src, name_node)
                    break
            cls = "const-skip" if const_is_identifier(value, name) else "const-text"
        classified.append((node, value, kind, translation, cls, rule))

    # File-level consistency, grouped by literal value.
    by_value = {}
    for item in classified:
        by_value.setdefault(item[1], []).append(item)

    edits = []       # (start, end, new_text)
    skipped = []     # report entries
    notes = []
    translated_count = 0

    for value, items in by_value.items():
        classes = {it[4] for it in items}
        hard = "hard" in classes or "const-skip" in classes
        soft = "soft" in classes
        plain = bool(classes & {"plain", "const-text"})

        if hard:
            action = "skip"
        elif soft and plain:
            action = "translate"
            notes.append({
                "literal": value,
                "note": "self-consistent pair: compared and created in the "
                        "same file; all occurrences translated together",
            })
        elif soft:
            action = "skip"
        else:
            action = "translate"

        for node, _v, kind, translation, cls, rule in items:
            line = node.start_point[0] + 1
            if action == "translate":
                edits.append((
                    node.start_byte,
                    node.end_byte,
                    emit_literal(translation, kind),
                ))
                translated_count += 1
            else:
                if cls in ("hard", "const-skip", "soft"):
                    rep_rule = rule
                else:
                    rep_rule = "file-consistency"
                skipped.append({
                    "line": line,
                    "literal": value if len(value) <= 80 else value[:77] + "...",
                    "rule": rep_rule,
                })

    if not edits:
        return None, {"translated": 0, "skipped": skipped, "notes": notes}

    out = src
    for start, end, text in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + text.encode("utf-8") + out[end:]
    return out, {"translated": translated_count, "skipped": skipped, "notes": notes}


# ---------------------------------------------------------------------------
# CLI (mirrors replace.py)
# ---------------------------------------------------------------------------

def resolve_dictionary(arg):
    """Accept a JSON file path or a directory holding one *.json dictionary."""
    if os.path.isfile(arg):
        return arg
    if os.path.isdir(arg):
        jsons = sorted(glob.glob(os.path.join(arg, "*.json")))
        if len(jsons) == 1:
            return jsons[0]
        raise SystemExit(
            f"Dictionary directory {arg!r} must contain exactly one *.json, "
            f"found: {jsons}"
        )
    # Try relative to this script's directory (e.g. `ja` from the repo root).
    alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), arg)
    if os.path.isfile(alt) or os.path.isdir(alt):
        return resolve_dictionary(alt)
    return arg  # let open() produce the error, like replace.py


def main(argv=None):
    ap = argparse.ArgumentParser(description="AST-aware replacement (see docstring)")
    ap.add_argument("dictionary", nargs="?", default="zh.json",
                    help="dictionary JSON file or language directory (default: zh.json)")
    ap.add_argument("--root", default=".",
                    help="base directory for dictionary file paths (default: .)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print per-file counts without writing")
    ap.add_argument("--report", metavar="PATH",
                    help="write a JSON report of skipped occurrences")
    args = ap.parse_args(argv)

    dict_path = resolve_dictionary(args.dictionary)
    with open(dict_path, "r", encoding="utf-8") as f:
        json_data = json.load(f)

    missing_files = []
    report = {"files": {}, "missing_files": []}
    total_translated = 0
    total_skipped = 0

    for file_path, replacements in json_data.items():
        replacements = {k: v for k, v in replacements.items() if v}
        if not replacements:
            continue
        real_path = os.path.join(args.root, file_path)
        if not os.path.exists(real_path):
            missing_files.append(file_path)
            continue
        new_content, stats = process_file(real_path, replacements)
        if stats["translated"] or stats["skipped"] or stats["notes"]:
            report["files"][file_path] = stats
        total_translated += stats["translated"]
        total_skipped += len(stats["skipped"])
        if args.dry_run:
            print(f"{file_path}: translated={stats['translated']} "
                  f"skipped={len(stats['skipped'])}")
        if new_content is not None and not args.dry_run:
            with open(real_path, "wb") as f:
                f.write(new_content)

    report["missing_files"] = missing_files
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)

    if args.dry_run:
        print(f"TOTAL: translated={total_translated} skipped={total_skipped}")
    else:
        print("Successfully replaced strings in the original files")
    if missing_files:
        print("The following files were not found:")
        for missing_file in missing_files:
            print(missing_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())

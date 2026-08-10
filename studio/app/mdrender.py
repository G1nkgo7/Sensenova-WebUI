#!/usr/bin/env python3
"""Minimal, dependency-free Markdown -> HTML renderer.

Covers the subset used by the project's .md docs: headings, paragraphs,
bold/italic/inline-code, fenced code blocks, blockquotes, horizontal rules,
ordered/unordered lists, GitHub task lists ([ ] / [x]) and pipe tables.

Not a full CommonMark implementation -- just enough to render workProgress.md
and friends cleanly, with zero third-party packages.
"""
import html
import re


def _inline(text):
    """Render inline spans. `text` is raw markdown (NOT yet escaped)."""
    # Protect inline code first so its contents are not further processed.
    codes = []

    def _stash(m):
        codes.append(m.group(1))
        return "\x00%d\x00" % (len(codes) - 1)

    text = re.sub(r"`([^`]+)`", _stash, text)
    text = html.escape(text, quote=False)

    # links [text](url)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>'
        % (html.escape(m.group(2), quote=True), m.group(1)),
        text,
    )
    # bold then italic
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)

    # restore inline code
    def _unstash(m):
        return "<code>%s</code>" % html.escape(codes[int(m.group(1))], quote=False)

    text = re.sub(r"\x00(\d+)\x00", _unstash, text)
    return text


def _is_table_sep(line):
    return bool(re.match(r"^\s*\|?[\s:|-]+\|?\s*$", line)) and "-" in line


def _split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def render(md):
    """Return an HTML fragment for the given markdown string."""
    lines = md.replace("\r\n", "\n").split("\n")
    out = []
    i = 0
    n = len(lines)

    def close_para(buf):
        if buf:
            out.append("<p>%s</p>" % _inline(" ".join(buf)))
            buf.clear()

    para = []

    while i < n:
        line = lines[i]

        # fenced code block
        m = re.match(r"^```(\w*)\s*$", line)
        if m:
            close_para(para)
            lang = m.group(1)
            code = []
            i += 1
            while i < n and not re.match(r"^```\s*$", lines[i]):
                code.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            cls = ' class="lang-%s"' % lang if lang else ""
            out.append(
                "<pre><code%s>%s</code></pre>"
                % (cls, html.escape("\n".join(code), quote=False))
            )
            continue

        # horizontal rule
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            close_para(para)
            out.append("<hr>")
            i += 1
            continue

        # heading
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_para(para)
            level = len(m.group(1))
            text = m.group(2).strip()
            anchor = re.sub(r"[^\w一-鿿-]+", "-", text).strip("-").lower()
            out.append(
                '<h%d id="%s">%s</h%d>' % (level, anchor, _inline(text), level)
            )
            i += 1
            continue

        # blockquote
        if re.match(r"^\s*>\s?", line):
            close_para(para)
            quote = []
            while i < n and re.match(r"^\s*>\s?", lines[i]):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>" % render("\n".join(quote)))
            continue

        # table
        if "|" in line and i + 1 < n and _is_table_sep(lines[i + 1]):
            close_para(para)
            header = _split_row(line)
            i += 2  # skip header + separator
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            thead = "".join("<th>%s</th>" % _inline(c) for c in header)
            tbody = "".join(
                "<tr>%s</tr>"
                % "".join("<td>%s</td>" % _inline(c) for c in r)
                for r in rows
            )
            out.append(
                "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
                % (thead, tbody)
            )
            continue

        # lists (unordered, ordered, task)
        if re.match(r"^\s*([-*+]|\d+\.)\s+", line):
            close_para(para)
            ordered = bool(re.match(r"^\s*\d+\.\s+", line))
            items = []
            while i < n and re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i]):
                content = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", lines[i])
                task = re.match(r"^\[([ xX])\]\s+(.*)$", content)
                if task:
                    checked = "checked" if task.group(1).lower() == "x" else ""
                    items.append(
                        '<li class="task"><input type="checkbox" disabled %s>%s</li>'
                        % (checked, _inline(task.group(2)))
                    )
                else:
                    items.append("<li>%s</li>" % _inline(content))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join(items), tag))
            continue

        # blank line ends paragraph
        if not line.strip():
            close_para(para)
            i += 1
            continue

        para.append(line.strip())
        i += 1

    close_para(para)
    return "\n".join(out)


if __name__ == "__main__":
    import sys

    print(render(sys.stdin.read()))

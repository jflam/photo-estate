#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["markdown-it-py>=3.0", "mdit-py-plugins>=0.4", "pygments>=2.17"]
# ///
"""Build the book (docs/book/*.md) into a static site for GitHub Pages.

    uv run tools/build_book.py [OUT_DIR]          # default: ../site

One flat folder of HTML pages: a sidebar of chapters, a table of contents for the page, light and dark
themes, client-side search, and Mermaid diagrams drawn in the browser. Links between chapters become links
between pages; links to code files become links to the file on GitHub. Nothing is fetched at build time.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import sys
from pathlib import Path

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound

REPO = "jflam/photo-estate"
BRANCH = "main"
ROOT = Path(__file__).resolve().parents[2]          # the repository
BOOK = ROOT / "docs" / "book"
ASSETS = Path(__file__).with_name("book_site")
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "site"

# The sidebar: (heading, [file stems]). A chapter not listed here still gets built, under "More".
GROUPS = [
    ("Overview", ["01-big-picture", "02-code-tour"]),
    ("Your photos, in", ["03-repatriation", "04-store-and-ingest", "05-catalog", "06-linking",
                         "07-places-events-trips", "08-duplicates"]),
    ("Finding things", ["09-text-search-and-tags", "10-embeddings", "11-server", "12-search-language", "13-photo-app"]),
    ("People", ["14-faces", "15-human-corrections"]),
    ("Running it", ["16-operations", "17-testing-and-demo", "18-design-and-future"]),
    ("The iPhone app", ["19-iphone-app", "20-sync", "21-offline-search", "22-photos-on-the-phone",
                        "23-native-interface", "24-uploading"]),
    ("Appendices", ["appendix-a-commands", "appendix-b-http-api", "appendix-c-glossary"]),
]


def slug(text: str) -> str:
    """GitHub's heading anchors, so links written for the Markdown still land."""
    s = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return re.sub(r" ", "-", s)


def split_title(title: str) -> tuple[str, str]:
    """"11. The photo server" -> ("11", "The photo server"); "Appendix B: HTTP API" -> ("B", "HTTP API")."""
    m = re.match(r"^(\d+)\.\s+(.*)$", title)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^Appendix ([A-Z]):\s*(.*)$", title)
    if m:
        rest = m.group(2)
        return m.group(1), rest[:1].upper() + rest[1:]
    return "", title


def highlighted(code: str, lang: str) -> str:
    try:
        lexer = get_lexer_by_name(lang) if lang else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    return highlight(code, lexer, HtmlFormatter(nowrap=True))


def highlight_css() -> str:
    """Token colours for both themes, as rules scoped under `.hl`, from two Pygments styles."""
    def rules(style: str, scope: str) -> str:
        out = []
        for line in HtmlFormatter(style=style).get_style_defs(".hl").splitlines():
            if line.startswith(".hl ."):             # a token rule; skip the block's own background and `pre`
                out.append(line.replace(".hl ", f"{scope} .hl ", 1) if scope else line)
        return "\n".join(r for r in out if "background" not in r.split("{")[0])
    light = rules("default", "")
    dark = rules("github-dark", ':root[data-theme="dark"]')
    dark_auto = rules("github-dark", ":root:not([data-theme])")
    return f"{light}\n{dark}\n@media (prefers-color-scheme: dark) {{\n{dark_auto}\n}}\n"


class Site:
    def __init__(self) -> None:
        self.md = (MarkdownIt("commonmark", {"html": True, "linkify": False})
                   .enable(["table", "strikethrough"])
                   .use(anchors_plugin, min_level=2, max_level=4, slug_func=slug, permalink=True,
                        permalinkSymbol="#", permalinkSpace=False))
        self.stems = [s for _, stems in GROUPS for s in stems]
        extra = sorted(p.stem for p in BOOK.glob("*.md") if p.stem not in self.stems and p.stem != "README")
        if extra:
            GROUPS.append(("More", extra))
            self.stems += extra
        self.pages: dict[str, dict] = {}
        self.search: list[dict] = []
        self.copied: set[Path] = set()

    # ---------------------------------------------------------------- links

    def page_of(self, stem: str) -> str:
        return "index.html" if stem == "README" else f"{stem}.html"

    def rewrite(self, href: str, source: Path) -> str:
        """A link or image path as written in `source`, as it should be on the site."""
        if re.match(r"^(https?:|mailto:|#|data:)", href):
            return href
        path, _, frag = href.partition("#")
        target = (source.parent / path).resolve()
        frag = f"#{frag}" if frag else ""
        if target.suffix == ".md" and target.parent == BOOK and target.exists():
            return self.page_of(target.stem) + frag
        try:
            rel = target.relative_to(ROOT)
        except ValueError:
            return href
        if rel.parts[:2] == ("docs", "screenshots") or rel.parts[:3] == ("docs", "book", "img"):
            return self.copy_asset(target)
        kind = "tree" if target.is_dir() else "blob"
        return f"https://github.com/{REPO}/{kind}/{BRANCH}/{rel.as_posix()}{frag}"

    def copy_asset(self, src: Path) -> str:
        rel = Path("assets") / src.name
        dest = OUT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src not in self.copied:
            shutil.copy2(src, dest)
            self.copied.add(src)
        return rel.as_posix()

    # ---------------------------------------------------------------- rendering

    def render(self, stem: str) -> dict:
        source = BOOK / f"{stem}.md"
        text = source.read_text()
        tokens = self.md.parse(text)
        title, headings, sections = "", [], []
        cur = {"h": "", "a": "", "t": []}
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t.type == "heading_open":
                inline = tokens[i + 1]
                label = inline.content
                level = int(t.tag[1])
                if level == 1 and not title:
                    title = re.sub(r"[`*]", "", label)
                elif level in (2, 3):
                    anchor = slug(label)
                    headings.append({"level": level, "text": re.sub(r"[`*]", "", label), "id": anchor})
                    sections.append(cur)
                    cur = {"h": re.sub(r"[`*]", "", label), "a": anchor, "t": []}
            elif t.type == "inline":
                cur["t"].append(t.content)
            for child in (t.children or []):
                if child.type == "link_open":
                    child.attrSet("href", self.rewrite(child.attrGet("href") or "", source))
                    if (child.attrGet("href") or "").startswith("http"):
                        child.attrSet("rel", "noopener")
                elif child.type == "image":
                    child.attrSet("src", self.rewrite(child.attrGet("src") or "", source))
                    child.attrSet("loading", "lazy")
            if t.type == "html_block":
                t.content = re.sub(r'(src|href)="([^"]+)"',
                                   lambda m: f'{m.group(1)}="{self.rewrite(m.group(2), source)}"', t.content)
            i += 1
        sections.append(cur)

        body = self.md.renderer.render(tokens, self.md.options, {})
        body = self.fence_blocks(body, tokens)
        body = re.sub(r"<table>", '<div class="table-wrap"><table>', body)
        body = body.replace("</table>", "</table></div>")

        for s in sections:
            words = " ".join(s["t"])
            words = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", words)         # links and images: keep their words
            words = re.sub(r"[`*_]|<[^>]+>", "", words)
            words = re.sub(r"\s+", " ", words).strip()
            if words:
                self.search.append({"p": self.page_of(stem), "t": title, "h": s["h"], "a": s["a"], "x": words[:1200]})
        return {"stem": stem, "title": title, "headings": headings, "body": body, "source": source}

    def fence_blocks(self, body: str, tokens) -> str:
        """Fenced code: Mermaid stays as source for the browser to draw; the rest is highlighted."""
        fences = [t for t in tokens if t.type == "fence"]
        parts = re.split(r'(<pre><code(?: class="language-[^"]*")?>.*?</code></pre>\n)', body, flags=re.S)
        n = 0
        for k, part in enumerate(parts):
            if not part.startswith("<pre><code"):
                continue
            f = fences[n]
            n += 1
            lang = (f.info or "").strip().split()[0] if (f.info or "").strip() else ""
            code = f.content
            if lang == "mermaid":
                parts[k] = (f'<figure class="diagram"><pre class="mermaid">{html.escape(code)}</pre></figure>\n')
            else:
                label = f'<span class="lang">{html.escape(lang)}</span>' if lang else ""
                parts[k] = (f'<div class="code">{label}<button class="copy" type="button" aria-label="Copy code">Copy</button>'
                            f'<pre class="hl"><code>{highlighted(code, lang)}</code></pre></div>\n')
        return "".join(parts)

    # ---------------------------------------------------------------- layout

    def nav(self, current: str) -> str:
        out = [f'<a class="home-link{" current" if current == "README" else ""}" href="index.html">Introduction</a>']
        for name, stems in GROUPS:
            out.append(f'<div class="group"><h2>{html.escape(name)}</h2><ul>')
            for s in stems:
                if s not in self.pages:
                    continue
                num, label = split_title(self.pages[s]["title"])
                label = f'<span class="num">{html.escape(num)}</span>{html.escape(label)}' if num else html.escape(label)
                cls = ' class="current" aria-current="page"' if s == current else ""
                out.append(f'<li><a{cls} href="{self.page_of(s)}">{label}</a></li>')
            out.append("</ul></div>")
        return "\n".join(out)

    def build(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)       # files are overwritten; start from an empty folder for a clean build
        for stem in ["README", *self.stems]:
            if (BOOK / f"{stem}.md").exists():
                self.pages[stem] = self.render(stem)
        order = ["README", *[s for s in self.stems if s in self.pages]]
        template = (ASSETS / "template.html").read_text()
        for k, stem in enumerate(order):
            p = self.pages[stem]
            prev_p = self.pages[order[k - 1]] if k else None
            next_p = self.pages[order[k + 1]] if k + 1 < len(order) else None
            toc = "".join(f'<li class="l{h["level"]}"><a href="#{h["id"]}">{html.escape(h["text"])}</a></li>' for h in p["headings"])
            num, heading = split_title(p["title"])
            eyebrow = ("Chapter " + num if num.isdigit() else "Appendix " + num) if num else ""
            body = p["body"]
            body = re.sub(r"^<h1[^>]*>.*?</h1>\n", "", body, count=1, flags=re.S)
            pager = ""
            if prev_p:
                pager += f'<a class="prev" href="{self.page_of(prev_p["stem"])}"><span>Previous</span>{html.escape(prev_p["title"])}</a>'
            if next_p:
                pager += f'<a class="next" href="{self.page_of(next_p["stem"])}"><span>Next</span>{html.escape(next_p["title"])}</a>'
            home = stem == "README"
            page = (template
                    .replace("{{title}}", html.escape(("Photo Estate: how it works" if home else f"{p['title']} · Photo Estate")))
                    .replace("{{eyebrow}}", html.escape("A technical book" if home else eyebrow))
                    .replace("{{cta}}", f'<p class="cta"><a href="{self.page_of(order[1])}">Start reading</a>'
                                        f'<a href="19-iphone-app.html">The iPhone app</a>'
                                        f'<a href="https://github.com/{REPO}">GitHub</a></p>' if home else "")
                    .replace("{{heading}}", html.escape(heading))
                    .replace("{{nav}}", self.nav(stem))
                    .replace("{{toc}}", toc)
                    .replace("{{body}}", body)
                    .replace("{{pager}}", pager)
                    .replace("{{bodyclass}}", "home" if home else "chapter")
                    .replace("{{source}}", f"https://github.com/{REPO}/blob/{BRANCH}/docs/book/{stem}.md")
                    .replace("{{repo}}", f"https://github.com/{REPO}"))
            (OUT / self.page_of(stem)).write_text(page)
        css = (ASSETS / "style.css").read_text() + "\n/* code */\n" + highlight_css()
        (OUT / "assets").mkdir(exist_ok=True)
        (OUT / "assets" / "book.css").write_text(css)
        shutil.copy2(ASSETS / "book.js", OUT / "assets" / "book.js")
        (OUT / "assets" / "search.json").write_text(json.dumps(self.search, separators=(",", ":")))
        (OUT / ".nojekyll").write_text("")
        print(f"built {len(order)} pages, {len(self.search)} search sections -> {OUT}")


if __name__ == "__main__":
    Site().build()

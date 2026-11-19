#!/usr/bin/env python3
"""Render the profile README cards as animated SVG files.

    python3 scripts/render_cards.py            # animated cards   -> cards/
    STATIC=1 python3 scripts/render_cards.py   # final frame only -> cards_static/ (+ preview.html)

Cards are shown through <img> tags, so everything is self-contained and limited to
what works there: one inline <style> block (CSS keyframes), SMIL <animate> for purely
decorative extras, and fonts subset with pyftsubset and embedded as base64 WOFF.

Data: data/contributions.json, refreshed daily by .github/workflows/cards.yml
(see scripts/fetch_data.sh for the schema). Only `pip install fonttools` is needed;
Pillow is optional (used to shrink assets/portrait.*).

Motion rules
  * Every animated element's base style IS its final frame; keyframes only have a
    `from` state. So `animation: none` (reduced motion / STATIC) shows the finished card.
  * SMIL is used only for decorative extras inside <g class="smil">, which CSS hides
    under prefers-reduced-motion (SMIL ignores `animation: none`).
  * Animated elements never carry a transform="" attribute (CSS transform would
    replace it); positions are absolute and any wrapper is a plain <g>.
"""
from __future__ import annotations

import base64
import bisect
import datetime as dt
import html
import io
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
STATIC = os.environ.get("STATIC", "").lower() in ("1", "true", "yes")
OUT_DIR = ROOT / ("cards_static" if STATIC else "cards")
DATA_FILE = Path(os.environ.get("DATA", ROOT / "data" / "contributions.json"))
FONT_DIR = ROOT / "fonts"

# --------------------------------------------------------------------------- config
W = 830  # GitHub's README column; cards are drawn 1:1 at this width

PROFILE = dict(
    login="lonerry",
    name="Ева Валлисту",
    role="backend-разработчик на Python",
    tagline="Backend-разработчик на Python. Проектирую API и сервисы на FastAPI и SQLAlchemy.",
    status="Открыта к предложениям",
    chips=["Python", "FastAPI", "PostgreSQL", "Redis", "Celery", "Docker"],
)

# Three tiles; the links themselves live in README.md.
TILES = [
    dict(id="case", icon="doc", label="портфолио", title="Кейс Rutrip",
         sub="Требования и архитектура"),
    dict(id="telegram", icon="send", label="связь", title="Telegram", sub="@lonerry"),
    dict(id="repos", icon="branch", label="код", title="Репозитории",
         sub="Весь код на GitHub"),  # title becomes "<n> репозиториев" when the data has it
]

# One card per entry. `blurb` wins over the GitHub description; the rest comes from data.
FEATURED = [
    dict(name="pii-service",
         blurb="API находит и маскирует персональные данные (ФИО, паспорт, телефон, карты) "
               "до обращения к LLM и восстанавливает значения в ответе.",
         tags=["FastAPI", "Redis", "Docker", "pytest", "Natasha"]),
    dict(name="task-platform",
         blurb="Микросервисная система задач: Auth (JWT), Task, Analytics и Notification "
               "(Telegram), обмен событиями через Kafka, мониторинг в Prometheus и Grafana.",
         tags=["FastAPI", "PostgreSQL", "Kafka", "Alembic", "Docker"]),
]

# Palette taken from the Rutrip case page (CSS variables --bg --ink --accent ...).
LIGHT = dict(bg="#fbfbfa", ink="#121413", ink2="#5c615e", ink3="#8a8f8c",
             rule="#e3e5e2", plate="#f0f1ef", accent="#0f766e")
DARK = dict(bg="#0f1110", ink="#eceeec", ink2="#a3a9a5", ink3="#727874",
            rule="#262a28", plate="#181b1a", accent="#4fd1c0")

# Seconds at which each card starts playing, so the page plays top to bottom.
T = dict(hero=0.0, tiles=0.55, activity=1.15, repos=1.95, graph=2.8)

MONTHS = "янв фев мар апр май июн июл авг сен окт ноя дек".split()


def _font(*names: str) -> Path:
    for n in names:
        if (FONT_DIR / n).exists():
            return FONT_DIR / n
    sys.exit(f"missing font: one of {names} in {FONT_DIR}")


# Inter Tight is Inter with tighter spacing. If fonts/InterTight-SemiBold.{ttf,otf} is
# present it is used; otherwise Inter SemiBold with extra negative tracking stands in.
_REAL_TIGHT = any((FONT_DIR / n).exists() for n in ("InterTight-SemiBold.ttf", "InterTight-SemiBold.otf"))
DISPLAY_TRACK = -0.035 if _REAL_TIGHT else -0.05  # em, for the large name only

FONTS = {
    "d": dict(css="Card Display", weight=600,
              file=_font("InterTight-SemiBold.ttf", "InterTight-SemiBold.otf", "Inter-SemiBold.otf")),
    "b": dict(css="Card Body", weight=400, file=_font("Inter-Regular.otf")),
    "m": dict(css="Card Mono", weight=400, file=_font("IBMPlexMono-Regular.ttf")),
}


# --------------------------------------------------------------------------- helpers
def esc(s) -> str:
    return html.escape(str(s), quote=True)


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return forms[2]
    r = n % 10
    return forms[0] if r == 1 else forms[1] if 2 <= r <= 4 else forms[2]


def fmt_date(d: dt.date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def mix(a: str, b: str, t: float) -> str:
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def ramp(p: dict) -> list[str]:
    """Contribution-graph levels: from the empty plate up to the brand colour."""
    return [mix(p["plate"], p["accent"], t) for t in (0.0, 0.28, 0.5, 0.75, 1.0)]


class Metrics:
    """Advance widths straight from the font files (no kerning: good to ~1-2%)."""

    def __init__(self, path: Path):
        f = TTFont(path)
        self.cmap, self.hmtx, self.upem = f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm

    def has(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def width(self, s: str, size: float, ls: float = 0.0) -> float:
        total = 0
        for ch in s:
            g = self.cmap.get(ord(ch), ".notdef")
            total += self.hmtx[g][0]
        return total * size / self.upem + ls * size * len(s)


METRICS = {k: Metrics(v["file"]) for k, v in FONTS.items()}


def wrap(s: str, font: str, size: float, maxw: float, ls: float = 0.0) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}" if cur else word
        if cur and METRICS[font].width(trial, size, ls) > maxw:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


_PYFTSUBSET = [shutil.which("pyftsubset")] if shutil.which("pyftsubset") else [sys.executable, "-m", "fontTools.subset"]
_subset_cache: dict[tuple[str, str], str] = {}


def woff_b64(font_key: str, chars: set[str]) -> str:
    """Subset one font to exactly the glyphs a card uses; return base64 WOFF."""
    text = "".join(sorted(chars | {" "}))
    key = (font_key, text)
    if key not in _subset_cache:
        with tempfile.TemporaryDirectory() as td:
            txt, out = Path(td, "chars.txt"), Path(td, "out.woff")
            txt.write_text(text, encoding="utf-8")
            subprocess.run(
                [*_PYFTSUBSET, str(FONTS[font_key]["file"]), f"--text-file={txt}", "--flavor=woff",
                 f"--output-file={out}", "--layout-features=kern,tnum,case", "--no-hinting"],
                check=True, capture_output=True, text=True)
            _subset_cache[key] = base64.b64encode(out.read_bytes()).decode("ascii")
    return _subset_cache[key]


# --------------------------------------------------------------------------- css
def _vars(p: dict) -> str:
    r = ramp(p)
    return (";".join(f"--{k}:{v}" for k, v in p.items()) + ";"
            + ";".join(f"--l{i}:{c}" for i, c in enumerate(r)) + f";--acs:{r[2]}")


BASE_CSS = (
    ":root{" + _vars(LIGHT) + "}"
    "@media (prefers-color-scheme:dark){:root{" + _vars(DARK) + "}}"
    ".bg{fill:var(--bg)}.pl{fill:var(--plate)}.ink{fill:var(--ink)}.i2{fill:var(--ink2)}"
    ".i3{fill:var(--ink3)}.ac{fill:var(--accent)}.acs{fill:var(--acs)}.nf{fill:none}"
    ".sr{stroke:var(--rule)}.sa{stroke:var(--accent)}.si{stroke:var(--ink)}.s2{stroke:var(--ink2)}"
    ".l0{fill:var(--l0)}.l1{fill:var(--l1)}.l2{fill:var(--l2)}.l3{fill:var(--l3)}.l4{fill:var(--l4)}"
    ".fd{font-family:'Card Display',Inter,system-ui,sans-serif;font-weight:600}"
    ".fb{font-family:'Card Body',Inter,system-ui,sans-serif;font-weight:400}"
    ".fm{font-family:'Card Mono','IBM Plex Mono',ui-monospace,monospace;font-weight:400}"
    ".tn{font-variant-numeric:tabular-nums}"
)

# Keyframes only define the `from` state: the base style is the final frame.
ANIM_CSS = (
    "@keyframes rise{from{opacity:0;transform:translateY(10px)}}"
    "@keyframes fade{from{opacity:0}}"
    "@keyframes grow{from{transform:scaleY(0)}}"
    "@keyframes growx{from{transform:scaleX(0)}}"
    "@keyframes draw{from{stroke-dashoffset:1}}"
    "@keyframes pop{from{opacity:0;transform:scale(.5)}}"
    "@keyframes nudge{0%,100%{transform:translateX(0)}50%{transform:translateX(4px)}}"
    ".rise{animation:rise .7s cubic-bezier(.2,.7,.2,1) both}"
    ".fade{animation:fade .8s ease-out both}"
    ".grow{transform-box:fill-box;transform-origin:50% 100%;animation:grow .9s cubic-bezier(.2,.7,.2,1) both}"
    ".growx{transform-box:fill-box;transform-origin:0 50%;animation:growx .9s cubic-bezier(.2,.7,.2,1) both}"
    ".draw{stroke-dasharray:1;animation:draw 1.1s ease-out both}"
    ".pop{transform-box:fill-box;transform-origin:50% 50%;animation:pop .5s cubic-bezier(.2,.9,.3,1.2) both}"
    ".nudge{animation:nudge 1.6s ease-in-out 3 both}"
    ".rise,.fade,.grow,.growx,.draw,.pop,.nudge{animation-delay:var(--d,0s)}"
    "@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}"
    ".smil{display:none}}"
)


# --------------------------------------------------------------------------- svg builder
class Card:
    def __init__(self, name: str, w: int, h: float, title: str, desc: str):
        self.name, self.w, self.h, self.title, self.desc = name, w, round(h), title, desc
        self.parts: list[str] = []
        self.chars: dict[str, set[str]] = defaultdict(set)
        self.defs: list[str] = []

    # -- animation plumbing
    @staticmethod
    def _a(anim: str | None, d: float) -> tuple[str, str]:
        return (f" {anim}", f' style="--d:{d:.2f}s"') if anim else ("", "")

    def raw(self, s: str):
        self.parts.append(s)

    def smil(self, s: str):
        """Decorative SMIL only. Omitted entirely in STATIC; hidden by CSS under reduced motion."""
        if not STATIC:
            self.parts.append(f'<g class="smil">{s}</g>')

    def open(self, anim: str | None = None, d: float = 0.0):
        cl, st = self._a(anim, d)
        self.parts.append(f'<g class="{cl.strip()}"{st}>' if anim else "<g>")

    def close(self):
        self.parts.append("</g>")

    # -- primitives
    def rect(self, x, y, w, h, cls="", rx=0, sw=None, anim=None, d=0.0):
        cl, st = self._a(anim, d)
        self.parts.append(
            f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}"'
            + (f' rx="{rx:g}"' if rx else "") + (f' stroke-width="{sw}"' if sw else "")
            + f' class="{cls}{cl}"{st}/>')

    def circle(self, cx, cy, r, cls="", sw=None, anim=None, d=0.0):
        cl, st = self._a(anim, d)
        self.parts.append(f'<circle cx="{cx:g}" cy="{cy:g}" r="{r:g}"' + (f' stroke-width="{sw}"' if sw else "")
                          + f' class="{cls}{cl}"{st}/>')

    def path(self, dattr, cls="nf si", sw=1, draw=False, anim=None, d=0.0, cap="butt"):
        if draw:
            anim = "draw"
        cl, st = self._a(anim, d)
        extra = ' pathLength="1"' if draw else ""
        self.parts.append(f'<path d="{dattr}"{extra} stroke-width="{sw}" stroke-linecap="{cap}" '
                          f'stroke-linejoin="round" class="{cls}{cl}"{st}/>')

    def text(self, x, y, s, font="b", size=15, cls="i2", anchor="start", ls=0.0, anim=None, d=0.0, extra=""):
        self.chars[font].update(s)
        for ch in s:
            if ch != " " and not METRICS[font].has(ch):
                print(f"warning: {self.name}: glyph U+{ord(ch):04X} {ch!r} missing in {font}", file=sys.stderr)
        cl, st = self._a(anim, d)
        lsa = f' letter-spacing="{ls * size:.2f}"' if ls else ""
        anc = f' text-anchor="{anchor}"' if anchor != "start" else ""
        self.parts.append(f'<text x="{x:g}" y="{y:g}" font-size="{size:g}"{lsa}{anc} '
                          f'class="f{font} {cls}{cl}"{st}{extra}>{esc(s)}</text>')

    # -- output
    def svg(self) -> str:
        faces = "".join(
            "@font-face{font-family:'%s';font-weight:%d;src:url(data:font/woff;base64,%s) format('woff')}"
            % (FONTS[k]["css"], FONTS[k]["weight"], woff_b64(k, chars))
            for k, chars in sorted(self.chars.items()) if chars)
        css = faces + BASE_CSS + ("" if STATIC else ANIM_CSS)
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-labelledby="t d">'
                f'<title id="t">{esc(self.title)}</title><desc id="d">{esc(self.desc)}</desc>'
                f"<style>{css}</style>{defs}{''.join(self.parts)}</svg>\n")


def frame(c: Card, rx: float = 14):
    c.rect(0.5, 0.5, c.w - 1, c.h - 1, "bg sr", rx=rx, sw=1)


# --------------------------------------------------------------------------- data
def synth() -> dict:
    """Placeholder numbers so STATIC=1 works before the first Action run. NOT real data."""
    rnd, end = random.Random(7), dt.date.today()
    days = []
    for i in range(364, -1, -1):
        d = end - dt.timedelta(days=i)
        busy = rnd.random() < (0.35 if d.weekday() < 5 else 0.12)
        days.append({"date": d.isoformat(), "count": rnd.choice([1, 1, 2, 3, 5, 8, 13]) if busy else 0})
    return dict(sample=True, login=PROFILE["login"], days=days, profile={"public_repos": 9},
                total=sum(x["count"] for x in days), repos=[])


def load_data() -> dict:
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    elif STATIC:
        print(f"warning: {DATA_FILE} not found - using SAMPLE numbers for the preview", file=sys.stderr)
        data = synth()
    else:
        sys.exit(f"{DATA_FILE} not found: run scripts/fetch_data.sh first")
    if data.get("sample"):
        print("warning: data is marked sample=true - the numbers on the cards are NOT real", file=sys.stderr)
    return data


class Stats:
    def __init__(self, data: dict):
        days = {dt.date.fromisoformat(x["date"]): int(x["count"]) for x in data["days"]}
        if not days:
            sys.exit("no contribution days in data")
        self.start, self.end = min(days), max(days)
        d = self.start
        while d <= self.end:
            days.setdefault(d, 0)
            d += dt.timedelta(days=1)
        self.days = days
        window = {d: c for d, c in days.items() if d > self.end - dt.timedelta(days=365)}
        self.total = sum(window.values())
        self.active = sum(1 for c in window.values() if c > 0)
        self.window_days = len(window)
        seq = [days[d] for d in sorted(days)]
        run = longest = 0
        for c in seq:
            run = run + 1 if c > 0 else 0
            longest = max(longest, run)
        i = len(seq) - 1
        if seq[i] == 0:  # today may still be empty without breaking the streak
            i -= 1
        cur = 0
        while i >= 0 and seq[i] > 0:
            cur += 1
            i -= 1
        self.streak, self.longest = cur, longest
        self.best_day, self.best_count = max(window.items(), key=lambda kv: (kv[1], kv[0]))
        # last 12 calendar months, current month included
        y, m, months = self.end.year, self.end.month, []
        for _ in range(12):
            months.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        self.months = [(y, m, sum(c for d, c in days.items() if (d.year, d.month) == (y, m)))
                       for y, m in reversed(months)]
        self.month_total = sum(c for *_, c in self.months)
        # grid: Sunday-first weeks, at most 53 columns, ending with the week of `end`
        end_sun = self.end - dt.timedelta(days=(self.end.weekday() + 1) % 7)
        first_sun = max(self.start - dt.timedelta(days=(self.start.weekday() + 1) % 7),
                        end_sun - dt.timedelta(weeks=52))
        self.first_sun, self.ncols = first_sun, (end_sun - first_sun).days // 7 + 1
        nz = sorted(c for c in window.values() if c > 0)
        self.cuts = [nz[min(len(nz) - 1, int(len(nz) * q))] for q in (0.25, 0.5, 0.75)] if nz else [1, 2, 3]

    def level(self, c: int) -> int:
        return 0 if c <= 0 else 1 + bisect.bisect_left(self.cuts, c) if c <= self.cuts[-1] else 4


# --------------------------------------------------------------------------- cards
def load_portrait() -> str | None:
    for ext in ("jpg", "jpeg", "png", "webp"):
        p = ROOT / "assets" / f"portrait.{ext}"
        if not p.exists():
            continue
        try:
            from PIL import Image  # optional
            im = Image.open(p).convert("RGB")
            s = min(im.size)
            l, t = (im.width - s) // 2, (im.height - s) // 2
            im = im.crop((l, t, l + s, t + s)).resize((320, 320), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=86, optimize=True)
            raw, mime = buf.getvalue(), "image/jpeg"
        except ImportError:
            raw, mime = p.read_bytes(), {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                                         "webp": "image/webp"}[ext]
            if len(raw) > 150_000:
                print("warning: portrait is large and Pillow is missing; pip install pillow to shrink it",
                      file=sys.stderr)
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    return None


def card_hero() -> Card:
    P, pad, t0 = PROFILE, 40, T["hero"]
    portrait = load_portrait()
    text_w = (W - 2 * pad) - (190 if portrait else 0)
    tag_size = next((z for z in (20, 19, 18, 17) if METRICS["b"].width(P["tagline"], z) <= text_w), 18)
    lines = wrap(P["tagline"], "b", tag_size, text_w)
    tag_y = 166
    chips_y = tag_y + 26 * (len(lines) - 1) + 24
    # chips layout
    chips, cx, cy = [], pad, chips_y
    for name in P["chips"]:
        w = METRICS["m"].width(name, 12) + 22
        if cx + w > pad + text_w:
            cx, cy = pad, cy + 32
        chips.append((name, cx, cy, w))
        cx += w + 8
    H = max(cy + 24 + 36, 250 if portrait else 0)
    c = Card("hero", W, H, f"{P['name']} - {P['role']}", P["tagline"])
    frame(c)
    # top bar
    c.text(pad, 36, f"github.com/{P['login']}", "m", 12, "i3", anim="fade", d=t0 + 0.05)
    sw = METRICS["m"].width(P["status"], 12)
    sx = W - pad
    c.text(sx, 36, P["status"], "m", 12, "i2", anchor="end", anim="fade", d=t0 + 0.15)
    dx = sx - sw - 14
    c.circle(dx, 32, 3.5, "ac", anim="pop", d=t0 + 0.25)
    c.smil(f'<circle cx="{dx:g}" cy="32" r="3.5" class="nf sa" stroke-width="1.2" opacity="0">'
           f'<animate attributeName="r" values="3.5;11" dur="2.6s" begin="{t0 + 1.2:.1f}s" repeatCount="indefinite"/>'
           f'<animate attributeName="opacity" values="0.7;0" dur="2.6s" begin="{t0 + 1.2:.1f}s" repeatCount="indefinite"/>'
           f"</circle>")
    # hairline that draws itself, with a glint that sweeps along it
    c.path(f"M{pad} 52H{W - pad}", "nf sr", 1, draw=True, d=t0 + 0.1)
    c.defs.append('<linearGradient id="gl"><stop offset="0" style="stop-color:var(--accent);stop-opacity:0"/>'
                  '<stop offset=".5" style="stop-color:var(--accent);stop-opacity:1"/>'
                  '<stop offset="1" style="stop-color:var(--accent);stop-opacity:0"/></linearGradient>')
    c.smil(f'<rect x="{pad - 120}" y="51" width="120" height="2" fill="url(#gl)">'
           f'<animate attributeName="x" values="{pad - 120};{W - pad};{W - pad}" keyTimes="0;.35;1" dur="7s" '
           f'begin="{t0 + 1.0:.1f}s" repeatCount="indefinite"/></rect>')
    # name + tagline
    c.text(pad, 124, P["name"], "d", 58, "ink", ls=DISPLAY_TRACK, anim="rise", d=t0 + 0.2)
    for i, line in enumerate(lines):
        c.text(pad, tag_y + 26 * i, line, "b", tag_size, "i2", anim="rise", d=t0 + 0.4 + 0.06 * i)
    for i, (name, x, y, w) in enumerate(chips):
        dd = t0 + 0.65 + 0.07 * i
        c.open("rise", dd)
        c.rect(x + 0.5, y + 0.5, w - 1, 23, "bg sr", rx=11.5, sw=1)
        c.text(x + 11, y + 16, name, "m", 12, "i2")
        c.close()
    if portrait:
        px, py, r = W - pad - 78, 150, 70
        c.defs.append(f'<clipPath id="pc"><circle cx="{px}" cy="{py}" r="{r}"/></clipPath>')
        c.open("pop", t0 + 0.35)
        c.circle(px, py, r + 7, "nf sa", sw=1.2)
        c.raw(f'<image href="{portrait}" x="{px - r}" y="{py - r}" width="{2 * r}" height="{2 * r}" '
              f'clip-path="url(#pc)" preserveAspectRatio="xMidYMid slice"/>')
        c.close()
    return c


ICONS = {  # 24x24 stroke icons, drawn by us
    "doc": '<rect x="4.5" y="3.5" width="15" height="17" rx="2.5"/><path d="M8 9h8M8 13h8M8 17h5"/>',
    "send": '<path d="M20.5 3.5 3.5 10.8l6.2 2.5 2.5 6.2z"/><path d="M9.7 13.3l10.8-9.8"/>',
    "branch": '<circle cx="6.5" cy="5.5" r="2.3"/><circle cx="6.5" cy="18.5" r="2.3"/><circle cx="17.5" cy="9" r="2.3"/>'
              '<path d="M6.5 7.8v8.4M17.5 11.3c0 4-11 2.5-11 5"/>',
}


def card_tile(spec: dict, i: int, n_repos: int | None) -> Card:
    TW, TH, t0 = 270, 112, T["tiles"] + 0.12 * i
    title = spec["title"]
    if spec["id"] == "repos" and n_repos:
        title = f"{n_repos} {plural(n_repos, ('репозиторий', 'репозитория', 'репозиториев'))}"
    c = Card(f"tile-{spec['id']}", TW, TH, f"{title} - {spec['sub']}", f"{spec['label']}: {title}, {spec['sub']}")
    frame(c, 12)
    c.open("rise", t0)
    c.text(20, 32, spec["label"].upper(), "m", 11, "i3", ls=0.08)
    c.raw(f'<g transform="translate({TW - 20 - 22} 17)" class="nf s2" stroke-width="1.5" '
          f'stroke-linecap="round" stroke-linejoin="round">{ICONS[spec["icon"]]}</g>')
    c.close()
    c.text(20, 70, title, "d", 22, "ink", ls=-0.02, anim="rise", d=t0 + 0.12)
    if METRICS["b"].width(spec["sub"], 13) > TW - 20 - 52:
        print(f"warning: tile {spec['id']!r}: subtitle runs into the arrow; shorten it", file=sys.stderr)
    c.text(20, 92, spec["sub"], "b", 13, "i2", anim="rise", d=t0 + 0.22)
    c.open("nudge", t0 + 1.2)
    c.path(f"M{TW - 38} 83H{TW - 22}M{TW - 28} 77.5l6 5.5-6 5.5", "nf sa", 1.6, cap="round")
    c.close()
    return c


def nice_top(v: int) -> int:
    for t in (4, 6, 8, 10, 12, 16, 20, 24, 30, 40, 60, 80, 100, 120, 160, 200, 240, 300, 400, 600, 800):
        if v <= t:
            return t
    return int(math.ceil(v / 200) * 200)


def card_activity(st: Stats) -> Card:
    H, pad, t0 = 352, 28, T["activity"]
    c = Card("activity", W, H, "Вклады по месяцам",
             f"{st.month_total} вкладов за 12 месяцев; серия {st.streak}, активных дней {st.active}, "
             f"рекорд за день {st.best_count} ({fmt_date(st.best_day)})")
    frame(c)
    c.rect(pad, 33, 7, 7, "ac", rx=1.5, anim="pop", d=t0)
    c.text(pad + 17, 41, "активность · 12 месяцев", "m", 12, "i2", anim="fade", d=t0 + 0.05)
    c.text(W - pad, 41, f"{st.month_total} {plural(st.month_total, ('вклад', 'вклада', 'вкладов'))}",
           "m", 12, "i2", anchor="end", anim="fade", d=t0 + 0.1)
    c.path(f"M{pad} 56H{W - pad}", "nf sr", 1, draw=True, d=t0 + 0.05)

    # bars
    px0, cw, top_y, base_y = 64, 452, 108, 288
    peak = max(m[2] for m in st.months)
    top = nice_top(peak)
    for frac in (1, 0.5):
        y = base_y - (base_y - top_y) * frac
        c.path(f"M{px0} {y:g}H{px0 + cw}", "nf sr", 1, draw=True, d=t0 + 0.15)
        c.text(px0 - 10, y + 4, f"{int(top * frac)}", "m", 10.5, "i3", anchor="end", anim="fade", d=t0 + 0.2)
    c.text(px0 - 10, base_y + 4, "0", "m", 10.5, "i3", anchor="end", anim="fade", d=t0 + 0.2)
    slot, bw = cw / 12, 22
    for i, (y, m, v) in enumerate(st.months):
        x = px0 + i * slot + (slot - bw) / 2
        h = (base_y - top_y) * v / top
        last = i == len(st.months) - 1
        d = t0 + 0.3 + 0.06 * i
        if v > 0:
            c.rect(x, base_y - max(h, 2), bw, max(h, 2), "ac" if last else "acs", rx=2, anim="grow", d=d)
            c.text(x + bw / 2, base_y - max(h, 2) - 7, str(v), "m", 10.5, "ink" if last else "i2",
                   anchor="middle", anim="fade", d=d + 0.5)
        c.text(x + bw / 2, base_y + 20, MONTHS[m - 1], "m", 11, "ink" if last else "i3", anchor="middle",
               anim="fade", d=d)
    c.path(f"M{px0} {base_y}H{px0 + cw}", "nf si", 1, draw=True, d=t0 + 0.2)

    # stats column
    c.path(f"M542 76V{H - 24}", "nf sr", 1, draw=True, d=t0 + 0.2)
    sx = 574
    blocks = [
        ("серия", str(st.streak), plural(st.streak, ("день", "дня", "дней")),
         f"рекорд: {st.longest} {plural(st.longest, ('день', 'дня', 'дней'))}"),
        ("активных дней", str(st.active), f"из {st.window_days}",
         f"{round(100 * st.active / st.window_days)}% дней за год"),
        ("самый активный день", str(st.best_count), plural(st.best_count, ("вклад", "вклада", "вкладов")),
         fmt_date(st.best_day)),
    ]
    for i, (label, num, unit, sub) in enumerate(blocks):
        y, d = 98 + i * 90, t0 + 0.35 + 0.15 * i
        c.text(sx, y, label.upper(), "m", 11, "i3", ls=0.08, anim="rise", d=d)
        c.text(sx, y + 38, num, "d", 40, "ink", ls=-0.03, anim="rise", d=d + 0.08)
        nw = METRICS["d"].width(num, 40, -0.03)
        c.text(sx + nw + 8, y + 38, unit, "b", 15, "i2", anim="rise", d=d + 0.12)
        c.text(sx, y + 58, sub, "b", 13, "i2", anim="rise", d=d + 0.16)
    return c


def icon_star(x, y, r=6.0, r2=2.6) -> str:
    cx, cy, pts = x + r, y + r, []
    for k in range(10):
        a, rad = -math.pi / 2 + k * math.pi / 5, r if k % 2 == 0 else r2
        pts.append(f"{cx + rad * math.cos(a):.2f} {cy + rad * math.sin(a):.2f}")
    return "M" + "L".join(pts) + "Z"


def card_repo(spec: dict, repo: dict | None, i: int) -> Card:
    pad, t0 = 28, T["repos"] + 0.22 * i
    name = spec["name"]
    desc = spec.get("blurb") or (repo or {}).get("description") or ""
    lines = wrap(desc, "b", 15, W - 2 * pad - 24) if desc else []
    H = 96 + 22 * max(len(lines) - 1, 0) + 24 + 30
    c = Card(f"repo-{name}", W, H, name, desc or name)
    frame(c)
    c.rect(pad, 24, 3, H - 48, "ac", rx=1.5, anim="grow", d=t0)
    x0 = pad + 20
    c.text(x0, 38, f"проект · {i + 1:02d}", "m", 11, "i3", ls=0.08, anim="fade", d=t0 + 0.1)
    c.text(x0, 70, name, "d", 24, "ink", ls=-0.025, anim="rise", d=t0 + 0.15)
    ax = x0 + METRICS["d"].width(name, 24, -0.025) + 12
    c.path(f"M{ax:g} 66L{ax + 10:g} 56M{ax + 1:g} 56H{ax + 10:g}V{65:g}", "nf sa", 1.7, cap="round",
           anim="fade", d=t0 + 0.3)
    for k, line in enumerate(lines):
        c.text(x0, 98 + 22 * k, line, "b", 15, "i2", anim="rise", d=t0 + 0.25 + 0.07 * k)
    by = H - 24
    d = t0 + 0.5
    c.text(x0, by, " · ".join(spec.get("tags", [])), "m", 11.5, "i3", anim="fade", d=d)
    # right-aligned meta: updated, forks, stars, language
    items: list[tuple[str, str, str]] = []  # (kind, text, extra)
    if repo and repo.get("pushedAt"):
        items.append(("txt", f"обновлён {fmt_date(dt.date.fromisoformat(repo['pushedAt'][:10]))}", ""))
    if repo:
        items.append(("fork", str(repo.get("forks", 0)), ""))
        items.append(("star", str(repo.get("stars", 0)), ""))
        if repo.get("language"):
            items.append(("lang", repo["language"], repo.get("languageColor") or ""))
    x = W - pad
    c.open("fade", d)
    for kind, text, extra in items:  # right to left
        tw = METRICS["m"].width(text, 11.5)
        c.text(x, by, text, "m", 11.5, "i2", anchor="end")
        x -= tw
        if kind == "star":
            x -= 18
            c.path(icon_star(x, by - 10, 6.0, 2.6), "nf s2", 1.2)
        elif kind == "fork":
            x -= 18
            c.raw(f'<g class="s2" style="fill:none" stroke-width="1.2" stroke-linecap="round">'
                  f'<circle cx="{x + 2.5:g}" cy="{by - 8:g}" r="1.7"/><circle cx="{x + 9.5:g}" cy="{by - 8:g}" r="1.7"/>'
                  f'<circle cx="{x + 6:g}" cy="{by:g}" r="1.7"/>'
                  f'<path d="M{x + 2.5:g} {by - 6.3:g}V{by - 4.5:g}Q{x + 2.5:g} {by - 3:g} {x + 4:g} {by - 3:g}H{x + 8:g}'
                  f'Q{x + 9.5:g} {by - 3:g} {x + 9.5:g} {by - 4.5:g}V{by - 6.3:g}M{x + 6:g} {by - 3:g}V{by - 1.7:g}"/></g>')
        elif kind == "lang":
            x -= 14
            fill = extra if extra.startswith("#") else None
            c.raw(f'<circle cx="{x + 4:g}" cy="{by - 4:g}" r="4" '
                  + (f'style="fill:{esc(fill)}"' if fill else 'class="ac"') + "/>")
        x -= 16
    c.close()
    return c


def card_graph(st: Stats) -> Card:
    pad, label_w, gap, t0 = 28, 26, 3, T["graph"]
    x0 = pad + label_w
    stride = (W - pad - x0 + gap) / st.ncols
    cell = stride - gap
    gy = 98
    H = round(gy + 7 * stride + 54)
    c = Card("graph", W, H, "Календарь вкладов",
             f"{st.total} вкладов за год, {st.active} активных дней")
    frame(c)
    c.rect(pad, 33, 7, 7, "ac", rx=1.5, anim="pop", d=t0)
    c.text(pad + 17, 41, "календарь · 365 дней", "m", 12, "i2", anim="fade", d=t0 + 0.05)
    c.text(W - pad, 41, f"{st.total} {plural(st.total, ('вклад', 'вклада', 'вкладов'))} · "
                        f"{st.active} {plural(st.active, ('активный день', 'активных дня', 'активных дней'))}",
           "m", 12, "i2", anchor="end", anim="fade", d=t0 + 0.1)
    c.path(f"M{pad} 56H{W - pad}", "nf sr", 1, draw=True, d=t0 + 0.05)
    for row, lab in ((1, "Пн"), (3, "Ср"), (5, "Пт")):
        c.text(pad, gy + row * stride + cell / 2 + 3.5, lab, "m", 10, "i3", anim="fade", d=t0 + 0.2)
    # month labels where the month changes
    last_lab_col = -9
    for col in range(st.ncols):
        sun = st.first_sun + dt.timedelta(weeks=col)
        prev = sun - dt.timedelta(weeks=1)
        if (col == 0 or sun.month != prev.month) and col - last_lab_col >= 3:
            if col == 0 and st.ncols > 1 and (sun + dt.timedelta(weeks=1)).month != sun.month:
                continue  # first column is the tail of a month; label the next one instead
            c.text(x0 + col * stride, gy - 12, MONTHS[sun.month - 1], "m", 10.5, "i3", anim="fade", d=t0 + 0.2)
            last_lab_col = col
    # cells, one animated group per week column
    last_cell = None
    for col in range(st.ncols):
        cells = []
        for row in range(7):
            d = st.first_sun + dt.timedelta(days=col * 7 + row)
            if d < st.start or d > st.end:
                continue
            v = st.days.get(d, 0)
            cx_, cy_ = x0 + col * stride, gy + row * stride
            cells.append(f'<rect x="{cx_:.2f}" y="{cy_:.2f}" width="{cell:.2f}" height="{cell:.2f}" rx="2.2" '
                         f'class="l{st.level(v)}"><title>{fmt_date(d)}: {v}</title></rect>')
            last_cell = (cx_, cy_)
        c.raw(f'<g class="rise" style="--d:{t0 + 0.2 + col * 0.013:.3f}s">{"".join(cells)}</g>')
    if last_cell:  # decorative ring on the latest day
        lx, ly = last_cell
        c.smil(f'<rect x="{lx - 1.5:.2f}" y="{ly - 1.5:.2f}" width="{cell + 3:.2f}" height="{cell + 3:.2f}" rx="3.5" '
               f'class="nf sa" stroke-width="1.2" opacity="0">'
               f'<animate attributeName="opacity" values="0;0.9;0" dur="2.4s" begin="{t0 + 1.4:.1f}s" repeatCount="indefinite"/></rect>')
    # legend
    ly = H - 30
    lx = W - pad
    c.open("fade", t0 + 1.0)
    c.text(lx, ly + 9, "больше", "m", 10.5, "i3", anchor="end")
    lx -= METRICS["m"].width("больше", 10.5) + 8
    for lvl in (4, 3, 2, 1, 0):
        lx -= cell
        c.rect(lx, ly, cell, cell, f"l{lvl}", rx=2.2)
        lx -= 4
    c.text(lx - 4, ly + 9, "меньше", "m", 10.5, "i3", anchor="end")
    c.close()
    return c


# --------------------------------------------------------------------------- main
PREVIEW = """<!doctype html><meta charset="utf-8"><meta name="color-scheme" content="light dark">
<title>cards preview (STATIC)</title>
<style>body{margin:0;padding:32px 0;background:#fff;color:#1f2328}
@media (prefers-color-scheme:dark){body{background:#0d1117}}
.w{width:830px;margin:0 auto}p{margin:0 0 16px}img{max-width:100%%;vertical-align:bottom}</style>
<div class="w">
%s
</div>
"""


def write_preview(cards: list[Card]):
    by = {c.name: c for c in cards}
    img = lambda n, w="100%": f'<img src="{n}.svg" width="{w}" alt="{esc(by[n].title)}">'  # noqa: E731
    rows = [f"<p>{img('hero')}</p>",
            "<p>" + "\n".join(img(f"tile-{t['id']}", "32.5%") for t in TILES) + "</p>",
            f"<p>{img('activity')}</p>"]
    rows += [f"<p>{img('repo-' + f['name'])}</p>" for f in FEATURED]
    rows.append(f"<p>{img('graph')}</p>")
    (OUT_DIR / "preview.html").write_text(PREVIEW % "\n".join(rows), encoding="utf-8")


def main():
    data = load_data()
    st = Stats(data)
    repos = {r["name"]: r for r in data.get("repos", [])}
    n_repos = (data.get("profile") or {}).get("public_repos")
    cards = [card_hero(), *[card_tile(t, i, n_repos) for i, t in enumerate(TILES)], card_activity(st)]
    for i, f in enumerate(FEATURED):
        if f["name"] not in repos and not data.get("sample"):
            print(f"warning: repo {f['name']!r} not in data - stats left blank", file=sys.stderr)
        cards.append(card_repo(f, repos.get(f["name"]), i))
    cards.append(card_graph(st))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.svg"):
        old.unlink()
    for c in cards:
        (OUT_DIR / f"{c.name}.svg").write_text(c.svg(), encoding="utf-8")
    if STATIC:
        write_preview(cards)
    mode = "STATIC (final frame, no animation)" if STATIC else "animated"
    print(f"{mode}: {len(cards)} cards -> {OUT_DIR.relative_to(ROOT)}/")
    for c in cards:
        print(f"  {c.name + '.svg':32s}{(OUT_DIR / (c.name + '.svg')).stat().st_size / 1024:7.1f} KB")


if __name__ == "__main__":
    main()

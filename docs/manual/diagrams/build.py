"""Draw the manual's SVG diagrams. Run from anywhere; writes next to this file."""

from __future__ import annotations

import math
from pathlib import Path

OUT = Path(__file__).resolve().parent

INK = "#1c2430"
MUTED = "#3d4c5c"
LINE = "#3d4c5c"
BLUE_F = "#e7eef6"
BLUE_S = "#2c4a6e"
GREEN_F = "#e7f2ea"
GREEN_S = "#1f5c38"
AMBER_F = "#fbf3e4"
AMBER_S = "#8a5a12"
RED_F = "#f8ecec"
RED_S = "#8a3030"
GRAY_F = "#f4f6f8"
GRAY_S = "#5c6b7a"
WHITE = "#ffffff"


class Svg:
    def __init__(self, width: int, height: int) -> None:
        self.w = width
        self.h = height
        self.parts: list[str] = []

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill: str,
        stroke: str = INK,
        rx: float = 7,
        sw: float = 1.6,
        dash: str | None = None,
    ) -> None:
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{extra}/>'
        )

    def polygon(self, points: list[tuple[float, float]], fill: str, stroke: str) -> None:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        self.parts.append(
            f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="1.6" '
            f'stroke-linejoin="round"/>'
        )

    def line(self, points: list[tuple[float, float]], color: str = LINE, sw: float = 1.5) -> None:
        if len(points) < 2:
            return
        d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in points)
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )

    def arrow(
        self,
        points: list[tuple[float, float]],
        color: str = LINE,
        sw: float = 1.5,
        head: float = 8,
    ) -> None:
        if len(points) < 2:
            return
        end = points[-1]
        prev = points[-2]
        dx, dy = end[0] - prev[0], end[1] - prev[1]
        length = math.hypot(dx, dy) or 1
        ux, uy = dx / length, dy / length
        base = (end[0] - ux * head, end[1] - uy * head)
        self.line([*points[:-1], base], color, sw)
        px, py = -uy, ux
        wing = head * 0.48
        self.polygon(
            [
                end,
                (base[0] + px * wing, base[1] + py * wing),
                (base[0] - px * wing, base[1] - py * wing),
            ],
            color,
            color,
        )

    def text(
        self,
        x: float,
        y: float,
        lines: list[str],
        size: float = 12,
        weight: str = "600",
        fill: str = INK,
        anchor: str = "middle",
        leading: float | None = None,
    ) -> None:
        lead = leading if leading is not None else size + 3
        start = y - (len(lines) - 1) * lead / 2
        tspans = []
        for i, line in enumerate(lines):
            dy = 0 if i == 0 else lead
            tspans.append(
                f'<tspan x="{x:.1f}" dy="{dy:.1f}">{_esc(line)}</tspan>'
            )
        self.parts.append(
            f'<text x="{x:.1f}" y="{start:.1f}" text-anchor="{anchor}" '
            f'font-family="Liberation Sans, Nimbus Sans, sans-serif" '
            f'font-size="{size}" font-weight="{weight}" fill="{fill}">{"".join(tspans)}</text>'
        )

    def label(self, x: float, y: float, value: str, size: float = 10) -> None:
        pad = 3
        width = max(28, len(value) * size * 0.52 + pad * 2)
        self.rect(x - width / 2, y - size * 0.75, width, size + 4, WHITE, WHITE, rx=2, sw=0)
        self.text(x, y + 1, [value], size=size, weight="500", fill=MUTED)

    def save(self, name: str) -> None:
        body = "\n".join(self.parts)
        doc = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}">\n'
            f'<rect width="100%" height="100%" fill="{WHITE}"/>\n'
            f"{body}\n</svg>\n"
        )
        (OUT / name).write_text(doc, encoding="utf-8")


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def node(
    svg: Svg,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    sub: list[str] | None = None,
    fill: str = WHITE,
    stroke: str = INK,
    dash: str | None = None,
) -> tuple[float, float, float, float]:
    svg.rect(x, y, w, h, fill, stroke, dash=dash)
    cx, cy = x + w / 2, y + h / 2
    if sub:
        svg.text(cx, cy - 7, [title], size=13, weight="700")
        svg.text(cx, cy + 11, sub, size=10.5, weight="500", fill=MUTED, leading=13)
    else:
        svg.text(cx, cy + 1, [title], size=13, weight="700")
    return x, y, w, h


def diamond(
    svg: Svg,
    cx: float,
    cy: float,
    w: float,
    h: float,
    title: str,
    fill: str = AMBER_F,
    stroke: str = AMBER_S,
) -> None:
    svg.polygon(
        [(cx, cy - h / 2), (cx + w / 2, cy), (cx, cy + h / 2), (cx - w / 2, cy)],
        fill,
        stroke,
    )
    svg.text(cx, cy + 1, [title], size=12, weight="700")


def system() -> None:
    s = Svg(880, 560)
    node(s, 24, 28, 168, 64, "Person", ["creates tasks, decides"], BLUE_F, BLUE_S)
    node(s, 268, 18, 230, 84, "API and dashboard", ["127.0.0.1:8000", "database URL only"], BLUE_F, BLUE_S)
    node(s, 572, 14, 284, 92, "PostgreSQL", ["tasks, events, approvals", "artifacts and checkpoints"], GRAY_F, GRAY_S)
    s.arrow([(192, 60), (268, 60)])
    s.label(230, 48, "HTTP")
    s.arrow([(498, 60), (572, 60)])
    s.label(535, 48, "read / write")

    node(s, 292, 168, 250, 88, "Worker", ["one task at a time", "model key and GitHub token"], GREEN_F, GREEN_S)
    s.arrow([(714, 106), (714, 140), (500, 140), (500, 168)])
    s.label(600, 128, "claim, stage, lease")

    s.line([(417, 256), (417, 292)])
    s.line([(120, 292), (754, 292)])
    for x in (120, 330, 540, 754):
        s.arrow([(x, 292), (x, 318)])

    node(s, 24, 318, 192, 86, "Checkouts", ["read-only mount", "worker only"], GRAY_F, GRAY_S)
    node(s, 234, 318, 192, 86, "Private worktrees", ["bare clone, task branch", "not the registered .git"], GREEN_F, GREEN_S)
    node(s, 444, 318, 192, 86, "Host Ollama", ["optional", "no credentials sent"], BLUE_F, BLUE_S)
    node(s, 654, 318, 202, 86, "GitHub API", ["second approval", "draft, never main"], AMBER_F, AMBER_S, dash="5 3")

    node(s, 234, 456, 192, 80, "Restricted runner", ["fixed pytest", "no network, no secrets"], AMBER_F, AMBER_S)
    s.arrow([(330, 404), (330, 456)])
    s.label(392, 430, "job id only")
    s.save("system.svg")


def submodels() -> None:
    s = Svg(880, 592)
    s.rect(16, 14, 412, 78, BLUE_F, BLUE_S, rx=8)
    s.text(222, 38, ["qwen3.5:4b"], size=16, weight="700", fill=BLUE_S)
    s.text(222, 60, ["one shared client  ·  triage, plan, supervisor"], size=11, weight="500", fill=MUTED)

    s.rect(452, 14, 412, 78, GREEN_F, GREEN_S, rx=8)
    s.text(658, 38, ["qwen2.5-coder:7b"], size=16, weight="700", fill=GREEN_S)
    s.text(658, 60, ["one shared client  ·  investigate, develop, review"], size=11, weight="500", fill=MUTED)

    stages = [
        (28, "triage", "TriageResult", "stored on the task", BLUE_F, BLUE_S, 222),
        (168, "plan", "ChangePlan", "path filter", BLUE_F, BLUE_S, 222),
        (308, "supervisor", "Recommendation", "allowlist only", BLUE_F, BLUE_S, 222),
        (464, "investigate", "Report", "citation ledger", GREEN_F, GREEN_S, 658),
        (604, "develop", "DevelopDiff", "git apply --check", GREEN_F, GREEN_S, 658),
        (744, "review", "ReviewResult", "review policy", GREEN_F, GREEN_S, 658),
    ]
    for x, title, schema, check, fill, stroke, origin in stages:
        s.arrow([(origin, 92), (origin, 118), (x + 60, 118), (x + 60, 140)])
        node(s, x, 140, 120, 58, title, fill=fill, stroke=stroke)
        s.arrow([(x + 60, 198), (x + 60, 228)])
        node(s, x, 228, 120, 52, schema, fill=WHITE, stroke=stroke)
        s.arrow([(x + 60, 280), (x + 60, 310)])
        node(s, x, 310, 120, 64, "checker", [check], AMBER_F, AMBER_S)

    s.rect(16, 408, 848, 168, GRAY_F, GRAY_S, rx=8)
    s.text(440, 432, ["Same graph, no model call. Each returns to the supervisor."], size=13, weight="700", fill=GRAY_S)
    quiet = [
        (36, "plan approval", "interrupt, then a person"),
        (206, "workspace", "private clone"),
        (376, "test", "baseline, then patch"),
        (546, "publish approval", "second interrupt"),
        (716, "publish / finalize", "draft PR or stop"),
    ]
    for x, title, sub in quiet:
        node(s, x, 456, 150, 64, title, [sub], WHITE, GRAY_S)
    s.text(440, 548, ["The supervisor names the next node. These five do not call a model."], size=11, weight="500", fill=MUTED)
    s.save("submodels.svg")


def pipeline() -> None:
    s = Svg(880, 250)
    row1 = [
        (16, "Bug report", "title and text"),
        (168, "TriageResult", "class, severity"),
        (320, "Investigation", "cited lines only"),
        (472, "ChangePlan", "allowed paths"),
        (624, "Person", "approve or reject"),
    ]
    for i, (x, title, sub) in enumerate(row1):
        fill, stroke = (AMBER_F, AMBER_S) if title == "Person" else (BLUE_F, BLUE_S)
        node(s, x, 28, 140, 62, title, [sub], fill, stroke)
        if i:
            s.arrow([(x - 12, 59), (x, 59)])
    s.arrow([(694, 90), (694, 150), (86, 150), (86, 168)])
    s.label(400, 138, "only after approval")

    row2 = [
        (16, "Worktree", "source HEAD"),
        (160, "Baseline", "pytest, no patch"),
        (304, "DevelopDiff", "three snippets"),
        (448, "Applied diff", "git apply"),
        (592, "Patched tests", "new job id"),
        (736, "Policy", "accept, repair, fail"),
    ]
    for i, (x, title, sub) in enumerate(row2):
        fill, stroke = (GREEN_F, GREEN_S) if i < 5 else (AMBER_F, AMBER_S)
        node(s, x, 168, 132, 62, title, [sub], fill, stroke)
        if i:
            s.arrow([(x - 12, 199), (x, 199)])
    s.save("pipeline.svg")


def call_flow() -> None:
    s = Svg(880, 300)
    boxes = [
        (16, "Stage", "schema"),
        (128, "Capability", "structured?"),
        (240, "Circuit", "two failures?"),
        (352, "Budget", "calls, tokens,"),
        (464, "Local lock", "Ollama only"),
        (576, "Provider", "Ollama or API"),
        (688, "Validate", "Pydantic"),
        (800, "Usage row", "no prompt text"),
    ]
    # last box would overflow 880 if width 100 at x=800. Recalculate.
    s.parts.clear()
    xs = [12, 122, 232, 342, 452, 562, 672, 782]
    labels = [
        ("Stage", "asks for schema"),
        ("Capability", "advertised?"),
        ("Circuit", "already open?"),
        ("Budget", "room left?"),
        ("Local lock", "Ollama only"),
        ("Provider", "HTTP call"),
        ("Validate", "Pydantic"),
        ("Usage row", "tokens, cost"),
    ]
    for i, (x, (title, sub)) in enumerate(zip(xs, labels, strict=True)):
        fill, stroke = (GREEN_F, GREEN_S) if i >= 5 else (BLUE_F, BLUE_S)
        if i == 7:
            fill, stroke = GRAY_F, GRAY_S
        node(s, x, 24, 96, 70, title, [sub], fill, stroke)
        if i:
            s.arrow([(x - 14, 59), (x, 59)])

    stops = [(122, "missing"), (232, "open"), (342, "exhausted")]
    for x, word in stops:
        s.arrow([(x + 48, 94), (x + 48, 150)])
        node(s, x, 150, 96, 52, "stop", [word], RED_F, RED_S)
    s.text(440, 250, ["A stop is recorded and the provider is not called. Invalid JSON retries, then opens the circuit."], size=12, weight="500", fill=MUTED)
    s.save("call.svg")


def flow_front() -> None:
    s = Svg(760, 830)
    cx = 250
    box_w = 230
    mid = cx + box_w / 2

    def step(y: float, title: str, sub: list[str], fill: str, stroke: str) -> float:
        node(s, cx, y, box_w, 52, title, sub, fill, stroke)
        return y + 52

    y = 16
    s.rect(cx + 55, y, 120, 32, INK, INK, rx=16)
    s.text(mid, y + 17, ["START"], size=12, weight="700", fill=WHITE)
    s.arrow([(mid, y + 32), (mid, 62)])
    y = 62
    y = step(y, "triage", ["TriageResult, no files"], BLUE_F, BLUE_S) + 28
    s.arrow([(mid, y - 28), (mid, y)])
    diamond(s, mid, y + 32, 210, 64, "Repository?")
    s.arrow([(mid + 105, y + 32), (620, y + 32)])
    node(s, 560, y + 8, 170, 48, "finalize", ["no checkout"], GRAY_F, GRAY_S)
    s.label(500, y + 20, "no")
    s.arrow([(mid, y + 64), (mid, y + 92)])
    s.label(mid + 18, y + 80, "yes")
    y = y + 92
    y = step(y, "investigate", ["read-only tools"], GREEN_F, GREEN_S) + 28
    s.arrow([(mid, y - 28), (mid, y)])
    diamond(s, mid, y + 32, 220, 64, "Python line cited?")
    # loop back on the left
    s.arrow([(mid - 110, y + 32), (150, y + 32), (150, y - 70), (cx, y - 46)])
    s.label(168, y - 8, "once more")
    s.arrow([(mid, y + 64), (mid, y + 92)])
    s.label(mid + 16, y + 80, "yes")
    y = y + 92
    y = step(y, "plan", ["ChangePlan"], BLUE_F, BLUE_S) + 28
    s.arrow([(mid, y - 28), (mid, y)])
    diamond(s, mid, y + 32, 220, 64, "Code change?")
    s.arrow([(mid + 110, y + 32), (620, y + 32)])
    node(s, 560, y + 8, 170, 48, "finalize", ["decline"], GRAY_F, GRAY_S)
    s.label(500, y + 20, "no")
    s.arrow([(mid - 110, y + 16), (150, y + 16), (150, y - 70), (cx, y - 46)])
    s.label(168, y - 20, "replan once")
    s.arrow([(mid, y + 64), (mid, y + 92)])
    s.label(mid + 16, y + 80, "yes")
    y = y + 92
    y = step(y, "plan approval", ["interrupt, no write yet"], AMBER_F, AMBER_S) + 28
    s.arrow([(mid, y - 28), (mid, y)])
    diamond(s, mid, y + 32, 200, 64, "Approved?")
    s.arrow([(mid + 100, y + 32), (620, y + 32)])
    node(s, 560, y + 8, 170, 48, "rejected", ["terminal"], RED_F, RED_S)
    s.label(500, y + 20, "no")
    s.arrow([(mid, y + 64), (mid, y + 100)])
    s.label(mid + 16, y + 84, "yes")
    s.rect(cx + 15, y + 100, 200, 36, GREEN_F, GREEN_S, rx=8)
    s.text(mid, y + 119, ["continues in figure: after approval"], size=11, weight="600", fill=GREEN_S)
    s.save("flow-front.svg")


def flow_back() -> None:
    s = Svg(760, 900)
    cx = 250
    box_w = 240
    mid = cx + box_w / 2

    def step(y: float, title: str, sub: list[str], fill: str, stroke: str) -> float:
        node(s, cx, y, box_w, 52, title, sub, fill, stroke)
        return y + 52

    y = 16
    s.rect(cx + 20, y, 200, 32, GREEN_F, GREEN_S, rx=16)
    s.text(mid, y + 17, ["after approval"], size=12, weight="700", fill=GREEN_S)
    s.arrow([(mid, 48), (mid, 70)])
    y = 70
    y = step(y, "prepare workspace", ["bare clone, task branch"], GREEN_F, GREEN_S) + 18
    s.arrow([(mid, y - 18), (mid, y)])
    y = step(y, "baseline test", ["pytest, tree still unmodified"], AMBER_F, AMBER_S) + 18
    s.arrow([(mid, y - 18), (mid, y)])
    develop_y = y
    y = step(y, "develop", ["up to three exact replacements"], GREEN_F, GREEN_S) + 18
    s.arrow([(mid, y - 18), (mid, y)])
    y = step(y, "patched test", ["new job id per repair"], AMBER_F, AMBER_S) + 18
    s.arrow([(mid, y - 18), (mid, y)])
    y = step(y, "review", ["recommendation only"], GREEN_F, GREEN_S) + 26
    s.arrow([(mid, y - 26), (mid, y)])
    diamond(s, mid, y + 34, 200, 68, "Policy")
    # repair loop on the left, back to develop
    s.arrow([(mid - 100, y + 34), (150, y + 34), (150, develop_y + 26), (cx, develop_y + 26)])
    s.label(168, (y + 34 + develop_y) / 2, "repair, at most 2")
    s.arrow([(mid + 100, y + 34), (600, y + 34)])
    node(s, 560, y + 10, 176, 48, "finalize", ["failed, person needed"], RED_F, RED_S)
    s.label(530, y + 22, "fail")
    s.arrow([(mid, y + 68), (mid, y + 100)])
    s.label(mid + 22, y + 86, "accept")
    y = y + 100
    diamond(s, mid, y + 32, 210, 64, "Publication on?")
    s.arrow([(mid + 105, y + 32), (600, y + 32)])
    node(s, 560, y + 8, 176, 48, "finalize", ["local only"], GRAY_F, GRAY_S)
    s.label(530, y + 20, "off")
    s.arrow([(mid, y + 64), (mid, y + 96)])
    s.label(mid + 18, y + 82, "on")
    y = y + 96
    y = step(y, "publish approval", ["interrupt, nothing pushed"], AMBER_F, AMBER_S) + 18
    s.arrow([(mid, y - 18), (mid, y)])
    y = step(y, "publish", ["task branch, draft PR"], AMBER_F, AMBER_S) + 18
    s.arrow([(mid, y - 18), (mid, y)])
    y = step(y, "finalize", ["business row for the API"], GRAY_F, GRAY_S) + 16
    s.arrow([(mid, y - 16), (mid, y)])
    s.rect(mid - 40, y, 80, 30, INK, INK, rx=15)
    s.text(mid, y + 16, ["END"], size=12, weight="700", fill=WHITE)
    s.save("flow-back.svg")


def main() -> None:
    system()
    submodels()
    pipeline()
    call_flow()
    flow_front()
    flow_back()


if __name__ == "__main__":
    main()

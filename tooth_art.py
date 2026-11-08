"""P29 NEW-07 / E-008: how the dental chart draws a tooth - from the front (crown, roots, the jaw bone behind them and
the gum over the neck) and from above (the chewing surface with its five surfaces, on the gum ridge). The shape
follows the tooth: incisor, canine, premolar or molar, upper or lower, adult or baby, at its real width, so a half
arch is as long as it is in the mouth. Heights are drawn at 78% so both arches fit their box.

Every tooth is drawn as if it sat in the upper right - mesial side on the right, cheek side on top, crown at the
bottom of the front view - and then mirrored into place: across for the patient's left, upside down for the lower
jaw. A surface keeps its name wherever the tooth sits and ends up where dental_chart.layout says it is.
"""
import re
import sys

from markupsafe import Markup

import dental_chart

VS = 0.78          # crowns and roots at 78% of their height
OVERLAP = 0.12     # the jaw and the gum reach this far into the next tooth's box, so they run on unbroken
DEPTH = 0.82       # cheek-to-tongue depth seen from above, likewise
NECK_BOW = 0.8     # how far the neck line bows toward the root in the middle of the tooth

# hf/ho: height of the front view and of the view from above (mm); px: screen pixels per mm; papilla: how far above
# the biting edge the gum between two teeth reaches; gum: the height of the gum band; end: the last tooth of a half arch
ADULT = {"hf": 23.0, "ho": 10.0, "px": 5.0, "papilla": 4.2, "gum": 6.5, "end": 8}
BABY = {"hf": 18.0, "ho": 8.5, "px": 6.5, "papilla": 3.0, "gum": 4.5, "end": 5}

# FDI position -> crown width, crown height, root length (mm), neck share of the width, the biting edge seen from the
# front, the roots, cheek-to-tongue depth (mm), the shape from above. Widths are average crown widths
UPPER = {
    1: (8.5, 10.5, 13.0, 0.72, "incisal", "one", 7.0, "incisor"),
    2: (6.5, 9.0, 13.0, 0.70, "incisal", "one", 6.0, "incisor"),
    3: (7.5, 10.0, 17.0, 0.70, "canine", "one", 8.0, "canine"),
    4: (7.0, 8.5, 14.0, 0.72, "premolar", "split", 9.0, "premolar"),
    5: (6.5, 7.5, 14.0, 0.72, "premolar", "one", 9.0, "premolar"),
    6: (10.0, 7.5, 12.5, 0.80, "molar2", "three", 11.0, "upper_molar"),
    7: (9.0, 7.0, 11.5, 0.80, "molar2", "three", 11.0, "upper_molar"),
    8: (8.5, 6.5, 10.0, 0.80, "molar2", "fused", 10.0, "upper_molar"),
}
LOWER = {
    1: (5.0, 9.0, 12.5, 0.70, "incisal", "one", 6.0, "incisor"),
    2: (5.5, 9.5, 14.0, 0.70, "incisal", "one", 6.5, "incisor"),
    3: (7.0, 11.0, 16.0, 0.70, "canine", "one", 7.5, "canine"),
    4: (7.0, 8.5, 14.0, 0.72, "premolar", "one", 7.5, "premolar"),
    5: (7.0, 8.0, 14.5, 0.72, "premolar", "one", 8.0, "premolar"),
    6: (11.0, 7.5, 14.0, 0.82, "molar3", "two", 10.5, "lower_molar"),
    7: (10.5, 7.0, 13.0, 0.82, "molar2", "two", 10.0, "lower_molar"),
    8: (10.0, 7.0, 11.0, 0.82, "molar2", "fused2", 9.5, "lower_molar"),
}
BABY_UPPER = {
    1: (6.5, 6.0, 10.0, 0.72, "incisal", "one", 5.0, "incisor"),
    2: (5.1, 5.6, 11.4, 0.70, "incisal", "one", 4.8, "incisor"),
    3: (7.0, 6.5, 13.5, 0.70, "canine", "one", 7.0, "canine"),
    4: (7.3, 5.1, 10.0, 0.80, "molar2", "three", 8.5, "upper_molar"),
    5: (8.2, 5.7, 11.7, 0.80, "molar2", "three", 10.0, "upper_molar"),
}
BABY_LOWER = {
    1: (4.2, 5.0, 9.0, 0.70, "incisal", "one", 4.0, "incisor"),
    2: (4.1, 5.2, 10.0, 0.70, "incisal", "one", 4.2, "incisor"),
    3: (5.0, 6.0, 11.5, 0.70, "canine", "one", 5.5, "canine"),
    4: (7.7, 6.0, 9.8, 0.80, "molar2", "two", 7.0, "lower_molar"),
    5: (9.9, 5.5, 10.0, 0.80, "molar3", "two", 8.7, "lower_molar"),
}
# where the crown is widest, as a share of its height from the biting edge
CONTOUR = {"incisal": 0.22, "canine": 0.3, "premolar": 0.35, "molar2": 0.38, "molar3": 0.38}

# outlines seen from above, in (u, v): u from distal 0 to mesial 1, v from the cheek side 0 to the tongue side 1
TOP = {
    "incisor": [("M", (0.02, 0.32)), ("C", (0.02, 0.08), (0.2, 0.0), (0.5, 0.0)), ("C", (0.8, 0.0), (0.98, 0.08), (0.98, 0.32)),
                ("C", (0.98, 0.56), (0.66, 0.98), (0.5, 0.98)), ("C", (0.34, 0.98), (0.02, 0.56), (0.02, 0.32))],
    "canine": [("M", (0.02, 0.42)), ("C", (0.04, 0.16), (0.26, 0.0), (0.52, 0.0)), ("C", (0.78, 0.02), (0.98, 0.18), (0.98, 0.44)),
               ("C", (0.98, 0.72), (0.7, 1.0), (0.5, 1.0)), ("C", (0.3, 1.0), (0.02, 0.7), (0.02, 0.42))],
    "premolar": [("M", (0.5, 0.0)), ("C", (0.86, 0.0), (1.0, 0.18), (1.0, 0.5)), ("C", (1.0, 0.84), (0.84, 1.0), (0.5, 1.0)),
                 ("C", (0.16, 1.0), (0.0, 0.84), (0.0, 0.5)), ("C", (0.0, 0.18), (0.14, 0.0), (0.5, 0.0))],
}
# molars from above are rounded-off corners: the upper one a rhomboid, the lower one a five-sided block
CORNERS = {
    "upper_molar": [(0.12, 0.02), (1.0, 0.0), (0.88, 0.98), (0.0, 1.0)],
    "lower_molar": [(0.02, 0.08), (0.5, -0.02), (0.98, 0.02), (1.0, 0.94), (0.03, 1.0)],
}
# the chewing surface (the biting edge on a front tooth) inside the outline; a molar's is its outline at half size
TABLE = {
    "incisor": ([(0.1, 0.16), (0.9, 0.16), (0.9, 0.34), (0.1, 0.34)], 0.45),
    "canine": ([(0.5, 0.16), (0.74, 0.36), (0.5, 0.56), (0.26, 0.36)], 0.3),
    "premolar": ([(0.2, 0.24), (0.8, 0.24), (0.8, 0.76), (0.2, 0.76)], 0.5),
}
GROOVES = {
    "incisor": [[(0.2, 0.44), (0.36, 0.84)], [(0.8, 0.44), (0.64, 0.84)]],
    "canine": [[(0.5, 0.56), (0.5, 0.88)]],
    "premolar": [[(0.26, 0.5), (0.74, 0.5)]],
    "upper_molar": [[(0.56, 0.06), (0.52, 0.46)], [(0.52, 0.46), (0.84, 0.42)], [(0.52, 0.46), (0.36, 0.62), (0.3, 0.92)]],
    "lower_molar": [[(0.14, 0.5), (0.86, 0.5)], [(0.6, 0.04), (0.58, 0.5)], [(0.46, 0.96), (0.45, 0.5)]],
}
EXTRA_GROOVE = [(0.3, 0.07), (0.31, 0.5)]   # the lower first molar's distal cusp


def _n(v):
    text = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _p(x, y):
    return f"{_n(x)},{_n(y)}"


def spec(tooth):
    """the drawing's measures for one FDI tooth"""
    quadrant, place = divmod(tooth, 10)
    baby = quadrant >= 5
    upper = quadrant in (1, 2, 5, 6)
    table = (BABY_UPPER if upper else BABY_LOWER) if baby else (UPPER if upper else LOWER)
    w, ch, rl, neck, edge, roots, bl, top = table[place]
    size = BABY if baby else ADULT
    w = round(w * size["px"]) / size["px"]   # a whole number of pixels, so neighbours meet without a seam
    return dict(size, tooth=tooth, baby=baby, upper=upper, mirror=quadrant in (2, 3, 6, 7), last=place == size["end"],
                w=w, ch=ch * VS, rl=rl * VS, cw=w * neck, edge=edge, roots=roots, bl=bl * DEPTH, top=top)


def _place(s, height):
    """the mirror that puts a tooth drawn as an upper right one where it belongs"""
    sx, sy = (-1 if s["mirror"] else 1), (1 if s["upper"] else -1)
    if sx == 1 and sy == 1:
        return ""
    return f' transform="translate({_n(s["w"] if sx < 0 else 0)} {_n(height if sy < 0 else 0)}) scale({sx} {sy})"'


def _mark(found):
    kind, state = found
    kind = kind if kind in dental_chart.KINDS else "other"
    state = state if state in dental_chart.STATES else "done"
    return f"tc-k-{kind} tc-{state}"


def plan(entry):
    """what to draw for one tooth, from its marks (dental_chart.chart, newest last)"""
    def last(*kinds):
        hits = [(k, st) for k, st, _draw in entry["whole"] if k in kinds]
        return hits[-1] if hits else None
    gone = last("extraction", "missing")
    cap = last("crown", "bridge", "denture")
    implant = last("implant")
    removed = bool(gone and gone[1] == "done")
    loose_kinds = [k for k, (_code, draw) in dental_chart.KINDS.items() if draw == "fill"]
    return {"ghost": removed and not cap and not implant,
            "roots": not removed and not (implant and implant[1] == "done"),
            "cross": gone if gone and gone[0] == "extraction" and not (removed and cap) else None,
            "cap": cap, "implant": implant, "bridge": last("bridge"), "rct": last("rct"), "post": last("post"),
            "perio": last("perio"), "fracture": last("fracture"), "impacted": last("impacted"),
            "loose": last(*loose_kinds), "fills": {} if removed and not cap else entry["fills"]}


# --- the front view --------------------------------------------------------------------------------------------------

def _edge(s, tip, hc, cd, cm):
    """the biting edge, from the distal contact to the mesial one"""
    w, ch, edge = s["w"], s["ch"], s["edge"]
    d = tip - hc
    if edge == "incisal":
        return [f"C {_p(cd, hc + 0.6 * d)} {_p(0.06 * w, tip)} {_p(0.16 * w, tip)}", f"L {_p(0.88 * w, tip)}",
                f"C {_p(0.94 * w, tip)} {_p(cm, tip - 0.25 * d)} {_p(cm, hc)}"]
    if edge == "canine":
        return [f"C {_p(cd + 0.03 * w, hc + 0.45 * d)} {_p(0.38 * w, tip - 0.25 * d)} {_p(0.52 * w, tip)}",
                f"C {_p(0.66 * w, tip - 0.25 * d)} {_p(cm - 0.03 * w, hc + 0.45 * d)} {_p(cm, hc)}"]
    if edge == "premolar":
        return [f"C {_p(cd + 0.02 * w, hc + 0.55 * d)} {_p(0.36 * w, tip - 0.12 * d)} {_p(0.5 * w, tip)}",
                f"C {_p(0.64 * w, tip - 0.12 * d)} {_p(cm - 0.02 * w, hc + 0.55 * d)} {_p(cm, hc)}"]
    groove = tip - 0.13 * ch
    if edge == "molar2":
        tips = [(0.27 * w, tip), (0.73 * w, tip)]
    else:   # the lower first molar: three cusps on the cheek side, the distal one the smallest
        tips = [(0.16 * w, tip - 0.05 * ch), (0.46 * w, tip), (0.77 * w, tip)]
    out = [f"C {_p(cd, hc + 0.6 * d)} {_p(tips[0][0] - 0.1 * w, tips[0][1])} {_p(*tips[0])}"]
    for (x0, y0), (x1, y1) in zip(tips, tips[1:]):
        mid = (x0 + x1) / 2
        out += [f"C {_p(x0 + 0.4 * (mid - x0), y0)} {_p(mid - 0.25 * (mid - x0), groove)} {_p(mid, groove)}",
                f"C {_p(mid + 0.25 * (x1 - mid), groove)} {_p(x1 - 0.4 * (x1 - mid), y1)} {_p(x1, y1)}"]
    out.append(f"C {_p(tips[-1][0] + 0.12 * w, tips[-1][1])} {_p(cm, hc + 0.6 * d)} {_p(cm, hc)}")
    return out


def _crown(s, tip, cej):
    w, ch, cw = s["w"], s["ch"], s["cw"]
    xd, xm = (w - cw) / 2, (w + cw) / 2
    hc = tip - ch * CONTOUR[s["edge"]]
    cd, cm = 0.03 * w, 0.97 * w
    up = hc - cej
    out = [f"M {_p(xd, cej)}", f"C {_p(xd - 0.25 * (xd - cd), cej + 0.35 * up)} {_p(cd, hc - 0.35 * up)} {_p(cd, hc)}"]
    out += _edge(s, tip, hc, cd, cm)
    out += [f"C {_p(cm, hc - 0.35 * up)} {_p(xm + 0.25 * (cm - xm), cej + 0.35 * up)} {_p(xm, cej)}",
            f"Q {_p(w / 2, cej - NECK_BOW)} {_p(xd, cej)}", "Z"]
    return " ".join(out)


def _roots(s, cej, apex):
    """(the roots' outline, a root behind them or None, each canal's apex)"""
    w, cw, rl = s["w"], s["cw"], s["rl"]
    xd, xm = (w - cw) / 2, (w + cw) / 2
    neck = f"Q {_p(w / 2, cej - NECK_BOW)} {_p(xd, cej)} Z"
    if s["roots"] in ("one", "fused"):
        xa = w / 2 - 0.05 * w                                   # the apex leans distally
        half = 0.08 * cw if s["roots"] == "one" else 0.2 * cw   # a fused root ends wide
        path = (f"M {_p(xd, cej)} C {_p(xd + 0.02 * w, cej - 0.45 * rl)} {_p(xa - half - 0.14 * cw, apex + 0.25 * rl)}"
                f" {_p(xa - half, apex + 0.06 * rl)} Q {_p(xa, apex - 0.03 * rl)} {_p(xa + half, apex + 0.06 * rl)}"
                f" C {_p(xa + half + 0.14 * cw, apex + 0.25 * rl)} {_p(xm - 0.02 * w, cej - 0.45 * rl)} {_p(xm, cej)} {neck}")
        return path, None, [(xa, apex + 0.08 * rl)]
    # two roots side by side: where they part (share of the length), the distal apex, the mesial apex
    furcation, ad, am = {"two": (0.25, xd + 0.18 * cw, xm - 0.3 * cw), "three": (0.3, xd + 0.12 * cw, xm - 0.18 * cw),
                         "split": (0.55, w / 2 - 0.17 * cw, w / 2 + 0.17 * cw),
                         "fused2": (0.68, w / 2 - 0.16 * cw, w / 2 + 0.12 * cw)}[s["roots"]]
    yf = cej - furcation * rl
    h = 0.07 * cw
    path = (f"M {_p(xd, cej)} C {_p(xd - 0.02 * w, cej - 0.4 * rl)} {_p(ad - h - 0.12 * cw, apex + 0.3 * rl)}"
            f" {_p(ad - h, apex + 0.07 * rl)} Q {_p(ad, apex - 0.02 * rl)} {_p(ad + h, apex + 0.07 * rl)}"
            f" C {_p(ad + h + 0.08 * cw, apex + 0.3 * rl)} {_p(w / 2 - 0.1 * cw, yf - 0.06 * rl)} {_p(w / 2, yf)}"
            f" C {_p(w / 2 + 0.1 * cw, yf - 0.06 * rl)} {_p(am - h - 0.08 * cw, apex + 0.3 * rl)} {_p(am - h, apex + 0.07 * rl)}"
            f" Q {_p(am, apex - 0.02 * rl)} {_p(am + h, apex + 0.07 * rl)}"
            f" C {_p(am + h + 0.12 * cw, apex + 0.3 * rl)} {_p(xm + 0.02 * w, cej - 0.4 * rl)} {_p(xm, cej)} {neck}")
    canals = [(ad, apex + 0.1 * rl), (am, apex + 0.1 * rl)]
    behind = None
    if s["roots"] == "three":   # the palatal root, between and behind the two cheek-side ones
        pa, b = w / 2, 0.22 * cw
        behind = (f"M {_p(pa - b, yf + 0.5)} C {_p(pa - b, apex + 0.5 * rl)} {_p(pa - 0.1 * cw, apex + 0.1 * rl)}"
                  f" {_p(pa - 0.06 * cw, apex + 0.03 * rl)} Q {_p(pa, apex - 0.05 * rl)} {_p(pa + 0.06 * cw, apex + 0.03 * rl)}"
                  f" C {_p(pa + 0.1 * cw, apex + 0.1 * rl)} {_p(pa + b, apex + 0.5 * rl)} {_p(pa + b, yf + 0.5)} Z")
        canals.append((pa, apex + 0.08 * rl))
    return path, behind, canals


def _implant(s, cej, found):
    w, cw, rl = s["w"], s["cw"], s["rl"]
    top, bottom, c = cej - 0.3, cej - 0.3 - 0.8 * rl, w / 2
    a = min(0.27 * cw, 2.1)
    b = 0.7 * a
    body = f"M {_p(c - a, top)} L {_p(c + a, top)} L {_p(c + b, bottom + 0.8)} Q {_p(c, bottom - 0.4)} {_p(c - b, bottom + 0.8)} Z"
    threads, y = [], top - 1.0
    while y > bottom + 1.2:
        half = a + (b - a) * (top - y) / (top - bottom)
        threads.append(f"M {_p(c - half, y)} H {_n(c + half)}")
        y -= 1.0
    return f'<path class="tc-implant {_mark(found)}" d="{body}"/><path class="tc-thread" d="{" ".join(threads)}"/>'


def front(entry):
    """the tooth from the front: jaw bone, roots, crown with its surfaces, gum, and the work done on it"""
    t = entry["tooth"]
    s = spec(t)
    p = plan(entry)
    w, hf, ch, rl = s["w"], s["hf"], s["ch"], s["rl"]
    tip = hf - 0.9
    cej = tip - ch
    apex = cej - rl
    roots, behind, canals = _roots(s, cej, apex)
    # the gum: highest over the middle of the neck, down to a point between the teeth; the bone behind it
    pap = tip - s["papilla"]
    zen = min(cej + 0.7, pap - 0.4)
    lift = 0.9 * (pap - zen)
    o = OVERLAP
    margin = f"M {_p(0, pap)} C {_p(0.1 * w, pap - lift)} {_p(0.3 * w, zen)} {_p(w / 2, zen)} S {_p(0.9 * w, pap - lift)} {_p(w, pap)}"
    back = (f"L {_p(w, pap)} C {_p(0.9 * w, pap - lift)} {_p(0.7 * w, zen)} {_p(w / 2, zen)}"
            f" S {_p(0.1 * w, pap - lift)} {_p(0, pap)} L {_p(-o, pap)}")
    corner = f"M {_p(-o, 3)} Q {_p(-o, 0)} {_p(3, 0)}" if s["last"] else f"M {_p(-o, 0)}"
    bone = f"{corner} H {_n(w + o)} V {_n(pap)} {back} Z"
    gum = f"M {_p(-o, pap - s['gum'])} H {_n(w + o)} V {_n(pap)} {back} Z"
    edge = tip - 0.26 * ch
    zones = [("O", f"M {_p(-1, edge)} H {_n(w + 1)} V {_n(tip + 1)} H -1 Z"),
             ("D", f"M {_p(-1, cej - 2)} H {_n(0.25 * w)} V {_n(edge)} H -1 Z"),
             ("B", f"M {_p(0.25 * w, cej - 2)} H {_n(0.75 * w)} V {_n(edge)} H {_n(0.25 * w)} Z"),
             ("M", f"M {_p(0.75 * w, cej - 2)} H {_n(w + 1)} V {_n(edge)} H {_n(0.75 * w)} Z")]

    below = []
    if p["roots"]:
        if behind:
            below.append(f'<path class="tc-root tc-root-back" d="{behind}"/>')
        below.append(f'<path class="tc-root" d="{roots}"/>')
    if p["implant"]:
        below.append(_implant(s, cej, p["implant"]))
    body = [f'<use href="#tc{t}c" class="tc-enamel"/>']
    if not p["ghost"]:
        cells = []
        for letter, d in zones:
            found = p["fills"].get(letter)
            cells.append(f'<path class="tc-z{" " + _mark(found) if found else ""}" data-surface="{letter}"'
                         f' data-view="front" d="{d}"/>')
        body.append(f'<g clip-path="url(#tc{t}k)">{"".join(cells)}</g>')
    if p["cap"]:
        body.append(f'<use href="#tc{t}c" class="tc-cap {_mark(p["cap"])}"/>')
    loose = f' tc-hl {_mark(p["loose"])}' if p["loose"] else ""
    body.append(f'<use href="#tc{t}c" class="tc-outline{loose}"/>')
    sunk = ' transform="translate(0 -2.5)"' if p["impacted"] else ""   # an impacted tooth sits in the bone

    ghost = " tc-ghost" if p["ghost"] else ""
    out = [f'<svg class="tc-front" viewBox="0 0 {_n(w)} {_n(hf)}" width="{_n(w * s["px"])}" height="{_n(hf * s["px"])}"'
           f' aria-hidden="true" focusable="false">',
           f'<defs><path id="tc{t}c" d="{_crown(s, tip, cej)}" vector-effect="non-scaling-stroke"/>'
           f'<clipPath id="tc{t}k"><use href="#tc{t}c"/></clipPath></defs>',
           f'<g{_place(s, hf)}>', f'<path class="tc-bone" d="{bone}"/>',
           f'<g class="tc-body{ghost}"{sunk}>{"".join(below)}</g>',
           f'<path class="tc-gum" d="{gum}" fill="url(#tc-gum)"/>',
           f'<g class="tc-body{ghost}"{sunk}>{"".join(body)}</g>']
    if p["impacted"]:
        out.append(f'<use href="#tc{t}c" class="tc-imp {_mark(p["impacted"])}"{sunk}/>')
    # what is in the root goes over the gum, so it can be seen
    chamber = (w / 2, cej + 0.3 * ch)
    if p["rct"] and p["roots"]:
        lines = " ".join(f"M {_p(*chamber)} L {_p(ax, ay)}" for ax, ay in canals)
        out.append(f'<path class="tc-canal {_mark(p["rct"])}" d="{lines}"/>')
    if p["post"] and p["roots"]:
        ax, ay = canals[0]
        end = (chamber[0] + 0.45 * (ax - chamber[0]), chamber[1] + 0.5 * (ay - chamber[1]))
        out.append(f'<path class="tc-post {_mark(p["post"])}" d="M {_p(*chamber)} L {_p(*end)}"/>')
    if p["perio"]:
        out.append(f'<path class="tc-perio {_mark(p["perio"])}" d="{margin}"/>')
    if p["fracture"]:
        out.append(f'<path class="tc-crack {_mark(p["fracture"])}" d="M {_p(0.22 * w, cej + 0.15 * ch)}'
                   f' L {_p(0.44 * w, cej + 0.45 * ch)} L {_p(0.34 * w, cej + 0.6 * ch)} L {_p(0.66 * w, tip - 0.1 * ch)}"/>')
    if p["bridge"]:
        out.append(f'<path class="tc-bar {_mark(p["bridge"])}" d="M {_p(0, tip - 0.5 * ch)} H {_n(w)}"/>')
    if p["cross"]:
        out.append(f'<path class="tc-cross {_mark(p["cross"])}" d="M {_p(0.1 * w, cej - 0.55 * rl)} L {_p(0.9 * w, tip)}'
                   f' M {_p(0.9 * w, cej - 0.55 * rl)} L {_p(0.1 * w, tip)}"/>')
    out.append("</g></svg>")
    return Markup("".join(out))


# --- the view from above ---------------------------------------------------------------------------------------------

def _xy(s, uv):
    md, bl = 0.94 * s["w"], min(s["bl"], s["ho"] - 0.8)
    return s["w"] / 2 + (uv[0] - 0.5) * md, s["ho"] / 2 + (uv[1] - 0.5) * bl


def _rounded(points, t):
    """a closed outline through the points with each corner rounded off (t: how much of each side the round takes)"""
    out = []
    for i, (x, y) in enumerate(points):
        px_, py_ = points[i - 1]
        nx, ny = points[(i + 1) % len(points)]
        out.append(f"{'L' if out else 'M'} {_p(x + t * (px_ - x), y + t * (py_ - y))}")
        out.append(f"Q {_p(x, y)} {_p(x + t * (nx - x), y + t * (ny - y))}")
    return " ".join(out) + " Z"


def top(entry):
    """the tooth from above: its outline on the gum ridge, the five surfaces, its grooves, and the work done on it"""
    t = entry["tooth"]
    s = spec(t)
    p = plan(entry)
    w, ho, shape = s["w"], s["ho"], s["top"]
    if shape in TOP:
        outline = " ".join(c + " " + " ".join(_p(*_xy(s, uv)) for uv in pts) for c, *pts in TOP[shape]) + " Z"
        corners, r = TABLE[shape]
    else:
        outline = _rounded([_xy(s, c) for c in CORNERS[shape]], 0.3)
        corners, r = [(0.5 + (u - 0.5) * 0.5, 0.5 + (v - 0.5) * 0.5) for u, v in CORNERS[shape]], 0.3
    table_pts = [_xy(s, c) for c in corners]
    x0, x1 = min(x for x, _y in table_pts), max(x for x, _y in table_pts)
    y0, y1 = min(y for _x, y in table_pts), max(y for _x, y in table_pts)
    ring = [("B", [(0, 0), (w, 0), (x1, y0), (x0, y0)]), ("M", [(w, 0), (w, ho), (x1, y1), (x1, y0)]),
            ("L", [(0, ho), (w, ho), (x1, y1), (x0, y1)]), ("D", [(0, 0), (x0, y0), (x0, y1), (0, ho)])]
    grooves = GROOVES[shape] + ([EXTRA_GROOVE] if s["edge"] == "molar3" else [])
    groove = " ".join("M " + " L ".join(_p(*_xy(s, uv)) for uv in line) for line in grooves)
    band = (0.6, ho - 0.6)
    re_ = (band[1] - band[0]) / 2
    o = OVERLAP
    ridge = (f"M {_p(re_, band[0])} H {_n(w + o)} V {_n(band[1])} H {_n(re_)} Q {_p(0, band[1])} {_p(0, ho / 2)}"
             f" Q {_p(0, band[0])} {_p(re_, band[0])} Z") if s["last"] else \
        f"M {_p(-o, band[0])} H {_n(w + o)} V {_n(band[1])} H {_n(-o)} Z"

    body = [f'<use href="#tc{t}o" class="tc-enamel"/>']
    if not p["ghost"]:
        cells = []
        for letter, pts in ring + [("O", None)]:
            found = p["fills"].get(letter)
            d = _rounded(table_pts, r) if pts is None else "M " + " L ".join(_p(x, y) for x, y in pts) + " Z"
            cells.append(f'<path class="tc-z{" " + _mark(found) if found else ""}" data-surface="{letter}"'
                         f' data-view="top" d="{d}"/>')
        body.append(f'<g clip-path="url(#tc{t}q)">{"".join(cells)}</g>')
        body.append(f'<path class="tc-groove" d="{groove}"/>')
    if p["cap"]:
        body.append(f'<use href="#tc{t}o" class="tc-cap {_mark(p["cap"])}"/>')
    elif p["implant"] and not p["roots"]:
        body.append(f'<circle class="tc-implant {_mark(p["implant"])}" cx="{_n(w / 2)}" cy="{_n(ho / 2)}"'
                    f' r="{_n(0.22 * min(w, ho))}"/>')
    loose = f' tc-hl {_mark(p["loose"])}' if p["loose"] else ""
    body.append(f'<use href="#tc{t}o" class="tc-outline{loose}"/>')
    out = [f'<svg class="tc-top" viewBox="0 0 {_n(w)} {_n(ho)}" width="{_n(w * s["px"])}" height="{_n(ho * s["px"])}"'
           f' aria-hidden="true" focusable="false">',
           f'<defs><path id="tc{t}o" d="{outline}" vector-effect="non-scaling-stroke"/><clipPath id="tc{t}q"><use href="#tc{t}o"/></clipPath></defs>',
           f'<g{_place(s, ho)}>', f'<path class="tc-ridge" d="{ridge}"/>',
           f'<g class="tc-body{" tc-ghost" if p["ghost"] else ""}">{"".join(body)}</g>']
    if p["cross"]:
        out.append(f'<path class="tc-cross {_mark(p["cross"])}" d="M {_p(0.15 * w, 0.8)} L {_p(0.85 * w, ho - 0.8)}'
                   f' M {_p(0.85 * w, 0.8)} L {_p(0.15 * w, ho - 0.8)}"/>')
    out.append("</g></svg>")
    return Markup("".join(out))


def views(entry):
    """both views of a tooth for the chart, which side of the mouth it is on, and how many work codes fit under it"""
    s = spec(entry["tooth"])
    return {"upper": s["upper"], "front": front(entry), "top": top(entry), "room": max(1, int(s["w"] * s["px"] // 13))}


def selftest():
    def entry(tooth, fills=None, whole=()):
        return {"tooth": tooth, "fills": fills or {}, "whole": list(whole), "marks": []}

    # 1. every tooth draws both views, with its five surfaces from above and four from the front, ids its own
    ids = set()
    for n in dental_chart.TEETH:
        f, t = str(front(entry(n))), str(top(entry(n)))
        for letter in "MODBL":
            assert f'data-surface="{letter}" data-view="top"' in t, f"1: {n} has no {letter} from above"
        assert len(re.findall(r'data-view="front"', f)) == 4 and 'data-surface="L" data-view="front"' not in f, \
            f"1: {n} front view surfaces"
        assert "nan" not in f + t and "inf" not in f + t, f"1: {n} drew a number that is not one"
        assert re.fullmatch(r"[MLHVCSQZ0-9., \-]+", re.search(r'id="tc\d+c" d="([^"]+)"', f).group(1)), f"1: {n} crown path"
        mine = set(re.findall(r'id="([^"]+)"', f + t))
        assert not mine & ids, f"1: {n} reuses an id"
        ids |= mine
        assert 'class="tc-bone"' in f and 'class="tc-gum"' in f and 'class="tc-ridge"' in t, f"1: {n} has no jaw or gum"

    # 2. a half arch is about as long as in the mouth (63.5 mm upper, 63 lower); widths follow the tooth
    for side, want in ((dental_chart.UPPER[:8], 63.5), (dental_chart.LOWER[:8], 63.0)):
        got = sum(spec(n)["w"] for n in side)
        assert abs(got - want) < 0.5, f"2: half arch {got} mm, want {want}"   # widths are whole pixels
    assert spec(36)["w"] > spec(35)["w"] > spec(31)["w"] and spec(11)["w"] > spec(12)["w"], "2: widths do not follow the teeth"
    assert spec(85)["px"] > spec(46)["px"], "2: baby teeth are not drawn larger to stay easy to click"

    # 3. mirrored into place: none in the upper right, across on the left, upside down in the lower jaw
    assert _place(spec(16), 23) == "" and "scale(-1 1)" in _place(spec(26), 23)
    assert "scale(-1 -1)" in _place(spec(36), 23) and "scale(1 -1)" in _place(spec(46), 23), "3: mirrors"
    assert "scale(-1 1)" in _place(spec(61), 18) and "scale(1 -1)" in _place(spec(84), 18), "3: baby mirrors"

    # 4. the roots each tooth type has, and a canal drawn in each for a root canal treatment
    def canals(n):
        d = re.search(r'class="tc-canal[^"]*" d="([^"]+)"', str(front(entry(n, whole=[("rct", "done", "root")])))).group(1)
        return d.count("M ")
    assert (canals(11), canals(14), canals(16), canals(18), canals(36), canals(38)) == (1, 2, 3, 1, 2, 2), \
        f"4: canals {[canals(n) for n in (11, 14, 16, 18, 36, 38)]}"
    assert "tc-root-back" in str(front(entry(16))) and "tc-root-back" not in str(front(entry(46))), \
        "4: an upper molar's palatal root"

    # 5. surfaces: filled in both views where seen; the tongue side only from above
    f = str(front(entry(46, {"M": ("filling", "done"), "L": ("caries", "planned")})))
    t = str(top(entry(46, {"M": ("filling", "done"), "L": ("caries", "planned")})))
    assert 'class="tc-z tc-k-filling tc-done" data-surface="M" data-view="front"' in f, "5: mesial from the front"
    assert 'class="tc-z tc-k-filling tc-done" data-surface="M" data-view="top"' in t, "5: mesial from above"
    assert 'class="tc-z tc-k-caries tc-planned" data-surface="L" data-view="top"' in t and "tc-k-caries" not in f, \
        "5: the tongue side is seen from above only"

    # 6. whole-tooth work: what goes, what replaces it, what is planned
    gone = plan(entry(36, {"O": ("filling", "done")}, [("extraction", "done", "cross")]))
    assert gone["ghost"] and not gone["roots"] and gone["cross"] and gone["fills"] == {}, "6: an extracted tooth"
    missing = plan(entry(36, whole=[("missing", "done", "cross")]))
    assert missing["ghost"] and not missing["cross"], "6: a missing tooth is shown gone, without a cross"
    pontic = plan(entry(36, whole=[("extraction", "done", "cross"), ("bridge", "done", "ring")]))
    assert not pontic["ghost"] and not pontic["roots"] and pontic["cap"] and not pontic["cross"], "6: a bridge pontic"
    implant = plan(entry(36, whole=[("missing", "done", "cross"), ("implant", "done", "implant")]))
    assert not implant["ghost"] and not implant["roots"] and implant["implant"], "6: an implant replaces the root"
    soon = plan(entry(48, whole=[("extraction", "planned", "cross")]))
    assert not soon["ghost"] and soon["roots"] and soon["cross"] == ("extraction", "planned"), "6: an extraction planned"
    f = str(front(entry(11, whole=[("crown", "planned", "ring")])))
    assert 'class="tc-cap tc-k-crown tc-planned"' in f, "6: a planned crown is drawn as planned"
    f = str(front(entry(21, whole=[("bogus", "nope", "ring"), ("caries", "done", "fill")])))
    assert "tc-k-bogus" not in f and 'tc-outline tc-hl tc-k-caries tc-done' in f, "6: unknown work, a caries with no surface"
    assert 'transform="translate(0 -2.5)"' in str(front(entry(38, whole=[("impacted", "done", "dashed")]))), \
        "6: an impacted tooth sits in the bone"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("usage: python tooth_art.py --selftest")

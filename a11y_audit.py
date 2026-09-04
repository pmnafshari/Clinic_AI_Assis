"""Accessibility audit across the three apps (UX-17).

Measures what a screenshot cannot: text contrast against the background
actually painted behind it, whether every interactive control shows a focus
indicator, and whether prefers-reduced-motion is honoured.

Needs the three apps running:
    .venv/bin/python run.py                                  # staff  5000
    PATIENT_COOKIE_SECURE=0 .venv/bin/python patient_run.py   # patient 5001
    .venv/bin/python site_run.py                             # site    5002

    .venv/bin/python a11y_audit.py [--width 390]

Exits 1 if any check fails. Deliberately NOT in run_selftests.sh - it needs
three servers and a browser, like shot_pages.py and the two walks.
"""

import sys

from playwright.sync_api import sync_playwright

# longer than --ds-duration-fast (120ms), so a transitioned focus ring has
# arrived at its final value before it is measured
SETTLE_MS = 220

STAFF, PATIENT, SITE = "http://127.0.0.1:5000", "http://127.0.0.1:5001", "http://127.0.0.1:5002"

# unauthenticated pages only: this audit is about chrome, and seeding three
# roles here would duplicate shot_pages.py's fixture handling for no gain.
PAGES = [
    ("site", f"{SITE}/"), ("site", f"{SITE}/services"), ("site", f"{SITE}/doctors"),
    ("site", f"{SITE}/clinic"), ("site", f"{SITE}/contact"), ("site", f"{SITE}/assistant"),
    ("site", f"{SITE}/reference"),
    ("staff", f"{STAFF}/login"),
    ("patient", f"{PATIENT}/login"),
]

# WCAG 2.1 AA: 4.5:1 for normal text, 3:1 for large (>=24px, or >=18.66px bold)
CONTRAST = """
() => {
  const lum = (c) => {
    const [r, g, b] = c.map(v => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const parse = (s) => {
    const m = s.match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x));
    if (p.length > 3 && p[3] === 0) return null;
    return p.slice(0, 3);
  };
  const bgOf = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c) return c;
      n = n.parentElement;
    }
    return [255, 255, 255];
  };
  const out = [];
  document.querySelectorAll('body *').forEach(el => {
    const txt = [...el.childNodes]
      .filter(n => n.nodeType === 3 && n.textContent.trim())
      .map(n => n.textContent.trim()).join(' ');
    if (!txt) return;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    const fg = parse(cs.color);
    if (!fg) return;
    const bg = bgOf(el);
    const l1 = lum(fg), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(cs.fontSize);
    const bold = parseInt(cs.fontWeight, 10) >= 700;
    const large = size >= 24 || (bold && size >= 18.66);
    const need = large ? 3.0 : 4.5;
    if (ratio < need) {
      out.push({tag: el.tagName, cls: el.className.toString().slice(0, 34),
                text: txt.slice(0, 34), ratio: Math.round(ratio * 100) / 100,
                need, size: Math.round(size), color: cs.color});
    }
  });
  return out;
}
"""

# Focus is checked one element at a time, driven from Playwright rather than
# in a single JS block. Three separate measurement traps had to be closed
# before this reported the truth:
#
#   1. one synchronous pass leaves the first element focused while later ones
#      are measured;
#   2. `document.body.focus()` does not blur anything - body is not focusable
#      - so an autofocused field reported its focus ring as its own baseline;
#   3. .ds-input TRANSITIONS box-shadow over 120ms, so reading straight after
#      focus catches the transition's start value (`rgba(0,0,0,0) 0 0 0 0`)
#      and looks like no indicator at all.
#
# All three produced false failures. The settle wait below closes the third.
FOCUS_TARGETS = """
() => {
  const sel = 'a[href], button, input, select, textarea, summary, [tabindex]:not([tabindex="-1"])';
  const out = [];
  document.querySelectorAll(sel).forEach((el, i) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return;
    el.setAttribute('data-a11y-i', String(i));
    out.push({i: String(i), tag: el.tagName,
              cls: el.className.toString().slice(0, 34),
              text: (el.textContent || el.value || '').trim().slice(0, 28)});
  });
  return out;
}
"""

STYLE_OF = """
(i) => {
  const el = document.querySelector('[data-a11y-i="' + i + '"]');
  const c = getComputedStyle(el);
  return c.outlineWidth + '|' + c.outlineStyle + '|' + c.boxShadow + '|' + c.borderColor;
}
"""

MOTION = """
() => {
  const moving = [];
  document.querySelectorAll('body *').forEach(el => {
    const cs = getComputedStyle(el);
    const dur = parseFloat(cs.animationDuration) || 0;
    const tdur = parseFloat(cs.transitionDuration) || 0;
    if (dur > 0.05 || tdur > 0.05) {
      moving.push({tag: el.tagName, cls: el.className.toString().slice(0, 30),
                   anim: cs.animationDuration, trans: cs.transitionDuration});
    }
  });
  return moving.slice(0, 8);
}
"""


def main():
    width = 1440
    if "--width" in sys.argv:
        width = int(sys.argv[sys.argv.index("--width") + 1])

    failures = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        ctx = browser.new_context(viewport={"width": width, "height": 900})
        page = ctx.new_page()
        for app, url in PAGES:
            page.goto(url, wait_until="networkidle")
            label = f"{app} {url.split('127.0.0.1:')[1]}"

            low = page.evaluate(CONTRAST)
            for c in low:
                failures.append(f"contrast {label}: {c['ratio']}:1 (needs {c['need']}) "
                                f"{c['tag']}.{c['cls']} {c['color']} {c['size']}px — {c['text']!r}")

            targets = page.evaluate(FOCUS_TARGETS)
            nofocus = []
            for tgt in targets:
                page.evaluate("() => document.activeElement && document.activeElement.blur()")
                page.wait_for_timeout(SETTLE_MS)
                base = page.evaluate(STYLE_OF, tgt["i"])
                page.evaluate("(i) => document.querySelector('[data-a11y-i=\"'+i+'\"]').focus()", tgt["i"])
                page.wait_for_timeout(SETTLE_MS)
                now = page.evaluate(STYLE_OF, tgt["i"])
                if now == base:
                    nofocus.append(tgt)
            for f in nofocus:
                failures.append(f"focus    {label}: no visible indicator on "
                                f"{f['tag']}.{f['cls']} — {f['text']!r}")
            print(f"  {label:<28} contrast fails={len(low):<3} focus fails={len(nofocus)}")
        ctx.close()

        # reduced motion: nothing may keep a real duration when it is asked for
        ctx = browser.new_context(viewport={"width": width, "height": 900},
                                  reduced_motion="reduce")
        page = ctx.new_page()
        for app, url in PAGES:
            page.goto(url, wait_until="networkidle")
            moving = page.evaluate(MOTION)
            for m in moving:
                failures.append(f"motion   {app} {url.split('127.0.0.1:')[1]}: "
                                f"{m['tag']}.{m['cls']} anim={m['anim']} trans={m['trans']}")
        print(f"  reduced-motion               violations="
              f"{len([f for f in failures if f.startswith('motion')])}")
        ctx.close()
        browser.close()

    print()
    if failures:
        print(f"{len(failures)} accessibility failure(s) at {width}px:\n")
        for f in failures[:40]:
            print("  " + f)
        if len(failures) > 40:
            print(f"  ... and {len(failures) - 40} more")
        sys.exit(1)
    print(f"no accessibility failures at {width}px")


if __name__ == "__main__":
    main()

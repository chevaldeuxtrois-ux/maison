#!/usr/bin/env python3
"""Essais du dashboard dans un vrai navigateur : dispositions, zones tactiles, logos, lecture des radios.

  python3 tests/verifier.py --url https://…/maison/ --out captures [--audio] [--webkit]

Écrit un rapport (rapport.json, rapport.txt) et des captures d'écran dans le dossier --out.
"""
import argparse
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

SECTIONS = ["weather", "radio", "podcasts", "spotify", "lights", "news0", "news1", "news2", "clock"]
VIEWS = [
    # nom, largeur, hauteur, tactile, disposition attendue
    ("iphone-portrait", 390, 844, True, "stack"),
    ("iphone-paysage", 844, 390, True, "rail"),
    ("petit-iphone-portrait", 320, 568, True, "stack"),
    ("ipad-paysage", 1024, 748, True, "grid"),
    ("ipad-portrait", 768, 1004, True, "stack"),
    ("ordinateur", 1440, 900, False, "grid"),
]

MEASURE = """(id) => {
  const s = document.getElementById('s-' + id), b = s.querySelector('.sec-body');
  const r = s.getBoundingClientRect(), W = window.innerWidth, H = window.innerHeight;
  const small = [], hidden = [];
  s.querySelectorAll('button, a, input').forEach(el => {
    const q = el.getBoundingClientRect();
    if (!q.width || !q.height || !el.offsetParent) return;
    const label = (el.getAttribute('aria-label') || el.textContent || el.id || el.className).trim().slice(0, 30);
    if (q.height < 40 || q.width < 40) small.push(label + ' ' + Math.round(q.width) + 'x' + Math.round(q.height));
    // Visible à l'écran ? Alors le doigt doit bien tomber dessus.
    const cx = q.left + q.width / 2, cy = q.top + q.height / 2;
    // Un élément sorti de sa zone de défilement n'est pas à l'écran : on ne le compte pas.
    let clipped = false;
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const o = getComputedStyle(p).overflowY;
      if (o !== 'visible') { const pr = p.getBoundingClientRect(); if (cy < pr.top || cy > pr.bottom || cx < pr.left || cx > pr.right) clipped = true; }
    }
    if (!clipped && cx > 0 && cx < W && cy > 0 && cy < H) {
      const top = document.elementFromPoint(cx, cy);
      // Téléphone vertical : ce qui passe sous le lecteur fixe se dégage en faisant défiler la page.
      const underPlayer = document.body.className.indexOf('m-stack') >= 0 && top && document.getElementById('player').contains(top);
      if (top && !underPlayer && !(el === top || el.contains(top) || top.contains(el))) hidden.push(label + ' <- ' + (top.id || top.className));
    }
  });
  return { bodyH: b.offsetHeight, left: Math.round(r.left), right: Math.round(r.right), bottom: Math.round(r.bottom),
           W: W, H: H, docW: document.documentElement.scrollWidth, small: small, hidden: hidden,
           mode: document.body.className };
}"""


def tap(page, selector, touch):
    loc = page.locator(selector).first
    loc.scroll_into_view_if_needed()
    if touch:
        loc.tap()
    else:
        loc.click()


def run_view(browser, url, out, view, report, engine):
    name, w, h, touch, expected = view
    ctx = browser.new_context(viewport={"width": w, "height": h}, has_touch=touch, is_mobile=touch and engine != "firefox",
                              device_scale_factor=2)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector("#stations .tile", timeout=30000, state="attached")
    page.wait_for_timeout(1500)
    res = {"vue": name, "moteur": engine, "taille": "%dx%d" % (w, h), "problemes": []}
    mode = page.evaluate("document.body.className")
    if ("m-" + expected) not in mode:
        res["problemes"].append("disposition %s au lieu de %s" % (mode, expected))
    page.screenshot(path=os.path.join(out, "%s-%s-0-accueil.png" % (engine, name)))
    for sec in SECTIONS:
        if expected == "stack" and sec == "clock":
            continue
        try:
            # En vue d'ensemble l'horloge s'ouvre en touchant l'heure ; ailleurs par son titre.
            head = "#s-%s .sec-head" % sec
            if not page.locator(head).first.is_visible():
                head = "#clock"
            if "open" not in (page.get_attribute("#s-" + sec, "class") or "") and page.evaluate("document.querySelector('#main #s-%s') === null" % sec):
                tap(page, head, touch)
            page.wait_for_timeout(350)
            m = page.evaluate(MEASURE, sec)
            if m["bodyH"] < 40:
                res["problemes"].append("%s : contenu non déployé (%dpx)" % (sec, m["bodyH"]))
            if m["docW"] > m["W"] + 1:
                res["problemes"].append("%s : la page déborde en largeur (%d > %d)" % (sec, m["docW"], m["W"]))
            if m["left"] < -1 or m["right"] > m["W"] + 1:
                res["problemes"].append("%s : section hors écran (%d à %d)" % (sec, m["left"], m["right"]))
            if expected != "stack" and m["bottom"] > m["H"] + 1:
                res["problemes"].append("%s : section coupée en bas (%d > %d)" % (sec, m["bottom"], m["H"]))
            for x in m["small"]:
                res["problemes"].append("%s : zone tactile trop petite : %s" % (sec, x))
            for x in m["hidden"]:
                res["problemes"].append("%s : élément recouvert : %s" % (sec, x))
            if sec in ("radio", "podcasts", "spotify", "news0", "weather", "lights"):
                page.screenshot(path=os.path.join(out, "%s-%s-%s.png" % (engine, name, sec)))
        except Exception as ex:
            res["problemes"].append("%s : %s" % (sec, str(ex).splitlines()[0][:200]))
    # Les commandes principales répondent-elles au toucher ?
    try:
        tap(page, "#s-podcasts .sec-head" if page.locator("#main #s-podcasts").count() == 0 and "open" not in (page.get_attribute("#s-podcasts", "class") or "") else "#pl-play", touch)
        page.wait_for_timeout(300)
        if page.locator("#pods .show .more").count():
            tap(page, "#pods .show .more", touch)
            page.wait_for_timeout(300)
            if not page.locator("#back").is_visible():
                res["problemes"].append("podcasts : la liste des épisodes ne s'ouvre pas")
            else:
                tap(page, "#back", touch)
    except Exception as ex:
        res["problemes"].append("podcasts (toucher) : %s" % str(ex).splitlines()[0][:200])
    if errors:
        res["problemes"].append("erreurs de script : %s" % " | ".join(errors)[:400])
    report["vues"].append(res)
    ctx.close()


def run_audio(browser, url, out, report):
    """Lance chaque radio comme le ferait un doigt et attend que le son avance vraiment."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_selector("#stations .tile", timeout=30000, state="attached")
    page.wait_for_timeout(2500)
    page.click("#s-radio .sec-head")
    page.wait_for_timeout(1500)
    n = page.locator("#stations .tile").count()
    for i in range(n):
        tile = page.locator("#stations .tile").nth(i)
        name = tile.get_attribute("aria-label")
        logo = page.evaluate("""(i) => { const im = document.querySelectorAll('#stations .tile')[i].querySelector('img');
            return im ? (im.complete && im.naturalWidth > 0 ? im.naturalWidth + 'x' + im.naturalHeight + ' ' + im.src : 'ILLISIBLE ' + im.src) : 'pas de logo'; }""", i)
        tile.click()
        ok, state, t0 = False, "", time.time()
        while time.time() - t0 < 30:
            page.wait_for_timeout(500)
            cur = page.evaluate("document.getElementById('au').currentTime")
            state = page.inner_text("#pl-kind")
            if cur > 0.5:
                ok = True
                break
            if "indisponible" in state:
                break
        report["radios"].append({"station": name, "lecture": ok, "delai_s": round(time.time() - t0, 1), "etat": state, "logo": logo,
                                 "source": page.evaluate("(document.getElementById('au').currentSrc || '').slice(0, 110)")})
        page.locator("#stations .tile").nth(i).click() if ok else None
        page.wait_for_timeout(300)
    page.screenshot(path=os.path.join(out, "radios-logos.png"))
    # La lecture tient-elle quand l'écran tourne (paysage -> portrait -> paysage) ?
    try:
        page.locator("#stations .tile").nth(0).click()
        page.wait_for_timeout(6000)
        steps = []
        for size in ((768, 1004), (1024, 748), (390, 844), (844, 390)):
            page.set_viewport_size({"width": size[0], "height": size[1]})
            a0 = page.evaluate("document.getElementById('au').currentTime")
            page.wait_for_timeout(3000)
            a1 = page.evaluate("document.getElementById('au').currentTime")
            steps.append({"taille": "%dx%d" % size, "son_continue": a1 > a0 + 1.5, "disposition": page.evaluate("document.body.className"),
                          "station_marquee": page.locator("#stations .tile.on").count() == 1})
        report["rotation"] = steps
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(500)
        if page.locator("#main #s-radio").count() == 0:
            page.click("#s-radio .sec-head")
        page.locator("#stations .tile.on").first.click()
    except Exception as ex:
        report["rotation"] = "erreur : %s" % str(ex).splitlines()[0][:200]
    # Un podcast : le dernier épisode de la première émission.
    try:
        page.click("#s-podcasts .sec-head")
        page.wait_for_timeout(600)
        page.click("#pods .show .main >> nth=0")
        ok, t0 = False, time.time()
        while time.time() - t0 < 30 and not ok:
            page.wait_for_timeout(500)
            ok = page.evaluate("document.getElementById('au').currentTime") > 0.5
        report["podcast"] = {"emission": page.inner_text("#pl-kind"), "lecture": ok}
    except Exception as ex:
        report["podcast"] = {"erreur": str(ex)[:200]}
    ctx.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", default="captures")
    ap.add_argument("--audio", action="store_true")
    ap.add_argument("--webkit", action="store_true")
    ap.add_argument("--chrome", default="")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    report = {"url": a.url, "date": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "vues": [], "radios": []}
    with sync_playwright() as p:
        opts = {"args": ["--no-sandbox", "--autoplay-policy=no-user-gesture-required"]}
        if a.chrome:
            opts["executable_path"] = a.chrome
        else:
            opts["channel"] = "chrome"
        chrome = p.chromium.launch(**opts)
        for v in VIEWS:
            run_view(chrome, a.url, a.out, v, report, "chrome")
        if a.audio:
            run_audio(chrome, a.url, a.out, report)
        chrome.close()
        if a.webkit:
            try:
                wk = p.webkit.launch()
                for v in VIEWS[:3]:
                    run_view(wk, a.url, a.out, v, report, "webkit")
                wk.close()
            except Exception as ex:
                report["webkit"] = "indisponible : %s" % str(ex).splitlines()[0][:200]
    with open(os.path.join(a.out, "rapport.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    lines = ["Essais du %s sur %s" % (report["date"], a.url), ""]
    for v in report["vues"]:
        lines.append("[%s %s %s] %s" % (v["moteur"], v["vue"], v["taille"], "OK" if not v["problemes"] else "%d problème(s)" % len(v["problemes"])))
        lines += ["   - " + x for x in v["problemes"]]
    if report["radios"]:
        lines.append("")
        for r in report["radios"]:
            lines.append("radio %-22s %s en %ss | %s | logo : %s" % (r["station"], "LECTURE OK" if r["lecture"] else "ÉCHEC (%s)" % r["etat"], r["delai_s"], r["source"][:70], r["logo"][:110]))
        lines.append("podcast : %s" % json.dumps(report.get("podcast"), ensure_ascii=False))
        lines.append("rotation : %s" % json.dumps(report.get("rotation"), ensure_ascii=False))
    if report.get("webkit"):
        lines.append("webkit : " + report["webkit"])
    text = "\n".join(lines)
    with open(os.path.join(a.out, "rapport.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()

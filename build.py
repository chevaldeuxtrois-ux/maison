#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fabrique data.json (météo, titres, épisodes) pour le Dashboard Maison.

Lancé toutes les 15 minutes par GitHub Actions. Quand une source est en panne,
on garde ce qui était déjà en ligne pour elle.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

BASE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (compatible; DashboardMaison/2.0; +https://github.com/chevaldeuxtrois-ux/maison)"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
EPISODES = 8


def log(msg):
    sys.stderr.write(msg + "\n")


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(40 * 1024 * 1024)


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def text_of(node, name):
    child = node.find(name)
    return child.text if child is not None and child.text else ""


def timestamp(value):
    try:
        return int(parsedate_to_datetime(value.strip()).timestamp())
    except Exception:
        return 0


def duration(value):
    try:
        parts = [int(float(p)) for p in (value or "").strip().split(":")]
    except ValueError:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def parse_items(data):
    items = []
    for it in ET.fromstring(data).iter("item"):
        enc = it.find("enclosure")
        items.append({
            "title": clean(text_of(it, "title")),
            "source": clean(text_of(it, "source")),
            "link": text_of(it, "link").strip(),
            "ts": timestamp(text_of(it, "pubDate")),
            "audio": enc.get("url", "") if enc is not None else "",
            "dur": duration(text_of(it, ITUNES + "duration")),
        })
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items


def build_news(src):
    for url in src["urls"]:
        try:
            rows, seen = [], set()
            for it in parse_items(fetch(url)):
                title = it["title"]
                if src.get("strip") and title.endswith(src["strip"]):
                    title = title[: -len(src["strip"])].strip()
                elif it["source"] and title.endswith(" - " + it["source"]):
                    title = title[: -len(it["source"]) - 3].strip()
                if title and title not in seen:
                    seen.add(title)
                    rows.append({"title": title, "link": it["link"], "ts": it["ts"]})
            if rows:
                return rows[:25]
        except Exception as ex:
            log("news %s : %s (%s)" % (src["id"], ex, url[:60]))
    return None


def build_weather(cfg):
    q = urllib.parse.urlencode({
        "latitude": cfg["latitude"], "longitude": cfg["longitude"],
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
        "timezone": "auto", "forecast_days": 5,
    })
    d = json.loads(fetch("https://api.open-meteo.com/v1/forecast?" + q).decode("utf-8"))
    cur, day = d["current"], d["daily"]
    return {
        "city": cfg.get("city", ""),
        "current": {"temp": cur["temperature_2m"], "feels": cur["apparent_temperature"],
                    "code": cur["weather_code"], "wind": cur["wind_speed_10m"]},
        "days": [{"date": day["time"][i], "code": day["weather_code"][i],
                  "tmax": day["temperature_2m_max"][i], "tmin": day["temperature_2m_min"][i],
                  "pop": day["precipitation_probability_max"][i],
                  "rise": day["sunrise"][i][-5:], "set": day["sunset"][i][-5:]}
                 for i in range(len(day["time"]))],
    }


def read_some(url, limit=40000, timeout=20):
    """Ouvre une adresse et lit ses premiers octets : (type de contenu, adresse finale, octets)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Icy-MetaData": "0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers.get("Content-Type", ""), resp.geturl(), resp.read(limit)


def check_stream(url):
    """Vérifie qu'un direct envoie réellement du son. Renvoie un court compte rendu, ou lève une erreur."""
    ctype, final, data = read_some(url)
    low = ctype.lower()
    if "mpegurl" in low or url.split("?")[0].endswith(".m3u8"):
        # Liste HLS : on la suit jusqu'à un premier segment audio.
        for _ in range(3):
            lines = [l.strip() for l in data.decode("utf-8", "replace").splitlines() if l.strip() and not l.startswith("#")]
            if not lines:
                raise RuntimeError("liste HLS vide")
            nxt = urllib.parse.urljoin(final, lines[-1])
            ctype2, final, data = read_some(nxt)
            if "mpegurl" not in ctype2.lower() and not nxt.split("?")[0].endswith(".m3u8"):
                if len(data) < 5000:
                    raise RuntimeError("segment HLS trop court (%d octets)" % len(data))
                return "HLS, segment %s de %d octets" % (ctype2, len(data)), final
        raise RuntimeError("liste HLS sans segment")
    if not (low.startswith("audio/") or "ogg" in low):
        raise RuntimeError("réponse non audio (%s)" % ctype)
    if len(data) < 20000:
        raise RuntimeError("flux trop court (%d octets)" % len(data))
    return "%s, %d octets lus" % (ctype, len(data)), final


def build_station(st, prev):
    """Adresse du direct : la première qui envoie vraiment du son parmi les réglages, puis l'annuaire radio-browser.info."""
    out = {"id": st["id"], "name": st["name"], "short": st.get("short", st["name"]), "url": ""}
    tried = []
    for url in [st.get("url")] + st.get("alt", []):
        if not url:
            continue
        try:
            out["check"], out["final"] = check_stream(url)
            out["url"] = url
            return out
        except Exception as ex:
            tried.append("%s -> %s" % (url, ex))
    try:
        q = urllib.parse.urlencode({"name": st.get("lookup") or st["name"], "order": "votes", "reverse": "true",
                                    "hidebroken": "true", "limit": 10})
        found = json.loads(fetch("https://all.api.radio-browser.info/json/stations/search?" + q).decode("utf-8"))
        for r in found:
            url = r.get("url_resolved") or r.get("url") or ""
            if not url.startswith("https://") or (st.get("country") and r.get("countrycode") != st["country"]):
                continue
            try:
                out["check"], out["final"] = check_stream(url)
                out["url"], out["found"] = url, r.get("name", "")
                return out
            except Exception as ex:
                tried.append("%s -> %s" % (url, ex))
    except Exception as ex:
        tried.append("annuaire -> %s" % ex)
    log("radio %s : aucun flux valide. %s" % (st["id"], " ; ".join(tried)[:900]))
    if prev and prev.get("url"):          # panne passagère : on garde l'adresse connue
        out["url"], out["check"] = prev["url"], "non vérifié ce tour-ci"
    elif st.get("url"):
        out["url"], out["check"] = st["url"], "ÉCHEC du contrôle"
    return out


def plain(text):
    """Nom réduit à ses lettres et chiffres, sans accents, pour comparer."""
    import unicodedata
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]", "", text)


def image_info(url, why=None):
    """(adresse finale, type, taille) si l'adresse répond bien par une image, sinon None."""
    last = ""
    for attempt in range(2):
        try:
            ctype, final, data = read_some(url, 600000, 12)
        except Exception as ex:
            last = str(ex)
            time.sleep(2)          # les serveurs d'images limitent parfois les rafales
            continue
        low = ctype.lower()
        if low.startswith("image/") and "icon" not in low and len(data) >= 300 and final.startswith("https://"):
            return final, ctype, len(data)
        last = "%s, %d octets" % (ctype, len(data))
        break
    if why is not None:
        why.append("%s -> %s" % (url[-60:], last))
    return None


def site_icons(site):
    """Images que le site de la station déclare pour se représenter (icône d'écran d'accueil, image de partage)."""
    page = fetch(site).decode("utf-8", "replace")[:400000]
    out = []
    for pat in (r'<link[^>]+rel=["\'][^"\']*apple-touch-icon[^"\']*["\'][^>]*>',
                r'<meta[^>]+property=["\']og:image["\'][^>]*>',
                r'<link[^>]+rel=["\'](?:shortcut )?icon["\'][^>]*>'):
        for tag in re.findall(pat, page, re.I):
            m = re.search(r'(?:href|content)=["\']([^"\']+)["\']', tag, re.I)
            if m and not m.group(1).lower().split("?")[0].endswith(".ico"):
                out.append(urllib.parse.urljoin(site, html.unescape(m.group(1))))
    return out


def find_logo(st, prev):
    """Adresse du logo officiel de la station. La page affiche l'image depuis cette adresse ; rien n'est copié ici.

    Ordre : adresses des réglages, puis images déclarées par le site de la station, puis fiche de la station
    (même nom, même pays) dans l'annuaire radio-browser.info. Chaque adresse doit répondre par une image.
    """
    if prev and prev.get("logo") and str(prev.get("logo_from", "")).startswith("v6 "):
        return prev["logo"], prev["logo_from"]
    why = []
    for url in st.get("logo", []):
        info = image_info(url, why)
        if info:
            return info[0], "v6 réglages (%s, %d octets)" % info[1:]
    if why:
        log("logo %s : adresses des réglages refusées : %s" % (st["id"], " ; ".join(why)))
    if st.get("site"):
        try:
            icons = site_icons(st["site"])[:6]
            log("logo %s : images du site : %s" % (st["id"], " ; ".join(icons) or "aucune"))
            for url in icons:
                info = image_info(url, why)
                if info:
                    return info[0], "v6 site %s (%s, %d octets)" % (st["site"], info[1], info[2])
        except Exception as ex:
            log("logo %s : site illisible (%s)" % (st["id"], ex))
    wanted = [plain(st.get("lookup", "")), plain(st["name"]), plain(st.get("short", ""))]
    q = urllib.parse.urlencode({"name": st.get("lookup") or st["name"], "order": "votes", "reverse": "true", "limit": 40})
    found = json.loads(fetch("https://all.api.radio-browser.info/json/stations/search?" + q).decode("utf-8"))
    seen = set()
    for r in found:
        if plain(r.get("name", "")) not in wanted or (st.get("country") and r.get("countrycode") != st["country"]):
            continue
        url = (r.get("favicon") or "").replace("http://", "https://")
        if not url.startswith("https://") or url in seen or url.lower().split("?")[0].endswith(".ico"):
            continue
        seen.add(url)
        info = image_info(url)
        if info:
            return info[0], "v6 annuaire : %s (%s, %d octets)" % (r.get("name", ""), info[1], info[2])
    return "", "aucun logo vérifié"


def find_feed(pod):
    """Retrouve le flux d'un podcast par son nom dans l'annuaire Apple Podcasts."""
    q = urllib.parse.urlencode({"media": "podcast", "country": "fr", "limit": 10, "term": pod["search"]})
    results = [r for r in json.loads(fetch("https://itunes.apple.com/search?" + q).decode("utf-8")).get("results", [])
               if r.get("feedUrl")]
    want = pod.get("match", "").lower()
    for r in results:
        if want and want in r.get("collectionName", "").lower():
            return r["feedUrl"], r.get("collectionName", ""), r.get("artistName", "")
    if results and not want:
        r = results[0]
        return r["feedUrl"], r.get("collectionName", ""), r.get("artistName", "")
    raise RuntimeError("introuvable (%d résultats : %s)" % (
        len(results), " / ".join(r.get("collectionName", "") for r in results[:5])))


def build_podcast(pod, prev):
    out = {"id": pod["id"], "name": pod["name"], "episodes": []}
    try:
        if pod.get("feed"):
            out["feed"], out["found"] = pod["feed"], ""
        elif prev and prev.get("feed") and prev.get("search") == pod["search"] and prev.get("match") == pod.get("match"):
            out["feed"], out["found"], out["by"] = prev["feed"], prev.get("found", ""), prev.get("by", "")
        else:
            out["feed"], out["found"], out["by"] = find_feed(pod)
        out["search"], out["match"] = pod.get("search"), pod.get("match")
        raw = fetch(out["feed"])
        if not out.get("found"):
            m = re.search(rb"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", raw[:4000], re.S)
            out["found"] = clean(m.group(1).decode("utf-8", "replace")) if m else ""
        eps = [{"title": i["title"], "ts": i["ts"], "audio": i["audio"], "dur": i["dur"]}
               for i in parse_items(raw) if i["audio"]]
        out["episodes"] = eps[:EPISODES]
    except Exception as ex:
        log("podcast %s : %s" % (pod["id"], ex))
        out["error"] = str(ex)[:300]
    if not out["episodes"] and prev and prev.get("episodes"):
        keep = dict(prev)
        keep["name"] = pod["name"]
        return keep
    return out


def main():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    prev = {}
    try:
        prev = json.loads(fetch(cfg["site_url"] + "data.json?t=%d" % time.time()).decode("utf-8"))
    except Exception as ex:
        log("pas de version précédente : %s" % ex)
    prev_news = {n["id"]: n for n in prev.get("news", [])}
    prev_pods = {p["id"]: p for p in prev.get("podcasts", [])}

    try:
        weather = build_weather(cfg["weather"])
    except Exception as ex:
        log("météo : %s" % ex)
        weather = prev.get("weather")

    news = []
    for src in cfg["news"]:
        items = build_news(src)
        if items is None:
            items = prev_news.get(src["id"], {}).get("items", [])
        news.append({"id": src["id"], "name": src["name"], "items": items})

    with ThreadPoolExecutor(max_workers=4) as pool:
        podcasts = list(pool.map(lambda p: build_podcast(p, prev_pods.get(p["id"])), cfg["podcasts"]))

    prev_st = {s["id"]: s for s in prev.get("stations", [])}
    stations = []
    for st in cfg["stations"]:
        s = build_station(st, prev_st.get(st["id"]))
        try:
            s["logo"], s["logo_from"] = find_logo(st, prev_st.get(st["id"]))
        except Exception as ex:
            s["logo"], s["logo_from"] = "", "erreur : %s" % ex
        log("bilan logo %s | %s | %s" % (st["id"], s["logo"], s["logo_from"]))
        if st.get("logo_search"):      # aide au choix d'un logo : fichiers proposés par Wikimedia Commons et par le site
            try:
                q = urllib.parse.urlencode({"action": "query", "format": "json", "list": "search", "srnamespace": 6,
                                            "srlimit": 12, "srsearch": st["logo_search"]})
                hits = json.loads(fetch("https://commons.wikimedia.org/w/api.php?" + q).decode("utf-8"))["query"]["search"]
                log("pistes logo %s (Commons) : %s" % (st["id"], " ; ".join(h["title"] for h in hits)))
            except Exception as ex:
                log("pistes logo %s (Commons) : erreur %s" % (st["id"], ex))
            if st.get("site"):
                try:
                    log("pistes logo %s (site) : %s" % (st["id"], " ; ".join(site_icons(st["site"])[:8])))
                except Exception as ex:
                    log("pistes logo %s (site) : erreur %s" % (st["id"], ex))
        log("bilan radio %s | %s | %s | %s" % (st["id"], s["url"], s.get("check"), (s.get("final") or "")[:90]))
        if s["url"]:
            stations.append(s)

    data = {"updated": int(time.time()), "stations": stations, "spotify": cfg.get("spotify_client_id", ""), "weather": weather,
            "news": news, "podcasts": podcasts}
    with open(os.path.join(BASE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    for p in podcasts:
        e = p["episodes"][0] if p["episodes"] else None
        log("bilan podcast %s | %s | %d | %s | %s | %s" % (
            p["name"], p.get("found"), len(p["episodes"]),
            time.strftime("%d/%m", time.gmtime(e["ts"])) if e else "-",
            e["title"][:50] if e else p.get("error"), e["audio"].split("/")[2] if e else ""))
    log("ok : %d podcasts avec épisodes sur %d, %s titres" % (
        sum(1 for p in podcasts if p["episodes"]), len(podcasts), "+".join(str(len(n["items"])) for n in news)))


if __name__ == "__main__":
    main()

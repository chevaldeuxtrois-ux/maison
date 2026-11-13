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
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) DashboardMaison/2.0"
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


def probe(url):
    """Type de contenu et adresse finale d'un flux (on ne lit que les en-têtes)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.headers.get("Content-Type", ""), resp.geturl()


def is_audio(ctype):
    return ctype.lower().startswith("audio/") or "ogg" in ctype.lower() or "mpegurl" in ctype.lower()


def build_station(st, prev):
    """Adresse du direct : celle des réglages si elle répond, sinon l'annuaire radio-browser.info."""
    out = {"id": st["id"], "name": st["name"], "short": st.get("short", st["name"]), "url": st.get("url", "")}
    if not st.get("lookup"):
        return out
    if out["url"]:
        try:
            out["type"], out["final"] = probe(out["url"])
            if is_audio(out["type"]):
                return out
        except Exception as ex:
            out["type"] = "erreur : %s" % ex
        log("radio %s : adresse des réglages en échec (%s)" % (st["id"], out["type"]))
    try:
        q = urllib.parse.urlencode({"name": st["lookup"], "order": "votes", "reverse": "true",
                                    "hidebroken": "true", "limit": 8})
        found = json.loads(fetch("https://all.api.radio-browser.info/json/stations/search?" + q).decode("utf-8"))
        for r in found:
            url = r.get("url_resolved") or r.get("url") or ""
            if not url.startswith("http") or ".m3u8" in url:
                continue
            try:
                ctype, final = probe(url)
            except Exception:
                continue
            if is_audio(ctype):
                out.update({"url": url, "found": r.get("name", ""), "type": ctype, "final": final})
                return out
        raise RuntimeError("aucun flux audio pour " + st["lookup"])
    except Exception as ex:
        log("radio %s : %s" % (st["id"], ex))
        if prev and prev.get("url") and not st.get("url"):
            out["url"], out["found"] = prev["url"], prev.get("found", "")
    return out


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
    stations = [s for s in (build_station(st, prev_st.get(st["id"])) for st in cfg["stations"]) if s["url"]]

    data = {"updated": int(time.time()), "stations": stations, "spotify": cfg.get("spotify_client_id", ""), "weather": weather,
            "news": news, "podcasts": podcasts}
    with open(os.path.join(BASE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    for s in stations:
        if s.get("type"):
            log("bilan radio %s | %s | %s | %s | %s" % (s["id"], s["url"], s.get("found"), s.get("type"), s.get("final")))
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

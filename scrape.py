#!/usr/bin/env python3
"""Scraper pentru cinemaurile care vând bilete prin Eventbook.

Scrie docs/data/screenings.json. Fiecare cinema e citit separat: dacă unul
cedează, restul merg, iar datele vechi (încă viitoare) se păstrează marcate "stale".
"""
import html
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path("docs/data/screenings.json")
BASE = "https://eventbook.ro"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

HALLS = {
    "elvire": {"name": "Elvire Popesco", "short": "Elvire", "slug": "cinema-elvire-popesco", "color": "#13727a", "tech": ["Barco laser"]},
    "union": {"name": "Union", "short": "Union", "slug": "cinema-union", "color": "#b0304f", "tech": []},
    "apollo111": {"name": "Apollo111", "short": "Apollo111", "slug": "apollo111-cinema", "color": "#6a4fb0", "tech": []},
    "eforie": {"name": "Eforie", "short": "Eforie", "slug": "cinema-eforie", "color": "#8a7a3a", "tech": []},
    "muzeu": {"name": "Muzeul Țăranului", "short": "MȚR", "slug": "cinema-muzeul-taranului-studio-horia-bernea", "color": "#b4651e", "tech": []},
}

# Reduceri valabile DOAR la casa de bilete (nu apar online). Cheie: prețul de adult.
BOX_RULES = {
    "union": {15: {"student": 5, "pensioner": 5}},
    "eforie": {15: {"student": 5, "pensioner": 5}},
    "muzeu": {32: {"pensioner": 16, "student": 8}, 40: {"pensioner": 20, "student": 10}},
}

MONTHS = {"jan": 1, "ian": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "mai": 5, "jun": 6, "iun": 6,
          "jul": 7, "iul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

DATE = re.compile(r"calendar_month\s*(\d{1,2})\s+([^\W\d_]+)\.?\s+(\d{4})\s*schedule\s*(\d{1,2}):(\d{2})")
LINK = re.compile(r'href="/film/(bilete-[a-z0-9-]+)\?hall=([a-z0-9-]+)"')
TITLE = re.compile(r'event-title[^"]*"[^>]*>\s*<h5[^>]*>(.*?)</h5>', re.S)
AGE = re.compile(r"\s+(A\.?G\.?|AP-?12|N-?15|IM-?18|\d{1,2}\+)\s*$", re.I)
AUDIO_N = re.compile(r"volume_up\s+(.{1,40}?)\s+notes\b")
AUDIO = re.compile(r"volume_up\s+([A-Z]{2,3}(?:\s*[,/]\s*[A-Z]{2,3})*)(?=\s|$)")
SUBS = re.compile(r"\bnotes\s+(?:(?i:subtitrat)\s+)?([A-Z]{2,3}(?:\s*[,/]\s*[A-Z]{2,3})*)(?=\s|$)")
PRICE = re.compile(r"(?:price|pre[tț])\s*:?\s*(\d+(?:[.,]\d+)?)\s*lei(?:\s*\(([^)]*)\))?", re.I)
SOLD = re.compile(r"sold\s*out|epuizat", re.I)
POSTER = re.compile(r'event-image-hall[^>]*>\s*<img[^>]+?(?:data-src|src)="([^"]+)"')
DESC = re.compile(r'program-description[^>]*>\s*<p>(.*?)</p>', re.S)
PRICE_BARE = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*lei\b", re.I)


def fetch(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def langs(s):
    out = []
    for x in re.split(r"[,/]", s or ""):
        x = x.strip()
        if x:
            out.append(x.upper() if len(x) <= 3 else x.capitalize())
    return out


def num(v):
    f = float(v.replace(",", "."))
    return int(f) if f == int(f) else f


def tiers(label):
    l = (label or "").lower()
    if re.search(r"adult\s*\+\s*copil|familie|family", l):
        return ["family"]
    if "copil" in l:
        return ["child"]
    stu = re.search(r"elev|student", l)
    pen = "pensionar" in l
    if stu or pen:
        return (["student"] if stu else []) + (["pensioner"] if pen else [])
    if "redus" in l:
        return ["student", "pensioner"]
    return ["adult"]


def parse_block(blk, hid, stats):
    t = text(blk)
    d = DATE.search(t)
    if not d:
        stats["no_date"] += 1  # carnete, abonamente etc.
        return None
    day, mon, year, hh, mm = d.groups()
    month = MONTHS.get(mon[:3].lower())
    lm = LINK.search(blk)
    tm = TITLE.search(blk)
    if not month or not lm:
        stats["no_link_or_month"] += 1
        print("  SARIT (fara link sau luna necunoscuta):", t[:160])
        return None
    raw = text(tm.group(1)) if tm else ""
    if not raw:
        stats["no_title"] += 1
        return None
    am = AGE.search(raw)
    age, title = (am.group(1).upper(), raw[:am.start()].strip()) if am else (None, raw)
    nm = re.search(re.escape(raw) + r"\s+(.{1,120}?)\s+volume_up", t)
    note = nm.group(1).strip() if nm else None
    au = AUDIO_N.search(t) or AUDIO.search(t)
    su = SUBS.search(t)
    prices = {}
    for val, label in PRICE.findall(t):
        for k in tiers(label):
            prices.setdefault(k, num(val))
    if not prices:  # ex. Muzeul Țăranului: doar "32 lei", fără "preț:"
        tail = t[t.rfind("location_on"):] if "location_on" in t else t
        pb = PRICE_BARE.search(tail)
        if pb:
            prices["adult"] = num(pb.group(1))
    box = {}
    if "adult" in prices:
        for k, v in BOX_RULES.get(hid, {}).get(prices["adult"], {}).items():
            if k not in prices:
                box[k] = v
    status = "sold_out" if ("shopping_cart" not in t and SOLD.search(t)) else "ok"
    slug = lm.group(1)
    pm = POSTER.search(blk)
    dm = DESC.search(blk)
    if not pm and not stats.get("poster_dbg"):
        stats["poster_dbg"] = 1
        i = blk.find("event-image-hall")
        print("  FARA POSTER, HTML in jurnal:", re.sub(r"\s+", " ", blk[max(0, i - 50): i + 400] if i >= 0 else blk[:400]))
    date_iso = f"{year}-{month:02d}-{int(day):02d}"
    time_s = f"{int(hh):02d}:{mm}"
    return {
        "id": f"{hid}-{date_iso}-{time_s}-{slug}", "cinema": hid, "date": date_iso, "time": time_s,
        "title": title, "age": age, "note": note,
        "audio": langs(au.group(1)) if au else [], "subs": langs(su.group(1)) if su else [],
        "prices": prices, "box": box, "status": status,
        "url": f"{BASE}/film/{slug}?hall={lm.group(2)}",
        "desc": text(dm.group(1))[:500] if dm else None,
        "poster": (pm.group(1) if pm.group(1).startswith("http") else BASE + pm.group(1)) if pm else None,
    }


def scrape_hall(hid):
    url = f"{BASE}/hall/{HALLS[hid]['slug']}"
    stats = {"pagini": 0, "blocuri": 0, "proiectii": 0, "no_date": 0, "no_link_or_month": 0, "no_title": 0}
    items = {}
    first = fetch(url)
    maxp = min(max([int(x) for x in re.findall(r"[?&]page=(\d+)", first)] or [1]), 15)
    page = first
    for p in range(1, maxp + 1):
        if p > 1:
            time.sleep(1.5)
            page = fetch(f"{url}?page={p}")
        blocks = page.split('id="performance"')[1:]
        stats["pagini"] += 1
        stats["blocuri"] += len(blocks)
        if not blocks:
            break
        for blk in blocks:
            it = parse_block(blk, hid, stats)
            if it:
                items[it["id"]] = it
    stats["proiectii"] = len(items)
    return list(items.values()), stats


def main():
    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text("utf-8"))
        except Exception:
            prev = {}
    today = datetime.now(ZoneInfo("Europe/Bucharest")).date().isoformat()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    all_items, sources = [], {}
    for hid, h in HALLS.items():
        err, stats, items = None, {}, []
        try:
            items, stats = scrape_hall(hid)
        except Exception as e:
            err = repr(e)
        print(f"\n=== {h['name']} ===")
        print("STATISTICI:", stats or "-", "| EROARE:", err or "nu")
        if items:
            print("  cu audio:", sum(1 for i in items if i["audio"]), "| cu subtitrare:", sum(1 for i in items if i["subs"]),
                  "| cu pret student:", sum(1 for i in items if "student" in i["prices"] or "student" in i["box"]),
                  "| sold out:", sum(1 for i in items if i["status"] == "sold_out"),
                  "| fara pret:", sum(1 for i in items if not i["prices"]))
            for i in sorted(items, key=lambda x: (x["date"], x["time"]))[:2]:
                print("  EXEMPLU:", json.dumps(i, ensure_ascii=False))
        old = [x for x in prev.get("screenings", []) if x["cinema"] == hid and x["date"] >= today]
        stale = False
        updated = now
        if not items and old:
            items, stale = old, True
            updated = prev.get("sources", {}).get(hid, {}).get("updated", now)
            print(f"  ATENTIE: nimic gasit, pastrez {len(old)} proiectii vechi (stale)")
        sources[hid] = {"name": h["name"], "ok": err is None and not stale, "stale": stale,
                        "count": len(items), "updated": updated, "error": err}
        all_items += items
    if not all_items:
        sys.exit("Niciun program gasit la niciun cinema.")
    all_items.sort(key=lambda x: (x["date"], x["time"], x["cinema"]))
    data = {
        "generated": now,
        "cinemas": {k: {"name": v["name"], "short": v["short"], "color": v["color"], "tech": v["tech"]} for k, v in HALLS.items()},
        "sources": sources,
        "screenings": all_items,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"\nSalvat {len(all_items)} proiectii in {OUT}")


if __name__ == "__main__":
    main()

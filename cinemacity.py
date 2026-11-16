#!/usr/bin/env python3
"""Cinema City România (București): citește API-ul public JSON al site-ului.

Formatul răspunsurilor a fost verificat pentru un cinema (film-events/in-cinema/{id}/at-date/{data});
restul câmpurilor (ex. sold out) sunt tratate defensiv și jurnalul arată cheile necunoscute.
"""
import json
import re
import time
import urllib.request
from datetime import date, timedelta

BASE = "https://www.cinemacity.ro/ro/data-api-service/v1/quickbook/10107/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
DAYS = 14

SHORT = {"1806": "AFI", "1818": "Mega Mall", "1824": "Park Lake", "1807": "Sun Plaza"}
COLORS = ["#3b6fd6", "#4f9d4f", "#c9a227", "#6b7a8f", "#c0556b", "#2f9e8f"]
AGE = {"ag": "AG", "ap-12": "AP-12", "n-15": "N-15", "im-18": "IM-18"}
TECH = {"imax": "IMAX", "4dx": "4DX", "3d": "3D", "vip": "VIP", "dolby-atmos": "Dolby Atmos", "laser-barco": "Barco laser"}
FALLBACK = [{"id": i, "displayName": n} for i, n in (("1806", "București AFI Cotroceni"), ("1818", "București Mega Mall"),
                                                     ("1824", "București Park Lake"), ("1807", "București Sun Plaza"))]


def get_json(path, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(BASE + path, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def until():
    return (date.today() + timedelta(days=DAYS)).isoformat()


def discover():
    """Cinemaurile din București, direct din API (apar automat și locațiile noi)."""
    try:
        cins = get_json(f"cinemas/with-event/until/{until()}?attr=&lang=ro_RO")["body"]["cinemas"]
        found = [c for c in cins if c.get("groupId") == "bucuresti"] or FALLBACK
    except Exception as e:
        print("Cinema City: nu am putut citi lista de cinemauri:", repr(e), "- folosesc lista fixă")
        found = FALLBACK
    out = []
    for n, c in enumerate(found):
        cid = str(c["id"])
        nm = re.sub(r"^București\s+", "", c.get("displayName", cid))
        out.append({"id": cid, "key": f"cc{cid}", "name": "Cinema City " + nm, "short": SHORT.get(cid, nm.split()[0]),
                    "color": COLORS[n % len(COLORS)]})
    return out


def scrape_cinema(c):
    cid = c["id"]
    stats = {"zile": 0, "evenimente": 0, "fara_film": 0}
    items, seen_keys = {}, set()
    dates = get_json(f"dates/in-cinema/{cid}/until/{until()}?attr=&lang=ro_RO")["body"].get("dates", [])
    for d in dates:
        body = get_json(f"film-events/in-cinema/{cid}/at-date/{d}?attr=&lang=ro_RO")["body"]
        films = {f["id"]: f for f in body.get("films", [])}
        stats["zile"] += 1
        for ev in body.get("events", []):
            stats["evenimente"] += 1
            seen_keys |= set(ev.keys())
            f = films.get(ev.get("filmId"))
            if not f:
                stats["fara_film"] += 1
                continue
            attrs = ev.get("attributeIds") or f.get("attributeIds") or []
            lang = ev.get("languages") or f.get("languages") or {}
            dubbed = bool(lang.get("dubbed")) or "dubbed" in attrs
            audio = [x.upper() for x in ((lang.get("dubbed") if dubbed else lang.get("original")) or [])]
            dt = ev.get("eventDateTime") or ""
            if len(dt) < 16:
                continue
            sold = bool(ev.get("soldOut") or ev.get("isSoldOut"))
            items[ev["id"]] = {
                "id": f"{c['key']}-{ev['id']}", "cinema": c["key"], "date": ev.get("businessDay") or dt[:10], "time": dt[11:16],
                "title": f.get("name"), "age": next((AGE[a] for a in attrs if a in AGE), None),
                "note": "Avanpremieră" if "preview" in attrs else None,
                "audio": audio, "subs": [x.upper() for x in lang.get("subtitles") or []], "dubbed": dubbed,
                "tech": [TECH[a] for a in attrs if a in TECH], "prices": {}, "box": {},
                "status": "sold_out" if sold else "ok", "url": ev.get("bookingLink") or f.get("link"),
                "poster": f.get("posterLink"), "year": f.get("releaseYear"), "runtime": f.get("length"),
            }
        time.sleep(0.6)
    known = {"id", "filmId", "cinemaId", "businessDay", "eventDateTime", "attributeIds", "languages", "bookingLink", "compositeBookingLink"}
    print("  Cinema City: chei necunoscute în evenimente:", sorted(seen_keys - known) or "-")
    return list(items.values()), stats

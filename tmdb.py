#!/usr/bin/env python3
"""Completează docs/data/screenings.json cu date TMDB (titlu RO, titlu original, regie, poster).

Rulează după scrape.py. Dacă lipsește TMDB_TOKEN sau TMDB nu răspunde, filmele rămân
cu titlul din Eventbook și cu posterul din Eventbook. Corecții manuale: overrides.json
(cheie = titlul normalizat din jurnal, valoare = id TMDB, sau 0 ca să NU folosească TMDB).
"""
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

DATA = Path("docs/data/screenings.json")
CACHE = Path("docs/data/tmdb_cache.json")
OVERRIDES = Path("overrides.json")
TOKEN = os.environ.get("TMDB_TOKEN", "").strip()
API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w185"
NOISE = re.compile(r"\b(proiec[tț]ie special[ăa]|avanpremier[ăa]|premier[ăa]|preview)\b|\(\d{4}\)", re.I)
YEAR = datetime.now().year


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def api(path, **params):
    url = f"{API}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}", "accept": "application/json"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < 2:
                time.sleep(2 * (i + 1))
                continue
            raise
    return {}


def queries(title):
    parts = [p.strip() for p in re.split(r"\s+[/|]\s+", title) if p.strip()]
    out = []
    for q in parts + [title]:
        q = re.sub(r"\s+", " ", NOISE.sub(" ", q)).strip()
        if q and q not in out:
            out.append(q)
    return out


def pick(results, qn):
    exact, fuzzy = [], []
    for r in results[:8]:
        names = [n for n in (norm(r.get("title")), norm(r.get("original_title"))) if n]
        if not names:
            continue
        rd = (r.get("release_date") or "")[:4]
        yr = int(rd) if rd.isdigit() else 0
        if qn in names:
            exact.append((yr >= YEAR - 2, r.get("popularity", 0), r))
        elif max(SequenceMatcher(None, qn, n).ratio() for n in names) >= 0.92 and yr >= YEAR - 2:
            fuzzy.append((True, r.get("popularity", 0), r))
    for group, kind in ((exact, "exact"), (fuzzy, "aproximativ")):
        if group:
            group.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return group[0][2]["id"], kind
    return None, None


def details(mid):
    d = api(f"/movie/{mid}", language="ro-RO", append_to_response="credits,external_ids")
    crew = (d.get("credits") or {}).get("crew") or []
    return {
        "title": d.get("title") or d.get("original_title"),
        "original_title": d.get("original_title"),
        "year": (d.get("release_date") or "")[:4] or None,
        "poster": IMG + d["poster_path"] if d.get("poster_path") else None,
        "directors": [c["name"] for c in crew if c.get("job") == "Director"][:3],
        "imdb_id": (d.get("external_ids") or {}).get("imdb_id") or None,
        "runtime": d.get("runtime") or None,
        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def age_days(iso):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).days
    except Exception:
        return 999


def load(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def main():
    data = load(DATA, None)
    if not data:
        raise SystemExit("Lipsește docs/data/screenings.json — rulează întâi scrape.py")
    cache = load(CACHE, {"queries": {}, "movies": {}})
    over = {norm(k): v for k, v in load(OVERRIDES, {}).items()}
    use_tmdb = bool(TOKEN)
    if not use_tmdb:
        print("ATENTIE: TMDB_TOKEN lipsește; filmele rămân cu titlul și posterul din Eventbook.")

    by_title = {}
    for s in data["screenings"]:
        by_title.setdefault(norm(s["title"]), []).append(s)

    errors = 0
    resolved = {}  # titlu normalizat -> id TMDB sau None
    for tn, items in sorted(by_title.items()):
        title = items[0]["title"]
        mid, kind = None, None
        if tn in over:
            mid, kind = (over[tn] or None), "manual"
        elif use_tmdb:
            for q in queries(title):
                qn = norm(q)
                c = cache["queries"].get(qn)
                if not c or (c.get("id") is None and age_days(c.get("ts", "")) > 7):
                    try:
                        res = api("/search/movie", query=q, language="ro-RO", include_adult="false").get("results", [])
                        i, k = pick(res, qn)
                        c = {"id": i, "kind": k, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                        cache["queries"][qn] = c
                        time.sleep(0.15)
                    except Exception as e:
                        errors += 1
                        print("  EROARE TMDB la căutare:", q, repr(e))
                        continue
                if c.get("id"):
                    mid, kind = c["id"], c.get("kind")
                    break
        if mid and use_tmdb:
            m = cache["movies"].get(str(mid))
            if not m or age_days(m.get("fetched", "")) > 14:
                try:
                    cache["movies"][str(mid)] = details(mid)
                    time.sleep(0.15)
                except Exception as e:
                    errors += 1
                    print("  EROARE TMDB la detalii:", mid, repr(e))
        if mid and str(mid) in cache["movies"]:
            resolved[tn] = mid
            m = cache["movies"][str(mid)]
            print(f"  {kind:12} {title!r} -> #{mid} {m['title']!r} ({m['year']}) regia: {', '.join(m['directors']) or '-'}")
        else:
            print(f"  NEPOTRIVIT   {title!r}   [cheie pentru overrides.json: {tn!r}]")

    films = {}
    for tn, items in by_title.items():
        mid = resolved.get(tn)
        if mid:
            key = f"tmdb:{mid}"
            m = cache["movies"][str(mid)]
            films.setdefault(key, {"title": m["title"], "original_title": m["original_title"], "year": m["year"],
                                   "poster": m["poster"], "directors": m["directors"], "imdb_id": m["imdb_id"],
                                   "runtime": m["runtime"], "source": "tmdb"})
            if not films[key]["poster"]:
                films[key]["poster"] = next((s["poster"] for s in items if s.get("poster")), None)
        else:
            key = f"t:{tn}"
            parts = [p.strip() for p in re.split(r"\s+[/|]\s+", items[0]["title"]) if p.strip()]
            films[key] = {"title": parts[-1] if parts else items[0]["title"],
                          "original_title": parts[0] if len(parts) > 1 else None, "year": None,
                          "poster": next((s["poster"] for s in items if s.get("poster")), None),
                          "directors": [], "imdb_id": None, "runtime": None, "source": "eventbook"}
        for s in items:
            s["film"] = key
    for s in data["screenings"]:
        s.setdefault("film", f"t:{norm(s['title'])}")
    data["films"] = films
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), "utf-8")
    n_t = sum(1 for f in films.values() if f["source"] == "tmdb")
    print(f"\nFilme: {len(films)} (TMDB: {n_t}, doar Eventbook: {len(films) - n_t}) | erori TMDB: {errors}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Completează docs/data/screenings.json: grupează titlurile aceluiași film și adaugă date TMDB
(titlu RO, titlu original, an, regie, poster), nota IMDb (din datasets IMDb) și, opțional, nota Letterboxd.

Fără TMDB_TOKEN sau cu token invalid, filmele rămân cu titlul și posterul din Eventbook, dar se comasează.
Corecții manuale: overrides.json (cheie = titlu normalizat din jurnal, valoare = id TMDB sau 0 = fără TMDB).
"""
import gzip
import io
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
TOKEN = os.environ.get("TMDB_TOKEN", "").strip().strip("\"'")
if TOKEN.lower().startswith("bearer "):
    TOKEN = TOKEN[7:].strip()
USE_LB = os.environ.get("LETTERBOXD", "").strip() == "1"
API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w185"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NOISE = re.compile(r"\b(proiec[tț]ie special[ăa]|avanpremier[ăa]|premier[ăa]|preview)\b|\(\d{4}\)", re.I)
YEAR = datetime.now().year


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def age_days(iso):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).days
    except Exception:
        return 999


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def load(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def api(path, **params):
    headers = {"accept": "application/json"}
    if re.fullmatch(r"[0-9a-fA-F]{32}", TOKEN):
        params["api_key"] = TOKEN
    else:
        headers["Authorization"] = f"Bearer {TOKEN}"
    url = f"{API}{path}?" + urllib.parse.urlencode(params)
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < 2:
                time.sleep(2 * (i + 1))
                continue
            raise
    return {}


def parts(title):
    """Titlul spart la ' / ' sau ' | ', fără cuvinte-zgomot. Programele cu ' + ' (scurtmetraje) nu se sparg."""
    if re.search(r"\s\+\s", title):
        return []
    out = []
    for p in re.split(r"\s+[/|]\s+", title):
        p = re.sub(r"\s+", " ", NOISE.sub(" ", p)).strip()
        if p and p not in out:
            out.append(p)
    return out


def groups(titles):
    """Unește titlurile care au măcar o parte în comun (ex. 'MINOTAURE / MINOTAUR' cu 'Minotaur')."""
    parent = {t: t for t in titles}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen = {}
    for t in titles:
        for a in {norm(p) for p in parts(t)} | {norm(t)}:
            if a in seen:
                parent[find(t)] = find(seen[a])
            else:
                seen[a] = t
    out = {}
    for t in titles:
        out.setdefault(find(t), []).append(t)
    return list(out.values())


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
    for grp, kind in ((exact, "exact"), (fuzzy, "aproximativ")):
        if grp:
            grp.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return grp[0][2]["id"], kind
    return None, None


LATIN = re.compile(r"^[A-Za-z\u00C0-\u024F .'’\-]+$")


def latin_name(name, pid):
    if not name or LATIN.match(name) or not pid:
        return name
    try:
        for n in api(f"/person/{pid}").get("also_known_as") or []:
            if LATIN.match(n):
                return n
    except Exception:
        pass
    return name


def details(mid):
    d = api(f"/movie/{mid}", language="ro-RO", append_to_response="credits,external_ids")
    cr = d.get("credits") or {}
    crew = cr.get("crew") or []
    return {
        "title": d.get("title") or d.get("original_title"),
        "original_title": d.get("original_title"),
        "orig_lang": d.get("original_language"),
        "year": (d.get("release_date") or "")[:4] or None,
        "poster": IMG + d["poster_path"] if d.get("poster_path") else None,
        "directors": [latin_name(c["name"], c.get("id")) for c in crew if c.get("job") == "Director"][:3],
        "cast": [latin_name(c["name"], c.get("id")) for c in (cr.get("cast") or [])[:6]],
        "v": 2,
        "overview": (d.get("overview") or "")[:600],
        "imdb_id": (d.get("external_ids") or {}).get("imdb_id") or None,
        "runtime": d.get("runtime") or None,
        "fetched": now(),
    }


STOP = set("aceasta acesta acest filmul filmului pentru care sunt este intr intre dintre dupa despre prin catre fiind "
           "viata poveste their about which with from film story after before between".split())


def toks(text):
    return {w for w in norm(text).split() if len(w) >= 5 and w not in STOP}


def search(q, year, cache):
    key = f"{norm(q)}|{year or ''}"
    c = cache.setdefault("search", {}).get(key)
    if c and age_days(c.get("ts", "")) <= 3:
        return c["res"]
    params = {"query": q, "language": "ro-RO", "include_adult": "false"}
    if year:
        params["primary_release_year"] = year
    res = api("/search/movie", **params).get("results", [])
    res = [{k: r.get(k) for k in ("id", "title", "original_title", "release_date", "popularity")} for r in res[:10]]
    cache["search"][key] = {"res": res, "ts": now()}
    time.sleep(0.12)
    return res


def page_text(url, cache):
    m = re.search(r"/film/([a-z0-9-]+)", url or "")
    if not m:
        return ""
    slug = m.group(1)
    c = cache.setdefault("pages", {}).get(slug)
    if c and age_days(c.get("ts", "")) <= 14:
        return c["text"]
    try:
        req = urllib.request.Request(f"https://eventbook.ro/film/{slug}", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", " ", raw)))[:10000]
        cache["pages"][slug] = {"text": txt, "ts": now()}
        time.sleep(1)
        return txt
    except Exception:
        return ""


def score(kind, yr, d, ev_norm, et, newish, hy=(), hr=()):
    sc, why = (3.0 if kind == "exact" else 1.0), []
    if any(f" {norm(n)} " in ev_norm for n in d.get("directors", []) if norm(n)):
        sc += 4
        why.append("regia apare în descriere")
    cast = [n for n in d.get("cast", []) if norm(n) and f" {norm(n)} " in ev_norm]
    if cast:
        sc += min(2, len(cast))
        why.append("distribuția apare în descriere")
    ot = toks(d.get("overview") or "")
    if ot and et:
        common = ot & et
        sc += 4 * len(common) / min(len(ot), len(et))
        if common:
            why.append(f"sinopsis: {len(common)} cuvinte comune")
        elif len(et) >= 8 and "regia apare în descriere" not in why and not cast:
            sc -= 1.5
            why.append("sinopsis fără legătură")
    if yr and hy:  # anul și durata din Cinema City
        if yr in hy:
            sc += 2.5
            why.append("anul coincide")
        elif min(abs(yr - h) for h in hy) >= 2:
            sc -= 2
            why.append("anul nu coincide")
    rt = d.get("runtime")
    if rt and hr and any(abs(rt - h) <= 4 for h in hr):
        sc += 1.5
        why.append("durata coincide")
    if yr >= YEAR - 1:
        sc += 1.5
    elif yr == YEAR - 2:
        sc += 0.5
    if newish and yr and yr < YEAR - 1:
        sc -= 4
        why.append("proiecția pare film nou, candidatul e vechi")
    return round(sc, 1), why


def resolve(grp, items, cache):
    qparts = []
    for t in grp:
        for p in parts(t):
            if p not in qparts:
                qparts.append(p)
    if not qparts:
        return None, None, "program de scurtmetraje, nu caut"
    cands = {}

    def collect(qlist):
        qns = [norm(p) for p in qlist]
        for q in qlist:
            for yr in (None, YEAR, YEAR - 1):
                for r in search(q, yr, cache):
                    names = [n for n in (norm(r.get("title")), norm(r.get("original_title"))) if n]
                    if not names:
                        continue
                    if any(qn in names or (len(qn) >= 38 and any(n.startswith(qn) for n in names)) for qn in qns):
                        kind = "exact"
                    elif max(SequenceMatcher(None, qn, n).ratio() for qn in qns for n in names) >= 0.92:
                        kind = "aproximativ"
                    else:
                        continue
                    cands.setdefault(r["id"], (kind, r))

    collect(qparts)
    need_ev = False
    if not cands and not re.search(r"\b(party|club|atelier|workshop|concert|lectur\w*|reading|seara|serata|dezbatere|discutie)\b", norm(" ".join(grp))):
        fb = []
        for t0 in grp:
            for p in re.split(r":\s+", t0):
                p = p.strip()
                if len(p) >= 4 and p not in fb and p not in qparts and ":" in t0:
                    fb.append(p)
        if fb:
            collect(fb)
            need_ev = bool(cands)
    if not cands:
        return None, None, "niciun candidat cu titlu potrivit"
    ev = " ".join(list(grp) + [s.get("note") or "" for s in items] + [s.get("desc") or "" for s in items]
                  + [page_text(next((s["url"] for s in items if s.get("url")), ""), cache)])
    ev_norm, et = f" {norm(ev)} ", toks(ev)
    hy = [int(s["year"]) for s in items if str(s.get("year") or "").isdigit()]
    hr = [int(s["runtime"]) for s in items if str(s.get("runtime") or "").isdigit()]
    newish = any(re.search(r"avanpremier|premier|preview", norm(s.get("note") or "")) for s in items)
    ranked = []
    for mid, (kind, r) in sorted(cands.items(), key=lambda kv: (kv[1][0] == "exact", kv[1][1].get("popularity") or 0), reverse=True)[:6]:
        m = cache["movies"].get(str(mid))
        if not m or m.get("v", 0) < 2 or age_days(m.get("fetched", "")) > 14:
            m = cache["movies"][str(mid)] = details(mid)
            time.sleep(0.12)
        rd = (r.get("release_date") or "")[:4]
        sc, why = score(kind, int(rd) if rd.isdigit() else 0, m, ev_norm, et, newish, hy, hr)
        ranked.append((sc, mid, kind, m, why))
    ranked.sort(key=lambda x: x[0], reverse=True)
    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    info = f"scor {best[0]}: {', '.join(best[4]) or 'doar titlul'}"
    if second:
        info += f" | al doilea: #{second[1]} {second[3]['title']!r} ({second[3]['year']}) scor {second[0]}"
    if "anul nu coincide" in best[4] and "regia apare în descriere" not in best[4]:
        return None, None, f"RESPINS (anul nu coincide) #{best[1]} {best[3]['title']!r} ({best[3]['year']}); {info}"
    if need_ev and best[0] < 5:
        return None, None, f"RESPINS (căutat după ':', dovezi insuficiente) #{best[1]} {best[3]['title']!r} ({best[3]['year']}); {info}"
    if best[0] < 3:
        return None, None, f"RESPINS #{best[1]} {best[3]['title']!r} ({best[3]['year']}); {info}"
    if second and best[0] - second[0] < 1.0 and best[0] < 5:
        return None, None, f"AMBIGUU #{best[1]} vs #{second[1]}; {info}"
    return best[1], best[2], info


def imdb_ratings(ids):
    """Notele IMDb din datasets oficiale (fără cheie). Doar uz necomercial."""
    out = {}
    req = urllib.request.Request("https://datasets.imdbws.com/title.ratings.tsv.gz", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as r, gzip.GzipFile(fileobj=r) as g:
        for line in io.TextIOWrapper(g, encoding="utf-8"):
            tid, _, rest = line.partition("\t")
            if tid in ids:
                rating, _, votes = rest.rstrip("\n").partition("\t")
                out[tid] = (float(rating), int(votes))
    return out


def letterboxd(tmdb_id):
    """Citește nota din pagina publică Letterboxd (nu există API liber). Nota e pe scara 0-5."""
    req = urllib.request.Request(f"https://letterboxd.com/tmdb/{tmdb_id}/", headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=30) as r:
        page = r.read().decode("utf-8", "replace")
    m = re.search(r'"aggregateRating"\s*:\s*\{[^}]*?"ratingValue"\s*:\s*"?([0-9.]+)', page)
    return round(float(m.group(1)), 2) if m else None


def main():
    data = load(DATA, None)
    if not data:
        raise SystemExit("Lipsește docs/data/screenings.json — rulează întâi scrape.py")
    cache = load(CACHE, {"queries": {}, "movies": {}})
    over = {norm(k): v for k, v in load(OVERRIDES, {}).items()}

    use_tmdb = bool(TOKEN)
    if not TOKEN:
        print("ATENTIE: TMDB_TOKEN lipsește; filmele rămân cu datele din Eventbook.")
    else:
        try:
            api("/configuration")
            print("TMDB: autentificare reușită.")
        except urllib.error.HTTPError as e:
            use_tmdb = False
            print(f"ATENTIE: TMDB a respins tokenul (HTTP {e.code}). Verifică secretul TMDB_TOKEN: trebuie 'API Read Access Token' "
                  "(lung, începe cu eyJ) sau 'API Key' (32 de caractere), cel curent, fără ghilimele. Continuu fără TMDB.")
        except Exception as e:
            use_tmdb = False
            print("ATENTIE: nu pot contacta TMDB:", repr(e))

    titles = sorted({s["title"] for s in data["screenings"]})
    by_title = {}
    for s in data["screenings"]:
        by_title.setdefault(s["title"], []).append(s)

    errors = 0
    films = {}
    work = []
    for g in groups(titles):
        its = [s for t in g for s in by_title[t]]
        years = {str(s.get("year")) for s in its if s.get("year")}
        if len(years) >= 2:  # același titlu, filme diferite (ani diferiți în Cinema City)
            for y in sorted(years | {""}):
                part = [s for s in its if str(s.get("year") or "") == y]
                if part:
                    work.append((g, part, y))
        else:
            work.append((g, its, ""))
    for grp, items, tag in work:
        mid, kind, why = None, None, ""
        for t in grp:
            if norm(t) in over:
                mid, kind, why = (over[norm(t)] or None), "manual", ""
                break
        if use_tmdb and not mid and not any(norm(t) in over for t in grp):
            try:
                mid, kind, why = resolve(grp, items, cache)
            except Exception as e:
                errors += 1
                why = "EROARE TMDB " + repr(e)
        movie = None
        if mid:
            movie = cache["movies"].get(str(mid))
            if use_tmdb and (not movie or movie.get("v", 0) < 2 or age_days(movie.get("fetched", "")) > 14):
                try:
                    movie = cache["movies"][str(mid)] = details(mid)
                except Exception as e:
                    errors += 1
                    print("  EROARE TMDB la detalii:", mid, repr(e))
        poster_eb = next((s["poster"] for s in items if s.get("poster")), None)
        label = " | ".join(grp)
        if movie:
            key = f"tmdb:{mid}"
            films.setdefault(key, {"tmdb_id": mid, "title": movie["title"], "original_title": movie["original_title"],
                                   "year": movie["year"], "orig_lang": movie.get("orig_lang"), "poster": movie["poster"] or poster_eb,
                                   "directors": movie["directors"], "imdb_id": movie["imdb_id"],
                                   "runtime": movie["runtime"], "source": "tmdb"})
            print(f"  {kind:12} {label!r} -> #{mid} {movie['title']!r} ({movie['year']}) regia: {', '.join(movie['directors']) or '-'}  [{why}]")
        else:
            key = "t:" + norm(sorted(grp, key=lambda t: ("/" in t, len(t)))[0]) + (f"|{tag}" if tag else "")
            single = [t for t in grp if not parts(t) or len(parts(t)) == 1]
            if single:
                title = single[0]
                other = [p for t in grp for p in parts(t) if norm(p) != norm(title)]
            else:
                p = parts(grp[0])
                title, other = (p[-1], p[:-1]) if p else (grp[0], [])
            films.setdefault(key, {"tmdb_id": None, "title": title, "original_title": other[0] if other else None,
                                   "year": None, "orig_lang": None, "poster": poster_eb, "directors": [], "imdb_id": None,
                                   "runtime": None, "source": "eventbook"})
            print(f"  NEPOTRIVIT   {label!r}   [{why}]   cheie overrides.json: {norm(grp[0])!r}")
        for s in items:
            s["film"] = key

    # note IMDb (datasets) și, opțional, Letterboxd
    ids = {f["imdb_id"] for f in films.values() if f.get("imdb_id")}
    if ids:
        try:
            r = imdb_ratings(ids)
            for f in films.values():
                if f.get("imdb_id") in r:
                    f["imdb_rating"], f["imdb_votes"] = r[f["imdb_id"]]
            print(f"IMDb: note găsite pentru {len(r)} din {len(ids)} filme.")
        except Exception as e:
            print("IMDb: nu am putut citi notele:", repr(e))
    if USE_LB:
        blocked = False
        for f in films.values():
            if not f.get("tmdb_id") or blocked:
                continue
            m = cache["movies"].setdefault(str(f["tmdb_id"]), {})
            if age_days(m.get("lb_ts", "")) > 7:
                try:
                    m["lb"], m["lb_ts"] = letterboxd(f["tmdb_id"]), now()
                    time.sleep(2)
                except urllib.error.HTTPError as e:
                    print("Letterboxd: răspuns", e.code, "- opresc citirea notelor.")
                    blocked = e.code in (403, 429)
                except Exception as e:
                    print("Letterboxd: eroare", repr(e))
            if m.get("lb"):
                f["lb_rating"] = m["lb"]
        print("Letterboxd: note găsite pentru", sum(1 for f in films.values() if f.get("lb_rating")), "filme.")

    for s in data["screenings"]:
        s.pop("desc", None)
    data["films"] = films
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), "utf-8")
    n_t = sum(1 for f in films.values() if f["source"] == "tmdb")
    print(f"\nFilme: {len(films)} (TMDB: {n_t}, doar Eventbook: {len(films) - n_t}) | erori TMDB: {errors}")


if __name__ == "__main__":
    main()

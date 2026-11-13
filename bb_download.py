#!/usr/bin/env python3
"""University of Sharjah Blackboard Ultra - course material downloader.

Session cookies come from the file named in env BB_COOKIES (Netscape
cookies.txt or a raw `Cookie:` header string / file).

Endpoints used (all under https://elearning.sharjah.ac.ae):
  GET /learn/api/v1/users/me                                   -> user id
  GET /learn/api/v1/users/{uid}/memberships?expand=course&limit=10000
  GET /learn/api/v1/terms                                      -> term names
  GET /learn/api/public/v1/terms/{termId}                      (fallback)
  GET /learn/api/public/v1/courses/{cid}/contents?limit=200    (paged)
  GET /learn/api/public/v1/courses/{cid}/contents/{id}/children
  GET /learn/api/public/v1/courses/{cid}/contents/{id}/attachments
  GET /learn/api/public/v1/courses/{cid}/contents/{id}/attachments/{aid}/download
      (302 -> signed Xythos/S3 URL; the redirect target must NOT get cookies)

Usage:
  BB_COOKIES=cookies.txt python api_client.py --dry-run
  python api_client.py --term "Fall2026" --course "Micro" --out "D:/Sharjah Courses"
"""
from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE = "https://elearning.sharjah.ac.ae"
DEFAULT_OUT = "D:/Sharjah Courses"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
FOLDER_HANDLERS = {"resource/x-bb-folder", "resource/x-bb-lesson"}
_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(*a, flush=True)


# ----------------------------------------------------------------- cookies
def load_cookies(path: str) -> dict[str, str]:
    """Parse Netscape cookies.txt or a raw Cookie header (file or literal)."""
    text = Path(path).read_text(encoding="utf-8", errors="ignore") if os.path.exists(path) else path
    cookies: dict[str, str] = {}
    lines = [l for l in text.splitlines() if l.strip()]
    if any("\t" in l for l in lines):  # Netscape format
        for line in lines:
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and "sharjah" in parts[0]:
                cookies[parts[5]] = parts[6].strip()
        return cookies
    raw = " ".join(lines)
    raw = re.sub(r"^\s*cookie:\s*", "", raw, flags=re.I)
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


# ----------------------------------------------------------------- helpers
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str, default: str = "untitled", maxlen: int = 120) -> str:
    """Sanitize for Windows filesystems."""
    name = html.unescape(name or "").strip()
    name = _BAD.sub("_", name)
    name = re.sub(r"\s+", " ", name).rstrip(" .")
    if not name:
        name = default
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > maxlen:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: maxlen - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            name = name[:maxlen].rstrip(" .")
    return name


def unique_path(path: Path, used: set[str]) -> Path:
    cand, i = path, 1
    while str(cand).lower() in used:
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        i += 1
    used.add(str(cand).lower())
    return cand


class BB:
    def __init__(self, cookies: dict[str, str]):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json, */*"})
        for k, v in cookies.items():
            self.s.cookies.set(k, v, domain="elearning.sharjah.ac.ae", path="/")

    def request(self, method: str, url: str, retries: int = 5, **kw) -> requests.Response:
        if url.startswith("/"):
            url = BASE + url
        last: Exception | None = None
        for i in range(retries):
            try:
                r = self.s.request(method, url, timeout=60, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"{r.status_code}", response=r)
                return r
            except (requests.RequestException,) as e:
                last = e
                time.sleep(min(30, 2 ** i))
        raise RuntimeError(f"failed after {retries} tries: {url}: {last}")

    def get_json(self, url: str, **kw) -> dict:
        r = self.request("GET", url, **kw)
        if r.status_code == 403 and "/courses/" in url:
            log(f"  ! 403 (no access / course unavailable): {url.split('?')[0]}")
            return {}
        if r.status_code in (401, 403):
            raise SystemExit(f"HTTP {r.status_code} for {url}: session cookies expired/invalid. "
                             "Re-export cookies from your logged-in browser.")
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()

    def paged(self, url: str) -> list[dict]:
        out: list[dict] = []
        sep = "&" if "?" in url else "?"
        nxt = f"{url}{sep}limit=200"
        while nxt:
            j = self.get_json(nxt)
            out += j.get("results", [])
            nxt = (j.get("paging") or {}).get("nextPage")
        return out

    # ---- API
    def me(self) -> str:
        return self.get_json("/learn/api/v1/users/me")["id"]

    def terms(self) -> dict[str, str]:
        j = self.get_json("/learn/api/v1/terms")
        return {t["id"]: t["name"] for t in j.get("results", [])}

    def memberships(self) -> list[dict]:
        uid = self.me()
        j = self.get_json(f"/learn/api/v1/users/{uid}/memberships?expand=course&limit=10000")
        return j.get("results", [])

    def children(self, cid: str, item_id: str | None) -> list[dict]:
        if item_id is None:
            return self.paged(f"/learn/api/public/v1/courses/{cid}/contents")
        return self.paged(f"/learn/api/public/v1/courses/{cid}/contents/{item_id}/children")

    def attachments(self, cid: str, item_id: str) -> list[dict]:
        return self.get_json(f"/learn/api/public/v1/courses/{cid}/contents/{item_id}/attachments"
                             ).get("results", [])


# ---------------------------------------------------------------- manifest
BBFILE_RE = re.compile(r'<a\s[^>]*?href="([^"]+)"[^>]*?data-bbfile="([^"]+)"', re.S)
LINK_RE = re.compile(r'<a\s[^>]*?href="(https?://[^"]+)"[^>]*>(.*?)</a>', re.S)


def build_manifest(bb: BB, course: dict, base_dir: Path) -> tuple[list[dict], list[dict]]:
    """Walk a course. Returns (downloads, links)."""
    cid = course["id"]
    downloads: list[dict] = []
    links: list[dict] = []
    used: set[str] = set()

    def walk(item_id: str | None, folder: Path, trail: list[str]):
        for it in bb.children(cid, item_id):
            title = it.get("title") or "untitled"
            h = (it.get("contentHandler") or {}).get("id", "")
            iid = it["id"]
            if h in FOLDER_HANDLERS or it.get("hasChildren"):
                walk(iid, folder / safe_name(title), trail + [title])
                if h in FOLDER_HANDLERS:
                    continue
            if h == "resource/x-bb-externallink":
                links.append({"title": title, "url": it["contentHandler"].get("url", ""),
                              "trail": trail, "kind": "link"})
                continue
            if h == "resource/x-bb-courselink":
                continue
            # attachments (files, and any item that has attached files)
            atts: list[dict] = []
            if h == "resource/x-bb-file" or h.startswith("resource/x-bb-asmt") \
                    or h in ("resource/x-bb-document", "resource/x-bb-blti-link") or True:
                try:
                    atts = bb.attachments(cid, iid)
                except requests.HTTPError as e:
                    if e.response is None or e.response.status_code != 400:  # 400 = item type has no attachments
                        log(f"  ! attachments failed {iid}: {e}")
                except Exception as e:  # noqa
                    log(f"  ! attachments failed {iid}: {e}")
            for a in atts:
                fname = safe_name(a.get("fileName") or title)
                dest = unique_path(folder / fname, used)
                downloads.append({
                    "url": f"{BASE}/learn/api/public/v1/courses/{cid}/contents/{iid}/attachments/{a['id']}/download",
                    "path": dest, "title": title})
            # files embedded in rich text bodies (documents / assessments)
            body = (it.get("body") or "") + " " + ((it.get("contentHandler") or {}).get("instructions") or "")
            for href, meta in BBFILE_RE.findall(body):
                try:
                    m = json.loads(html.unescape(meta))
                except Exception:
                    continue
                dest = unique_path(folder / safe_name(m.get("fileName") or m.get("linkName") or "file"), used)
                downloads.append({"url": html.unescape(href), "path": dest, "title": title})
            for href, text in LINK_RE.findall(body):
                href = html.unescape(href)
                if "bbcswebdav" in href or "/sessions/" in href:
                    continue
                links.append({"title": re.sub(r"<[^>]+>", "", text).strip() or title,
                              "url": href, "trail": trail + [title], "kind": "embedded link"})
            if h in ("resource/x-plugin-scormengine",) or "video" in h or "mediasite" in h:
                links.append({"title": title, "url": f"{BASE}/ultra/courses/{cid}/outline",
                              "trail": trail, "kind": f"non-downloadable ({h})"})

    walk(None, base_dir, [])
    return downloads, links


# --------------------------------------------------------------- download
def download_one(bb: BB, d: dict) -> str:
    path: Path = d["path"]
    if path.exists() and path.stat().st_size > 0:
        return "skip"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    last = None
    for i in range(5):
        try:
            url = d["url"]
            # follow redirects manually: signed storage URLs must not receive cookies
            for _ in range(6):
                same = urlparse(url).netloc == urlparse(BASE).netloc
                if same:
                    r = bb.s.get(url, stream=True, timeout=120, allow_redirects=False)
                else:
                    r = requests.get(url, stream=True, timeout=120, headers={"User-Agent": UA})
                if r.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, r.headers["Location"])
                    r.close()
                    continue
                break
            if r.status_code == 401 and same:
                raise SystemExit("Session expired (401/403). Re-export cookies.")
            if r.status_code == 404:
                return "missing"
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(str(r.status_code))
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
            tmp.replace(path)
            return "ok"
        except SystemExit:
            raise
        except Exception as e:  # noqa
            last = e
            time.sleep(min(30, 2 ** i))
    return f"error: {last}"


def write_links_index(course_dir: Path, course_name: str, links: list[dict]):
    if not links:
        return
    course_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# {course_name} - links & non-downloadable items\n"]
    seen = set()
    for l in links:
        key = (l["url"], l["title"])
        if key in seen:
            continue
        seen.add(key)
        where = " / ".join(l["trail"]) or "(root)"
        lines.append(f"- [{l['title']}]({l['url']})  \n  _{l['kind']}_ - {where}")
    (course_dir / "_links_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# -------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download Blackboard course material")
    ap.add_argument("--term", action="append", help="term name substring (repeatable, case-insens.)")
    ap.add_argument("--course", action="append", help="course name/id substring (repeatable)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output root")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="print manifest only")
    ap.add_argument("--cookies", default=os.environ.get("BB_COOKIES"),
                    help="cookie file/header (default env BB_COOKIES)")
    ap.add_argument("--list-courses", action="store_true", help="only list courses/terms")
    a = ap.parse_args(argv)
    if not a.cookies:
        ap.error("set env BB_COOKIES (or --cookies) to a cookies.txt / raw Cookie header file")
    cookies = load_cookies(a.cookies)
    if not cookies:
        ap.error("no cookies parsed")
    bb = BB(cookies)
    terms = bb.terms()
    courses = []
    for m in bb.memberships():
        c = m.get("course") or {}
        if not c or c.get("isOrganization"):
            continue
        tname = (c.get("term") or {}).get("name") or terms.get(c.get("termId", ""), "") or "NoTerm"
        courses.append({"id": c["id"], "courseId": c.get("courseId", ""), "name": c.get("name", c["id"]),
                        "term": tname})
    tf = [t.lower() for t in a.term or []]
    cf = [t.lower() for t in a.course or []]
    sel = [c for c in courses
           if (not tf or any(t in c["term"].lower() for t in tf))
           and (not cf or any(t in (c["name"] + " " + c["courseId"] + " " + c["id"]).lower() for t in cf))]
    log(f"{len(courses)} courses, {len(sel)} selected")
    if a.list_courses:
        for c in sel:
            log(f"{c['term']:16} {c['id']:12} {c['courseId']} | {c['name']}")
        return 0

    root = Path(a.out)
    total = ok = skipped = failed = 0
    for c in sel:
        cname = safe_name(f"{c['courseId'].split('_')[0]} {c['name']}" if c["courseId"] else c["name"], maxlen=80)
        cdir = root / safe_name(c["term"]) / cname
        log(f"\n== [{c['term']}] {c['name']} ({c['id']})")
        try:
            downloads, links = build_manifest(bb, c, cdir)
        except SystemExit:
            raise
        except Exception as e:  # noqa
            log(f"  ! failed to walk course: {e}")
            continue
        for d in downloads:
            log(f"  FILE {d['path']}")
        for l in links:
            log(f"  LINK {l['title']} -> {l['url']}")
        if a.dry_run:
            continue
        write_links_index(cdir, c["name"], links)
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(download_one, bb, d): d for d in downloads}
            for f in as_completed(futs):
                res = f.result()
                total += 1
                if res == "ok":
                    ok += 1
                elif res == "skip":
                    skipped += 1
                else:
                    failed += 1
                    log(f"  ! {futs[f]['path'].name}: {res}")
    if not a.dry_run:
        log(f"\nDone: {ok} downloaded, {skipped} skipped, {failed} failed, of {total}")
    return 1 if (failed and not a.dry_run) else 0


if __name__ == "__main__":
    sys.exit(main())

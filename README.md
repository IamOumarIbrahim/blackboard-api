# blackboard-api

Downloads all course material from a Blackboard Ultra site (built against
`elearning.sharjah.ac.ae`) using your own logged-in session. No browser is needed
at runtime. The client was generated with
[reverse-api-engineer](https://github.com/kalil0321/reverse-api-engineer) from captured
traffic (`prompt.txt` is the prompt used), then lightly patched.

## Output layout

```
<out>/<Semester>/<CourseNo Course Name>/<content folders...>/<file>
```

Each course also gets `_links_index.md` listing external links and items that cannot be
downloaded (videos, SCORM).

## Setup

```
python -m venv .venv
.venv\Scripts\pip install requests
```

## Authenticate (cookies)

Log in to Blackboard in your browser, then either:

1. DevTools → Network → right-click a `memberships?...` request → Copy → *Copy as cURL (bash)*,
   save it as `curl.txt`, then run `python curl_to_cookies.py curl.txt cookies.txt`; or
2. save the request's raw `Cookie:` header in `cookies.txt`.

`cookies.txt` and `curl.txt` hold your live session. They are git-ignored; never commit
or share them. The session lasts about 3 hours; on 401 the script stops and asks you to
re-export.

## Usage

```
set BB_COOKIES=cookies.txt
python bb_download.py --list-courses
python bb_download.py --dry-run --term Fall2026
python bb_download.py --out "D:/Sharjah Courses" --workers 4
python bb_download.py --term Spr2024 --course "Digital Logic"
```

Options: `--term`, `--course` (repeatable substrings), `--out`, `--workers`, `--dry-run`,
`--list-courses`, `--cookies`. Re-running skips files already downloaded and retries with
backoff. Courses/files returning 403 (unavailable courses) are logged and skipped.

Use only on your own account and respect your institution's acceptable-use policy.

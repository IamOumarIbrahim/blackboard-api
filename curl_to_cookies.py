"""Extract the Cookie header from a 'Copy as cURL' file into cookies.txt.

Usage: python curl_to_cookies.py curl.txt cookies.txt
Prints only the cookie NAMES (never the values).
"""
import re
import sys

src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8", errors="ignore").read().replace("^", "")
m = re.search(r"""(?:-b|--cookie)\s+(['"])(.*?)\1""", text, re.S) or \
    re.search(r"""-H\s+(['"])\s*cookie:\s*(.*?)\1""", text, re.S | re.I)
if not m:
    sys.exit("No cookie found in the cURL file - make sure you copied a request to elearning.sharjah.ac.ae")
cookie = m.group(2).strip()
names = [p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p]
open(dst, "w", encoding="utf-8").write("Cookie: " + cookie + "\n")
print(f"Wrote {dst} with {len(names)} cookies: {', '.join(names)}")

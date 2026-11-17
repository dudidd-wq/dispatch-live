"""Build content/hayomyom.json from the Lubavitch Library (chabadlibrary.org).

    python tools/build_hayomyom.py  [out.json]

Hayom Yom - all rights reserved to Kehot Publication Society. Used with
permission, for non-commercial use only (see content/NOTICE.md).

The library's book tree: Hayom Yom (section 3700000000) has the months of
5703-5704 in book order (Kislev ... Kislev), each with a page per day whose
heading starts with the day in Hebrew numerals ("יט כסלו", "א טבת ר"ח ...").
Output keys are Hebcal's date names ("19 Kislev"), as the Shabbat band asks.
Standard library only; one request at a time, with a pause between them.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://chabadlibrary.org/books/api/main?path="
BOOK = "3700000000"
MONTHS = {"תשרי": "Tishrei", "חשון": "Cheshvan", "מרחשון": "Cheshvan", "כסלו": "Kislev", "טבת": "Tevet",
          "שבט": "Sh'vat", "אדר א": "Adar I", "אדר ב": "Adar II", "אדר": "Adar", "ניסן": "Nisan", "אייר": "Iyyar",
          "סיון": "Sivan", "תמוז": "Tamuz", "מנחם אב": "Av", "אב": "Av", "אלול": "Elul"}
VALUES = {"א": 1, "ב": 2, "ג": 3, "ד": 4, "ה": 5, "ו": 6, "ז": 7, "ח": 8, "ט": 9, "י": 10, "כ": 20, "ל": 30}


def get(path):
    """The library always answers in brotli, which the standard library cannot
    read: use the brotli package if installed, otherwise curl --compressed."""
    url = API + urllib.parse.quote(str(path))
    for attempt in range(4):
        try:
            try:
                import brotli
                req = urllib.request.Request(url, headers={"User-Agent": "dispatch-live/hayomyom-build",
                                                           "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    if (r.headers.get("Content-Encoding") or "").lower() == "br":
                        raw = brotli.decompress(raw)
            except ImportError:
                import subprocess
                raw = subprocess.run(["curl", "-s", "--compressed", "--max-time", "30", "-A",
                                      "dispatch-live/hayomyom-build", url], capture_output=True, check=True).stdout
            return json.loads(raw.decode("utf-8"))
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


def plain(s):
    return re.sub(r"<[^>]+>", "", str(s or "")).strip()


def day_number(heading):
    first = plain(heading).split()[0] if plain(heading) else ""
    n = sum(VALUES.get(ch, 0) for ch in re.sub(r"[^א-ת]", "", first))
    return n if 1 <= n <= 30 else None


def month_name(heading):
    h = " ".join(re.sub(r"[^א-ת ]", "", plain(heading)).split())
    return MONTHS.get(h)


def build():
    out = {}
    for month in get(BOOK)["content"]["data"]:
        name = month_name(month["heading"])
        if not name:
            raise SystemExit("unknown month heading: %r" % plain(month["heading"]))
        time.sleep(0.3)
        prev = 0
        for day in get(month["id"])["content"]["data"]:
            n = day_number(day["heading"])
            if not n:
                print("  skipped a page without a day number: %r" % plain(day["heading"])[:30])
                continue
            if n <= prev:
                # a heading repeated by mistake ("כז אלול" twice): the pages
                # are in order, so this one is the next day
                print("  %s: day %d repeated, taken as %d" % (name, n, prev + 1))
                n = prev + 1
            prev = n
            time.sleep(0.3)
            page = get(day["id"])["content"]["data"]
            text = "\n".join(line.strip() for line in plain(page.get("text")).splitlines() if line.strip())
            if text:
                out["%d %s" % (n, name)] = text
        print("  %-8s done (%d days so far)" % (name, len(out)), flush=True)
    return out


def main():
    dst = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "content", "hayomyom.json")
    data = build()
    doc = {"_source": "היום יום - ספריית ליובאוויטש, chabadlibrary.org", "_rights": "כל הזכויות שמורות להוצאת קה\"ת. "
           "בשימוש ברשות, לשימוש לא מסחרי בלבד."}
    doc.update(dict(sorted(data.items(), key=lambda kv: (list(MONTHS.values()).index(kv[0].split(" ", 1)[1]),
                                                        int(kv[0].split(" ")[0])))))
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("[OK] %d days -> %s" % (len(data), dst))


if __name__ == "__main__":
    main()

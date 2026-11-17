"""Turn the owner's own Word copy of Hayom Yom into hayomyom.json.

Usage:  python make_hayomyom.py  path\\to\\book.docx
        (or drag the .docx onto Make-HayomYom.bat)

The book is expected in month order: a Heading 1 with the month name
("תשרי", "אדר א'"), then a Heading 1 per day ("ליום ז'"), then that day's
text. The result is written next to this script and is read only by the
local server (the Shabbat band). It is never part of the repository or an
update. Standard library only.
"""
import json
import os
import re
import sys
import zipfile

MONTHS = [  # (pattern on the heading, Hebcal's name)
    (r"^תשרי$", "Tishrei"), (r"^(מר)?-?חשו?ון$", "Cheshvan"), (r"^כסלו$", "Kislev"), (r"^טבת$", "Tevet"),
    (r"^שבט$", "Sh'vat"), (r"^אדר\s*(א|ראשון)$", "Adar I"), (r"^אדר\s*(ב|שני)$", "Adar II"), (r"^אדר$", "Adar"),
    (r"^ניסן$", "Nisan"), (r"^איי?ר$", "Iyyar"), (r"^סיו?ון$", "Sivan"), (r"^תמוז$", "Tamuz"),
    (r"^(מנחם\s*)?אב$", "Av"), (r"^אלול$", "Elul"),
]
VALUES = {"א": 1, "ב": 2, "ג": 3, "ד": 4, "ה": 5, "ו": 6, "ז": 7, "ח": 8, "ט": 9, "י": 10, "כ": 20, "ל": 30}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def letters(s):
    return re.sub(r"[^א-ת\s]", "", s).strip()


def month_of(heading):
    h = " ".join(letters(heading).split())
    for pat, name in MONTHS:
        if re.match(pat, h):
            return name
    return None


def day_of(heading):
    m = re.match(r"^\s*ליום\s+(.+)$", heading)
    if not m:
        return None
    n = sum(VALUES.get(ch, 0) for ch in letters(m.group(1)).replace(" ", ""))
    return n if 1 <= n <= 30 else None


def paragraphs(docx):
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(docx) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    for p in root.iter(W + "p"):
        style = p.find("./%spPr/%spStyle" % (W, W))
        st = style.get(W + "val") if style is not None else ""
        text = "".join(t.text or "" for t in p.iter(W + "t")).strip()
        yield st, text


def convert(docx):
    out, month, day, body = {}, None, None, []

    def flush():
        if month and day and body:
            out["%d %s" % (day, month)] = "\n".join(body)

    for style, text in paragraphs(docx):
        if style.lower().startswith("heading"):
            flush()
            body = []
            m = month_of(text)
            if m:
                month, day = m, None
            else:
                day = day_of(text)
        elif text and day and not style.lower().startswith("toc"):
            body.append(text)
    flush()
    return out


def main():
    if len(sys.argv) < 2:
        print("Usage: python make_hayomyom.py book.docx")
        return 2
    src = sys.argv[1]
    data = convert(src)
    if len(data) < 300:
        print("[!] Only %d days were found. The file's layout is not the expected one "
              "(a heading per month, then a heading per day such as \"ליום ז'\")." % len(data))
        return 1
    dst = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hayomyom.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)
    print("[OK] %d days written to %s" % (len(data), dst))
    full = {"Tishrei": 30, "Cheshvan": 30, "Kislev": 30, "Tevet": 29, "Sh'vat": 30, "Adar I": 30, "Adar II": 29,
            "Nisan": 30, "Iyyar": 29, "Sivan": 30, "Tamuz": 29, "Av": 30, "Elul": 29}
    for mo, n in full.items():
        miss = [d for d in range(1, n + 1) if "%d %s" % (d, mo) not in data]
        if len(miss) == n:
            print("     missing in the file: all of %s" % mo)
        elif miss and not (mo in ("Cheshvan", "Kislev") and miss == [30]):
            print("     missing in the file: %s %s" % (mo, ", ".join(map(str, miss))))
    print("     On a missing day the Shabbat band shows the link to Chabad.org instead.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""make_hayomyom: a Word file in month/day order becomes {"11 Kislev": text}.
Built from a synthetic document; no book text is used in the tests."""
import json, os, sys, tempfile, unittest, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import make_hayomyom as mk
import server

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def para(text, style=None):
    ppr = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
    return '<w:p>%s<w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (ppr, text)


def docx(paras):
    path = os.path.join(tempfile.mkdtemp(), "b.docx")
    body = "".join(paras)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document %s><w:body>%s</w:body></w:document>' % (W, body))
    return path


class TestConvert(unittest.TestCase):
    def test_months_days_and_the_table_of_contents(self):
        p = docx([para("כסלו", "TOC1"), para("כסלו", "Heading1"),
                  para("ליום א'", "Heading1"), para("sample one"), para("second line"),
                  para("ליום י\"א", "Heading1"), para("sample eleven"),
                  para("ליום כ'ז", "Heading1"), para("sample 27"),
                  para("אדר א'", "Heading1"), para("ליום ט\"ו", "Heading1"), para("adar one 15"),
                  para("מנחם אב", "Heading1"), para("ליום ל'", "Heading1"), para("av 30")])
        d = mk.convert(p)
        self.assertEqual(d["1 Kislev"], "sample one\nsecond line")
        self.assertEqual(d["11 Kislev"], "sample eleven")
        self.assertEqual(d["27 Kislev"], "sample 27")
        self.assertEqual(d["15 Adar I"], "adar one 15")
        self.assertEqual(d["30 Av"], "av 30")
        self.assertEqual(len(d), 5)

    def test_hebrew_day_numbers(self):
        for h, n in (("ליום א'", 1), ("ליום ט\"ו", 15), ("ליום ט\"ז", 16), ("ליום כ'ט", 29), ("ליום ל'", 30), ("ליום", None)):
            self.assertEqual(mk.day_of(h), n, h)


class TestCommonYearAdar(unittest.TestCase):
    def test_adar_in_a_common_year_reads_the_books_adar_two(self):
        root = server.ROOT
        try:
            server.ROOT = tempfile.mkdtemp()
            server._hayom_cache.update(key=None, data={})
            with open(os.path.join(server.ROOT, "hayomyom.json"), "w", encoding="utf-8") as f:
                json.dump({"14 Adar I": "one", "14 Adar II": "two"}, f)
            self.assertEqual(server.hayomyom_entry("14 Adar 5787")["text"], "two")
            self.assertEqual(server.hayomyom_entry("14 Adar I 5790")["text"], "one")
        finally:
            server.ROOT = root


class TestBundledAndLocal(unittest.TestCase):
    def test_the_local_file_wins_day_by_day_and_the_bundled_text_fills_the_rest(self):
        root = server.ROOT
        try:
            server.ROOT = tempfile.mkdtemp()
            server._hayom_cache.update(key=None, data={})
            os.makedirs(os.path.join(server.ROOT, "content"))
            with open(os.path.join(server.ROOT, "content", "hayomyom.json"), "w", encoding="utf-8") as f:
                json.dump({"_rights": "x", "1 Elul": "bundled elul", "11 Kislev": "bundled kislev"}, f)
            self.assertEqual(server.hayomyom_entry("1 Elul 5787")["text"], "bundled elul")
            self.assertIsNone(server.hayomyom_entry("_rights"))
            with open(os.path.join(server.ROOT, "hayomyom.json"), "w", encoding="utf-8") as f:
                json.dump({"11 Kislev": "local kislev"}, f)
            self.assertEqual(server.hayomyom_entry("11 Kislev 5787")["text"], "local kislev")
            self.assertEqual(server.hayomyom_entry("1 Elul 5787")["text"], "bundled elul")
        finally:
            server.ROOT = root

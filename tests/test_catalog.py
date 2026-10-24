"""Offline catalog tests use only synthetic note inventories."""
import hashlib
import html
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from wizbackup.catalog import finish_catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "metadata").mkdir()
        (self.root / "records").mkdir()
        self.notes = []

    def tearDown(self):
        self.temp.cleanup()

    def save(self, path, value):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def add_note(self, guid, *, title="Note", category="/", md="body", reading_html="<p>body</p>", folder=None, status="exported"):
        note = {"docGuid": guid, "title": title, "category": category, "version": 1}
        self.notes.append(note)
        raw = self.root / "objects" / guid / "1" / "original.ziw"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"synthetic-original-" + guid.encode())
        paths = [raw]
        relative = folder or "readable/" + guid
        dest = self.root / relative
        if status == "exported":
            dest.mkdir(parents=True)
            (dest / "note.md").write_text(md, encoding="utf-8")
            (dest / "index.html").write_text(reading_html, encoding="utf-8")
            original = dest / "original" / "immutable" / "index.html"
            original.parent.mkdir(parents=True)
            original.write_text("<p>original reading extraction</p>")
            paths += [dest / "note.md", dest / "index.html", original]
        record = {"guid": guid, "version": 1, "status": status, "files": [
            {"path": path.relative_to(self.root).as_posix(), "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in paths
        ]}
        if status == "exported":
            record["readable_path"] = relative
        self.save("records/" + guid + ".json", record)
        self.save("metadata/notes.json", self.notes)
        return dest, raw

    def test_links_catalog_search_and_idempotent_checksum_updates(self):
        md = ('---\nsource: "wiz://open_document?guid=b"\n---\n\n'
              '[B](wiz://open_document?guid=b#section)\n'
              '[B two](wiz:open_document?guid={b})\n'
              '[B web](https://synthetic-ks.wiz.cn/ks/note/view/kb/b#段落%20one)\n'
              '[External](https://example.com/ks/note/view/kb/b)\n'
              '[Locked](wiz://open_document?guid=c)\n'
              '`wiz://open_document?guid=b`\n\n````md\n[B](wiz://open_document?guid=b)\n````\n')
        source, raw = self.add_note("a", title='<img src=x onerror="alert(1)"> & A', category="/学习/项目/", md=md,
                                    reading_html='<a href="wiz://open_document?guid=b&amp;kbguid=kb#section">B</a><a href="https://example.com/">external</a>')
        target, _ = self.add_note("b", title="B #1", category="/学习/文档 空间/", folder="readable/文档 空间/B #1")
        self.add_note("c", title="Locked", status="locked")
        raw_before = raw.read_bytes()
        extraction = source / "original/immutable/index.html"
        extracted_before = extraction.read_bytes()
        result = finish_catalog(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(3, result["notes"])
        self.assertEqual(2, result["readable_notes"])
        self.assertEqual(2, result["rewritten_files"])
        self.assertEqual(1, result["updated_records"])
        self.assertEqual(4, result["rewritten_links"])
        self.assertEqual(1, result["unresolved_links"])
        relative = quote(os.path.relpath(target / "note.md", source).replace(os.sep, "/"), safe="/")
        updated = (source / "note.md").read_text()
        self.assertIn(f"[B]({relative}#section)", updated)
        self.assertIn(f"[B two]({relative})", updated)
        self.assertIn("#%E6%AE%B5%E8%90%BD%20one)", updated)
        self.assertIn('[External](https://example.com/ks/note/view/kb/b)', updated)
        self.assertIn('[Locked](wiz://open_document?guid=c)', updated)
        self.assertIn('source: "wiz://open_document?guid=b"', updated)
        self.assertIn('`wiz://open_document?guid=b`', updated)
        self.assertIn('[B](wiz://open_document?guid=b)\n````', updated)
        self.assertIn("index.html#section", (source / "index.html").read_text())
        self.assertEqual(raw_before, raw.read_bytes())
        self.assertEqual(extracted_before, extraction.read_bytes())
        record = json.loads((self.root / "records/a.json").read_text())
        for item in record["files"]:
            path = self.root / item["path"]
            self.assertEqual(path.stat().st_size, item["size"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
        page = (self.root / "index.html").read_text()
        self.assertIn("搜索笔记标题", page)
        self.assertIn("class=\"folder\"", page)
        self.assertIn("%20", page)
        self.assertIn("%23", page)
        self.assertIn(html.escape('<img src=x onerror="alert(1)"> & A'), page)
        self.assertNotIn('<img src=x onerror="alert(1)">', page)
        self.assertIn("Content-Security-Policy", page)
        self.assertIn("connect-src &#x27;none&#x27;", page)
        catalog = json.loads((self.root / "catalog.json").read_text())
        self.assertEqual("readable/文档 空间/B #1/note.md", catalog["b"]["md"])
        self.assertIsNone(catalog["c"]["md"])
        record_before = (self.root / "records/a.json").read_bytes()
        page_before = (self.root / "index.html").read_bytes()
        second = finish_catalog(self.root)
        self.assertTrue(second["ok"])
        self.assertEqual(0, second["rewritten_files"])
        self.assertEqual(0, second["updated_records"])
        self.assertEqual(record_before, (self.root / "records/a.json").read_bytes())
        self.assertEqual(page_before, (self.root / "index.html").read_bytes())

    def test_corrupt_original_aborts_whole_batch_without_blessing_new_hash(self):
        source, raw = self.add_note("a", md="[B](wiz://open_document?guid=b)")
        self.add_note("b")
        before = (source / "note.md").read_bytes()
        record = (self.root / "records/a.json").read_bytes()
        raw.write_bytes(b"damaged original")
        result = finish_catalog(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any(problem["kind"] == "checksum_mismatch" for problem in result["problems"]))
        self.assertEqual(before, (source / "note.md").read_bytes())
        self.assertEqual(record, (self.root / "records/a.json").read_bytes())
        self.assertEqual(b"damaged original", raw.read_bytes())
        self.assertFalse((self.root / "index.html").exists())
        self.assertFalse((self.root / "catalog.json").exists())

    def test_unregistered_reading_copy_is_not_given_a_new_checksum(self):
        source, _ = self.add_note("a", md="[B](wiz://open_document?guid=b)")
        self.add_note("b")
        record = json.loads((self.root / "records/a.json").read_text())
        record["files"] = [item for item in record["files"] if not item["path"].endswith("note.md")]
        self.save("records/a.json", record)
        result = finish_catalog(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual("reading_copy_not_uniquely_registered", result["problems"][0]["kind"])
        self.assertIn("wiz://", (source / "note.md").read_text())

    def test_can_build_index_without_changing_reading_copies(self):
        source, _ = self.add_note("a", md="[B](wiz://open_document?guid=b)")
        self.add_note("b")
        before = (source / "note.md").read_bytes()
        record = (self.root / "records/a.json").read_bytes()
        result = finish_catalog(self.root, apply_wiz_links=False)
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["rewritten_files"])
        self.assertEqual(before, (source / "note.md").read_bytes())
        self.assertEqual(record, (self.root / "records/a.json").read_bytes())

    def test_external_urls_and_other_wiz_commands_are_unchanged(self):
        body = ('[one](https://wiz.cn.attacker.example/ks/note/view/kb/b)\n'
                '[two](https://wiz.cn@attacker.example/ks/note/view/kb/b)\n'
                '[three](wiz://open_attachment?guid=b)\n')
        source, _ = self.add_note("a", md=body)
        self.add_note("b")
        result = finish_catalog(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["rewritten_links"])
        self.assertEqual(body, (source / "note.md").read_text())

    def test_autolinks_reference_links_and_uri_labels_remain_valid_on_rerun(self):
        body = ('<wiz://open_document?guid=b#section>\n'
                '[angle](<wiz:open_document?guid=b>)\n'
                '[reference]: wiz://open_document?guid=b "Title"\n'
                '[wiz://open_document?guid=b](https://example.com/)\n')
        source, _ = self.add_note("a", md=body)
        self.add_note("b")
        result = finish_catalog(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(3, result["rewritten_links"])
        updated = (source / "note.md").read_text()
        self.assertIn('[wiz://open_document?guid=b#section](../b/note.md#section)', updated)
        self.assertIn('[angle](<../b/note.md>)', updated)
        self.assertIn('[reference]: ../b/note.md "Title"', updated)
        self.assertIn('[wiz://open_document?guid=b](https://example.com/)', updated)
        again = finish_catalog(self.root)
        self.assertTrue(again["ok"])
        self.assertEqual(0, again["rewritten_files"])
        self.assertEqual(updated, (source / "note.md").read_text())

    def test_readable_directory_symlink_cannot_redirect_writes_to_objects(self):
        source, raw = self.add_note("a", md="[B](wiz://open_document?guid=b)")
        self.add_note("b")
        alternate = self.root / "readable/alias"
        alternate.symlink_to(raw.parent, target_is_directory=True)
        record = json.loads((self.root / "records/a.json").read_text())
        record["readable_path"] = "readable/alias"
        self.save("records/a.json", record)
        result = finish_catalog(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("wiz://", (source / "note.md").read_text())
        self.assertFalse((self.root / "catalog.json").exists())

    def test_non_readable_path_is_refused_without_mutating_raw_data(self):
        self.add_note("a")
        record = json.loads((self.root / "records/a.json").read_text())
        record["readable_path"] = "objects/a/1"
        self.save("records/a.json", record)
        result = finish_catalog(self.root)
        self.assertFalse(result["ok"])
        self.assertFalse((self.root / "catalog.json").exists())

    def test_empty_inventory_has_a_searchable_empty_index(self):
        self.save("metadata/notes.json", [])
        result = finish_catalog(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["notes"])
        self.assertEqual({}, json.loads((self.root / "catalog.json").read_text()))
        self.assertIn("没有匹配的笔记", (self.root / "index.html").read_text())

    def test_empty_categories_are_kept_in_directory_navigation(self):
        self.add_note("a", category="/学习/")
        self.save("metadata/categories.json", {"result": ["/学习/", "/空目录/子目录/"]})
        result = finish_catalog(self.root)
        self.assertTrue(result["ok"])
        page = (self.root / "index.html").read_text()
        self.assertIn('data-category="/空目录/"', page)
        self.assertIn('data-category="/空目录/子目录/"', page)
        self.assertIn("子目录<span>0</span>", page)


if __name__ == "__main__":
    unittest.main()

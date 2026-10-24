"""Exporter security and fidelity tests; no real accounts or private data."""
import hashlib
import html
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from wizbackup.catalog import finish_catalog
from wizbackup.exporter import export_note, safe_filename


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = self.root / "sample.ziw"
        self.dest = self.root / "export"

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, members):
        with zipfile.ZipFile(self.raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members:
                archive.writestr(name, content)

    def test_offline_html_and_markdown_preserve_resources_and_original(self):
        self.archive([
            ("index.html", '<html><head><title>T</title></head><body><h1>你好</h1><img src="index_files/pic.png"><a href="index_files/file.pdf">附件</a><p><strong>粗体</strong></p></body></html>'),
            ("index_files/pic.png", b"\x89PNG sample"),
            ("index_files/file.pdf", b"%PDF sample"),
            ("meta.xml", "<meta><title>你好</title></meta>"),
        ])
        original_hash = hashlib.sha256(self.raw.read_bytes()).hexdigest()
        result = export_note(self.raw, self.dest, {"guid": "sample", "title": "你好", "category": "/学习/", "tags": ["tag"]})
        self.assertEqual("exported", result["status"])
        self.assertEqual(original_hash, hashlib.sha256(self.raw.read_bytes()).hexdigest())
        self.assertEqual(3, result["resource_count"])
        self.assertEqual([], result["missing_resources"])
        self.assertTrue((self.dest / result["member_paths"]["index_files/pic.png"]).read_bytes().startswith(b"\x89PNG"))
        reading_html = (self.dest / "index.html").read_text()
        self.assertIn('http-equiv="Content-Security-Policy"', reading_html)
        self.assertIn(result["member_paths"]["index_files/pic.png"], reading_html)
        markdown = (self.dest / "note.md").read_text()
        self.assertIn("# 你好", markdown)
        self.assertIn("**粗体**", markdown)
        self.assertIn('category: "/学习/"', markdown)

    def test_native_markdown_content_and_code_survive(self):
        body = "# 原文\n\n- [x] 任务\n\n```python\nprint('你好')\n# ![example](images/pic.png)\n```\n\n`![example](images/pic.png)`\n\n![图](images/pic.png)\n"
        self.archive([("index.md", body), ("images/pic.png", b"PNG")])
        result = export_note(self.raw, self.dest, {"title": "标题"})
        self.assertEqual("exported", result["status"])
        original = self.dest / result["body_original"]
        self.assertEqual(body, original.read_text())
        markdown = (self.dest / "note.md").read_text()
        self.assertIn("# ![example](images/pic.png)", markdown)
        self.assertIn("`![example](images/pic.png)`", markdown)
        self.assertIn(result["member_paths"]["images/pic.png"], markdown)

    def test_embedded_markdown_is_not_html_roundtripped(self):
        self.archive([("index.html", '<html><body><pre id="wiz-code"># 标题\n\n```sql\nselect &lt; 3;\n```</pre></body></html>')])
        result = export_note(self.raw, self.dest, {})
        self.assertEqual("exported", result["status"])
        markdown = (self.dest / "note.md").read_text()
        self.assertIn("```sql\nselect < 3;\n```", markdown)

    def test_explicit_html_anchors_survive_catalog_self_and_cross_note_links(self):
        library = self.root / "library"
        (library / "metadata").mkdir(parents=True)
        (library / "records").mkdir()
        notes = []
        originals = {}
        for guid, body in (
            ("a", '<body><h2 id="demo">Demo</h2><p id="wiz://open_document?guid=b">URI-like target</p>'
             '<p><a href="wiz://open_document?guid=a#demo">Self</a> '
             '<a href="wiz://open_document?guid=b#target">Other note</a></p></body>'),
            ("b", '<body><h2 id="target">Target heading</h2><a name="legacy"></a><p>Target body</p></body>'),
        ):
            note = {"docGuid": guid, "title": guid, "category": "/", "version": 1}
            notes.append(note)
            raw = library / "objects" / guid / "1/content.ziw"
            raw.parent.mkdir(parents=True)
            with zipfile.ZipFile(raw, "w") as archive:
                archive.writestr("index.html", body)
            originals[raw] = raw.read_bytes()
            dest = library / "readable" / guid
            result = export_note(raw, dest, note)
            self.assertEqual("exported", result["status"])
            self.assertEqual(body, (dest / result["body_original"]).read_text())
            record = {"guid": guid, "version": 1, "status": "exported", "readable_path": "readable/" + guid,
                      "files": [{"path": path.relative_to(library).as_posix(), "size": path.stat().st_size,
                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                for path in [raw, *sorted(p for p in dest.rglob("*") if p.is_file())]]}
            (library / "records" / (guid + ".json")).write_text(json.dumps(record))
        (library / "metadata/notes.json").write_text(json.dumps(notes))
        self.assertTrue(finish_catalog(library)["ok"])
        source = (library / "readable/a/note.md").read_text()
        self.assertIn("[Self](note.md#demo)", source)
        self.assertIn("[Other note](../b/note.md#target)", source)
        self.assertEqual(["demo", "wiz://open_document?guid=b"],
                         [tag["id"] for tag in BeautifulSoup(source, "html.parser").find_all("a", id=True)])
        target = (library / "readable/b/note.md").read_text()
        self.assertEqual(["target", "legacy"], [tag["id"] for tag in BeautifulSoup(target, "html.parser").find_all("a", id=True)])
        self.assertIn("## Target heading", target.replace('<a id="target"></a>', ""))
        for path, original in originals.items():
            self.assertEqual(original, path.read_bytes())

    def test_html_anchors_are_escaped_inert_and_deduplicated_in_body_order(self):
        dangerous_id = 'quote"&<img/src=x/onerror=alert(1)>|'
        self.archive([("index.html", '<html><head><title id="head">Hidden title</title></head><body>'
                       '<script id="script">alert(1)</script><p id="' + html.escape(dangerous_id, quote=True) + '">Safe body</p>'
                       '<div id="duplicate"><span id="duplicate">First target</span></div>'
                       '<a id="both" name="legacy" href="javascript:alert(1)" onclick="alert(1)">Link</a>'
                       '<a name="both"></a><p id="Case">Upper</p><p id="case">Lower</p><p id="中文">Unicode</p>'
                       '<p id="bad&#9;id">Invalid id</p></body></html>')])
        result = export_note(self.raw, self.dest, {})
        self.assertEqual("exported", result["status"])
        expected = [dangerous_id, "duplicate", "both", "legacy", "Case", "case", "中文"]
        self.assertEqual(expected, result["preserved_anchor_ids"])
        markdown = (self.dest / "note.md").read_text().split("---\n", 2)[2]
        anchors = BeautifulSoup(markdown, "html.parser").find_all("a", id=True)
        self.assertEqual(expected, [tag["id"] for tag in anchors])
        self.assertTrue(all(set(tag.attrs) == {"id"} and not tag.contents for tag in anchors))
        self.assertNotIn("javascript:", markdown)
        self.assertNotIn("<script", markdown)
        self.assertNotIn("<img/src", markdown)
        self.assertIn("&quot;", markdown)
        self.assertIn("&#124;", markdown)
        self.assertEqual(expected, json.loads((self.dest / "metadata.json").read_text())["export"]["preserved_anchor_ids"])

    def test_hidden_and_code_ids_do_not_invent_markdown_targets(self):
        self.archive([("index.html", '<body><div hidden="false" id="visible"><span id="hidden-child">Hidden</span></div>'
                       '<div style="display: none !important" id="display-hidden">Hidden</div>'
                       '<div style="visibility: hidden"><span id="visibility-hidden">Hidden</span></div>'
                       '<input type="hidden" id="input-hidden"><template><p id="template-hidden">Hidden</p></template>'
                       '<pre id="pre-id"><code id="code-id">print(1)</code></pre><kbd id="kbd-id">Ctrl</kbd>'
                       '<p id="visible">Visible target</p><ul><li id="item">Item</li></ul>'
                       '<table id="table"><tr id="row"><td id="cell">Cell</td></tr></table>'
                       '<p><a href="https://example.com"><span id="linked-text">Link</span></a></p></body>')])
        result = export_note(self.raw, self.dest, {})
        self.assertEqual(["visible", "item", "table", "row", "cell", "linked-text"], result["preserved_anchor_ids"])
        markdown = (self.dest / "note.md").read_text().split("---\n", 2)[2]
        self.assertIn('* <a id="item"></a>Item', markdown)
        self.assertIn('| <a id="cell"></a>Cell |', markdown)
        self.assertIn('<a id="linked-text"></a>[Link](https://example.com)', markdown)
        self.assertNotIn('[<a id=', markdown)
        self.assertNotIn('<a id=', markdown.split("```", 2)[1])
        self.assertEqual(1, markdown.count('<a id="visible"></a>'))

    def test_native_markdown_and_wrapped_payload_do_not_gain_html_anchors(self):
        body = '# Heading\n\n<a id="native"></a>\n\n[Self](#native)\n\n```html\n<h2 id="literal">Example</h2>\n```\n'
        for members, metadata in (
            ([("index.md", body)], {}),
            ([("index.html", '<body id="wrapper"><pre id="wiz-code">' + html.escape(body) + '</pre></body>')], {}),
            ([("index.html", '<body id="wrapper"><pre>' + html.escape(body) + '</pre></body>')], {"type": "lite/markdown"}),
            ([("index.html", '<body id="wrapper"><script type="text/markdown">' + body + '</script></body>')], {}),
        ):
            with self.subTest(metadata=metadata, member=members[0][0]):
                self.archive(members)
                before = self.raw.read_bytes()
                result = export_note(self.raw, self.dest, metadata)
                self.assertEqual("exported", result["status"])
                self.assertEqual([], result["preserved_anchor_ids"])
                self.assertEqual(body, (self.dest / "note.md").read_text().split("\n---\n\n", 1)[1])
                self.assertEqual(before, self.raw.read_bytes())

    def test_markdown_attachment_does_not_replace_html_body(self):
        self.archive([("index.html", "<p>Main body</p>"), ("index_files/example.md", "attachment text")])
        result = export_note(self.raw, self.dest, {})
        self.assertEqual("exported", result["status"])
        self.assertIn("Main body", (self.dest / "note.md").read_text())
        self.assertNotIn("attachment text", (self.dest / "note.md").read_text())

    def test_plain_pre_markdown_wrapper_uses_type_hint(self):
        body = "# Heading\n\n- [x] task\n\n```python\nprint(1 &lt; 2)\n```\n"
        self.archive([("index.html", "<html><body><pre>" + body + "</pre></body></html>")])
        result = export_note(self.raw, self.dest, {"type": "lite/markdown", "title": "Untitled"})
        self.assertEqual("exported", result["status"])
        markdown = (self.dest / "note.md").read_text()
        self.assertIn("# Heading\n\n- [x] task", markdown)
        self.assertIn("```python\nprint(1 < 2)\n```", markdown)
        self.assertNotIn("    # Heading", markdown)

    def test_plain_pre_markdown_wrapper_uses_title_hint_but_not_title_alone(self):
        self.archive([("index.html", "<body><pre># Heading\n\n**bold**</pre></body>")])
        result = export_note(self.raw, self.dest, {"title": "Note.md"})
        self.assertEqual("exported", result["status"])
        self.assertIn("# Heading\n\n**bold**", (self.dest / "note.md").read_text())
        self.archive([("index.html", "<body><h1>Actual HTML</h1><p><strong>bold</strong></p></body>")])
        result = export_note(self.raw, self.dest, {"title": "Note.md"})
        self.assertEqual("exported", result["status"])
        self.assertIn("# Actual HTML", (self.dest / "note.md").read_text())
        self.assertIn("**bold**", (self.dest / "note.md").read_text())

    def test_ordinary_pre_code_is_not_assumed_markdown(self):
        self.archive([("index.html", "<body><pre><code>print(1)</code></pre></body>")])
        result = export_note(self.raw, self.dest, {"title": "Example.md"})
        self.assertEqual("exported", result["status"])
        self.assertIn("```", (self.dest / "note.md").read_text())

    def test_path_traversal_refused_before_any_member_is_published(self):
        for unsafe in ("../escape", "/absolute", "a/../../escape", "a\\..\\escape", "C:/escape"):
            with self.subTest(unsafe=unsafe):
                self.archive([("index.html", "<p>safe</p>"), (unsafe, b"bad")])
                result = export_note(self.raw, self.dest, {})
                self.assertEqual("unsupported", result["status"])
                self.assertFalse((self.dest / "original").exists())
                self.assertFalse((self.root / "escape").exists())

    def test_symlink_member_refused(self):
        link = zipfile.ZipInfo("evil")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.raw, "w") as archive:
            archive.writestr("index.html", "<p>safe</p>")
            archive.writestr(link, "../../outside")
        self.assertEqual("unsupported", export_note(self.raw, self.dest, {})["status"])

    def test_identical_duplicate_resources_are_extracted_once_without_losing_images(self):
        with zipfile.ZipFile(self.raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("index.html", '<body><link rel="stylesheet" href="index_files/style.css"><img src="index_files/image.png"><p>Body</p></body>')
            archive.writestr("index_files/style.css", "p { color: blue; }")
            archive.writestr("index_files/image.png", b"\x89PNG synthetic image")
            with self.assertWarns(UserWarning):
                archive.writestr("index_files/style.css", "p { color: blue; }")
        before = hashlib.sha256(self.raw.read_bytes()).hexdigest()
        result = export_note(self.raw, self.dest, {})
        self.assertEqual("exported", result["status"])
        self.assertEqual(1, result["duplicate_entries"])
        self.assertEqual(2, result["resource_count"])
        self.assertEqual([], result["missing_resources"])
        self.assertEqual([], result["warnings"])
        self.assertEqual(before, hashlib.sha256(self.raw.read_bytes()).hexdigest())
        self.assertEqual("p { color: blue; }", (self.dest / result["member_paths"]["index_files/style.css"]).read_text())
        image = self.dest / result["member_paths"]["index_files/image.png"]
        self.assertEqual(b"\x89PNG synthetic image", image.read_bytes())
        self.assertIn(result["member_paths"]["index_files/image.png"], (self.dest / "index.html").read_text())
        self.assertEqual(1, json.loads((self.dest / "metadata.json").read_text())["export"]["duplicate_entries"])
        with zipfile.ZipFile(self.raw) as archive:
            self.assertEqual(4, len(archive.infolist()))

    def test_identical_duplicate_cannot_bypass_traversal_or_symlink_checks(self):
        with zipfile.ZipFile(self.raw, "w") as archive:
            archive.writestr("index.html", "<p>safe</p>")
            archive.writestr("../escape.css", "same")
            with self.assertWarns(UserWarning):
                archive.writestr("../escape.css", "same")
        result = export_note(self.raw, self.dest, {})
        self.assertEqual("unsupported", result["status"])
        self.assertFalse((self.root / "escape.css").exists())
        link = zipfile.ZipInfo("index_files/style.css")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.raw, "w") as archive:
            archive.writestr("index.html", "<p>safe</p>")
            archive.writestr("index_files/style.css", "same")
            with self.assertWarns(UserWarning):
                archive.writestr(link, "same")
        self.assertEqual("unsupported", export_note(self.raw, self.dest, {})["status"])

    def test_noncanonical_paths_cannot_alias_an_existing_member(self):
        self.archive([("index.html", "<p>safe</p>"), ("index_files/style.css", "one"), ("index_files//style.css", "two")])
        self.assertEqual("unsupported", export_note(self.raw, self.dest, {})["status"])

    def test_conflicting_duplicate_body_members_are_refused(self):
        with zipfile.ZipFile(self.raw, "w") as archive:
            archive.writestr("index.html", "<p>one</p>")
            with self.assertWarns(UserWarning):
                archive.writestr("index.html", "<p>two</p>")
        self.assertEqual("unsupported", export_note(self.raw, self.dest, {})["status"])

    def test_archive_size_limit_refused_without_extraction(self):
        self.archive([("index.html", "<p>safe</p>"), ("large.txt", "x" * 1024)])
        with patch("wizbackup.exporter.MAX_UNCOMPRESSED", 100):
            result = export_note(self.raw, self.dest, {})
        self.assertEqual("unsupported", result["status"])
        self.assertFalse((self.dest / "original").exists())

    def test_case_collisions_and_explicit_directory_after_file(self):
        self.archive([
            ("index.md", "![A](Images/PIC.png)\n![B](images/pic.png)\n"),
            ("Images/PIC.png", b"one"),
            ("Images/", b""),
            ("images/pic.png", b"two"),
        ])
        result = export_note(self.raw, self.dest, {})
        self.assertEqual("exported", result["status"])
        one = result["member_paths"]["Images/PIC.png"]
        two = result["member_paths"]["images/pic.png"]
        self.assertNotEqual(one.casefold(), two.casefold())
        self.assertEqual(b"one", (self.dest / one).read_bytes())
        self.assertEqual(b"two", (self.dest / two).read_bytes())

    def test_credentials_removed_recursively_and_frontmatter_cannot_inject(self):
        self.archive([("index.md", "content")])
        metadata = {"title": 'title\n---\ninjected: true', "token": "topsecret", "nested": {"accessToken": "anothersecret", "okay": 1}, "url": "https://example.com/a?token=secret&keep=1"}
        result = export_note(self.raw, self.dest, metadata)
        self.assertEqual("exported", result["status"])
        saved = (self.dest / "metadata.json").read_text()
        markdown = (self.dest / "note.md").read_text()
        self.assertNotIn("topsecret", saved + markdown)
        self.assertNotIn("anothersecret", saved + markdown)
        self.assertNotIn("token=secret", saved + markdown)
        self.assertNotIn("\n---\ninjected: true", markdown)
        self.assertEqual(1, json.loads(saved)["metadata"]["nested"]["okay"])

    def test_active_html_removed_and_remote_resources_reported(self):
        self.archive([("index.html", '<html><head><meta http-equiv="refresh" content="0;url=https://example.com"><base href="https://evil.example"></head><body onload="alert(1)"><script>alert(1)</script><iframe src="https://evil.example"></iframe><img src="https://example.com/photo.png"><img src="missing.png"><a href="javascript:alert(1)">click</a><p>okay</p></body></html>')])
        result = export_note(self.raw, self.dest, {})
        self.assertEqual("exported", result["status"])
        offline = (self.dest / "index.html").read_text()
        for unsafe in ("<script", "<iframe", "onload=", "javascript:", 'http-equiv="refresh"', "<base"):
            self.assertNotIn(unsafe, offline)
        self.assertEqual(["https://example.com/photo.png"], result["external_resources"])
        self.assertEqual(["missing.png"], result["missing_resources"])

    def test_unused_exact_old_client_icon_css_is_ignored_only_in_reading_copy(self):
        dependency = "/Applications/WizNote.app/Contents/Resources/files/wizeditor/dependency/fonts.css"
        body = '<html><head><link rel="stylesheet" href="' + dependency + '"></head><body><p>Content</p></body></html>'
        self.archive([("index.html", body)])
        before = hashlib.sha256(self.raw.read_bytes()).hexdigest()
        result = export_note(self.raw, self.dest, {})
        self.assertEqual("exported", result["status"])
        self.assertEqual([], result["missing_resources"])
        self.assertEqual([], result["warnings"])
        self.assertEqual([{"href": dependency, "reason": "unused-client-editor-icon-fonts"}], result["ignored_client_dependencies"])
        self.assertNotIn(dependency, (self.dest / "index.html").read_text())
        self.assertEqual(body, (self.dest / result["body_original"]).read_text())
        self.assertEqual(before, hashlib.sha256(self.raw.read_bytes()).hexdigest())
        self.assertEqual(result["ignored_client_dependencies"], json.loads((self.dest / "metadata.json").read_text())["export"]["ignored_client_dependencies"])

    def test_used_editor_icon_class_or_font_keeps_old_client_dependency_missing(self):
        dependency = "/Applications/WizNote.app/Contents/Resources/files/wizeditor/dependency/fonts.css"
        for fragment in ('<span class="editor-icon-bold">icon</span>', '<span style="font-family: wizEditorIcons">icon</span>'):
            with self.subTest(fragment=fragment):
                self.archive([("index.html", '<html><head><link rel="stylesheet" href="' + dependency + '"></head><body>' + fragment + '</body></html>')])
                result = export_note(self.raw, self.dest, {})
                self.assertEqual([dependency], result["missing_resources"])
                self.assertEqual([], result["ignored_client_dependencies"])
                self.assertIn(dependency, (self.dest / "index.html").read_text())

    def test_other_css_and_relative_fonts_css_are_not_ignored(self):
        for dependency in ("index_files/fonts.css", "index_files/article.css", "/Applications/Other.app/Contents/Resources/files/wizeditor/dependency/fonts.css"):
            with self.subTest(dependency=dependency):
                self.archive([("index.html", '<html><head><link rel="stylesheet" href="' + dependency + '"></head><body><p>Content</p></body></html>')])
                result = export_note(self.raw, self.dest, {})
                self.assertEqual([dependency], result["missing_resources"])
                self.assertEqual([], result["ignored_client_dependencies"])

    def test_encrypted_original_retained_and_reported_locked(self):
        original = b"ZIWR" + bytes(range(32))
        self.raw.write_bytes(original)
        result = export_note(self.raw, self.dest, {"encrypted": True})
        self.assertEqual("locked", result["status"])
        self.assertEqual(original, self.raw.read_bytes())
        self.assertFalse((self.dest / "note.md").exists())
        self.assertTrue((self.dest / "metadata.json").exists())

    def test_filename_bytes_and_collision_suffixes(self):
        name = safe_filename("学习" * 100 + ".md")
        self.assertLessEqual(len(name.encode("utf-8")), 180)
        self.assertTrue(name.endswith(".md"))
        self.assertNotEqual(safe_filename("a/b"), safe_filename("a:b"))
        self.assertNotEqual(safe_filename("same", identity="a"), safe_filename("same", identity="b"))
        self.assertNotEqual("CON", safe_filename("CON"))

    def test_rerun_and_changed_original_keep_both_extractions(self):
        self.archive([("index.md", "version one")])
        first = export_note(self.raw, self.dest, {})
        second = export_note(self.raw, self.dest, {})
        self.assertEqual(first["extraction_dir"], second["extraction_dir"])
        self.archive([("index.md", "version two")])
        third = export_note(self.raw, self.dest, {})
        self.assertNotEqual(first["extraction_dir"], third["extraction_dir"])
        self.assertEqual("version one", (self.dest / first["body_original"]).read_text())
        self.assertEqual("version two", (self.dest / third["body_original"]).read_text())


if __name__ == "__main__":
    unittest.main()

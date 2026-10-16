"""Negative fixtures prove CI fails for malformed skills and broken links."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "repository_validation", Path(__file__).resolve().parents[1] / "scripts/validate_repository.py")
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class RepositoryValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.files = []
        self.skill = self.write("skills/example/SKILL.md",
                                "---\nname: example\ndescription: A useful skill\n---\n# Instructions\n")

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.files.append(path)
        return path

    def test_valid_skill_and_reference_link(self):
        self.write("README.md", "[skill][target]\n\n[target]: skills/example/SKILL.md#instructions\n")
        self.assertEqual([], validator.validate_structure(self.root, self.files))
        self.assertEqual([], validator.validate_local_links(self.root, self.files))

    def test_invalid_frontmatter_name_description_and_body(self):
        for content in ["# No metadata", "---\nname: [broken\n---\nBody",
                        "---\nname: wrong\ndescription: Text\n---\nBody",
                        "---\nname: example\ndescription: ''\n---\nBody",
                        "---\nname: example\ndescription: Text\n---\n"]:
            with self.subTest(content=content):
                self.skill.write_text(content)
                self.assertTrue(validator.validate_structure(self.root, self.files))

    def test_missing_skill_entrypoint(self):
        self.write("skills/missing/references/info.md", "Info")
        self.assertTrue(validator.validate_structure(self.root, self.files))

    def test_broken_inline_image_and_reference_links(self):
        for content in ["[missing](missing.md)", "![missing](missing.png)",
                        "[missing][target]\n\n[target]: missing.md\n"]:
            with self.subTest(content=content):
                page = self.write("README.md", content)
                self.assertTrue(validator.validate_local_links(self.root, self.files))
                self.files.remove(page)

    def test_escape_absolute_and_untracked_targets(self):
        self.write("untracked.md", "Not in index")
        self.files.pop()
        for target in ["../outside.md", "/etc/passwd", "untracked.md"]:
            with self.subTest(target=target):
                page = self.write("README.md", f"[link]({target})")
                self.assertTrue(validator.validate_local_links(self.root, self.files))
                self.files.remove(page)

    def test_encoded_local_path_and_code_examples(self):
        self.write("docs/a b.md", "Target")
        self.write("README.md", "[link](docs/a%20b.md)\n`[example](missing.md)`\n"
                   "\n```md\n[example](missing.md)\n```\n[web](https://example.org)\n")
        self.assertEqual([], validator.validate_local_links(self.root, self.files))

    def test_vault_link_resolution(self):
        page = self.write("vault-template/Index.md", "[[Folder/Note|Alias]]")
        self.assertTrue(validator.validate_local_links(self.root, self.files))
        self.write("vault-template/Folder/Note.md", "Note")
        self.assertEqual([], validator.validate_local_links(self.root, self.files))


if __name__ == "__main__":
    unittest.main()

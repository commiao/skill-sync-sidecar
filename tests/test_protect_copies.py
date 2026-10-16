from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from skill_sync_sidecar import conflicts, sync_cycle, tombstones
from skill_sync_sidecar.protect import copy_tree_writable, is_readonly, make_readonly, make_writable


def _skill(root: Path, name: str) -> Path:
    d = root / name
    (d / "sub").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n", encoding="utf-8")
    (d / "sub" / "f.md").write_text("x", encoding="utf-8")
    return d


class ReadOnlyCanonicalCopiesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.canonical = self.tmp / "canonical"
        make_readonly(_skill(self.canonical, "demo"))

    def tearDown(self):
        make_writable(self.tmp)
        self._tmp.cleanup()

    def test_copy_tree_writable(self):
        copy_tree_writable(self.canonical / "demo", self.tmp / "copy")
        self.assertTrue(is_readonly(self.canonical / "demo"))
        self.assertFalse(is_readonly(self.tmp / "copy"))
        (self.tmp / "copy" / "sub" / "new.md").write_text("ok", encoding="utf-8")

    def test_conflict_and_tombstone_material_can_be_reset_by_the_cycle(self):
        # sync_cycle 每轮会重建这些报告目录；复制品只读的话，删除就会让这一轮报错
        package = self.tmp / "report" / "conflicts" / "demo"
        package.mkdir(parents=True)
        conflicts._write_local_material(package, self.canonical, "demo")
        tomb = self.tmp / "report" / "tombstones" / "demo"
        tomb.mkdir(parents=True)
        tombstones._write_local_material(tomb, self.canonical, "demo")
        sync_cycle._reset_generated_report_dir(self.tmp / "report")
        self.assertFalse((self.tmp / "report").exists())


if __name__ == "__main__":
    unittest.main()

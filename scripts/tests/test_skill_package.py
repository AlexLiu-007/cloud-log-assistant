"""Validate the portable distribution without copying local runtime data."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from package_skill import PACKAGE_FILES, build_package


class SkillPackageTests(unittest.TestCase):
    def test_package_is_reproducible_and_contains_runnable_sources(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / 'first.zip', Path(directory) / 'second.zip'
            self.assertEqual(build_package(root, first)['sha256'], build_package(root, second)['sha256'])
            with ZipFile(first) as archive:
                self.assertEqual(set(archive.namelist()), set(PACKAGE_FILES))
                self.assertIsNone(archive.testzip())
                self.assertIn('SKILL.md', archive.namelist())
                for name in archive.namelist():
                    self.assertNotIn(b'\r', archive.read(name))
                    if name.endswith('.py'):
                        compile(archive.read(name), name, 'exec')

    def test_only_allowlisted_files_ship_and_env_values_are_rejected(self):
        source = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            for name in PACKAGE_FILES:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target)
            (root / '.env').write_text('SECRET=private-runtime-value', encoding='utf-8')
            (root / 'assets/connections.json').write_text('{"secret":"private-config"}', encoding='utf-8')
            output = Path(directory) / 'package.zip'
            build_package(root, output)
            with ZipFile(output) as archive:
                content = b''.join(archive.read(name) for name in archive.namelist())
                self.assertNotIn(b'private-runtime-value', content)
                self.assertNotIn(b'private-config', content)
            (root / '.env.example').write_text('SECRET=nonempty-value', encoding='utf-8')
            with self.assertRaises(ValueError):
                build_package(root, output)


if __name__ == '__main__':
    unittest.main()

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from wechat_slim import (
    AccountProfile,
    ScanCategory,
    execute_slimming,
    format_bytes,
    parse_size_str,
    scan_directory,
)


class TestWeChatSlim(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(format_bytes(500), '500.0 B')
        self.assertEqual(format_bytes(1024), '1.0 KB')
        self.assertEqual(format_bytes(1024 * 1024 * 5), '5.0 MB')
        self.assertEqual(format_bytes(1024 * 1024 * 1024 * 2.5), '2.5 GB')

    def test_parse_size_str(self):
        self.assertEqual(parse_size_str('10MB'), 10 * 1024 * 1024)
        self.assertEqual(parse_size_str('500KB'), 500 * 1024)
        self.assertEqual(parse_size_str('1GB'), 1024 * 1024 * 1024)
        self.assertEqual(parse_size_str('0B'), 0)
        self.assertEqual(parse_size_str(''), 0)

    def test_execute_slimming_safeguards_db(self):
        test_dir = Path(tempfile.mkdtemp())
        try:
            video_dir = test_dir / 'msg/video'
            video_dir.mkdir(parents=True)
            db_dir = test_dir / 'db_storage'
            db_dir.mkdir(parents=True)

            # Create mock video and db
            v_file = video_dir / 'large_video.mp4'
            v_file.write_bytes(b'x' * 1024 * 100)  # 100KB

            db_file = db_dir / 'message.db'
            db_file.write_bytes(b'sqlite_database_content')

            acc = AccountProfile(
                account_id='test_wxid',
                version_type='test',
                root_path=test_dir,
                db_path=db_dir,
                msg_video_path=video_dir,
            )

            cat_video = scan_directory('video', 'videos', video_dir)
            cat_db = scan_directory('db', 'db', db_dir, is_protected=True)

            categories = {'video': cat_video, 'db': cat_db}

            # Dry run test
            count, freed = execute_slimming(
                acc, categories, days=0, min_size_bytes=1000, selected_types=['video', 'db'], dry_run=True
            )
            self.assertEqual(count, 1)
            self.assertEqual(freed, 1024 * 100)
            self.assertTrue(v_file.exists())
            self.assertTrue(db_file.exists())

            # Archive test
            archive_dir = test_dir / 'external_ssd'
            count, freed = execute_slimming(
                acc, categories, days=0, min_size_bytes=1000, selected_types=['video', 'db'], dry_run=False, archive_to=archive_dir
            )
            self.assertEqual(count, 1)
            self.assertFalse(v_file.exists())
            self.assertTrue(db_file.exists(), 'Database file MUST NEVER be touched')
            self.assertTrue((archive_dir / 'msg/video/large_video.mp4').exists())

        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()

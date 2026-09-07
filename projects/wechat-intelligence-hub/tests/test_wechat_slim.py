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


    def test_cli_integration_custom_path(self):
        """端到端集成测试: 测试 scan 与 clean --archive-to 命令行调用."""
        import subprocess

        test_dir = Path(tempfile.mkdtemp())
        archive_dir = Path(tempfile.mkdtemp())
        try:
            # 创建真实微信目录结构
            (test_dir / 'db_storage').mkdir(parents=True)
            (test_dir / 'msg/video').mkdir(parents=True)
            (test_dir / 'msg/file').mkdir(parents=True)
            (test_dir / 'cache').mkdir(parents=True)

            # 写入模拟数据
            (test_dir / 'db_storage/contact.db').write_bytes(b'sqlite_header_protected')
            (test_dir / 'msg/video/demo_presentation.mp4').write_bytes(b'0' * (1024 * 1024))  # 1MB
            (test_dir / 'msg/file/quarterly_report.pdf').write_bytes(b'1' * (512 * 1024))      # 512KB
            (test_dir / 'cache/thumb_001.tmp').write_bytes(b'2' * 2048)

            script_path = str(Path(__file__).resolve().parents[3] / 'wechat_slim.py')

            # 1. 测试 scan 命令
            scan_res = subprocess.run(
                [sys.executable, script_path, 'scan', '--path', str(test_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(scan_res.returncode, 0)
            self.assertIn('WeChat Slim - 微信智能存储透视器', scan_res.stdout)
            self.assertIn('db_storage', scan_res.stdout)
            self.assertIn('[🔒 数据库绝对保护]', scan_res.stdout)
            self.assertIn('video', scan_res.stdout)

            # 2. 测试 clean --dry-run
            dry_res = subprocess.run(
                [sys.executable, script_path, 'clean', '--path', str(test_dir), '--types', 'video,file', '--days', '0', '--dry-run'],
                capture_output=True,
                text=True,
            )
            self.assertEqual(dry_res.returncode, 0)
            self.assertIn('演练模式 Dry-Run', dry_res.stdout)
            self.assertIn('共计 2 个文件', dry_res.stdout)
            # 确认文件仍在原处
            self.assertTrue((test_dir / 'msg/video/demo_presentation.mp4').exists())
            self.assertTrue((test_dir / 'msg/file/quarterly_report.pdf').exists())

            # 3. 测试 clean --archive-to (实际执行外置归档)
            clean_res = subprocess.run(
                [sys.executable, script_path, 'clean', '--path', str(test_dir), '--types', 'video,file', '--days', '0', '--archive-to', str(archive_dir), '-f'],
                capture_output=True,
                text=True,
            )
            self.assertEqual(clean_res.returncode, 0)
            self.assertIn('处理完成', clean_res.stdout)

            # 验证原目录大文件已被移走
            self.assertFalse((test_dir / 'msg/video/demo_presentation.mp4').exists())
            self.assertFalse((test_dir / 'msg/file/quarterly_report.pdf').exists())

            # 验证归档目录已完整保存文件与目录结构
            self.assertTrue((archive_dir / 'msg/video/demo_presentation.mp4').exists())
            self.assertTrue((archive_dir / 'msg/file/quarterly_report.pdf').exists())

            # 核心安全底线：核心数据库绝不可被移动或触碰
            self.assertTrue((test_dir / 'db_storage/contact.db').exists())
            self.assertEqual((test_dir / 'db_storage/contact.db').read_bytes(), b'sqlite_header_protected')

        finally:
            shutil.rmtree(test_dir, ignore_errors=True)
            shutil.rmtree(archive_dir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()

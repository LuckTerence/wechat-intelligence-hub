import json
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

            self.assertTrue((test_dir / 'db_storage/contact.db').exists())
            self.assertEqual((test_dir / 'db_storage/contact.db').read_bytes(), b'sqlite_header_protected')
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)
            shutil.rmtree(archive_dir, ignore_errors=True)

    def test_dedup_hardlink_and_trash(self):
        """测试多群重复文件查重与 APFS 硬链接替换去重."""
        from wechat_slim import find_duplicates, execute_dedup, compute_fast_hash, compute_full_hash

        test_dir = Path(tempfile.mkdtemp())
        try:
            file_dir = test_dir / 'msg/file'
            file_dir.mkdir(parents=True)

            # 创建 3 个内容完全相同的文件 (模拟转发到 3 个不同的群)
            content = b'IMPORTANT_MEETING_PRESENTATION_CONTENT' * 1000 # 38KB
            f1 = file_dir / 'chat1_meeting.pdf'
            f2 = file_dir / 'chat2_meeting.pdf'
            f3 = file_dir / 'chat3_meeting.pdf'
            unique_file = file_dir / 'other_file.pdf'

            f1.write_bytes(content)
            f2.write_bytes(content)
            f3.write_bytes(content)
            unique_file.write_bytes(b'different_content')

            cat_file = scan_directory('file', 'files', file_dir)
            categories = {'file': cat_file}

            # 1. 查重
            groups = find_duplicates(categories, ['file'], min_size_bytes=100)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].wasted_count, 2)
            self.assertEqual(groups[0].saving_bytes, len(content) * 2)
            self.assertEqual(len(groups[0].files), 3)

            # 2. 执行硬链接去重
            count, freed = execute_dedup(groups, action='hardlink', dry_run=False)
            self.assertEqual(count, 2)
            self.assertEqual(freed, len(content) * 2)

            # 3. 验证 3 个文件依然全部完好存在 (微信聊天窗口永不断链)
            self.assertTrue(f1.exists())
            self.assertTrue(f2.exists())
            self.assertTrue(f3.exists())
            self.assertEqual(f1.read_bytes(), content)
            self.assertEqual(f2.read_bytes(), content)

            # 4. 验证在文件系统层，3 个文件已指向同一个 inode (只占 1 份物理磁盘)
            st1 = f1.stat()
            st2 = f2.stat()
            st3 = f3.stat()
            self.assertEqual(st1.st_ino, st2.st_ino)
            self.assertEqual(st1.st_ino, st3.st_ino)

            # 5. 再次查重，应该感知到已硬链接，不会重复计算浪费
            cat_file2 = scan_directory('file', 'files', file_dir)
            groups2 = find_duplicates({'file': cat_file2}, ['file'], min_size_bytes=100)
            self.assertEqual(len(groups2), 1)
            self.assertEqual(groups2[0].wasted_count, 0)
            self.assertEqual(groups2[0].saving_bytes, 0)

        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_cli_dedup_command(self):
        """测试 dedup 命令行调用 (dry-run 与 force 执行)."""
        import subprocess

        test_dir = Path(tempfile.mkdtemp())
        try:
            video_dir = test_dir / 'msg/video'
            video_dir.mkdir(parents=True)
            v1 = video_dir / 'shared_video_a.mp4'
            v2 = video_dir / 'shared_video_b.mp4'
            data = b'VIDEO_DATA_FOR_TESTING' * 2000 # ~44KB
            v1.write_bytes(data)
            v2.write_bytes(data)

            script_path = str(Path(__file__).resolve().parents[3] / 'wechat_slim.py')

            # 1. 测试 dedup --dry-run
            res_dry = subprocess.run(
                [sys.executable, script_path, 'dedup', '--path', str(test_dir), '--types', 'video', '--min-size', '10KB', '--dry-run'],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_dry.returncode, 0)
            self.assertIn('发现 1 组重复文件', res_dry.stdout)
            self.assertIn('演练模式', res_dry.stdout)
            self.assertNotEqual(v1.stat().st_ino, v2.stat().st_ino)

            # 2. 测试 dedup -f 执行硬链接去重
            res_run = subprocess.run(
                [sys.executable, script_path, 'dedup', '--path', str(test_dir), '--types', 'video', '--min-size', '10KB', '--action', 'hardlink', '-f'],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_run.returncode, 0)
            self.assertIn('去重成功', res_run.stdout)
            self.assertEqual(v1.stat().st_ino, v2.stat().st_ino)
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_web_server_endpoints(self):
        """测试 WebUI 接口响应 (GET /, GET /api/stats)."""
        import threading
        import urllib.request
        from http.server import HTTPServer
        from wechat_slim import WeChatSlimWebHandler

        test_dir = Path(tempfile.mkdtemp())
        try:
            (test_dir / 'db_storage').mkdir(parents=True)
            (test_dir / 'db_storage/test.db').write_bytes(b'db')
            WeChatSlimWebHandler.custom_path = test_dir

            server = HTTPServer(('127.0.0.1', 0), WeChatSlimWebHandler)
            port = server.server_port
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()

            # 1. 测试首页 HTML
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/') as resp:
                self.assertEqual(resp.status, 200)
                html = resp.read().decode('utf-8')
                self.assertIn('WeChat Slim', html)
                self.assertIn('<!DOCTYPE html>', html)

            # 2. 测试 /api/stats JSON 接口
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/stats') as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode('utf-8'))
                self.assertIn('account', data)
                self.assertIn('categories', data)
                self.assertIn('db', data['categories'])

            server.shutdown()
            server.server_close()
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()

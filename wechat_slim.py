#!/usr/bin/env python3
"""WeChat Slim - 微信智能瘦身与无损归档极简 CLI.

支持自动探测 Mac 微信 4.0+ 与 3.x 存储目录，
支持文件类型过滤（视频/文件/附件/缓存）、时间跨度过滤与大小过滤，
支持安全移入废纸篓（可撤销）与无损外置硬盘归档，
绝不修改或删除核心聊天记录数据库 (db_storage / *.db)。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple


def format_bytes(size: float) -> str:
    """格式化字节大小为人类可读字符串."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0 or unit == 'TB':
            return f'{size:.1f} {unit}'
        size /= 1024.0
    return f'{size:.1f} B'


def parse_size_str(size_str: str) -> int:
    """解析大小字符串 (如 10MB, 500KB, 1GB) 为字节数."""
    s = size_str.strip().upper()
    if not s:
        return 0
    multipliers = {
        'B': 1,
        'K': 1024,
        'KB': 1024,
        'M': 1024 * 1024,
        'MB': 1024 * 1024,
        'G': 1024 * 1024 * 1024,
        'GB': 1024 * 1024 * 1024,
    }
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            num = s[: -len(suffix)].strip()
            try:
                return int(float(num) * mult)
            except ValueError:
                break
    try:
        return int(s)
    except ValueError:
        raise ValueError(f'无法识别的大小格式: {size_str} (例如: 10MB, 500KB)')


@dataclass
class AccountProfile:
    """微信账号存储路径描述."""
    account_id: str
    version_type: str  # 'v4' or 'v3'
    root_path: Path
    db_path: Optional[Path] = None
    msg_video_path: Optional[Path] = None
    msg_file_path: Optional[Path] = None
    msg_attach_path: Optional[Path] = None
    cache_path: Optional[Path] = None
    temp_path: Optional[Path] = None


@dataclass
class ScanCategory:
    """某一类文件的空间统计."""
    name: str
    description: str
    path: Path
    is_protected: bool = False
    file_count: int = 0
    total_bytes: int = 0
    files: List[Tuple[Path, int, float]] = field(default_factory=list)  # (path, size, mtime)


def discover_accounts() -> List[AccountProfile]:
    """自动发现当前 Mac 上的微信存储账号路径."""
    accounts: List[AccountProfile] = []
    home = Path.home()

    # 1. 微信 4.0+ 路径: ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/
    v4_base = home / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files'
    if v4_base.is_dir():
        for item in v4_base.iterdir():
            if item.is_dir() and item.name not in ['all_users', 'Backup'] and not item.name.startswith('.'):
                acc = AccountProfile(
                    account_id=item.name,
                    version_type='v4 (微信 4.0+)',
                    root_path=item,
                    db_path=item / 'db_storage' if (item / 'db_storage').exists() else None,
                    msg_video_path=item / 'msg/video' if (item / 'msg/video').exists() else None,
                    msg_file_path=item / 'msg/file' if (item / 'msg/file').exists() else None,
                    msg_attach_path=item / 'msg/attach' if (item / 'msg/attach').exists() else None,
                    cache_path=item / 'cache' if (item / 'cache').exists() else None,
                    temp_path=item / 'temp' if (item / 'temp').exists() else None,
                )
                accounts.append(acc)

    # 2. 微信 3.x 传统路径: ~/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat/
    v3_base = home / 'Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat'
    if v3_base.is_dir():
        for ver in v3_base.iterdir():
            if ver.is_dir() and not ver.name.startswith('.'):
                for acc_dir in ver.iterdir():
                    if acc_dir.is_dir() and len(acc_dir.name) == 32 and not acc_dir.name.startswith('.'):
                        acc = AccountProfile(
                            account_id=acc_dir.name[:8] + '...',
                            version_type=f'v3 ({ver.name})',
                            root_path=acc_dir,
                            msg_attach_path=acc_dir / 'Message/MessageTemp' if (acc_dir / 'Message/MessageTemp').exists() else None,
                            cache_path=acc_dir / 'Caches' if (acc_dir / 'Caches').exists() else None,
                        )
                        accounts.append(acc)

    return accounts


def scan_directory(category_name: str, desc: str, dir_path: Optional[Path], is_protected: bool = False) -> ScanCategory:
    """递归统计指定目录下的文件数量与总大小."""
    cat = ScanCategory(name=category_name, description=desc, path=dir_path or Path('/dev/null'), is_protected=is_protected)
    if not dir_path or not dir_path.exists():
        return cat

    for root, _, files in os.walk(dir_path):
        for f in files:
            fp = Path(root) / f
            try:
                st = fp.stat()
                cat.file_count += 1
                cat.total_bytes += st.st_size
                cat.files.append((fp, st.st_size, st.st_mtime))
            except (OSError, PermissionError):
                continue

    return cat


def scan_account(acc: AccountProfile) -> Dict[str, ScanCategory]:
    """对单个账号执行全量存储透视扫描."""
    results: Dict[str, ScanCategory] = {}

    # 1. 核心数据库 (必须保护)
    results['db'] = scan_directory('db_storage', '核心聊天数据库与文字索引 [🔒 绝对保护，禁止删除]', acc.db_path, is_protected=True)

    # 2. 视频缓存
    results['video'] = scan_directory('video', '接收与缓存的视频文件 (msg/video)', acc.msg_video_path)

    # 3. 接收文件
    results['file'] = scan_directory('file', '接收的文档与办公文件 (msg/file)', acc.msg_file_path)

    # 4. 聊天图片与多媒体附件
    results['attach'] = scan_directory('attach', '聊天图片、表情与多媒体附件 (msg/attach)', acc.msg_attach_path)

    # 5. 缓存与临时文件
    cache_files: List[Tuple[Path, int, float]] = []
    total_cache_size = 0
    total_cache_count = 0
    for p in [acc.cache_path, acc.temp_path]:
        if p and p.exists():
            c = scan_directory('cache_raw', '', p)
            cache_files.extend(c.files)
            total_cache_size += c.total_bytes
            total_cache_count += c.file_count

    results['cache'] = ScanCategory(
        name='cache',
        description='运行临时缓存与缩略图 (cache/temp) [可安全清理]',
        path=acc.cache_path or acc.root_path,
        file_count=total_cache_count,
        total_bytes=total_cache_size,
        files=cache_files,
    )

    return results


def move_to_trash(file_path: Path) -> bool:
    """安全将文件移入 macOS 废纸篓 (可通过访达随时放回原处)."""
    try:
        resolved = str(file_path.resolve())
        cmd = ['osascript', '-e', f'tell application "Finder" to delete POSIX file "{resolved}"']
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.returncode == 0
    except Exception:
        return False


def execute_slimming(
    acc: AccountProfile,
    categories: Dict[str, ScanCategory],
    days: int,
    min_size_bytes: int,
    selected_types: List[str],
    dry_run: bool = False,
    archive_to: Optional[Path] = None,
) -> Tuple[int, int]:
    """执行瘦身与清理操作.
    
    返回: (清理文件数, 释放字节数)
    """
    cutoff_time = datetime.now() - timedelta(days=days) if days > 0 else datetime.now() + timedelta(days=99999)
    cutoff_ts = cutoff_time.timestamp()

    freed_bytes = 0
    freed_count = 0

    if archive_to:
        archive_to = archive_to.resolve()
        if not dry_run:
            archive_to.mkdir(parents=True, exist_ok=True)

    for type_key in selected_types:
        cat = categories.get(type_key)
        if not cat or cat.is_protected:
            continue

        for fp, size, mtime in cat.files:
            # 绝对安全护栏：绝不处理数据库文件
            if fp.suffix in ['.db', '.db-wal', '.db-shm', '.sqlite', '.wcdb'] or 'db_storage' in fp.parts:
                continue

            # 过滤条件 1: 文件大小阈值
            if size < min_size_bytes:
                continue

            # 过滤条件 2: 时间跨度 (mtime 必须早于截断时间)
            if days > 0 and mtime > cutoff_ts:
                continue

            # 命中待处理文件
            freed_count += 1
            freed_bytes += size

            if dry_run:
                continue

            if archive_to:
                # 归档模式：计算相对路径并安全移动到外置目录
                try:
                    rel_path = fp.relative_to(acc.root_path)
                except ValueError:
                    rel_path = Path(cat.name) / fp.name
                dest_path = archive_to / rel_path
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(fp), str(dest_path))
            else:
                # 默认安全清理：移至 macOS 废纸篓
                move_to_trash(fp)

    return freed_count, freed_bytes


def cmd_scan(args: argparse.Namespace) -> None:
    """执行扫描并展示存储透视概览."""
    accounts = discover_accounts()
    if not accounts:
        print('[-] 未在默认 macOS 容器中发现微信数据目录。')
        print('    提示: 请确认微信是否安装，或是否有登录过的账号。')
        return

    print('=' * 66)
    print('       WeChat Slim - 微信智能存储透视器')
    print('=' * 66)

    for idx, acc in enumerate(accounts, 1):
        print(f"\n[账号 {idx}] ID: {acc.account_id} | 版本: {acc.version_type}")
        print(f"路径: {acc.root_path}")
        print("-" * 66)

        categories = scan_account(acc)
        total_account_size = sum(c.total_bytes for c in categories.values())
        cleanable_size = sum(c.total_bytes for k, c in categories.items() if not c.is_protected)

        for key, cat in categories.items():
            status_tag = '[🔒 数据库绝对保护]' if cat.is_protected else '[可瘦身]'
            percent = (cat.total_bytes / total_account_size * 100) if total_account_size > 0 else 0
            size_str = format_bytes(cat.total_bytes).rjust(10)
            count_str = f'({cat.file_count:,} 个文件)'.rjust(16)
            print(f'  • {cat.name.ljust(12)} : {size_str}  {count_str}  {percent:5.1f}%  {status_tag}')

        print('-' * 66)
        clean_ratio = (cleanable_size / total_account_size * 100) if total_account_size > 0 else 0
        print(f'  总空间占用   : {format_bytes(total_account_size)}')
        print(f'  可瘦身潜力   : {format_bytes(cleanable_size)} ({clean_ratio:.1f}% 的空间可被安全瘦身/转存)')
    print()


def cmd_clean(args: argparse.Namespace) -> None:
    """执行瘦身清理或外置归档."""
    accounts = discover_accounts()
    if not accounts:
        print('[-] 未发现可操作的微信账号目录。')
        return

    acc = accounts[0]
    categories = scan_account(acc)
    types = [t.strip() for t in args.types.split(',') if t.strip()]
    min_size_bytes = parse_size_str(args.min_size)
    archive_dir = Path(args.archive_to) if args.archive_to else None

    action_name = f'无损转存归档至 [{archive_dir}]' if archive_dir else '安全移入系统废纸篓 (Trash)'

    print('=' * 66)
    print('  WeChat Slim - 执行配置')
    print('=' * 66)
    print(f'  • 目标账号   : {acc.account_id} ({acc.version_type})')
    print(f'  • 清理类型   : {", ".join(types)}')
    print(f'  • 时间过滤   : 清理 {args.days} 天前的文件' if args.days > 0 else '  • 时间过滤   : 不限时间')
    print(f'  • 大小过滤   : 仅处理超过 {args.min_size} 的文件' if min_size_bytes > 0 else '  • 大小过滤   : 不限文件大小')
    print(f'  • 执行动作   : {action_name}')
    if args.dry_run:
        print('  • 模拟运行   : [演练模式 Dry-Run - 不实际移动或删除任何文件]')
    print('-' * 66)

    pre_count, pre_bytes = execute_slimming(
        acc, categories, args.days, min_size_bytes, types, dry_run=True, archive_to=archive_dir
    )

    print(f'  预估影响     : 共计 {pre_count:,} 个文件，可释放 {format_bytes(pre_bytes)} 空间')

    if pre_count == 0:
        print('\n[✓] 没有符合当前过滤条件的文件，无需清理。')
        return

    if not args.force and not args.dry_run:
        confirm = input(f'\n确认要对这 {pre_count:,} 个文件执行 {action_name} 吗? [y/N]: ').strip().lower()
        if confirm != 'y':
            print('[x] 操作已取消。')
            return

    if not args.dry_run:
        print('\n正在处理中，请稍候...')
        actual_count, actual_bytes = execute_slimming(
            acc, categories, args.days, min_size_bytes, types, dry_run=False, archive_to=archive_dir
        )
        print(f'[✓] 处理完成！成功释放 {format_bytes(actual_bytes)} 空间（处理了 {actual_count:,} 个文件）。')
        if not archive_dir:
            print('    提示: 文件已被安全放入废纸篓。如需彻底释放磁盘空间，请清空废纸篓。')
        else:
            print(f'    提示: 所有文件已完整保存至外置目录: {archive_dir}')
    else:
        print('\n[演练完成] 实际执行时请去掉 --dry-run 参数。')


def interactive_wizard() -> None:
    """极简交互式向导."""
    accounts = discover_accounts()
    if not accounts:
        print('[-] 未发现微信数据目录。')
        return

    acc = accounts[0]
    categories = scan_account(acc)
    total_size = sum(c.total_bytes for c in categories.values())
    cleanable_size = sum(c.total_bytes for k, c in categories.items() if not c.is_protected)

    print('=' * 66)
    print('       WeChat Slim - 微信智能瘦身与无损归档工具 (Mac版)')
    print('=' * 66)
    print(f'[✓] 自动定位账号: {acc.account_id} ({acc.version_type})')
    print(f'    总占用: {format_bytes(total_size)} | 瘦身潜力: {format_bytes(cleanable_size)}')
    print('-' * 66)

    print('\n请选择要执行的操作:')
    print('  [1] 快速瘦身 (推荐: 清理 90 天前且 >10MB 的视频/文件，移入废纸篓)')
    print('  [2] 极限瘦身 (清理所有 30 天前的缓存、视频与下载文件)')
    print('  [3] 仅清理临时缓存 (仅清理 cache/temp，绝不触碰任何聊天文件)')
    print('  [4] 存储空间详细扫描 (查看各分类占用)')
    print('  [q] 退出')

    choice = input('\n请输入选项 [1-4/q]: ').strip().lower()
    if choice == '1':
        args = argparse.Namespace(
            types='video,file',
            days=90,
            min_size='10MB',
            dry_run=False,
            archive_to=None,
            force=False,
        )
        cmd_clean(args)
    elif choice == '2':
        args = argparse.Namespace(
            types='video,file,cache',
            days=30,
            min_size='0B',
            dry_run=False,
            archive_to=None,
            force=False,
        )
        cmd_clean(args)
    elif choice == '3':
        args = argparse.Namespace(
            types='cache',
            days=0,
            min_size='0B',
            dry_run=False,
            archive_to=None,
            force=False,
        )
        cmd_clean(args)
    elif choice == '4':
        cmd_scan(argparse.Namespace())
    else:
        print('已退出。')


def main() -> None:
    parser = argparse.ArgumentParser(
        description='WeChat Slim - 微信智能存储透视与安全瘦身工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest='subcommand')

    subparsers.add_parser('scan', help='扫描并展示微信存储空间深度分布')

    clean_p = subparsers.add_parser('clean', help='执行文件瘦身或外置归档')
    clean_p.add_argument('--days', type=int, default=90, help='清理多少天前的文件 (默认: 90 天，0 为不限时间)')
    clean_p.add_argument('--min-size', default='0B', help='文件最小大小阈值 (例如: 20MB, 10MB，默认: 0B)')
    clean_p.add_argument('--types', default='video,file,cache', help='清理文件类型，逗号分隔 (可选: video,file,attach,cache)')
    clean_p.add_argument('--dry-run', action='store_true', help='模拟预演，只统计不实际移动任何文件')
    clean_p.add_argument('--archive-to', default=None, help='指定外置移动硬盘或备份目录 (将文件安全移动至该目录，而非废纸篓)')
    clean_p.add_argument('-f', '--force', action='store_true', help='跳过确认提示直接执行')

    args = parser.parse_args()

    if args.subcommand == 'scan':
        cmd_scan(args)
    elif args.subcommand == 'clean':
        cmd_clean(args)
    else:
        interactive_wizard()


if __name__ == '__main__':
    main()

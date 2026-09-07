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
from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple, Set
import urllib.parse
import webbrowser


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


def discover_accounts(custom_path: Optional[Path] = None) -> List[AccountProfile]:
    """自动发现或指定当前 Mac 上的微信存储账号路径."""
    if custom_path:
        cp = Path(custom_path).resolve()
        if cp.is_dir():
            if (cp / 'db_storage').exists() or (cp / 'msg').exists() or (cp / 'cache').exists():
                return [
                    AccountProfile(
                        account_id=cp.name,
                        version_type='custom (自定义目录)',
                        root_path=cp,
                        db_path=cp / 'db_storage' if (cp / 'db_storage').exists() else None,
                        msg_video_path=cp / 'msg/video' if (cp / 'msg/video').exists() else None,
                        msg_file_path=cp / 'msg/file' if (cp / 'msg/file').exists() else None,
                        msg_attach_path=cp / 'msg/attach' if (cp / 'msg/attach').exists() else None,
                        cache_path=cp / 'cache' if (cp / 'cache').exists() else None,
                        temp_path=cp / 'temp' if (cp / 'temp').exists() else None,
                    )
                ]
            accs = []
            for sub in cp.iterdir():
                if sub.is_dir() and not sub.name.startswith('.'):
                    accs.append(AccountProfile(
                        account_id=sub.name,
                        version_type='custom (自定义目录)',
                        root_path=sub,
                        db_path=sub / 'db_storage' if (sub / 'db_storage').exists() else None,
                        msg_video_path=sub / 'msg/video' if (sub / 'msg/video').exists() else None,
                        msg_file_path=sub / 'msg/file' if (sub / 'msg/file').exists() else None,
                        msg_attach_path=sub / 'msg/attach' if (sub / 'msg/attach').exists() else None,
                        cache_path=sub / 'cache' if (sub / 'cache').exists() else None,
                        temp_path=sub / 'temp' if (sub / 'temp').exists() else None,
                    ))
            if accs:
                return accs
        return []

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


@dataclass
class DuplicateGroup:
    """一组内容完全相同的重复文件."""
    file_hash: str
    file_size: int
    files: List[Path]
    saving_bytes: int = 0
    wasted_count: int = 0


def compute_fast_hash(fp: Path, size: int) -> str:
    """快速稀疏哈希: 仅采样头、中、尾生成指纹，大幅加速大文件初筛."""
    chunk = 16384
    hasher = hashlib.md5()
    try:
        with open(fp, 'rb') as f:
            if size <= chunk * 3:
                hasher.update(f.read())
            else:
                hasher.update(f.read(chunk))
                f.seek(size // 2 - chunk // 2)
                hasher.update(f.read(chunk))
                f.seek(size - chunk)
                hasher.update(f.read(chunk))
    except (OSError, PermissionError):
        return ''
    return hasher.hexdigest()


def compute_full_hash(fp: Path) -> str:
    """全量 MD5 计算完整文件校验和."""
    hasher = hashlib.md5()
    try:
        with open(fp, 'rb') as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
    except (OSError, PermissionError):
        return ''
    return hasher.hexdigest()


def find_duplicates(
    categories: Dict[str, ScanCategory],
    selected_types: List[str],
    min_size_bytes: int = 1024,
) -> List[DuplicateGroup]:
    """三级流水线快速查找重复文件 (大小桶分流 -> 稀疏哈希 -> 全量哈希)."""
    # 1. 收集文件并按文件精确大小归类 (大小不同的文件绝不可能是重复文件)
    size_buckets: Dict[int, List[Path]] = defaultdict(list)
    for t in selected_types:
        cat = categories.get(t)
        if not cat or cat.is_protected:
            continue
        for fp, size, _ in cat.files:
            if fp.suffix in ['.db', '.db-wal', '.db-shm', '.sqlite', '.wcdb'] or 'db_storage' in fp.parts:
                continue
            if size >= min_size_bytes:
                size_buckets[size].append(fp)

    # 2. 仅对存在相同大小的文件进行快速哈希初筛
    candidate_buckets = [fps for fps in size_buckets.values() if len(fps) > 1]
    fast_hash_buckets: Dict[Tuple[int, str], List[Path]] = defaultdict(list)
    for fps in candidate_buckets:
        for fp in fps:
            try:
                sz = fp.stat().st_size
                fh = compute_fast_hash(fp, sz)
                if fh:
                    fast_hash_buckets[(sz, fh)].append(fp)
            except (OSError, PermissionError):
                continue

    # 3. 仅对稀疏哈希碰撞的文件进行全量 MD5 确认
    full_hash_groups: Dict[str, Tuple[int, List[Path]]] = defaultdict(lambda: (0, []))
    for (size, _), fps in fast_hash_buckets.items():
        if len(fps) <= 1:
            continue
        for fp in fps:
            try:
                full_h = compute_full_hash(fp)
                if full_h:
                    prev_size, prev_list = full_hash_groups[full_h]
                    full_hash_groups[full_h] = (size, prev_list + [fp])
            except (OSError, PermissionError):
                continue

    duplicate_groups: List[DuplicateGroup] = []
    for fhash, (size, fps) in full_hash_groups.items():
        if len(fps) > 1:
            # 检查是否有文件已经互为硬链接 (相同 st_dev 和 st_ino)
            inodes_seen: Set[Tuple[int, int]] = set()
            wasted_count = 0
            for fp in fps:
                try:
                    st = fp.stat()
                    key = (st.st_dev, st.st_ino)
                    if key in inodes_seen:
                        continue
                    inodes_seen.add(key)
                except OSError:
                    continue

            if len(inodes_seen) > 1:
                wasted_count = len(inodes_seen) - 1
                saving = wasted_count * size
            else:
                wasted_count = 0
                saving = 0

            # 按修改时间排序，保留最早或最基础的文件为主副本
            fps.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
            duplicate_groups.append(DuplicateGroup(
                file_hash=fhash,
                file_size=size,
                files=fps,
                saving_bytes=saving,
                wasted_count=wasted_count,
            ))

    # 按可释放空间由大到小排序
    duplicate_groups.sort(key=lambda g: g.saving_bytes, reverse=True)
    return duplicate_groups


def execute_dedup(
    groups: List[DuplicateGroup],
    action: str = 'hardlink',
    dry_run: bool = False,
) -> Tuple[int, int]:
    """执行重复文件去重.
    
    action='hardlink': (推荐) 将重复文件原子替换为系统硬链接，原路径原文件名完全保留，
                       微信内各群聊仍可正常读取，但在 macOS APFS 物理磁盘仅占一份空间！
    action='trash':    将冗余副本直接移至 macOS 废纸篓。
    """
    processed_count = 0
    freed_bytes = 0

    for grp in groups:
        if grp.wasted_count == 0 or len(grp.files) < 2:
            continue
        primary = grp.files[0]
        try:
            prim_st = primary.stat()
            prim_ino_key = (prim_st.st_dev, prim_st.st_ino)
        except OSError:
            continue

        for dup in grp.files[1:]:
            try:
                dup_st = dup.stat()
                if (dup_st.st_dev, dup_st.st_ino) == prim_ino_key:
                    continue
            except OSError:
                continue

            processed_count += 1
            freed_bytes += grp.file_size

            if dry_run:
                continue

            if action == 'hardlink':
                try:
                    # 使用临时硬链接原子替换，确保过程安全
                    tmp_link = dup.with_name(f".tmp_link_{os.getpid()}_{dup.name}")
                    os.link(primary, tmp_link)
                    os.replace(tmp_link, dup)
                except Exception:
                    continue
            elif action == 'trash':
                move_to_trash(dup)

    return processed_count, freed_bytes


def cmd_dedup(args: argparse.Namespace) -> None:
    """执行重复文件查重与硬链接/废纸篓去重."""
    custom_path = getattr(args, 'path', None)
    accounts = discover_accounts(custom_path)
    if not accounts:
        print('[-] 未发现可操作的微信账号目录。')
        return

    acc = accounts[0]
    categories = scan_account(acc)
    types = [t.strip() for t in args.types.split(',') if t.strip()]
    min_size_bytes = parse_size_str(args.min_size)

    action_desc = '转为 APFS 硬链接 (零风险: 微信各群仍能正常打开，但只占 1 份物理磁盘)' if args.action == 'hardlink' else '将多余副本移入系统废纸篓'

    print('=' * 66)
    print('       WeChat Slim - 多群重复文件智能查重与去重 (Phase 2)')
    print('=' * 66)
    print(f'  • 目标账号   : {acc.account_id} ({acc.version_type})')
    print(f'  • 查重范围   : {", ".join(types)}')
    print(f'  • 大小阈值   : 仅检查超过 {args.min_size} 的文件')
    print(f'  • 处理方式   : {action_desc}')
    if args.dry_run:
        print('  • 演练模式   : [Dry-Run - 仅分析展示，不实际修改磁盘]')
    print('-' * 66)
    print('正在计算文件特征哈希指纹，请稍候...')

    groups = find_duplicates(categories, types, min_size_bytes=min_size_bytes)
    actionable_groups = [g for g in groups if g.wasted_count > 0]
    total_wasted_copies = sum(g.wasted_count for g in actionable_groups)
    total_saving_bytes = sum(g.saving_bytes for g in actionable_groups)

    print(f'\n[✓] 查重完成: 发现 {len(actionable_groups)} 组重复文件，包含 {total_wasted_copies:,} 个多余副本')
    print(f'    可释放物理空间: {format_bytes(total_saving_bytes)}')

    if not actionable_groups:
        print('\n[✓] 太棒了！当前范围内未发现占用多份空间的重复文件。')
        return

    # 展示前 5 组最占空间的重复文件
    print('\n[Top 重复文件样本]:')
    for idx, grp in enumerate(actionable_groups[:5], 1):
        print(f"  {idx}. [{format_bytes(grp.file_size)}/个] 共有 {len(grp.files)} 个副本 (可释放 {format_bytes(grp.saving_bytes)}):")
        print(f"     底稿保留: {grp.files[0].name}")
        for dup in grp.files[1:3]:
            print(f"     重复副本: {dup.parent.name}/{dup.name}")
        if len(grp.files) > 3:
            print(f"     ... 等共 {len(grp.files)} 个文件")

    if args.dry_run:
        print('\n[演练完成] 如需实际执行去重，请去掉 --dry-run 参数。')
        return

    if not args.force:
        confirm = input(f'\n确认要对这 {total_wasted_copies:,} 个副本执行 [{args.action}] 去重吗? [y/N]: ').strip().lower()
        if confirm != 'y':
            print('[x] 操作已取消。')
            return

    print('\n正在执行去重处理...')
    done_count, done_bytes = execute_dedup(actionable_groups, action=args.action, dry_run=False)
    print(f'[✓] 去重成功！已处理 {done_count:,} 个重复副本，成功释放 {format_bytes(done_bytes)} 物理磁盘空间！')
    if args.action == 'hardlink':
        print('    提示: 已转换为 APFS 硬链接，微信中所有聊天窗口里的文件依然可原样点击打开！')


def cmd_scan(args: argparse.Namespace) -> None:
    """执行扫描并展示存储透视概览."""
    custom_path = getattr(args, 'path', None)
    accounts = discover_accounts(custom_path)
    if not accounts:
        print('[-] 未在指定或默认微信容器中发现微信数据目录。')
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
    custom_path = getattr(args, 'path', None)
    accounts = discover_accounts(custom_path)
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

WEB_UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WeChat Slim - 微信智能存储透视与安全瘦身大盘</title>
    <style>
        :root {
            --bg: #f5f6f8;
            --card-bg: #ffffff;
            --text-main: #1d1d1f;
            --text-sub: #86868b;
            --border: #e5e5ea;
            --primary: #0071e3;
            --primary-hover: #0077ed;
            --success: #34c759;
            --warning: #ff9500;
            --danger: #ff3b30;
            --radius: 12px;
            --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: var(--font); background: var(--bg); color: var(--text-main); line-height: 1.5; padding: 24px 16px; }
        .container { max-width: 980px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
        .logo { font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
        .badge { background: #e8f2ff; color: var(--primary); padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; }
        .grid-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .card { background: var(--card-bg); border-radius: var(--radius); border: 1px solid var(--border); padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.03); }
        .stat-label { font-size: 13px; color: var(--text-sub); font-weight: 500; margin-bottom: 6px; }
        .stat-val { font-size: 26px; font-weight: 700; letter-spacing: -0.5px; }
        .stat-desc { font-size: 12px; color: var(--text-sub); margin-top: 4px; }
        .progress-bar-container { background: #e5e5ea; border-radius: 8px; height: 14px; overflow: hidden; display: flex; margin: 16px 0 8px 0; }
        .progress-seg { height: 100%; transition: width 0.3s; }
        .bg-attach { background: #0071e3; }
        .bg-video { background: #5856d6; }
        .bg-file { background: #34c759; }
        .bg-cache { background: #ff9500; }
        .bg-db { background: #8e8e93; }
        .legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--text-sub); margin-bottom: 24px; }
        .legend-item { display: flex; align-items: center; gap: 6px; }
        .legend-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
        .tabs { display: flex; gap: 8px; border-bottom: 1px solid var(--border); margin-bottom: 20px; }
        .tab-btn { background: none; border: none; padding: 10px 16px; font-size: 14px; font-weight: 600; color: var(--text-sub); cursor: pointer; border-bottom: 2px solid transparent; }
        .tab-btn.active { color: var(--primary); border-bottom-color: var(--primary); }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .form-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 16px; }
        .form-group label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
        .form-group input, .form-group select { width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 14px; }
        .checkbox-group { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; margin: 12px 0; font-size: 13px; }
        .btn-group { display: flex; gap: 12px; margin-top: 18px; }
        .btn { padding: 10px 20px; border-radius: 8px; border: none; font-size: 14px; font-weight: 600; cursor: pointer; transition: background 0.2s; }
        .btn-primary { background: var(--primary); color: white; }
        .btn-primary:hover { background: var(--primary-hover); }
        .btn-secondary { background: #e5e5ea; color: var(--text-main); }
        .btn-secondary:hover { background: #d1d1d6; }
        .btn-success { background: var(--success); color: white; }
        .console { background: #1c1c1e; color: #30d158; padding: 16px; border-radius: 8px; font-family: ui-monospace, Menlo, monospace; font-size: 12px; max-height: 200px; overflow-y: auto; white-space: pre-wrap; margin-top: 20px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid var(--border); }
        th { color: var(--text-sub); font-weight: 600; }
    </style>
</head>
<body>
<div class="container">
    <header>
        <div class="logo">🧹 WeChat Slim <span class="badge" id="accountBadge">正在连接...</span></div>
        <div style="font-size: 13px; color: var(--text-sub);" id="accountPath"></div>
    </header>

    <div class="grid-stats">
        <div class="card">
            <div class="stat-label">微信总占用空间</div>
            <div class="stat-val" id="totalSize">--</div>
            <div class="stat-desc" id="totalFiles">正在扫描数据...</div>
        </div>
        <div class="card">
            <div class="stat-label">可安全释放潜力</div>
            <div class="stat-val" style="color: var(--success);" id="cleanableSize">--</div>
            <div class="stat-desc" id="cleanableRatio">大文件与缓存可瘦身</div>
        </div>
        <div class="card">
            <div class="stat-label">核心数据库与文字消息</div>
            <div class="stat-val" style="color: var(--text-sub);" id="dbSize">--</div>
            <div class="stat-desc">🔒 100% 绝对保护，绝不误删</div>
        </div>
    </div>

    <div class="card" style="margin-bottom: 24px;">
        <div style="font-size: 14px; font-weight: 600; margin-bottom: 8px;">存储空间结构分布</div>
        <div class="progress-bar-container" id="progressBar"></div>
        <div class="legend" id="legend"></div>
    </div>

    <div class="card">
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('slim')">🚀 智能安全瘦身</button>
            <button class="tab-btn" onclick="switchTab('dedup')">🔗 多群查重 (APFS硬链接)</button>
            <button class="tab-btn" onclick="switchTab('details')">📋 存储明细</button>
        </div>

        <!-- 瘦身 Tab -->
        <div id="tab-slim" class="tab-content active">
            <div class="form-row">
                <div class="form-group">
                    <label>时间范围</label>
                    <select id="slimDays">
                        <option value="90" selected>清理 90 天前的文件 (推荐)</option>
                        <option value="30">清理 30 天前的文件 (深度)</option>
                        <option value="180">清理 180 天前的文件 (保守)</option>
                        <option value="0">不限时间 (全量清理)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>单文件大小阈值</label>
                    <select id="slimMinSize">
                        <option value="10MB" selected>大于 10MB 的大文件 (推荐)</option>
                        <option value="20MB">大于 20MB 的超大文件</option>
                        <option value="50MB">大于 50MB 的特大视频/文档</option>
                        <option value="0B">不限大小 (清理所有选定类型)</option>
                    </select>
                </div>
            </div>
            <div class="form-group">
                <label>清理类型</label>
                <div class="checkbox-group">
                    <label><input type="checkbox" id="typeVideo" checked> 聊天视频 (msg/video)</label>
                    <label><input type="checkbox" id="typeFile" checked> 接收的文档 (msg/file)</label>
                    <label><input type="checkbox" id="typeAttach"> 聊天多媒体图片 (msg/attach)</label>
                    <label><input type="checkbox" id="typeCache" checked> 临时运行缓存 (cache/temp)</label>
                </div>
            </div>
            <div class="form-group" style="margin-top: 12px;">
                <label>外置硬盘归档目录 (可选，留空则默认移入废纸篓)</label>
                <input type="text" id="archivePath" placeholder="例如: /Volumes/MySSD/WeChatBackup (自动建立对应文件夹保持结构)">
            </div>
            <div class="btn-group">
                <button class="btn btn-secondary" onclick="executeSlim(true)">🔍 模拟演练 (Dry-Run)</button>
                <button class="btn btn-primary" onclick="executeSlim(false)">⚡ 开始执行安全瘦身</button>
            </div>
        </div>

        <!-- 查重 Tab -->
        <div id="tab-dedup" class="tab-content">
            <p style="font-size: 13px; color: var(--text-sub); margin-bottom: 16px;">
                在多群中被多次转发的相同大文件，将通过 APFS 硬链接秒级去重：所有聊天窗口里依然可正常打开文件，但在物理 SSD 磁盘上只占 1 份空间！
            </p>
            <div class="form-row">
                <div class="form-group">
                    <label>查重文件大小门槛</label>
                    <select id="dedupMinSize">
                        <option value="500KB" selected>大于 500KB (推荐)</option>
                        <option value="1MB">大于 1MB</option>
                        <option value="5MB">大于 5MB (仅查大视频/文档)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>去重动作</label>
                    <select id="dedupAction">
                        <option value="hardlink" selected>替换为 APFS 硬链接 (零风险，强烈推荐)</option>
                        <option value="trash">移入 macOS 废纸篓</option>
                    </select>
                </div>
            </div>
            <div class="btn-group">
                <button class="btn btn-secondary" onclick="scanDedup()">🔍 扫描重复文件</button>
                <button class="btn btn-success" onclick="executeDedup()">🔗 执行硬链接秒级去重</button>
            </div>
            <div id="dedupResults" style="margin-top: 16px;"></div>
        </div>

        <!-- 明细 Tab -->
        <div id="tab-details" class="tab-content">
            <table>
                <thead>
                    <tr><th>目录类别</th><th>占用大小</th><th>文件数量</th><th>占比</th><th>安全状态</th></tr>
                </thead>
                <tbody id="detailsBody"></tbody>
            </table>
        </div>

        <div class="console" id="logConsole">> WeChat Slim 就绪。等待指令...</div>
    </div>
</div>

<script>
    let globalData = null;
    function log(msg) {
        const c = document.getElementById('logConsole');
        c.innerText += '\\n' + msg;
        c.scrollTop = c.scrollHeight;
    }

    function switchTab(name) {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        event.target.classList.add('active');
        document.getElementById('tab-' + name).classList.add('active');
    }

    async function loadStats() {
        log('正在扫描本地微信存储...');
        const res = await fetch('/api/stats');
        globalData = await res.json();
        renderStats();
    }

    function renderStats() {
        if (!globalData || !globalData.account) return;
        document.getElementById('accountBadge').innerText = globalData.account.id + ' (' + globalData.account.version + ')';
        document.getElementById('accountPath').innerText = globalData.account.root_path;
        document.getElementById('totalSize').innerText = globalData.total_size_str;
        document.getElementById('totalFiles').innerText = globalData.total_files + ' 个文件';
        document.getElementById('cleanableSize').innerText = globalData.cleanable_size_str;
        document.getElementById('cleanableRatio').innerText = globalData.cleanable_ratio + '% 空间可被安全瘦身';
        document.getElementById('dbSize').innerText = globalData.categories.db.size_str;

        // Progress Bar
        const bar = document.getElementById('progressBar');
        const legend = document.getElementById('legend');
        bar.innerHTML = '';
        legend.innerHTML = '';

        const colors = { attach: 'bg-attach', video: 'bg-video', file: 'bg-file', cache: 'bg-cache', db: 'bg-db' };
        const hex = { attach: '#0071e3', video: '#5856d6', file: '#34c759', cache: '#ff9500', db: '#8e8e93' };

        for (let k in globalData.categories) {
            const cat = globalData.categories[k];
            const seg = document.createElement('div');
            seg.className = 'progress-seg ' + (colors[k] || 'bg-db');
            seg.style.width = cat.percent + '%';
            seg.title = cat.name + ': ' + cat.size_str;
            bar.appendChild(seg);

            legend.innerHTML += `<div class="legend-item"><span class="legend-dot" style="background:${hex[k] || '#8e8e93'}"></span>${cat.name} (${cat.size_str}, ${cat.percent}%)</div>`;
        }

        // Details Table
        const tbody = document.getElementById('detailsBody');
        tbody.innerHTML = '';
        for (let k in globalData.categories) {
            const cat = globalData.categories[k];
            const badge = cat.is_protected ? '<span style="color:#8e8e93; font-weight:600;">🔒 绝对保护</span>' : '<span style="color:#34c759; font-weight:600;">✓ 可瘦身</span>';
            tbody.innerHTML += `<tr><td><b>${cat.name}</b><br><small style="color:#86868b">${cat.description}</small></td><td>${cat.size_str}</td><td>${cat.file_count}</td><td>${cat.percent}%</td><td>${badge}</td></tr>`;
        }
        log('扫描完成: 微信总占用 ' + globalData.total_size_str + '，可瘦身潜力 ' + globalData.cleanable_size_str);
    }

    async function executeSlim(isDryRun) {
        const types = [];
        if (document.getElementById('typeVideo').checked) types.push('video');
        if (document.getElementById('typeFile').checked) types.push('file');
        if (document.getElementById('typeAttach').checked) types.push('attach');
        if (document.getElementById('typeCache').checked) types.push('cache');

        const payload = {
            days: parseInt(document.getElementById('slimDays').value),
            min_size: document.getElementById('slimMinSize').value,
            types: types.join(','),
            archive_to: document.getElementById('archivePath').value.trim() || null,
            dry_run: isDryRun
        };

        log((isDryRun ? '[演练开始]' : '[开始执行]') + ' 正在处理符合条件的文件...');
        const res = await fetch('/api/clean', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        const data = await res.json();
        log(data.message);
        if (!isDryRun) loadStats();
    }

    async function scanDedup() {
        const minSize = document.getElementById('dedupMinSize').value;
        log('正在计算特征哈希并排查重复副本 (门槛 ' + minSize + ')...');
        const res = await fetch('/api/dedup_scan?min_size=' + minSize);
        const data = await res.json();
        const container = document.getElementById('dedupResults');
        if (data.actionable_groups_count === 0) {
            container.innerHTML = '<div style="color:var(--success); font-weight:600; margin-top:8px;">✓ 太棒了！未发现占用多份空间的重复文件。</div>';
            log('查重完成: 未发现冗余副本。');
            return;
        }
        container.innerHTML = `<div style="margin-top:12px; font-weight:600;">发现 ${data.actionable_groups_count} 组重复文件，共 ${data.total_wasted_copies} 个副本，可节省 ${data.total_saving_str} 物理空间！</div>`;
        log(`查重完成: 发现 ${data.actionable_groups_count} 组重复，可释放 ${data.total_saving_str}`);
    }

    async function executeDedup() {
        const minSize = document.getElementById('dedupMinSize').value;
        const action = document.getElementById('dedupAction').value;
        log('正在执行去重 (模式: ' + action + ')...');
        const res = await fetch('/api/dedup_exec', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ min_size: minSize, action: action })
        });
        const data = await res.json();
        log(data.message);
        loadStats();
    }

    window.onload = loadStats;
</script>
</body>
</html>
"""


class WeChatSlimWebHandler(BaseHTTPRequestHandler):
    """本地轻量级 WebUI HTTP 请求处理器."""
    custom_path: Optional[Path] = None

    def _send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/':
            raw = WEB_UI_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif parsed.path == '/api/stats':
            accounts = discover_accounts(self.custom_path)
            if not accounts:
                self._send_json({'error': '未找到微信账号目录'}, status=404)
                return
            acc = accounts[0]
            categories = scan_account(acc)
            total_bytes = sum(c.total_bytes for c in categories.values())
            cleanable_bytes = sum(c.total_bytes for k, c in categories.items() if not c.is_protected)
            clean_ratio = (cleanable_bytes / total_bytes * 100) if total_bytes > 0 else 0

            cat_dict = {}
            for k, c in categories.items():
                pct = (c.total_bytes / total_bytes * 100) if total_bytes > 0 else 0
                cat_dict[k] = {
                    'name': c.name,
                    'description': c.description,
                    'file_count': f'{c.file_count:,}',
                    'size_bytes': c.total_bytes,
                    'size_str': format_bytes(c.total_bytes),
                    'percent': f'{pct:.1f}',
                    'is_protected': c.is_protected,
                }

            self._send_json({
                'account': {
                    'id': acc.account_id,
                    'version': acc.version_type,
                    'root_path': str(acc.root_path),
                },
                'total_size_bytes': total_bytes,
                'total_size_str': format_bytes(total_bytes),
                'total_files': f'{sum(c.file_count for c in categories.values()):,}',
                'cleanable_size_bytes': cleanable_bytes,
                'cleanable_size_str': format_bytes(cleanable_bytes),
                'cleanable_ratio': f'{clean_ratio:.1f}',
                'categories': cat_dict,
            })
        elif parsed.path == '/api/dedup_scan':
            query = urllib.parse.parse_qs(parsed.query)
            min_size = query.get('min_size', ['500KB'])[0]
            accounts = discover_accounts(self.custom_path)
            if not accounts:
                self._send_json({'error': '未找到微信账号目录'}, status=404)
                return
            acc = accounts[0]
            categories = scan_account(acc)
            groups = find_duplicates(categories, ['video', 'file', 'attach'], min_size_bytes=parse_size_str(min_size))
            actionable = [g for g in groups if g.wasted_count > 0]
            total_saving = sum(g.saving_bytes for g in actionable)
            total_wasted = sum(g.wasted_count for g in actionable)

            self._send_json({
                'actionable_groups_count': len(actionable),
                'total_wasted_copies': total_wasted,
                'total_saving_bytes': total_saving,
                'total_saving_str': format_bytes(total_saving),
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length).decode('utf-8')) if length > 0 else {}

        accounts = discover_accounts(self.custom_path)
        if not accounts:
            self._send_json({'error': '未找到微信账号目录'}, status=404)
            return
        acc = accounts[0]
        categories = scan_account(acc)

        if parsed.path == '/api/clean':
            days = int(body.get('days', 90))
            min_size = parse_size_str(body.get('min_size', '0B'))
            types = [t.strip() for t in body.get('types', 'video,file').split(',') if t.strip()]
            dry_run = bool(body.get('dry_run', True))
            archive_to = Path(body['archive_to']) if body.get('archive_to') else None

            count, freed = execute_slimming(acc, categories, days, min_size, types, dry_run=dry_run, archive_to=archive_to)
            msg = f"[演练完成] 预计影响 {count:,} 个文件，可释放 {format_bytes(freed)} 空间" if dry_run else f"[处理完成] 成功处理 {count:,} 个文件，释放 {format_bytes(freed)} 空间！"
            self._send_json({'count': count, 'freed_bytes': freed, 'freed_str': format_bytes(freed), 'message': msg})
        elif parsed.path == '/api/dedup_exec':
            min_size = parse_size_str(body.get('min_size', '500KB'))
            action = body.get('action', 'hardlink')
            groups = find_duplicates(categories, ['video', 'file', 'attach'], min_size_bytes=min_size)
            actionable = [g for g in groups if g.wasted_count > 0]
            count, freed = execute_dedup(actionable, action=action, dry_run=False)
            msg = f"[去重完成] 成功转换 {count:,} 个重复副本为 APFS 硬链接，物理释放 {format_bytes(freed)} 磁盘空间！" if action == 'hardlink' else f"[去重完成] 成功移入废纸篓 {count:,} 个重复副本，释放 {format_bytes(freed)} 空间！"
            self._send_json({'count': count, 'freed_bytes': freed, 'freed_str': format_bytes(freed), 'message': msg})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """静默默认 HTTP 请求日志，避免刷屏."""
        return


def cmd_web(args: argparse.Namespace) -> None:
    """启动本地轻量 WebUI 大盘."""
    port = getattr(args, 'port', 8080)
    custom_path = getattr(args, 'path', None)
    WeChatSlimWebHandler.custom_path = custom_path

    server = HTTPServer(('127.0.0.1', port), WeChatSlimWebHandler)
    url = f"http://127.0.0.1:{port}"
    print('=' * 66)
    print('       WeChat Slim - 本地可视化图形大盘 (WebUI)')
    print('=' * 66)
    print(f'  • 网页服务已就绪: {url}')
    print('  • 按 Ctrl+C 可停止服务')
    print('-' * 66)

    if not getattr(args, 'no_browser', False):
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[✓] Web 服务已停止。')
        server.server_close()


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
    print('  [5] 智能查重去重 (多群重复转发秒级查重，转换为 APFS 硬链接释放空间)')
    print('  [6] 启动网页大盘 (启动本地现代化 WebUI 并在浏览器中查看)')
    print('  [q] 退出')

    choice = input('\n请输入选项 [1-6/q]: ').strip().lower()
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
    elif choice == '5':
        args = argparse.Namespace(
            types='video,file,attach',
            min_size='500KB',
            action='hardlink',
            dry_run=False,
            force=False,
            path=None,
        )
        cmd_dedup(args)
    elif choice == '6':
        cmd_web(argparse.Namespace(port=8080, path=None, no_browser=False))
    else:
        print('已退出。')


def main() -> None:
    parser = argparse.ArgumentParser(
        description='WeChat Slim - 微信智能存储透视与安全瘦身工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest='subcommand')

    scan_p = subparsers.add_parser('scan', help='扫描并展示微信存储空间深度分布')
    scan_p.add_argument('--path', default=None, help='指定自定义微信存储目录 (默认: 自动发现系统微信目录)')

    clean_p = subparsers.add_parser('clean', help='执行文件瘦身或外置归档')
    clean_p.add_argument('--path', default=None, help='指定自定义微信存储目录 (默认: 自动发现系统微信目录)')
    clean_p.add_argument('--days', type=int, default=90, help='清理多少天前的文件 (默认: 90 天，0 为不限时间)')
    clean_p.add_argument('--min-size', default='0B', help='文件最小大小阈值 (例如: 20MB, 10MB，默认: 0B)')
    clean_p.add_argument('--types', default='video,file,cache', help='清理文件类型，逗号分隔 (可选: video,file,attach,cache)')
    clean_p.add_argument('--dry-run', action='store_true', help='模拟预演，只统计不实际移动任何文件')
    clean_p.add_argument('--archive-to', default=None, help='指定外置移动硬盘或备份目录 (将文件安全移动至该目录，而非废纸篓)')
    clean_p.add_argument('-f', '--force', action='store_true', help='跳过确认提示直接执行')

    dedup_p = subparsers.add_parser('dedup', help='多群转发重复文件智能查重与去重 (Phase 2)')
    dedup_p.add_argument('--path', default=None, help='指定自定义微信存储目录 (默认: 自动发现系统微信目录)')
    dedup_p.add_argument('--types', default='video,file,attach', help='查重类型，逗号分隔 (可选: video,file,attach)')
    dedup_p.add_argument('--min-size', default='500KB', help='查重最小文件大小 (例如: 1MB, 500KB，默认: 500KB)')
    dedup_p.add_argument('--action', choices=['hardlink', 'trash'], default='hardlink', help='去重动作: hardlink (转为硬链接，零风险) 或 trash (移入废纸篓)')
    dedup_p.add_argument('--dry-run', action='store_true', help='模拟预演，只分析展示不实际修改')
    dedup_p.add_argument('-f', '--force', action='store_true', help='跳过确认提示直接执行')

    web_p = subparsers.add_parser('web', help='启动本地可视化大盘 (WebUI Dashboard)')
    web_p.add_argument('--port', type=int, default=8080, help='指定本地网页端口 (默认: 8080)')
    web_p.add_argument('--path', default=None, help='指定自定义微信存储目录 (默认: 自动发现系统微信目录)')
    web_p.add_argument('--no-browser', action='store_true', help='不自动打开默认浏览器')

    args = parser.parse_args()

    if args.subcommand == 'scan':
        cmd_scan(args)
    elif args.subcommand == 'clean':
        cmd_clean(args)
    elif args.subcommand == 'dedup':
        cmd_dedup(args)
    elif args.subcommand == 'web':
        cmd_web(args)
    else:
        interactive_wizard()


if __name__ == '__main__':
    main()

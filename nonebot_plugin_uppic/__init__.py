import os
import re
import time
import uuid
from httpx import AsyncClient
from typing import Any, Dict, List, Set, Tuple
from nonebot.adapters.onebot.v11 import MessageSegment, Message, GroupMessageEvent, Bot
from nonebot.adapters.onebot.v11 import GROUP, GROUP_ADMIN, GROUP_OWNER
from nonebot.plugin import on_command, on_message, on_regex, on_fullmatch
from nonebot.plugin import PluginMetadata
from nonebot.params import Arg, CommandArg, RegexGroup
from nonebot.rule import Rule, to_me
from nonebot import get_driver, Driver
from nonebot.log import logger
from nonebot.matcher import Matcher
import hashlib
import aiosqlite
from urllib import parse
from urllib.parse import urlparse
import importlib
import imagehash
from .config import *
from .config import save_commands_file, is_commands_file_writable, VALID_COMMAND_PATTERN
from .config import save_permissions_file, is_permissions_file_writable
from .config import save_aliases_file, is_aliases_file_writable
from . import config as _config
from .ali_oss import *
from .compress import compress_image_from_bytes, get_image_extension, compute_phash, clear_compress_cache
from .web import StaticImageGalleryGenerator

__plugin_meta__ = PluginMetadata(
    name="随机发送图片",
    description="发送自定义指令后bot会随机发出一张你所存储的图片",
    usage="使用命令：<你设置的指令>",
    type="application",
    homepage="https://github.com/yourusername/nonebot-plugin-uppic",
    config=Config,
    supported_adapters={"nonebot.adapters.onebot.v11"},
)

# 当前生效的指令配置（运行时可变）：{ 指令名 -> 在该群号集合中被禁用 }。
# 直接引用 config 模块的字典对象，保证插件内的所有修改都能在持久化时被一并写回。
current_commands_config: Dict[str, Set[int]] = _config.uppic_commands_config
current_upload_permissions: Dict[int, str] = _config.uppic_upload_permissions
# 别名配置：{ 别名 -> 原指令名 }
current_aliases_config: Dict[str, str] = _config.uppic_aliases_config
uppic_path = Path(uppic_store_dir_path)
uppic_img_path = uppic_path / 'img'
uppic_database_path = uppic_path / 'database'


def _persist_commands() -> bool:
    """把当前指令配置全量写回 JSON。
    若启动时检测到 JSON 损坏，则跳过写入，避免覆盖管理员的原始内容。
    返回是否真的写入。
    """
    if not is_commands_file_writable():
        logger.warning(
            f"{_config.COMMANDS_FILENAME} 处于损坏状态，跳过本次持久化。"
            f"新增的指令仅在本次会话内生效，重启后会丢失。"
        )
        return False
    save_commands_file(uppic_store_dir_path, current_commands_config)
    return True


def _persist_permissions() -> bool:
    """把当前上传权限配置全量写回 JSON。"""
    if not is_permissions_file_writable():
        logger.warning(
            f"{_config.PERMISSIONS_FILENAME} 处于损坏状态，跳过本次持久化。"
            f"权限变更仅在本次会话内生效，重启后会丢失。"
        )
        return False
    save_permissions_file(uppic_store_dir_path, current_upload_permissions)
    return True


def _persist_aliases() -> bool:
    """把当前别名配置全量写回 JSON。"""
    if not is_aliases_file_writable():
        logger.warning(
            f"{_config.ALIASES_FILENAME} 处于损坏状态，跳过本次持久化。"
            f"别名变更仅在本次会话内生效，重启后会丢失。"
        )
        return False
    save_aliases_file(uppic_store_dir_path, current_aliases_config)
    return True


def _resolve_command(name: str) -> str:
    """别名解析：把输入名解析为最终原指令名；找不到别名就原样返回。"""
    if name in current_commands_config:
        return name
    return current_aliases_config.get(name, name)


def _is_super_user(uid: int) -> bool:
    return uid in uppic_super_users


def _can_upload(event: GroupMessageEvent) -> bool:
    """检查用户是否有权限上传图片。
    
    返回 True 的情况：
    1. 用户是超级用户
    2. 用户是群管/群主
    3. 用户在该群的允许用户列表中
    4. 当前群配置为所有群员可上传
    """
    uid = event.user_id
    if uid in uppic_super_users:
        return True
    if event.sender.role in ("admin", "owner"):
        return True
    gid = event.group_id
    config = current_upload_permissions.get(gid, {"mode": "admin_only", "allowed_users": []})
    if uid in config.get("allowed_users", []):
        return True
    return config.get("mode", "admin_only") == "all_members"


uppic_filename: str = 'uppic_{command}_{index}'

connection: aiosqlite.Connection = None

# 每个指令的"最近发送冷却池"，避免短时间内重复发同一张
_recent_sent: Dict[str, Set[int]] = {}

# 激活驱动器
driver = get_driver()


@driver.on_startup
async def _():
    logger.info("正在检查文件...")
    clear_compress_cache()
    await connect()
    await create_dir()
    web_app_init(driver)
    logger.info("文件检查完成，欢迎使用！")

@driver.on_shutdown
async def _():
    clear_compress_cache()

# 连接数据库
async def connect():
    # 创建数据库
    global connection
    if not uppic_database_path.exists():
        uppic_database_path.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(uppic_database_path / "data.db")

async def _is_folder_changed(command: str, folder_path: Path) -> bool:
    cursor = await connection.cursor()
    await cursor.execute(
        "SELECT folder_mtime FROM folder_snapshot WHERE command = ?",
        (command,)
    )
    row = await cursor.fetchone()
    if row is None:
        return True  # 新命令，无记录
    return int(os.path.getmtime(folder_path)) != int(row[0])

async def _save_snapshot(command: str, folder_path: Path):
    await connection.execute(
        """INSERT OR REPLACE INTO folder_snapshot (command, folder_mtime, checked_at)
           VALUES (?, ?, ?)""",
        (command, os.path.getmtime(folder_path), time.time())
    )
    await connection.commit()


# 创建所需文件夹和数据库
async def create_dir():
    command_list = sorted(current_commands_config)

    # 先创建文件夹
    for command in command_list:
        path = uppic_img_path / command
        if not path.exists():
            logger.warning('未找到{path}文件夹，准备创建{path}文件夹...'.format(path=path))
            path.mkdir(parents=True, exist_ok=True)

    cursor = await connection.cursor()
    await cursor.execute('''
        CREATE TABLE IF NOT EXISTS folder_snapshot (
            command      TEXT PRIMARY KEY,
            folder_mtime REAL NOT NULL,
            checked_at   REAL NOT NULL
        );
    ''')
    # 创建表
    for command in command_list:
        if not await _is_folder_changed(command, uppic_img_path / command):
            continue
        await cursor.execute('DROP table if exists Pic_of_{command};'.format(command=command))
        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS Pic_of_{command} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                img_url TEXT NOT NULL,
                phash TEXT NOT NULL
            )
            '''.format(command=command))
        await connection.commit()

    # 读取所有文件夹文件，调整文件夹内图片，并写入数据库
        global uppic_filename
        path: Path = uppic_img_path / command
        uppic_file_list = os.listdir(path)

        # 文件名哈希化
        def get_uuid(command: str):
            return uuid.uuid5(uuid.uuid4(), command).hex
        hash_str = get_uuid(command)
        for i in range(len(uppic_file_list)):
            filename = uppic_file_list[i]
            filename_without_extension, filename_extension = os.path.splitext(filename)
            format_str = uppic_filename.format( command=command, index=str(i + 1).zfill(10) )
            if not filename_extension:
                with (path / filename).open('rb') as f:
                    data = f.read()
                    filename_extension = get_image_extension(data)
            hash_new_filename =  f"{format_str}_{hash_str}{filename_extension}"
            os.rename(path / filename, path / hash_new_filename)

        # 将哈希化的文件名订正为规范名
        uppic_file_list = os.listdir(path)
        for i in range(len(uppic_file_list)):
            hash_filename = uppic_file_list[i]
            new_filename = hash_filename.replace(f"_{hash_str}", '')
            os.rename(path / hash_filename, path / new_filename)

        uppic_file_list = sorted( os.listdir(path) )
        for i in range(len(uppic_file_list)):
            filename = uppic_file_list[i]
            try:
                with (path / filename).open('rb') as f:
                    data = f.read()
                data = compress_image_from_bytes(data)
                with (path / filename).open('wb') as f:
                    f.write(data)

                new_phash_str = compute_phash(data)
                cursor = await connection.cursor()
                await cursor.execute(
                    'INSERT INTO Pic_of_{command}(img_url, phash) VALUES (?, ?)'.format(command=command),
                    (str(Path() / command / filename), new_phash_str))
                await connection.commit()
            except Exception as e:
                logger.warning(f"处理图片 {filename} 失败: {e}，跳过该图片")
                continue

            await _save_snapshot(command, path)

def web_app_init(web_driver: Driver):
    global connection
    try:
        _module = importlib.import_module(
            f"nonebot_plugin_uppic.drivers.{driver.type.split('+')[0]}"
        )
    except ImportError:
        logger.warning(f"Driver {driver.type} not supported")
        return
    
    # 生成静态网站（只生成HTML，不复制图片）
    StaticImageGalleryGenerator(uppic_img_path, uppic_path / 'public').generate_static_site(uppic_oss_no_upload_list)
    if not os.path.exists(uppic_path / 'public'):
        return
    
    # 初始化API配置（传入删除后的回调函数）
    init_app_config_fn = getattr(_module, "init_app_config", None)
    if init_app_config_fn:
        init_app_config_fn(uppic_img_path, uppic_super_users, connection, uppic_oss_no_upload_list, regenerate_web_site, _recent_sent, delete_folder_cleanup)
    
    # 注册路由（挂载HTML + 原图目录 + API）
    register_route = getattr(_module, "register_route")
    register_route(web_driver, uppic_path / 'public', uppic_img_path)
    
    host = str(web_driver.config.host)
    port = web_driver.config.port
    if host in {"0.0.0.0", "127.0.0.1"}:
        host = "localhost"
    logger.opt(colors=True).info(
        f"图片库: <b><u>http://{host}:{port}/uppic/</u></b>"
    )


def regenerate_web_site():
    """重新生成静态网站（在图片增删后调用）"""
    try:
        generator = StaticImageGalleryGenerator(uppic_img_path, uppic_path / 'public')
        generator.generate_static_site(uppic_oss_no_upload_list)
    except Exception as e:
        logger.warning(f"重新生成网站失败: {e}")


async def delete_folder_cleanup(command: str):
    """网页端删除分类后的清理回调：数据库+配置+别名+冷却池+网页"""
    # 1. 删除数据库表和快照
    cursor = await connection.cursor()
    await cursor.execute(f'DROP TABLE IF EXISTS Pic_of_{command}')
    await cursor.execute('DELETE FROM folder_snapshot WHERE command = ?', (command,))
    await connection.commit()
    logger.info(f"已删除数据库表: Pic_of_{command}")

    # 2. 从指令配置中移除
    current_commands_config.pop(command, None)
    _persist_commands()

    # 3. 删除关联别名
    removed_aliases = [a for a, t in current_aliases_config.items() if t == command]
    for a in removed_aliases:
        current_aliases_config.pop(a, None)
    if removed_aliases:
        _persist_aliases()

    # 4. 清除冷却池
    for key in list(_recent_sent.keys()):
        if key == command or _resolve_command(key) == command:
            _recent_sent.pop(key, None)

    # 5. 重新生成网页
    regenerate_web_site()
    logger.info(f"分类「{command}」清理完成，删除别名: {removed_aliases}")


# 单次发图上限（防止刷屏）
MAX_BATCH_COUNT = 3
# 匹配 <指令><数字> ，数字可选；例如 czy / czy1 / czy2 / czy3
_COMMAND_NUM_RE = re.compile(r'^(.+?)([1-9]\d*)?$')


def _split_command_and_count(raw_msg: str):
    """把原始消息拆成 (指令名, 张数)，张数限制在 1~MAX_BATCH_COUNT。

    精确匹配优先：如果 raw_msg 本身就是一个已注册指令或别名，即使末尾带数字也不拆。
    """
    if raw_msg in current_commands_config or raw_msg in current_aliases_config:
        return raw_msg, 1

    m = _COMMAND_NUM_RE.fullmatch(raw_msg)
    if not m:
        return raw_msg, 1
    cmd, num_str = m.group(1), m.group(2)
    if num_str is None:
        return cmd, 1
    try:
        n = int(num_str)
    except ValueError:
        return cmd, 1
    if n < 1:
        n = 1
    if n > MAX_BATCH_COUNT:
        n = MAX_BATCH_COUNT
    return cmd, n


async def _is_known_command(event: GroupMessageEvent) -> bool:
    msg = str(event.get_message()).strip()
    # 精确匹配优先（原指令 + 别名）
    if msg in current_commands_config:
        disabled = current_commands_config[msg]
        return event.group_id not in disabled
    if msg in current_aliases_config:
        target = current_aliases_config[msg]
        disabled = current_commands_config.get(target)
        if disabled is None:
            return False
        return event.group_id not in disabled

    cmd_or_alias, _ = _split_command_and_count(msg)
    resolved = _resolve_command(cmd_or_alias)
    disabled = current_commands_config.get(resolved)
    if disabled is None:
        return False
    return event.group_id not in disabled


picture = on_message(rule=Rule(_is_known_command), permission=GROUP, priority=2, block=True)


@picture.handle()
async def pic(event: GroupMessageEvent):
    if event.group_id in uppic_banner_group:
        return
    global connection
    cursor = await connection.cursor()
    raw_msg = str(event.get_message()).strip()
    cmd_or_alias, count = _split_command_and_count(raw_msg)
    command = _resolve_command(cmd_or_alias)

    # 获取该指令的图片总数，决定冷却池大小 + 实际能抽的张数
    await cursor.execute(f'SELECT COUNT(*) FROM Pic_of_{command}')
    total = (await cursor.fetchone())[0]
    if total == 0:
        await picture.finish('当前还没有图片!')

    # 实际抽取张数不能超过总张数，也不能超过上限
    count = min(count, total, MAX_BATCH_COUNT) if total > 0 else 1

    # 冷却池按原指令名记录（别名共享同一份冷却池）
    cool_size = max(1, total // 2)
    if command not in _recent_sent:
        _recent_sent[command] = set()
    recent = _recent_sent[command]

    selected: List[Tuple[int, str]] = []
    excluded_ids = list(recent)

    # 先尝试排除冷却池抽取 count 张不重复的
    if excluded_ids:
        placeholders = ','.join('?' * len(excluded_ids))
        await cursor.execute(
            f'SELECT id, img_url FROM Pic_of_{command} WHERE id NOT IN ({placeholders}) ORDER BY RANDOM() limit ?',
            [*excluded_ids, count]
        )
        rows = await cursor.fetchall()
        selected.extend(rows)

    # 如果不够 count 张（冷却池占满了或占太多），补齐剩余，必要时允许重复从全体中抽
    if len(selected) < count:
        already = {r[0] for r in selected}
        still_need = count - len(selected)
        await cursor.execute(
            f'SELECT id, img_url FROM Pic_of_{command} ORDER BY RANDOM() limit ?',
            (max(still_need * 2, count),)  # 多抽一些去重
        )
        for row in await cursor.fetchall():
            if row[0] not in already and len(selected) < count:
                selected.append(row)
                already.add(row[0])
        # 如果还不够（图片极少且需要多张），放宽允许重复
        if len(selected) < count:
            for row in await cursor.fetchall():
                if len(selected) < count:
                    selected.append(row)

    if not selected:
        await picture.finish('当前还没有图片!')

    # 全部入选 id 加入冷却池，超过大小时踢掉旧的
    for img_id, _ in selected:
        recent.add(img_id)
    while len(recent) > cool_size:
        recent.pop()

    # 依次发送每张图片（先验证文件存在，不存在则跳过并清理数据库记录）
    fail_cnt = 0
    missing_ids: List[int] = []
    for img_id, file_name in selected:
        file_path = str((uppic_img_path / file_name).resolve())
        if not os.path.isfile(file_path):
            fail_cnt += 1
            missing_ids.append(img_id)
            logger.warning(f"图片文件不存在，跳过发送: {file_name} (id={img_id})")
            continue
        if os.path.getsize(file_path) < 100:
            fail_cnt += 1
            missing_ids.append(img_id)
            logger.warning(f"图片文件过小（可能损坏），跳过: {file_name}")
            continue
        try:
            # 使用 file= 关键字参数明确告诉 NoneBot 这是本地文件，
            # 避免被误判为 URL（sub_type=0）导致 OneBot 去下载失败
            await picture.send(MessageSegment.image(file=file_path))
        except Exception as e:
            fail_cnt += 1
            logger.warning(f"发送图片失败 {file_name}: {e}")

    # 清理丢失的图片的数据库记录，避免后续继续抽到
    if missing_ids:
        try:
            cursor = await connection.cursor()
            placeholders = ','.join('?' * len(missing_ids))
            await cursor.execute(
                f'DELETE FROM Pic_of_{command} WHERE id IN ({placeholders})',
                missing_ids
            )
            await connection.commit()
            logger.info(f"已清理 {len(missing_ids)} 条丢失图片的数据库记录")
            # 从冷却池移除
            if command in _recent_sent:
                _recent_sent[command] = _recent_sent[command] - set(missing_ids)
        except Exception as e:
            logger.error(f"清理丢失记录失败: {e}")

    if fail_cnt == len(selected):
        await picture.send(f'{cmd_or_alias}出不来了，稍后再试试吧~')
    elif fail_cnt > 0:
        await picture.send(f'{cmd_or_alias}有{fail_cnt}张出不来，稍后再试试吧~')


add = on_regex(r"^添加(.+)$", permission=GROUP, priority=2, block=True)


async def _ensure_new_command(command: str) -> None:
    """为新指令建文件夹、建表，并加入运行时配置 + 写回 JSON。"""
    global connection
    path = uppic_img_path / command
    path.mkdir(parents=True, exist_ok=True)
    cursor = await connection.cursor()
    await cursor.execute(
        f'''CREATE TABLE IF NOT EXISTS Pic_of_{command} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            img_url TEXT NOT NULL,
            phash TEXT NOT NULL
        )'''
    )
    await connection.commit()
    current_commands_config.setdefault(command, set())
    _persist_commands()
    logger.info(f"已动态创建新指令分类：{command}")


@add.handle()
async def add_check_reply(matcher: Matcher, event: GroupMessageEvent):
    """如果用户通过引用回复图片来添加，提前提取图片到 pic 参数，跳过 got 提示。"""
    # 当前消息已包含图片，走正常流程
    if any(seg.type == 'image' for seg in event.get_message()):
        return
    # 尝试从引用回复中提取图片
    if event.reply:
        images = [seg for seg in event.reply.message if seg.type == 'image']
        if images:
            matcher.set_arg("pic", Message(images))


@add.got("pic", prompt="请发送图片！")
async def add_pic(bot: Bot, event: GroupMessageEvent, matched: Tuple[Any, ...] = RegexGroup(), pic_list: Message = Arg('pic')):
    if not _can_upload(event):
        await add.finish("你没有上传图片的权限！")
    
    global connection
    input_name = matched[0].strip()
    command = _resolve_command(input_name)  # 别名→原指令

    if not VALID_COMMAND_PATTERN.fullmatch(input_name):
        await add.finish("名称仅支持字母、汉字和数字，且不能为空！")

    # 若目标指令不存在，按输入的原名（非别名）建一个新指令
    if command not in current_commands_config:
        command = input_name
        try:
            await _ensure_new_command(command)
        except Exception as e:
            logger.warning(e)
            await add.finish(f"创建新分类「{command}」失败！")
        msg = f"已新建分类「{command}」，本张图片将作为首张保存。"
        if not is_commands_file_writable():
            msg += f"\n⚠️ {_config.COMMANDS_FILENAME} 损坏，本次新增不会持久化，重启后会丢失。请管理员尽快修复 JSON。"
        await add.send(msg)

    cursor = await connection.cursor()

    for pic_name in pic_list:
        if pic_name.type != 'image':
            await add.send(MessageSegment.text("\n输入格式有误，请重新触发指令！"), at_sender=True)
            continue
        pic_url = pic_name.data.get('url', '')
        file_id = pic_name.data.get('file', '')
        if not pic_url and not file_id:
            logger.warning("图片消息缺少 url 和 file 字段，跳过")
            await add.send(MessageSegment.text('\n这张图片无法获取，跳过了'))
            continue

        data = None
        import base64 as _b64

        # 方案1: 通过 OneBot get_image API 获取本地缓存图片
        if file_id:
            try:
                img_info = await bot.get_image(file=file_id)
                logger.debug(f"get_image 返回: {img_info}")
                if isinstance(img_info, dict):
                    local_path = img_info.get('file') or img_info.get('filename') or ''
                    if local_path.startswith('file://'):
                        local_path = local_path[7:]
                    # 尝试直接读取本地缓存文件
                    if local_path:
                        try:
                            with open(local_path, 'rb') as f:
                                data = f.read()
                            logger.info(f"通过本地缓存获取图片: {len(data)} bytes")
                        except Exception as read_err:
                            logger.debug(f"本地文件读取失败: {read_err}")
                    # 尝试 base64 字段
                    if not data and img_info.get('base64'):
                        data = _b64.b64decode(img_info['base64'])
                        logger.info(f"通过 base64 获取图片: {len(data)} bytes")
                    # 使用 get_image 返回的 URL
                    if not data and img_info.get('url'):
                        pic_url = img_info['url']
            except Exception as e:
                logger.debug(f"get_image API 失败: {e}")

        # 方案1.5: 通过 NapCat get_file API 获取 base64（绕过文件权限问题）
        if not data and file_id:
            try:
                file_info = await bot.call_api("get_file", file=file_id)
                logger.debug(f"get_file 返回: {file_info}")
                if isinstance(file_info, dict):
                    if file_info.get('base64'):
                        data = _b64.b64decode(file_info['base64'])
                        logger.info(f"通过 get_file base64 获取图片: {len(data)} bytes")
                    elif file_info.get('file'):
                        try:
                            with open(file_info['file'], 'rb') as f:
                                data = f.read()
                            logger.info(f"通过 get_file 本地路径获取图片: {len(data)} bytes")
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"get_file API 失败: {e}")

        # 方案2: httpx 直接下载 URL（带浏览器请求头）
        if not data and pic_url:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://qpic.cn/",
                "Accept": "image/*,*/*",
            }
            try:
                async with AsyncClient(verify=True, timeout=15.0, follow_redirects=True) as client:
                    resp = await client.get(pic_url, headers=headers)
                    resp.raise_for_status()
                    data = resp.content
            except Exception as e:
                logger.warning(f"下载图片失败(尝试1): {e}")
                try:
                    async with AsyncClient(verify=False, timeout=15.0, follow_redirects=True) as client:
                        resp = await client.get(pic_url, headers=headers)
                        resp.raise_for_status()
                        data = resp.content
                        logger.info("图片下载成功(重试)")
                except Exception as e2:
                    logger.warning(f"下载图片失败(重试): {e2}")

        # 方案3: 通过 sudo cat 读取 NapCat 缓存文件（权限不足时的兜底）
        if not data and file_id:
            try:
                img_info2 = await bot.get_image(file=file_id)
                lp = (img_info2 or {}).get('file', '') if isinstance(img_info2, dict) else ''
                if lp:
                    import subprocess
                    result = subprocess.run(['sudo', 'cat', lp], capture_output=True, timeout=10)
                    if result.returncode == 0 and len(result.stdout) > 50:
                        data = result.stdout
                        logger.info(f"通过 sudo cat 获取图片: {len(data)} bytes")
            except Exception as e:
                logger.debug(f"sudo cat 读取失败: {e}")

        if not data or len(data) < 50:
            logger.warning(f"图片下载失败，跳过 (大小: {len(data) if data else 0})")
            await add.send(MessageSegment.text(
                '\n图片获取失败！可能是：\n'
                '1. 图片URL已过期（引用了旧消息）→ 请直接发送图片\n'
                '2. NapCat缓存文件权限不足 → 请将NoneBot以root运行，或执行: chmod -R 755 /root/.config/QQ/'
            ))
            continue
        data = compress_image_from_bytes(data)  # 若图片超规格，压缩图片
        new_phash_str = compute_phash(data)

        # 去重检查（仅记录，不阻断保存；相似图会标记但仍保存）
        is_duplicate = False
        if new_phash_str:
            try:
                new_phash = imagehash.hex_to_hash(new_phash_str)
                await cursor.execute(f'SELECT phash FROM Pic_of_{command}')
                existing = await cursor.fetchall()
                SIMILARITY_THRESHOLD = 5
                for ex_phash_str, in existing:
                    if ex_phash_str:
                        try:
                            ex_phash = imagehash.hex_to_hash(ex_phash_str)
                            if (new_phash - ex_phash) < SIMILARITY_THRESHOLD:
                                is_duplicate = True
                                logger.info("检测到相似图，仍允许保存（以新图为准）")
                                break
                        except Exception:
                            continue
            except Exception as e:
                logger.warning(f"图片去重检查失败: {e}，跳过去重")

        # 先保存文件和数据库，确保即使后续发送失败，图片也已入库
        uppic_cur_picnum = len(os.listdir(uppic_img_path / command))
        file_name = (uppic_filename.format(command=command, index=str(uppic_cur_picnum + 1).zfill(10))
                    + get_image_extension(data))
        file_path = uppic_img_path / command / file_name

        try:
            with file_path.open("wb") as f:
                f.write(data)
            await cursor.execute('insert into Pic_of_{command}(img_url, phash) values (?, ?)'.format(command=command),
                                 (str(Path() / command / file_name), new_phash_str))
            await connection.commit()
        except Exception as e:
            logger.warning(e)
            await add.send(MessageSegment.text("\n导入失败！"), at_sender=True)
            continue

        # 保存成功后再发送提示
        try:
            msg = "\n导入成功！"
            if is_duplicate:
                msg += "（与已有图片相似，仍已保存）"
            if isOss and command not in uppic_oss_no_upload_list:
                msg += f'可去 {endpoint}/{parse.quote(command)}/ 查看'
                StaticImageGalleryGenerator(uppic_img_path, uppic_path / 'public').generate_command_html(command, file_name, uppic_oss_no_upload_list)
                await OSSUploaderV2().upload_file(str(uppic_path/ 'public' / command / 'index.html'), f'{command}/index.html')
                await OSSUploaderV2().upload_file(str(uppic_path / 'public' / 'index.html'), 'index.html')
                await OSSUploaderV2().upload_file(file_path, f'{command}/{file_name}')
            _recent_sent.pop(command, None)
            regenerate_web_site()
            await add.send(MessageSegment.text(msg), at_sender=True)
        except Exception as e:
            logger.warning(f"发送确认消息失败（但图片已保存）: {e}")

OSS = on_fullmatch('上传oss', ignorecase=True, permission=GROUP_ADMIN | GROUP_OWNER, priority=1, block=True, )
@OSS.handle()
async def handle_oss(event: GroupMessageEvent) -> None:
    if not isOss:
        return
    await OSS.send('正在上传至OSS...')
    start_time = time.time()

    StaticImageGalleryGenerator(uppic_img_path, uppic_path / 'public').generate_static_site(uppic_oss_no_upload_list)
    await OSSUploaderV2().upload_folder(str(uppic_path / 'public'))

    end_time = time.time()
    elapsed_time = end_time - start_time
    await OSS.finish(f'上传完成，用时: {elapsed_time:.2f}秒，地址：{endpoint}/')


# 分群管理：@bot 禁用<指令> / @bot 启用<指令>
disable = on_command("禁用", rule=to_me(), permission=GROUP_ADMIN | GROUP_OWNER, priority=2, block=True)


@disable.handle()
async def handle_disable(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    input_name = args.extract_plain_text().strip()
    if not input_name:
        await disable.finish("请指定要禁用的指令名！例如：@bot 禁用 capoo")
    if not VALID_COMMAND_PATTERN.fullmatch(input_name):
        await disable.finish("指令名称仅支持字母、汉字和数字！")
    command = _resolve_command(input_name)
    if command not in current_commands_config:
        await disable.finish(f"指令「{input_name}」不存在！")
    gid = event.group_id
    disabled = current_commands_config[command]
    if gid in disabled:
        await disable.finish(f"指令「{input_name}」在本群已经是禁用状态。")
    disabled.add(gid)
    msg = f"已在本群禁用指令「{input_name}」。"
    if not _persist_commands():
        msg += f"\n⚠️ {_config.COMMANDS_FILENAME} 损坏，本次禁用不会持久化，重启后会丢失。请管理员尽快修复 JSON。"
    await disable.finish(msg)


enable = on_command("启用", rule=to_me(), permission=GROUP_ADMIN | GROUP_OWNER, priority=2, block=True)


@enable.handle()
async def handle_enable(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    input_name = args.extract_plain_text().strip()
    if not input_name:
        await enable.finish("请指定要启用的指令名！例如：@bot 启用 capoo")
    if not VALID_COMMAND_PATTERN.fullmatch(input_name):
        await enable.finish("指令名称仅支持字母、汉字和数字！")
    command = _resolve_command(input_name)
    if command not in current_commands_config:
        await enable.finish(f"指令「{input_name}」不存在！")
    gid = event.group_id
    disabled = current_commands_config[command]
    if gid not in disabled:
        await enable.finish(f"指令「{input_name}」在本群本来就没有被禁用。")
    disabled.discard(gid)
    msg = f"已在本群重新启用指令「{input_name}」。"
    if not _persist_commands():
        msg += f"\n⚠️ {_config.COMMANDS_FILENAME} 损坏，本次启用不会持久化，重启后会丢失。请管理员尽快修复 JSON。"
    await enable.finish(msg)


upload_permission = on_command("上传权限", rule=to_me(), priority=2, block=True)


@upload_permission.handle()
async def handle_upload_permission(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    uid = event.user_id
    if uid not in uppic_super_users:
        await upload_permission.finish("你没有权限管理上传权限！")
    
    params = args.extract_plain_text().strip().split()
    if len(params) != 2:
        await upload_permission.finish("用法：@bot 上传权限 <群号> <权限模式>\n权限模式：admin_only（仅群管）/ all_members（所有群员）")
    
    try:
        gid = int(params[0])
    except ValueError:
        await upload_permission.finish("群号必须是数字！")
    
    mode = params[1]
    if mode not in ("admin_only", "all_members"):
        await upload_permission.finish("权限模式必须是 admin_only 或 all_members！")
    
    current_upload_permissions.setdefault(gid, {"mode": "admin_only", "allowed_users": []})
    current_upload_permissions[gid]["mode"] = mode
    msg = f"已设置群 {gid} 的上传权限为「{mode}」。"
    if not _persist_permissions():
        msg += f"\n⚠️ {_config.PERMISSIONS_FILENAME} 损坏，本次设置不会持久化。"
    await upload_permission.finish(msg)


add_upload_user = on_command("添加上传用户", rule=to_me(), priority=2, block=True)


@add_upload_user.handle()
async def handle_add_upload_user(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    uid = event.user_id
    if uid not in uppic_super_users:
        await add_upload_user.finish("你没有权限管理上传权限！")
    
    params = args.extract_plain_text().strip().split()
    if len(params) != 2:
        await add_upload_user.finish("用法：@bot 添加上传用户 <群号> <用户ID>")
    
    try:
        gid = int(params[0])
        target_uid = int(params[1])
    except ValueError:
        await add_upload_user.finish("群号和用户ID必须是数字！")
    
    current_upload_permissions.setdefault(gid, {"mode": "admin_only", "allowed_users": []})
    if target_uid in current_upload_permissions[gid]["allowed_users"]:
        await add_upload_user.finish(f"用户 {target_uid} 已经在群 {gid} 的允许列表中！")
    
    current_upload_permissions[gid]["allowed_users"].append(target_uid)
    msg = f"已将用户 {target_uid} 添加到群 {gid} 的上传允许列表。"
    if not _persist_permissions():
        msg += f"\n⚠️ {_config.PERMISSIONS_FILENAME} 损坏，本次设置不会持久化。"
    await add_upload_user.finish(msg)


remove_upload_user = on_command("移除上传用户", rule=to_me(), priority=2, block=True)


@remove_upload_user.handle()
async def handle_remove_upload_user(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    uid = event.user_id
    if uid not in uppic_super_users:
        await remove_upload_user.finish("你没有权限管理上传权限！")
    
    params = args.extract_plain_text().strip().split()
    if len(params) != 2:
        await remove_upload_user.finish("用法：@bot 移除上传用户 <群号> <用户ID>")
    
    try:
        gid = int(params[0])
        target_uid = int(params[1])
    except ValueError:
        await remove_upload_user.finish("群号和用户ID必须是数字！")
    
    config = current_upload_permissions.get(gid)
    if not config or target_uid not in config.get("allowed_users", []):
        await remove_upload_user.finish(f"用户 {target_uid} 不在群 {gid} 的允许列表中！")
    
    current_upload_permissions[gid]["allowed_users"].remove(target_uid)
    msg = f"已将用户 {target_uid} 从群 {gid} 的上传允许列表移除。"
    if not _persist_permissions():
        msg += f"\n⚠️ {_config.PERMISSIONS_FILENAME} 损坏，本次设置不会持久化。"
    await remove_upload_user.finish(msg)


delete_pic = on_regex(r"^删除图片(.+)$", permission=GROUP_ADMIN | GROUP_OWNER, priority=2, block=True)

delete_session_data: Dict[int, Dict[str, Any]] = {}

PAGE_SIZE = 4


async def _get_image_list(command: str) -> List[Dict[str, Any]]:
    cursor = await connection.cursor()
    await cursor.execute(f'SELECT id, img_url FROM Pic_of_{command} ORDER BY id')
    rows = await cursor.fetchall()
    result = []
    for row in rows:
        result.append({"id": row[0], "img_url": row[1]})
    return result


async def _send_image_page(images: List[Dict[str, Any]], page: int):
    total = len(images)
    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    page_images = images[start:end]
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    await delete_pic.send(f"=== 第 {page} 页 / 共 {total_pages} 页 ===")

    for i, img in enumerate(page_images, start + 1):
        file_path = str((uppic_img_path / img["img_url"]).resolve())
        if os.path.exists(file_path):
            await delete_pic.send(f"序号 {i}:")
            await delete_pic.send(MessageSegment.image(file_path))
        else:
            await delete_pic.send(f"序号 {i}: {img['img_url']} (文件不存在)")


@delete_pic.handle()
async def handle_delete_pic(matched: Tuple[Any, ...] = RegexGroup()):
    input_name = matched[0].strip()
    if not VALID_COMMAND_PATTERN.fullmatch(input_name):
        await delete_pic.finish("指令名称仅支持字母、汉字和数字！")
    command = _resolve_command(input_name)
    if command not in current_commands_config:
        await delete_pic.finish(f"指令「{input_name}」不存在！")

    images = await _get_image_list(command)
    if not images:
        await delete_pic.finish(f"指令「{input_name}」下没有图片！")

    total_pages = (len(images) + PAGE_SIZE - 1) // PAGE_SIZE
    delete_session_data[id(delete_pic)] = {
        "command": command,
        "input_name": input_name,
        "images": images,
        "total_pages": total_pages,
        "state": "select_page"
    }

    await delete_pic.send(f"删除图片「{input_name}」，共 {len(images)} 张图片，共 {total_pages} 页。\n请输入页码（1-{total_pages}），或发送「取消」放弃删除。")


@delete_pic.got("input", prompt="请输入页码")
async def process_delete_input(event: GroupMessageEvent, input_msg: Message = Arg('input')):
    session_key = id(delete_pic)
    if session_key not in delete_session_data:
        await delete_pic.finish("会话已过期，请重新触发删除指令！")

    config = delete_session_data[session_key]
    command = config["command"]
    images = config["images"]
    total_pages = config["total_pages"]
    state = config["state"]

    input_text = input_msg.extract_plain_text().strip()

    if input_text == "取消":
        del delete_session_data[session_key]
        await delete_pic.finish("已取消删除！")

    if state == "select_page":
        try:
            page = int(input_text)
        except ValueError:
            await delete_pic.finish("输入格式错误！请输入数字页码。")

        if page < 1 or page > total_pages:
            await delete_pic.finish(f"页码无效！请输入 1-{total_pages} 之间的数字。")

        config["current_page"] = page
        config["state"] = "select_image"

        await _send_image_page(images, page)
        start = (page - 1) * PAGE_SIZE + 1
        end = min(page * PAGE_SIZE, len(images))
        await delete_pic.send(f"请输入要删除的序号（{start}-{end}），或发送「取消」放弃删除。")
        await delete_pic.reject()

    elif state == "select_image":
        try:
            indices = [int(x.strip()) for x in input_text.split() if x.strip()]
        except ValueError:
            await delete_pic.finish("输入格式错误！请输入数字序号，用空格分隔。")

        if not indices:
            await delete_pic.finish("请输入至少一个序号！")

        del delete_session_data[session_key]

        valid_indices = []
        for idx in indices:
            if 1 <= idx <= len(images):
                valid_indices.append(idx)
            else:
                await delete_pic.send(f"序号 {idx} 无效，已跳过")

        if not valid_indices:
            await delete_pic.finish("没有有效序号！")

        deleted_count = 0
        for idx in sorted(valid_indices, reverse=True):
            img_info = images[idx - 1]
            img_id = img_info["id"]
            img_url = img_info["img_url"]
            file_path = uppic_img_path / img_url

            try:
                if file_path.exists():
                    os.remove(file_path)
                cursor = await connection.cursor()
                await cursor.execute(f'DELETE FROM Pic_of_{command} WHERE id = ?', (img_id,))
                await connection.commit()

                # 从冷却池中删除已删 id，避免长期堆积无效数据
                recent = _recent_sent.get(command)
                if recent and img_id in recent:
                    recent.discard(img_id)

                if isOss and command not in uppic_oss_no_upload_list:
                    try:
                        oss_key = f'{command}/{os.path.basename(img_url)}'
                        await OSSUploaderV2().delete_file(oss_key)
                    except Exception as e:
                        logger.warning(f"删除 OSS 文件失败: {e}")

                deleted_count += 1
                logger.info(f"已删除图片: {img_url}")
            except Exception as e:
                logger.warning(f"删除图片 {img_url} 失败: {e}")
                await delete_pic.send(f"删除第 {idx} 张图片失败: {e}")

        if deleted_count > 0:
            msg = f"成功删除 {deleted_count} 张图片！"
            if isOss and command not in uppic_oss_no_upload_list:
                StaticImageGalleryGenerator(uppic_img_path, uppic_path / 'public').generate_command_html(command, "", uppic_oss_no_upload_list)
                await OSSUploaderV2().upload_file(str(uppic_path / 'public' / command / 'index.html'), f'{command}/index.html')
                await OSSUploaderV2().upload_file(str(uppic_path / 'public' / 'index.html'), 'index.html')
            # 刷新静态网站
            regenerate_web_site()
            await delete_pic.finish(msg)
        else:
            await delete_pic.finish("删除失败！")


# ====================== 别名管理（仅限超级用户） ======================

# 用法：添加别名 <原指令> <别名>
add_alias = on_regex(r"^添加别名\s+(.+)$", priority=1, block=True)


@add_alias.handle()
async def handle_add_alias(event: GroupMessageEvent, matched: Tuple[Any, ...] = RegexGroup()):
    uid = event.user_id
    if not _is_super_user(uid):
        await add_alias.finish("只有超级用户可以管理别名！")

    params = matched[0].strip().split()
    if len(params) != 2:
        await add_alias.finish("用法：添加别名 <原指令> <别名>\n例如：添加别名 插蒺蒼 czy")

    original, alias = params[0].strip(), params[1].strip()

    if not VALID_COMMAND_PATTERN.fullmatch(original):
        await add_alias.finish(f"原指令「{original}」非法，仅支持字母、汉字、数字！")
    if not VALID_COMMAND_PATTERN.fullmatch(alias):
        await add_alias.finish(f"别名「{alias}」非法，仅支持字母、汉字、数字！")

    if original not in current_commands_config:
        await add_alias.finish(f"原指令「{original}」不存在！请先通过「添加{original}」创建该指令。")

    # 冲突检查：别名不能与已注册指令名相同
    if alias in current_commands_config:
        await add_alias.finish(f"别名「{alias}」与已存在的指令名冲突，请换一个。")

    # 覆盖写入：同一别名重复设置时，更新到最新原指令
    current_aliases_config[alias] = original
    msg = f"别名设置成功：「{alias}」 → 「{original}」"
    if not _persist_aliases():
        msg += f"\n⚠️ {_config.ALIASES_FILENAME} 损坏，本次设置不会持久化，重启后会丢失。"
    await add_alias.finish(msg)


# 用法：删除别名 <别名>
remove_alias = on_regex(r"^删除别名\s+(.+)$", priority=1, block=True)


@remove_alias.handle()
async def handle_remove_alias(event: GroupMessageEvent, matched: Tuple[Any, ...] = RegexGroup()):
    uid = event.user_id
    if not _is_super_user(uid):
        await remove_alias.finish("只有超级用户可以管理别名！")

    alias = matched[0].strip()
    if not VALID_COMMAND_PATTERN.fullmatch(alias):
        await remove_alias.finish("别名非法，仅支持字母、汉字、数字！")

    if alias not in current_aliases_config:
        await remove_alias.finish(f"别名「{alias}」不存在！")

    target = current_aliases_config.pop(alias)
    msg = f"已删除别名：「{alias}」 → 「{target}」"
    if not _persist_aliases():
        msg += f"\n⚠️ {_config.ALIASES_FILENAME} 损坏，本次删除不会持久化。"
    await remove_alias.finish(msg)


# 用法：别名列表
list_alias = on_fullmatch("别名列表", ignorecase=True, priority=1, block=True)


@list_alias.handle()
async def handle_list_alias(event: GroupMessageEvent):
    uid = event.user_id
    if not _is_super_user(uid):
        await list_alias.finish("只有超级用户可以查看别名列表！")

    if not current_aliases_config:
        await list_alias.finish("当前没有设置任何别名。")

    lines = [f"共 {len(current_aliases_config)} 个别名："]
    # 按原指令分组展示，方便管理
    grouped: Dict[str, List[str]] = {}
    for alias, target in current_aliases_config.items():
        grouped.setdefault(target, []).append(alias)
    for target, aliases in sorted(grouped.items()):
        aliases_str = "、".join(sorted(aliases))
        lines.append(f"「{target}」← {aliases_str}")

    await list_alias.finish("\n".join(lines))


# ====================== 删除分类（仅限超级用户） ======================

# 用法：删除分类 <指令名>
delete_category = on_regex(r"^删除分类\s+(.+)$", priority=1, block=True)


@delete_category.handle()
async def handle_delete_category(event: GroupMessageEvent, matched: Tuple[Any, ...] = RegexGroup()):
    uid = event.user_id
    if not _is_super_user(uid):
        await delete_category.finish("只有超级用户可以删除分类！")

    input_name = matched[0].strip()
    if not VALID_COMMAND_PATTERN.fullmatch(input_name):
        await delete_category.finish("指令名称仅支持字母、汉字和数字！")

    command = _resolve_command(input_name)
    if command not in current_commands_config:
        await delete_category.finish(f"指令「{input_name}」不存在！")

    # 二次确认
    folder_path = uppic_img_path / command
    img_count = 0
    if folder_path.exists():
        img_count = len([f for f in os.listdir(folder_path) if f != '.gitkeep'])

    delete_category.set_arg("confirm_target", command)
    await delete_category.send(
        f"即将删除分类「{command}」：\n"
        f"  - 文件夹：{folder_path}\n"
        f"  - 图片数量：{img_count}\n"
        f"  - 数据库表：Pic_of_{command}\n"
        f"  - 相关别名也会一并清除\n\n"
        f"确认删除请发送「确认」，发送其他内容取消。"
    )


@delete_category.got("confirm")
async def confirm_delete_category(event: GroupMessageEvent, confirm: Message = Arg('confirm')):
    command = delete_category.get_arg("confirm_target")
    if command is None:
        await delete_category.finish("会话已过期，请重新触发删除指令！")

    text = confirm.extract_plain_text().strip()
    if text != "确认":
        await delete_category.finish(f"已取消删除分类「{command}」。")

    # 1. 删除文件夹及内部文件
    folder_path = uppic_img_path / command
    deleted_files = 0
    if folder_path.exists():
        import shutil
        for f in os.listdir(folder_path):
            file_full = folder_path / f
            if file_full.is_file():
                file_full.unlink()
                deleted_files += 1
        shutil.rmtree(folder_path, ignore_errors=True)
        logger.info(f"已删除文件夹: {folder_path} ({deleted_files} 个文件)")

    # 2. 删除数据库表和快照记录
    cursor = await connection.cursor()
    await cursor.execute(f'DROP TABLE IF EXISTS Pic_of_{command}')
    await cursor.execute('DELETE FROM folder_snapshot WHERE command = ?', (command,))
    await connection.commit()
    logger.info(f"已删除数据库表: Pic_of_{command}")

    # 3. 从指令配置中移除
    current_commands_config.pop(command, None)
    _persist_commands()

    # 4. 删除所有指向该指令的别名
    removed_aliases = [a for a, t in current_aliases_config.items() if t == command]
    for a in removed_aliases:
        current_aliases_config.pop(a, None)
    if removed_aliases:
        _persist_aliases()

    # 5. 从冷却池中清除
    for key in list(_recent_sent.keys()):
        if key == command or _resolve_command(key) == command:
            _recent_sent.pop(key, None)

    # 6. 重新生成网页
    try:
        StaticImageGalleryGenerator(uppic_img_path, uppic_path / 'public').generate_static_site(uppic_oss_no_upload_list)
    except Exception as e:
        logger.warning(f"重新生成网页失败: {e}")

    msg = f"分类「{command}」已删除！\n  - 删除文件：{deleted_files} 个\n  - 删除别名：{len(removed_aliases)} 个"
    if removed_aliases:
        msg += f"（{'、'.join(removed_aliases)}）"
    await delete_category.finish(msg)

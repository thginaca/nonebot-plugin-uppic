import time
import uuid
from httpx import AsyncClient
import ssl
from typing import Any, Dict, List, Set, Tuple
from nonebot.adapters.onebot.v11 import MessageSegment, Message, GroupMessageEvent
from nonebot.adapters.onebot.v11 import GROUP, GROUP_ADMIN, GROUP_OWNER
from nonebot.plugin import on_command, on_message, on_regex, on_fullmatch
from nonebot.plugin import PluginMetadata
from nonebot.params import Arg, CommandArg, RegexGroup
from nonebot.rule import Rule, to_me
from nonebot import get_driver, Driver
from nonebot.log import logger
import hashlib
import aiosqlite
from urllib import parse
from urllib.parse import urlparse
import importlib
import imagehash
from .config import *
from .config import save_commands_file, is_commands_file_writable, VALID_COMMAND_PATTERN
from .config import save_permissions_file, is_permissions_file_writable
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

connection: aiosqlite.Connection

# 激活驱动器
driver = get_driver()


@driver.on_startup
async def _():
    logger.info("正在检查文件...")
    clear_compress_cache()
    await connect()
    await create_dir()
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
        init_app_config_fn(uppic_img_path, uppic_super_users, connection, uppic_oss_no_upload_list, regenerate_web_site)
    
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

web_app_init(driver)


def regenerate_web_site():
    """重新生成静态网站（在图片增删后调用）"""
    try:
        generator = StaticImageGalleryGenerator(uppic_img_path, uppic_path / 'public')
        generator.generate_static_site(uppic_oss_no_upload_list)
    except Exception as e:
        logger.warning(f"重新生成网站失败: {e}")


async def _is_known_command(event: GroupMessageEvent) -> bool:
    msg = str(event.get_message()).strip()
    disabled = current_commands_config.get(msg)
    if disabled is None:
        return False
    # 在本群被禁用：rule 直接不匹配，避免 block=True 拦下其它插件
    return event.group_id not in disabled


picture = on_message(rule=Rule(_is_known_command), permission=GROUP, priority=2, block=True)


@picture.handle()
async def pic(event: GroupMessageEvent):
    if event.group_id in uppic_banner_group:
        return
    global connection
    cursor = await connection.cursor()
    command = str(event.get_message()).strip()
    await cursor.execute(f'SELECT img_url FROM Pic_of_{command} ORDER BY RANDOM() limit 1')
    data = await cursor.fetchone()
    if data is None:
        await picture.finish('当前还没有图片!')
    file_name = data[0]
    img = uppic_img_path / file_name
    try:
        await picture.send(MessageSegment.image(img))
    except Exception as e:
        logger.info(e)
        await picture.send(f'{command}出不来了，稍后再试试吧~')


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


@add.got("pic", prompt="请发送图片！")
async def add_pic(event: GroupMessageEvent, matched: Tuple[Any, ...] = RegexGroup(), pic_list: Message = Arg('pic')):
    if not _can_upload(event):
        await add.finish("你没有上传图片的权限！")
    
    global connection
    command = matched[0].strip()

    if not VALID_COMMAND_PATTERN.fullmatch(command):
        await add.finish("名称仅支持字母、汉字和数字，且不能为空！")

    # 若是新指令，先建文件夹与数据表，再续接添加流程
    if command not in current_commands_config:
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
            await add.send(pic_name + MessageSegment.text("\n输入格式有误，请重新触发指令！"), at_sender=True)
            continue
        pic_url = pic_name.data['url']

        ssl_context = ssl.create_default_context()
        ssl_context.set_ciphers("DEFAULT")
        async with AsyncClient(verify=ssl_context) as client:
            resp = await client.get(pic_url, timeout=5.0)

        try:
            resp.raise_for_status()
        except Exception as e:
            logger.warning(e)
            await add.send(
                pic_name +
                MessageSegment.text('\n保存出错了，这张请重试')
            )
            continue

        data = resp.content
        data = compress_image_from_bytes(data)  # 若图片超规格，压缩图片
        new_phash_str = compute_phash(data)

        if new_phash_str:
            try:
                new_phash = imagehash.hex_to_hash(new_phash_str)
                await cursor.execute(f'SELECT phash FROM Pic_of_{command}')
                existing = await cursor.fetchall()
                SIMILARITY_THRESHOLD = 5  # 汉明距离 ≤5 认为相似
                for ex_phash_str, in existing:
                    if ex_phash_str:
                        try:
                            ex_phash = imagehash.hex_to_hash(ex_phash_str)
                            if (new_phash - ex_phash) < SIMILARITY_THRESHOLD:
                                await add.finish(pic_name + Message('\n这张已经有相似图，不能重复添加！'))
                        except Exception:
                            continue
            except Exception as e:
                logger.warning(f"图片去重检查失败: {e}，跳过去重")

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
            await add.finish(pic_name + Message("\n导入失败！"), at_sender=True)

        msg = "\n导入成功！"
        if isOss and command not in uppic_oss_no_upload_list:
            msg += f'可去 {endpoint}/{parse.quote(command)}/ 查看'
            # StaticImageGalleryGenerator(uppic_img_path, uppic_path / 'public').generate_static_site()
            StaticImageGalleryGenerator(uppic_img_path, uppic_path / 'public').generate_command_html(command, file_name, uppic_oss_no_upload_list)
            await OSSUploaderV2().upload_file(str(uppic_path/ 'public' / command / 'index.html'), f'{command}/index.html') # 修改index.html文件
            await OSSUploaderV2().upload_file(str(uppic_path / 'public' / 'index.html'), 'index.html')
            await OSSUploaderV2().upload_file(file_path, f'{command}/{file_name}') # 上传新增的图片到OSS
        await add.finish(pic_name + Message(msg), at_sender=True)

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
    command = args.extract_plain_text().strip()
    if not command:
        await disable.finish("请指定要禁用的指令名！例如：@bot 禁用 capoo")
    if not VALID_COMMAND_PATTERN.fullmatch(command):
        await disable.finish("指令名称仅支持字母、汉字和数字！")
    if command not in current_commands_config:
        await disable.finish(f"指令「{command}」不存在！")
    gid = event.group_id
    disabled = current_commands_config[command]
    if gid in disabled:
        await disable.finish(f"指令「{command}」在本群已经是禁用状态。")
    disabled.add(gid)
    msg = f"已在本群禁用指令「{command}」。"
    if not _persist_commands():
        msg += f"\n⚠️ {_config.COMMANDS_FILENAME} 损坏，本次禁用不会持久化，重启后会丢失。请管理员尽快修复 JSON。"
    await disable.finish(msg)


enable = on_command("启用", rule=to_me(), permission=GROUP_ADMIN | GROUP_OWNER, priority=2, block=True)


@enable.handle()
async def handle_enable(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    command = args.extract_plain_text().strip()
    if not command:
        await enable.finish("请指定要启用的指令名！例如：@bot 启用 capoo")
    if not VALID_COMMAND_PATTERN.fullmatch(command):
        await enable.finish("指令名称仅支持字母、汉字和数字！")
    if command not in current_commands_config:
        await enable.finish(f"指令「{command}」不存在！")
    gid = event.group_id
    disabled = current_commands_config[command]
    if gid not in disabled:
        await enable.finish(f"指令「{command}」在本群本来就没有被禁用。")
    disabled.discard(gid)
    msg = f"已在本群重新启用指令「{command}」。"
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
        file_path = uppic_img_path / img["img_url"]
        if file_path.exists():
            await delete_pic.send(f"序号 {i}:")
            await delete_pic.send(MessageSegment.image(file_path))
        else:
            await delete_pic.send(f"序号 {i}: {img['img_url']} (文件不存在)")


@delete_pic.handle()
async def handle_delete_pic(matched: Tuple[Any, ...] = RegexGroup()):
    command = matched[0].strip()
    if not VALID_COMMAND_PATTERN.fullmatch(command):
        await delete_pic.finish("指令名称仅支持字母、汉字和数字！")
    if command not in current_commands_config:
        await delete_pic.finish(f"指令「{command}」不存在！")

    images = await _get_image_list(command)
    if not images:
        await delete_pic.finish(f"指令「{command}」下没有图片！")

    total_pages = (len(images) + PAGE_SIZE - 1) // PAGE_SIZE
    delete_session_data[id(delete_pic)] = {
        "command": command,
        "images": images,
        "total_pages": total_pages,
        "state": "select_page"
    }

    await delete_pic.send(f"删除图片「{command}」，共 {len(images)} 张图片，共 {total_pages} 页。\n请输入页码（1-{total_pages}），或发送「取消」放弃删除。")


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

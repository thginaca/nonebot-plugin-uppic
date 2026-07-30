import json
import re
from pathlib import Path
from pydantic import BaseModel, Extra
from typing import Dict, List, Optional, Set
from nonebot import require
from nonebot import get_driver
from nonebot.log import logger

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_data_dir


class Config(BaseModel, extra='ignore'):
    uppic_store_dir_path: str = get_data_dir("nonebot_plugin_uppic")
    uppic_banner_group: List[int] = []
    uppic_endpoint: Optional[str] = None
    uppic_bucket: Optional[str] = None
    uppic_region: Optional[str] = None
    uppic_oss_access_key_id: Optional[str] = None
    uppic_oss_access_key_secret: Optional[str] = None
    uppic_oss_no_upload_list: List[str] = []
    uppic_super_users: List[int] = []


COMMANDS_FILENAME = 'uppic_commands.json'
PERMISSIONS_FILENAME = 'uppic_permissions.json'
DEFAULT_COMMANDS: List[str] = ["capoo"]

VALID_COMMAND_PATTERN = re.compile(r"^[A-Za-z0-9一-龥]+$")

commands_file_corrupted: bool = False
permissions_file_corrupted: bool = False


def is_commands_file_writable() -> bool:
    return not commands_file_corrupted


def is_permissions_file_writable() -> bool:
    return not permissions_file_corrupted


def commands_file_path(store_dir: str) -> Path:
    return Path(store_dir) / COMMANDS_FILENAME


def permissions_file_path(store_dir: str) -> Path:
    return Path(store_dir) / PERMISSIONS_FILENAME


def load_commands_file(store_dir: str) -> Dict[str, Set[int]]:
    """读取指令配置 JSON。返回「指令名 -> 该指令被禁用的群号集合」映射。

    - 文件不存在：写入默认配置并返回。
    - 解析失败 / 顶层既不是 list 也不是 dict：置 corrupted 标记，返回空字典，运行时不再覆盖。
    - 旧格式兼容（顶层为 list of str）：等价为「指令名 -> 空集合」，并立即迁移写回为新版 dict 格式。
    - 新格式（顶层为 dict）：键是指令名，值是禁用群号的 list[int]。
    - 单条非法（指令名非 str/空/含特殊字符、群号非 int）：跳过并告警。
    """
    global commands_file_corrupted
    path = commands_file_path(store_dir)
    if not path.exists():
        logger.info(f"未找到 {path}，使用默认指令列表 {DEFAULT_COMMANDS} 并写入文件")
        default: Dict[str, Set[int]] = {name: set() for name in DEFAULT_COMMANDS}
        save_commands_file(store_dir, default)
        return default
    try:
        with path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        commands_file_corrupted = True
        logger.error(
            f"读取 {path} 失败（JSON 格式错误）：{e}；本次启动以空指令列表运行，"
            f"且运行时不会写回此文件以避免覆盖。请修复后重启 bot。"
        )
        return {}

    # 旧格式：顶层 list[str]，自动迁移为新版 dict 并立即回写
    if isinstance(data, list):
        logger.info(f"{path} 为旧版数组格式，将迁移为新版字典格式（每个指令带空的禁用群号列表）")
        migrated: Dict[str, Set[int]] = {}
        for x in data:
            if not isinstance(x, str):
                logger.warning(f"{path} 中忽略非字符串条目: {x!r}")
                continue
            name = x.strip()
            if not name:
                logger.warning(f"{path} 中忽略空字符串条目")
                continue
            if not VALID_COMMAND_PATTERN.fullmatch(name):
                logger.warning(f"{path} 中忽略非法名称 {name!r}（仅允许字母、汉字、数字）")
                continue
            migrated.setdefault(name, set())
        save_commands_file(store_dir, migrated)
        return migrated

    if not isinstance(data, dict):
        commands_file_corrupted = True
        logger.error(
            f"{path} 顶层必须是 dict 或旧版的 list[str]，得到 {type(data).__name__}；本次启动以空指令列表运行，"
            f"且运行时不会写回此文件。"
        )
        return {}

    result: Dict[str, Set[int]] = {}
    for name, disabled in data.items():
        if not isinstance(name, str):
            logger.warning(f"{path} 中忽略非字符串键: {name!r}")
            continue
        name = name.strip()
        if not name:
            logger.warning(f"{path} 中忽略空字符串键")
            continue
        if not VALID_COMMAND_PATTERN.fullmatch(name):
            logger.warning(f"{path} 中忽略非法名称 {name!r}（仅允许字母、汉字、数字）")
            continue
        if not isinstance(disabled, list):
            logger.warning(f"{path} 中指令 {name!r} 的禁用群号必须是数组，得到 {type(disabled).__name__}；视为空")
            result.setdefault(name, set())
            continue
        gids: Set[int] = set()
        for g in disabled:
            # bool 是 int 的子类，要单独排除，避免 True/False 被当成群号
            if isinstance(g, bool) or not isinstance(g, int):
                logger.warning(f"{path} 中指令 {name!r} 忽略非整数群号: {g!r}")
                continue
            gids.add(g)
        result[name] = gids
    return result


def save_commands_file(store_dir: str, commands: Dict[str, Set[int]]) -> None:
    """全量覆盖写入指令配置 JSON。注意：调用方应先检查 is_commands_file_writable()。

    文件结构：`{ "指令名": [禁用群号, ...], ... }`。键按字母排序，群号按升序，保证可读性。
    """
    path = commands_file_path(store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {name: sorted(gids) for name, gids in sorted(commands.items())}
    with path.open('w', encoding='utf-8') as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def load_permissions_file(store_dir: str) -> Dict[int, Dict[str, Any]]:
    """读取上传权限配置 JSON。返回「群号 -> {mode, allowed_users}」映射。

    mode:
    - "admin_only": 仅群管/群主可上传（默认）
    - "all_members": 所有群员可上传

    allowed_users: 超级用户单独授权的用户ID列表
    """
    global permissions_file_corrupted
    path = permissions_file_path(store_dir)
    if not path.exists():
        logger.info(f"未找到 {path}，使用默认权限配置（仅群管可上传）")
        return {}
    try:
        with path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        permissions_file_corrupted = True
        logger.error(
            f"读取 {path} 失败（JSON 格式错误）：{e}；本次启动以默认权限运行，"
            f"且运行时不会写回此文件以避免覆盖。请修复后重启 bot。"
        )
        return {}

    if not isinstance(data, dict):
        permissions_file_corrupted = True
        logger.error(
            f"{path} 顶层必须是 dict，得到 {type(data).__name__}；本次启动以默认权限运行"
        )
        return {}

    result: Dict[int, Dict[str, Any]] = {}
    for gid_str, config in data.items():
        try:
            gid = int(gid_str)
        except (ValueError, TypeError):
            logger.warning(f"{path} 中忽略非法群号: {gid_str!r}")
            continue

        if isinstance(config, str):
            mode = config
            allowed_users = []
        elif isinstance(config, dict):
            mode = config.get("mode", "admin_only")
            allowed_users = config.get("allowed_users", [])
        else:
            mode = "admin_only"
            allowed_users = []

        if mode not in ("admin_only", "all_members"):
            logger.warning(f"{path} 中群号 {gid} 的权限模式 {mode!r} 无效，设为 admin_only")
            mode = "admin_only"

        validated_users = []
        for uid in allowed_users:
            try:
                validated_users.append(int(uid))
            except (ValueError, TypeError):
                logger.warning(f"{path} 中群号 {gid} 的用户ID {uid!r} 无效，已跳过")

        result[gid] = {"mode": mode, "allowed_users": validated_users}
    return result


def save_permissions_file(store_dir: str, permissions: Dict[int, Dict[str, Any]]) -> None:
    """全量覆盖写入上传权限配置 JSON。注意：调用方应先检查 is_permissions_file_writable()。

    文件结构：`{ "群号": { "mode": "权限模式", "allowed_users": [用户ID列表] }, ... }`。
    """
    path = permissions_file_path(store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {}
    for gid, config in sorted(permissions.items()):
        serializable[str(gid)] = {
            "mode": config.get("mode", "admin_only"),
            "allowed_users": sorted(config.get("allowed_users", []))
        }
    with path.open('w', encoding='utf-8') as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


config_dict = Config.model_validate(get_driver().config.dict())
uppic_store_dir_path: str = config_dict.uppic_store_dir_path
uppic_banner_group = config_dict.uppic_banner_group

# 指令配置唯一来源：uppic_commands.json
# 结构：{ 指令名 -> 该指令被禁用的群号集合 }；运行时被插件直接读写，保存时整体回写。
uppic_commands_config: Dict[str, Set[int]] = load_commands_file(uppic_store_dir_path)
# 仅作快照，保留旧名以兼容外部引用；运行期请通过 uppic_commands_config 取最新键集合。
uppic_command_list: List[str] = list(uppic_commands_config.keys())

uppic_endpoint = config_dict.uppic_endpoint
uppic_bucket = config_dict.uppic_bucket
uppic_region = config_dict.uppic_region
uppic_oss_access_key_id = config_dict.uppic_oss_access_key_id
uppic_oss_access_key_secret = config_dict.uppic_oss_access_key_secret
uppic_oss_no_upload_list = config_dict.uppic_oss_no_upload_list
uppic_super_users = config_dict.uppic_super_users

uppic_upload_permissions: Dict[int, str] = load_permissions_file(uppic_store_dir_path)

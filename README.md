# nonebot-plugin-uppic

_✨ 发送指令即可让 Bot 返回对应图片 ✨_

[![license](https://img.shields.io/github/license/HuParry/nonebot-plugin-uppic.svg)](./LICENSE)

---

## 📖 介绍

支持自定义指令、随机发送图片的 NoneBot2 插件。通过配置文件设置触发指令，在群聊中发送对应指令即可让 Bot 随机返回存储的图片。

- **智能去重**：内置感知哈希算法，自动识别相似图片
- **权限控制**：超级用户可跨群配置上传权限
- **自动压缩**：超过 1MB 的图片自动压缩至 1MB 以内
- **网页图库**：内置 Web 查看界面，支持在线浏览和删除
- **启动同步**：启动时自动同步数据库与文件系统

---

## 💿 安装

```bash
# nb-cli
nb plugin install nonebot-plugin-uppic

# pip
pip install nonebot-plugin-uppic
```

在 `pyproject.toml` 中添加：

```toml
plugins = ["nonebot_plugin_uppic"]
```

---

## ⚙️ 配置

### 环境变量（.env）

| 配置项 | 必填 | 默认值 | 说明 |
|-------|------|--------|------|
| `uppic_store_dir_path` | 否 | 本地数据目录 | 图片存储路径 |
| `uppic_banner_group` | 否 | `[]` | 禁用发图的群号 |
| `uppic_super_users` | 否 | `[]` | 超级用户 QQ 号 |
| `uppic_endpoint` | 否 | - | OSS 自定义域名 |
| `uppic_bucket` | 否 | - | OSS Bucket 名称 |
| `uppic_region` | 否 | - | OSS Bucket 地域 |
| `uppic_oss_access_key_id` | 否 | - | 阿里云 AccessKey ID |
| `uppic_oss_access_key_secret` | 否 | - | 阿里云 AccessKey Secret |
| `uppic_oss_no_upload_list` | 否 | `[]` | 不上传 OSS 的指令 |

### 指令配置（uppic_commands.json）

```json
{
  "demo": [],
  "test": [123456789]
}
```

表示 `demo` 全局启用，`test` 在群 `123456789` 中禁用。

### .env 示例

```ini
uppic_store_dir_path="data/uppic"
uppic_super_users=[123456789]
```

---

## 🎉 使用

### 基础指令

| 指令 | 权限 | 需要@ | 说明 |
|------|------|-------|------|
| `<预设指令>` | 群员 | 否 | 随机发送一张图片 |
| `添加<指令>` | 群管 | 否 | 回复图片添加到指令 |
| `删除图片<指令>` | 群管/群主 | 否 | 分页预览删除 |
| `禁用<指令>` | 群管/群主 | 是 | 本群禁用指令 |
| `启用<指令>` | 群管/群主 | 是 | 本群启用指令 |

### 网页图库

Bot 启动后自动生成静态网页，访问：`http://<host>:<port>/uppic/`

功能：浏览图片、一键删除、自动刷新。

### 删除图片示例

```
发送「删除图片demo」
→ 共 12 张，共 3 页。输入页码（1-3）
→ 2
→ 序号 5-8: [图片]
→ 输入要删除的序号：6 8
→ 成功删除 2 张！
```

---

## ⚠️ 注意

- 图片超过 1MB 会被自动压缩
- 运行期间请勿手动修改图片文件夹
- 建议合理控制图片数量

---

## 🔧 技术文档

### 项目结构

```
nonebot-plugin-uppic/
├── __init__.py          # 主入口 · 事件处理器
├── config.py            # 配置管理 · JSON 配置读写
├── compress.py          # 图片压缩 · 感知哈希去重
├── ali_oss.py           # 阿里云 OSS 上传
├── web.py               # 静态网页生成器
└── drivers/
    └── fastapi.py       # FastAPI 路由与 API
```

### 运行时文件

```
uppic/
├── img/<指令名>/          # 图片存储
├── database/data.db      # SQLite 数据库
├── public/               # 静态网页（自动生成）
├── uppic_commands.json   # 指令配置
└── uppic_permissions.json # 权限配置
```

### 数据库结构

**`folder_snapshot` 表**

| 字段 | 类型 | 说明 |
|------|------|------|
| `command` | TEXT | 指令名（主键） |
| `folder_mtime` | REAL | 文件夹修改时间戳 |
| `checked_at` | REAL | 上次检查时间 |

**`Pic_of_<指令名>` 表**（动态创建）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER | 自增主键 |
| `img_url` | TEXT | 图片相对路径 |
| `phash` | TEXT | 感知哈希值 |

### 核心流程

| 流程 | 步骤 |
|------|------|
| **启动** | 加载配置 → 连接数据库 → 创建文件夹 → 同步图片 → 初始化网页 |
| **发图** | 匹配指令 → 检查禁用状态 → 随机查询 → 发送图片 |
| **添加** | 下载 → 压缩 → pHash 去重 → 保存 → 写入数据库 |
| **删除** | 分页预览 → 序号删除 → 删除文件 + 数据库记录 + 刷新网页 |

### 技术栈

| 技术 | 用途 |
|------|------|
| NoneBot 2 | 机器人框架 |
| SQLite (aiosqlite) | 异步数据库 |
| Pillow | 图片处理 |
| imagehash | 感知哈希 |
| FastAPI | 网页图库服务 |
| alibabacloud_oss_v2 | 阿里云 OSS（可选） |

---

## 🙏 鸣谢

基于 [nonebot-plugin-randpic](https://github.com/HuParry/nonebot-plugin-randpic) 衍生开发。

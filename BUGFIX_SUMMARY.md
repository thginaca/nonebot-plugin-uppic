# nonebot-plugin-uppic 问题汇总

> 更新日期：2026-11-20
> 本文档分两部分：**第一部分**为已修复的问题存档；**第二部分**为本次代码审计新发现的隐患（尚未修复）。
> 涉及文件：`__init__.py`、`drivers/fastapi.py`、`web.py`、`config.py`、`compress.py`、`pyproject.toml`

---

# 第一部分：已修复问题

## 1. 安全类

### 1.1 Web API 完全无鉴权

| 项 | 内容 |
|----|------|
| 现象 | `/uppic/api/delete`、`/uppic/api/delete_folder`、`/uppic/api/upload` 对任何能访问 Web 服务的人开放 |
| 影响 | 任何人可删光图片、删除分类、上传任意文件 |
| 修复 | 新增 `UPPIC_WEB_TOKEN` 配置项；配了 token 后所有 `/uppic/*` 请求必须携带 `?token=xxx` 或 header `X-Uppic-Token` |
| 配置 | `.env` 加一行：`UPPIC_WEB_TOKEN=你的随机字符串` |

### 1.2 SQL 注入（Web API folder 参数）

| 项 | 内容 |
|----|------|
| 现象 | Web API 的 `folder` 参数直接拼入 SQL：`f"DELETE FROM Pic_of_{command} ..."` |
| 修复 | 在 `drivers/fastapi.py` 三个 API 入口对 `folder` 做校验；表名拼接统一在入口校验之后 |

## 2. 权限系统

### 2.1 上传权限管理命令只认超管

| 项 | 内容 |
|----|------|
| 现象 | 群管/群主用不了 `上传权限` / `添加上传用户` / `移除上传用户` |
| 修复 | 新增 `_can_manage_permissions(event)`：超管 OR 当前群的群管/群主 |

### 2.2 管理命令改造为 on_regex（免 `/` 前缀、免 @bot）

| 项 | 内容 |
|----|------|
| 现象 | `on_command(..., rule=to_me())` 要求必须带 `/` 前缀且必须 @bot，群里直接发命令无反应 |
| 影响命令 | `禁用`、`启用`、`上传权限`、`添加上传用户`、`移除上传用户` |
| 修复 | 全部改成 `on_regex`，正则 `(?:/)?` 让 `/` 可选，去掉 `rule=to_me()`，权限判断挪进 handler |

改造后正则：

| 命令 | 正则 |
|------|------|
| 禁用 | `r"^(?:/)?禁用\s+(.+)$"` |
| 启用 | `r"^(?:/)?启用\s+(.+)$"` |
| 上传权限 | `r"^(?:/)?上传权限(?:\s+(.+))?$"` |
| 添加上传用户 | `r"^(?:/)?添加上传用户\s+(.+)$"` |
| 移除上传用户 | `r"^(?:/)?移除上传用户\s+(.+)$"` |

handler 参数要点：`on_command` 用 `CommandArg()`；`on_regex` 用 `RegexGroup()`，返回 tuple，必须取 `matched[0]`。

## 3. 功能 Bug

### 3.1 roll 命令无法命中

- 根因链：`on_command` 权限限制 → `command_start` 参数不支持 → `RegexGroup()` 返回 tuple 却直接 `.strip()`
- 最终写法：`on_regex(r"^(?:/)?roll\s+(.+)$", priority=5, block=True)`，handler 里 `raw = (matched[0] if matched else "").strip()`
- 选项分隔支持空格、英文逗号、中文逗号、顿号：`re.split(r"[\s,，、]+", raw)`

### 3.2 「添加/上传」抢匹配「上传权限」

- 现象：发 `上传权限 xxx` 被 `^(?:添加|上传)(.+)$` 抢走，回复"请发送图片"
- 修复：加负向前瞻 `r"^(?:添加|上传)(?!权限|oss)(.+)$"`

### 3.3 启动自检 `_startup_cleanup` 不完整

修复后覆盖：

| 场景 | 处理 |
|------|------|
| 指令文件夹不存在 | DROP 对应 DB 表 + 移除 commands 配置 + 清理指向别名 |
| DB 记录指向的文件缺失 | 删除孤儿记录 |
| 别名指向不存在的原指令 | 移除别名 |
| folder_snapshot 孤儿条目 | DELETE |
| dialogs_img 失配图片 | 清除文件 |
| 汇总 | 启动日志输出清理统计 |

## 4. Web 图片墙

### 4.1 `import_module` 找不到插件

- 根因：`plugin_dirs=["plugins"]` 时模块名注册为 `plugins.nonebot_plugin_uppic`，顶层绝对导入失败
- 修复：改用相对导入 `from . import drivers` + `importlib.import_module(f".drivers.{driver_name}", __name__)`

### 4.2 middleware 挂载时机（最终方案：子应用）

- 根因：主 app 已 start 后 `app.add_middleware()` 永久不可用（`Cannot add middleware after an application has started`）
- 修复：把 `StaticFiles` 包进全新的子 `Starlette()` 实例，在子应用上 `add_middleware` 后再 `app.mount()`
- 兜底：3 个 API 路由另加 `dependencies=[Depends(_check)]`，不依赖 middleware

鉴权层次：

| 层次 | 保护对象 | 机制 |
|------|---------|------|
| 第 1 层 | `/uppic/`（图片墙 HTML）、`/uppic/img/`（原图） | 子 Starlette 应用 + `BaseHTTPMiddleware` |
| 第 2 层 | 3 个 `/uppic/api/*` 路由 | FastAPI `dependencies=[Depends(_check)]` |

### 4.3 缺少 python-multipart 依赖

- 现象：`RuntimeError: Form data requires "python-multipart"`
- 修复：`pyproject.toml` 加 `python-multipart`

### 4.4 drivers/ 子包 pip 安装后缺失

- 根因：poetry 打包漏掉子目录
- 修复：新增 `drivers/__init__.py` 使其成为正式子包

## 5. 默认行为调整

- 去掉默认 `capoo` 指令：`DEFAULT_COMMANDS = []`

## 6. 部署易错点

### 6.1 必须在正确目录跑 `nb run`

- 现象：`ProjectNotFoundError: pyproject.toml file not exists`
- 解决：`cd` 到含 `pyproject.toml` 的项目根目录再 `nb run`

### 6.2 8080 端口被占用（新代码没生效的常见原因）

- 现象：`[Errno 98] address already in use`；旧进程还在跑，浏览器访问的是旧代码，改动"看起来没生效"
- 排查与解决：

```bash
# 1. 找到占用进程
sudo lsof -i:8080
sudo ss -ltnp | grep 8080

# 2. 杀掉
sudo lsof -ti:8080 | xargs -r kill -9

# 3. 确认端口空出来（应无输出）
sudo ss -ltnp | grep 8080

# 4. 检查是否有多个 bot 进程
ps aux | grep -E "nb run|nonebot|uvicorn" | grep -v grep
pkill -f "nb run"

# 5. 若由 tmux / screen / systemd 托管
tmux ls && tmux kill-session -t <session名>
screen -ls && screen -S <session名> -X quit
sudo systemctl restart <服务名>

# 6. 重新启动
cd /home/ubuntu/mybot/my_bot && nb run
```

- 验证新代码生效：启动日志必须出现下面两行，且无 `❌ middleware 添加失败`：

```
[Web] _web_token='xxx'，用子应用 middleware 方案
[Web] ✅ 子应用 middleware 已就位，/uppic/img 和 /uppic 均带 token 校验
```

### 6.3 配了 token 但访问仍不需要 token

- 根因链：config 读取正常 → 主 app 已 start，`add_middleware` 必然失败 → middleware 没挂上 → 同时旧进程占着 8080，新代码没跑
- 修复：子应用 middleware 方案（见 4.2）+ 杀旧进程释放端口（见 6.2）

---

# 第二部分：本次审计新发现的隐患（未修复）

## 🔴 HIGH-1 水平越权：群管可跨群操作上传权限

- 位置：`__init__.py` `handle_add_upload_user`（约 L1138-L1162）、`handle_remove_upload_user`（约 L1168-L1192）
- 问题：两个命令只检查 `_can_manage_permissions(event)`（超管 OR 当前群群管/群主），但参数里的目标群号 `params[0]` **没有校验是否等于当前群**，也没有"跨群需超管"的限制
- 攻击路径：群 A 的群管发送 `添加上传用户 <群B群号> <任意用户>`，即可把用户加进群 B 的上传白名单（`移除上传用户` 同理）
- 对比：`上传权限` 命令在指定其他群号时已正确要求超管（L1106），这两个命令漏掉了同样的检查
- 建议：当 `gid != event.group_id` 时强制要求 `_is_super_user(event.user_id)`，与 `上传权限` 逻辑对齐

## 🟡 MEDIUM-1 Web Token 明文写入日志

- 位置（4 处）：
  - `__init__.py` L458：`logger.info(f"[Web] 传给 init_app_config 的 web_token={uppic_web_token!r}")`
  - `__init__.py` L464：`logger.info(f"[Web] register_route 前 uppic_web_token={uppic_web_token!r}")`
  - `drivers/fastapi.py` L229：`_log.info(f"[Web] _web_token={_web_token!r}，用子应用 middleware 方案")`
  - `drivers/fastapi.py` L237：`_log.info(f"[Web] 子应用 middleware 拦截：... token={token!r}")`
- 风险：日志被查看（运维、日志文件泄露）即可拿到 token，绕过 Web 端全部鉴权，执行删除/上传等高危操作
- 建议：脱敏打印（如只显示前后 3 位 + 长度），或 INFO 级别不打印原文；L237 请求级日志还会把每次请求携带的 token 记下来，建议降为 DEBUG 且脱敏

## 🟡 MEDIUM-2 存储型 XSS：静态 HTML 生成未转义目录名/文件名

- 位置：`web.py`
  - L410 JS 字符串注入：`const folderPath = "''' + folder_path + '''";`（目录名含 `"` 即可打断 JS）
  - L138-L143 HTML 属性注入：`onclick="location.href='{folder_url}'"`（目录名含 `'` 即可注入属性）
  - L428-L435 图片名直接拼入 `alt` / `title` / `onclick` 模板
- 攻击前提：攻击者能在图片目录创建恶意命名的目录/文件（手动操作文件系统，或诱导管理员创建含特殊字符的分类）
- 影响：管理员打开 `/uppic/` 图片墙时触发 XSS，可窃取 Web Token、以管理员身份调用删除/上传 API
- 建议：目录名/文件名拼入 HTML 前做 HTML 实体转义（`< > & " '`），拼入 JS 字符串前做 JS 转义

## 🟡 MEDIUM-3 配置 token 后，网页图片墙前端整体不可用

- 位置：`web.py` 生成的页面 JS（图片 URL、删除/上传/删分类的 fetch 调用）
- 问题：服务端要求 token 后，前端所有请求都不携带 token——页面 URL 里的 `?token=` 不会自动带给后续请求
- 失败链路：
  1. 带 `?token=xxx` 打开 `/uppic/`，页面本身能加载
  2. `<img src="/uppic/img/...">` 不带 token → 缩略图全部 401
  3. `fetch('/uppic/api/upload', {body: formData})` 不带 token → 上传每张显示 ✗，「成功 0 张，失败 N 张」，文件根本没存
  4. `fetch('/uppic/api/delete')` 同理 → 弹「删除失败: undefined」（401 响应体是 `{"detail": ...}`，前端却读 `data.message`，报错信息还误导）
- 不受影响：QQ 群内发图上传（走 OneBot 消息，不经过 Web API）；不配 token 时网页一切正常
- 性质：改动前就存在，本次鉴权重构没有解决，只是从 middleware 换成了 dependency + 子应用

### 解决方案（三选一）

| 方案 | 原理 | 前端改动 | 体验 | 安全性 |
|------|------|---------|------|--------|
| **A. Cookie 会话**（推荐） | 带 token 访问一次登录入口 → 服务端种 Cookie → 之后所有请求（含 `<img>`）浏览器自动携带 | 几乎为零 | 只输一次 token，图片/删除/上传全部自动正常 | 好（token 不再出现在后续 URL；HttpOnly 防 JS 读取） |
| **B. Basic Auth** | 服务端返回 401 + `WWW-Authenticate`，浏览器弹框输入一次后自动带 `Authorization` 头 | 零改动 | 浏览器原生弹框，但退出登录很麻烦（凭据缓存到关浏览器） | 中 |
| **C. 前端 JS 拼 token** | 从 `location.search` 读 token，手动附加到每个 img src 和 fetch | 要改 web.py 生成的所有 JS | 能用，但每个 URL 都拖着 token | 差（token 进浏览器历史、图片 URL 进 access log） |

**推荐 A 的实现草图**（改动集中在 `drivers/fastapi.py`，web.py 一行不用动）：

1. 新增登录入口 `GET /uppic/api/login?token=xxx`：校验通过后 302 跳回 `/uppic/`，并 `Set-Cookie: uppic_token=...; HttpOnly; SameSite=Lax; Path=/uppic`
2. `_verify_web_token` 和 `_TokenMiddleware` 取 token 顺序改为：query → header → cookie
3. （可选）middleware 对未登录的 HTML 请求直接返回一页密码输入框（提交到登录入口），替代裸 401 JSON，用户连手动拼 `?token=` 都省了

**配套修复**（实施 A 时一并处理）：
- MEDIUM-1 的 4 处 token 明文日志删除/脱敏（否则 Cookie 方案做得再好，日志里还是裸 token）
- `_check` 里 `HTTPException(detail=resp.body.decode())` 导致前端弹「删除失败: undefined」，改为直接返回 JSONResponse 或规范 detail

## 🟢 低风险 / 已有防护但值得注意

| 项 | 位置 | 现状与建议 |
|----|------|-----------|
| SQL 表名拼接 | `__init__.py` 约 15 处 `Pic_of_{command}` | 已有 `VALID_COMMAND_PATTERN`（字母/汉字/数字）白名单，实际不可注入；SQLite 不支持表名参数化，建议在校验处加注释说明防护来源 |
| `delete_image` 的 folder 无显式 `..` 检查 | `drivers/fastapi.py` L85 | 有 `resolve().relative_to()` 兜底可拦截，但建议与 `upload_image`/`delete_folder` 一致，补显式检查 |
| 压缩缓存无上限 | `compress.py` L11 `_compress_cache = {}` | 仅在启动/关闭时清空，长期运行会持续积累；单机场景影响小，可加条目上限或 LRU |
| 删除图片会话按 matcher 共享 | `__init__.py` L1244 `delete_session_data[id(delete_pic)]` | 多个用户同时触发「删除图片」会互相覆盖会话数据，后者可继续前者的删除流程；建议会话 key 改为 `(group_id, user_id)` |

## 修复优先级

1. **立即修复**：HIGH-1 跨群越权（`添加上传用户` / `移除上传用户`）
2. **尽快修复**：MEDIUM-1 token 明文日志（4 处）
3. **尽快修复**：MEDIUM-3 前端 token 传递（推荐 Cookie 会话方案，见上文）
4. **计划修复**：MEDIUM-2 web.py HTML/JS 转义
5. **可选改进**：低风险项表格中的防御性加固

---

# 附：修复后验证清单

- [ ] Web 鉴权：配 token 时 `curl /uppic/` 返回 401，带 `?token=xxx` 返回 200
- [ ] 启动日志出现 `[Web] ✅ 子应用 middleware 已就位`，无 `❌ middleware 添加失败`
- [ ] 启动日志无 `[Errno 98] address already in use`
- [ ] `roll a b` 能命中并随机返回
- [ ] 管理命令三种写法全通：`禁用 capoo` / `/禁用 capoo` / `@bot 禁用 capoo`
- [ ] `上传权限 all_members`（无 `/` 无 @bot）能设置成功
- [ ] `上传权限`（不带参数）能查询当前群权限
- [ ] `添加图片 capoo` 不再抢 `上传权限`
- [ ] 启动日志 `自检 数据库 / 文件系统 / 配置 完全一致 ✅`
- [ ] `/uppic/`、`/uppic/img/`、3 个 API 路由都被 token 覆盖

验证命令：

```bash
curl -i http://127.0.0.1:8080/uppic/                          # 期望 401
curl -i "http://127.0.0.1:8080/uppic/?token=<你的token>"      # 期望 200
curl -i http://127.0.0.1:8080/uppic/img/                       # 期望 401
curl -i http://127.0.0.1:8080/uppic/api/delete -X POST \
     -H "Content-Type: application/json" -d '{}'               # 期望 401/422
```

# 附：B 服务器上线操作顺序

```bash
# 1. 停掉所有旧 bot 进程
pkill -f "nb run"
sudo lsof -ti:8080 | xargs -r kill -9

# 2. 确认端口已空
sudo ss -ltnp | grep 8080     # 应无输出

# 3. 拉新代码
cd /home/ubuntu/mybot/my_bot/plugins/nonebot_plugin_uppic
git pull origin main

# 4. 装依赖（python-multipart 等）
cd /home/ubuntu/mybot/my_bot
nb plugin install 2>/dev/null || true
poetry install 2>/dev/null || true

# 5. 启动
nb run

# 6. 另开终端验证
curl -i http://127.0.0.1:8080/uppic/                         # 401
curl -i "http://127.0.0.1:8080/uppic/?token=<你的token>"      # 200
```

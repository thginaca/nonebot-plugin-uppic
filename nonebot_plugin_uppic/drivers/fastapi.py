import os
from pathlib import Path
from typing import Optional, Callable

from fastapi.staticfiles import StaticFiles
from fastapi import UploadFile, File, Form, Request, Depends
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from nonebot.drivers.fastapi import Driver

# 全局 token 引用，由 init_app_config 设置
_web_token: Optional[str] = None

# 登录成功后种下的会话 Cookie（浏览器关闭即失效）
_COOKIE_NAME = "uppic_token"


def _extract_token(request: Request) -> Optional[str]:
    """按优先级提取 token：query 参数 → X-Uppic-Token 头 → 会话 Cookie"""
    return (
        request.query_params.get("token")
        or request.headers.get("X-Uppic-Token")
        or request.cookies.get(_COOKIE_NAME)
    )


def _verify_web_token(request: Request) -> Optional[JSONResponse]:
    """FastAPI dependency：如果配了 UPPIC_WEB_TOKEN，校验请求是否携带正确 token。
    - 不配 token 时返回 None（不拦截）
    - 配了 token 但没带或不对，返回 401 JSONResponse
    - 带对了返回 None（放行）
    """
    if _web_token is None:
        return None

    if _extract_token(request) != _web_token:
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "Unauthorized: 请先打开 /uppic/ 输入访问令牌登录"},
        )
    return None


# 存储全局配置引用，供API使用
_img_path: Optional[Path] = None
_db_connection = None
_on_delete_callback: Optional[Callable] = None
_on_delete_folder_callback: Optional[Callable] = None  # async callback
_on_upload_callback: Optional[Callable] = None  # async callback
_recent_sent_ref = None  # 冷却池引用


class DeleteRequest(BaseModel):
    folder: str = ""
    filename: str


class DeleteFolderRequest(BaseModel):
    folder: str


def init_app_config(img_path: Path, super_users: list, db_connection, no_upload_list: list, on_delete_callback: Callable = None, recent_sent=None, on_delete_folder_callback: Callable = None, on_upload_callback: Callable = None, web_token: Optional[str] = None):
    """初始化应用配置，供API使用"""
    global _img_path, _db_connection, _on_delete_callback, _on_delete_folder_callback, _on_upload_callback, _recent_sent_ref, _web_token
    _img_path = img_path
    _db_connection = db_connection
    _on_delete_callback = on_delete_callback
    _on_delete_folder_callback = on_delete_folder_callback
    _on_upload_callback = on_upload_callback
    _recent_sent_ref = recent_sent
    _web_token = web_token


def register_route(driver: Driver, uppic_public_path: Path, uppic_img_path: Path):
    app = driver.server_app

    # ===== Token 校验包装器 =====
    # 配了 UPPIC_WEB_TOKEN 时，所有 /uppic/api/* 请求都会先过 _verify_web_token
    def _check(request: Request):
        if _verify_web_token(request) is not None:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="未授权：请先打开 /uppic/ 输入访问令牌登录")

    # 先注册 API 路由（否则会被 /uppic mount 拦截返回 405）
    @app.post("/uppic/api/delete", dependencies=[Depends(_check)])
    async def delete_image(req: DeleteRequest):
        """删除图片文件和数据库记录"""
        if _img_path is None:
            return {"success": False, "message": "服务未初始化"}

        # 安全检查：防止路径遍历攻击
        folder = req.folder.replace('/', os.sep).replace('\\', os.sep) if req.folder else ""
        
        # 文件名不应包含路径分隔符
        if '/' in req.filename or '\\' in req.filename:
            return {"success": False, "message": "非法文件名"}
        
        filename = os.path.basename(req.filename)

        if not filename:
            return {"success": False, "message": "非法文件名"}

        # 构建完整路径
        if folder:
            file_path = _img_path / folder / filename
        else:
            file_path = _img_path / filename

        # 验证文件在允许的目录内
        try:
            file_path.resolve().relative_to(_img_path.resolve())
        except ValueError:
            return {"success": False, "message": "非法路径"}

        if not file_path.exists():
            return {"success": False, "message": "文件不存在"}

        # 获取指令名称（folder的第一部分）
        command = folder.split(os.sep)[0] if folder else ""
        
        # 从数据库删除记录
        if command and _db_connection:
            try:
                cursor = await _db_connection.cursor()
                rel_path = f"{folder}/{filename}" if folder else filename
                await cursor.execute(
                    f"DELETE FROM Pic_of_{command} WHERE img_url = ?",
                    (rel_path,)
                )
                await _db_connection.commit()
            except Exception:
                pass

        # 清空该指令的冷却池（避免保留已删除 id）
        if command and _recent_sent_ref is not None:
            try:
                _recent_sent_ref.pop(command, None)
            except Exception:
                pass

        # 删除文件
        try:
            file_path.unlink()
            # 调用回调函数刷新网站
            if _on_delete_callback:
                try:
                    _on_delete_callback()
                except Exception:
                    pass
            return {"success": True, "message": "删除成功"}
        except Exception as e:
            return {"success": False, "message": f"删除文件失败: {str(e)}"}

    @app.post("/uppic/api/delete_folder", dependencies=[Depends(_check)])
    async def delete_folder(req: DeleteFolderRequest):
        """删除整个分类（文件夹+数据库+配置）"""
        if _img_path is None:
            return {"success": False, "message": "服务未初始化"}

        folder = req.folder.strip()
        if not folder or '/' in folder or '\\' in folder or '..' in folder:
            return {"success": False, "message": "非法文件夹名"}

        folder_path = _img_path / folder

        # 验证路径安全
        try:
            folder_path.resolve().relative_to(_img_path.resolve())
        except ValueError:
            return {"success": False, "message": "非法路径"}

        if not folder_path.exists():
            return {"success": False, "message": "文件夹不存在"}

        # 统计文件数
        deleted_files = 0
        if folder_path.exists():
            for f in os.listdir(folder_path):
                if (folder_path / f).is_file():
                    deleted_files += 1

        # 删除文件夹
        try:
            import shutil
            shutil.rmtree(folder_path)
        except Exception as e:
            return {"success": False, "message": f"删除文件夹失败: {e}"}

        # 调用回调清理数据库、配置、别名等
        if _on_delete_folder_callback:
            try:
                await _on_delete_folder_callback(folder)
            except Exception as e:
                return {"success": True, "message": f"文件夹已删除，但清理失败: {e}"}

        return {"success": True, "message": f"已删除分类「{folder}」，共 {deleted_files} 个文件"}

    @app.post("/uppic/api/upload", dependencies=[Depends(_check)])
    async def upload_image(folder: str = Form(...), file: UploadFile = File(...)):
        """网页端上传图片到指定分类"""
        if _on_upload_callback is None:
            return {"success": False, "message": "服务未初始化"}

        # 安全检查
        if not folder or '/' in folder or '\\' in folder or '..' in folder:
            return {"success": False, "message": "非法文件夹名"}

        # 读取文件数据
        file_data = await file.read()
        if not file_data or len(file_data) < 50:
            return {"success": False, "message": "文件为空或太小"}

        # 检查文件类型
        allowed_types = {'image/jpeg', 'image/png', 'image/gif', 'image/bmp', 'image/webp', 'image/tiff'}
        if file.content_type and file.content_type not in allowed_types:
            # 也通过扩展名检查
            ext = os.path.splitext(file.filename or '')[1].lower()
            if ext not in {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg'}:
                return {"success": False, "message": f"不支持的文件类型: {file.content_type or ext}"}

        try:
            result = await _on_upload_callback(folder, file_data)
            return result
        except Exception as e:
            return {"success": False, "message": f"上传失败: {e}"}

    # ===== 登录入口：校验 token 后种会话 Cookie =====
    def _login_page(error: str = "") -> str:
        err_html = f'<p class="err">{error}</p>' if error else ""
        return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>图片库 - 登录</title>
<style>
body{{font-family:sans-serif;background:#f5f5f5;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}}
.card{{background:#fff;border-radius:8px;padding:32px;box-shadow:0 2px 8px rgba(0,0,0,.12);width:300px}}
h3{{margin:0 0 16px;font-weight:normal}}
input{{width:100%;padding:8px;box-sizing:border-box;border:1px solid #ccc;border-radius:4px;margin-bottom:12px}}
button{{width:100%;padding:8px;background:#4a90d9;color:#fff;border:0;border-radius:4px;cursor:pointer}}
.err{{color:#d33;font-size:13px;margin:-4px 0 12px}}
</style></head><body>
<form class="card" method="post" action="/uppic/api/login">
<h3>图片库访问验证</h3>{err_html}
<input type="password" name="token" placeholder="请输入访问令牌" autofocus>
<button type="submit">登录</button>
</form></body></html>"""

    def _do_login(token: Optional[str]):
        if _web_token is None:
            return RedirectResponse("/uppic/", status_code=302)
        if token and token == _web_token:
            resp = RedirectResponse("/uppic/", status_code=302)
            resp.set_cookie(
                _COOKIE_NAME, token,
                httponly=True, samesite="lax", path="/uppic",
            )
            return resp
        return HTMLResponse(_login_page("令牌不正确，请重试"), status_code=401)

    @app.get("/uppic/api/login")
    async def web_login_get(request: Request):
        # 未配置 token 时也提供入口，直接跳转
        if _web_token is None:
            return RedirectResponse("/uppic/", status_code=302)
        token = request.query_params.get("token")
        if token is None:
            return HTMLResponse(_login_page())
        return _do_login(token)

    @app.post("/uppic/api/login")
    async def web_login_post(token: str = Form(...)):
        return _do_login(token)

    # ===== 挂载静态文件 + 统一 token 校验 =====
    # 主 app 已 start，不能 add_middleware → 改为把 StaticFiles 包成带 middleware 的子应用再 mount
    import logging
    _log = logging.getLogger("nonebot_plugin_uppic")

    img_path = str(uppic_img_path.resolve())
    html_path = str(uppic_public_path.resolve())

    if _web_token is not None:
        _log.info("[Web] 已配置 UPPIC_WEB_TOKEN，启用 Cookie 登录 + 子应用 middleware 方案")
        from starlette.applications import Starlette as _Starlette
        from starlette.middleware.base import BaseHTTPMiddleware

        class _TokenMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                token = _extract_token(request)
                if token == _web_token:
                    return await call_next(request)
                # 浏览器直接访问页面 → 返回登录页；图片/接口等 → 返回 401 JSON
                accept = request.headers.get("accept", "")
                path = request.url.path
                if "text/html" in accept and (path.endswith("/") or path.endswith(".html")):
                    return HTMLResponse(_login_page(), status_code=401)
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "Unauthorized: 请先打开 /uppic/ 输入访问令牌登录"},
                )

        # 子应用 1：/uppic/img（原图）
        img_sub = _Starlette()
        img_sub.add_middleware(_TokenMiddleware)
        img_sub.mount("/", StaticFiles(directory=img_path), name="img_static")
        app.mount("/uppic/img", img_sub, name="uppic_img")

        # 子应用 2：/uppic（图片墙 HTML）
        uppic_sub = _Starlette()
        uppic_sub.add_middleware(_TokenMiddleware)
        uppic_sub.mount("/", StaticFiles(directory=html_path, html=True), name="html_static")
        app.mount("/uppic", uppic_sub, name="uppic")

        _log.info("[Web] ✅ 子应用 middleware 已就位，/uppic/img 和 /uppic 均带 token 校验")
    else:
        _log.warning("[Web] ⚠️ 没有配 UPPIC_WEB_TOKEN，静态文件公开访问")
        app.mount("/uppic/img", StaticFiles(directory=img_path), name="uppic_img")
        app.mount("/uppic", StaticFiles(directory=html_path, html=True), name="uppic")

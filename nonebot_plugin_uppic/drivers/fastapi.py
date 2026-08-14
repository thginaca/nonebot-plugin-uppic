import os
from pathlib import Path
from typing import Optional, Callable

from fastapi.staticfiles import StaticFiles
from fastapi import UploadFile, File, Form
from pydantic import BaseModel

from nonebot.drivers.fastapi import Driver


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


def init_app_config(img_path: Path, super_users: list, db_connection, no_upload_list: list, on_delete_callback: Callable = None, recent_sent=None, on_delete_folder_callback: Callable = None, on_upload_callback: Callable = None):
    """初始化应用配置，供API使用"""
    global _img_path, _db_connection, _on_delete_callback, _on_delete_folder_callback, _on_upload_callback, _recent_sent_ref
    _img_path = img_path
    _db_connection = db_connection
    _on_delete_callback = on_delete_callback
    _on_delete_folder_callback = on_delete_folder_callback
    _on_upload_callback = on_upload_callback
    _recent_sent_ref = recent_sent


def register_route(driver: Driver, uppic_public_path: Path, uppic_img_path: Path):
    app = driver.server_app

    # 先注册API路由（否则会被 /uppic mount 拦截返回 405）
    @app.post("/uppic/api/delete")
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

    @app.post("/uppic/api/delete_folder")
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

    @app.post("/uppic/api/upload")
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

    # 挂载原图目录
    img_path = str(uppic_img_path.resolve())
    app.mount("/uppic/img", StaticFiles(directory=img_path), name="uppic_img")

    # 挂载HTML静态文件
    html_path = str(uppic_public_path.resolve())
    app.mount("/uppic", StaticFiles(directory=html_path, html=True), name="uppic")

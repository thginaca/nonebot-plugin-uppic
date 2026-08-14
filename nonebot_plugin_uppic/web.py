import os
import shutil
import time
from pathlib import Path


class StaticImageGalleryGenerator:
    def __init__(self, source_folder, output_folder):
        self.source_folder = Path(source_folder)
        self.output_folder = Path(output_folder)
        self.supported_formats = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg'}

    def get_all_images_with_folders(self, no_upload_oss_list):
        """获取所有文件夹及其包含的图片文件"""
        result = {}

        if self.source_folder.exists():
            for root, dirs, files in os.walk(self.source_folder):
                rel_path = os.path.relpath(root, self.source_folder)
                if rel_path == '.':
                    folder_key = ''
                else:
                    folder_key = rel_path

                if folder_key in no_upload_oss_list:
                    continue

                image_files = []
                for filename in files:
                    if Path(filename).suffix.lower() in self.supported_formats:
                        image_files.append(filename)

                if image_files:
                    result[folder_key] = {
                        'path': rel_path,
                        'images': sorted(image_files),
                        'image_count': len(image_files),
                        'subfolders': dirs
                    }

        return result

    def generate_index_html(self, all_folders):
        """生成主页HTML"""
        html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>图片库 - uppic</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .header {
            text-align: center;
            margin-bottom: 30px;
        }
        .header h1 {
            color: #333;
            margin: 10px 0;
        }
        .folder-container {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 20px;
            max-width: 1200px;
            margin: 0 auto;
        }
        .folder-card {
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
            transition: transform 0.3s, box-shadow 0.3s;
            text-align: center;
            padding: 20px;
            cursor: pointer;
        }
        .folder-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 4px 20px rgba(0,0,0,0.15);
        }
        .folder-icon {
            font-size: 48px;
            margin-bottom: 15px;
        }
        .folder-name {
            font-weight: bold;
            margin-bottom: 10px;
            word-break: break-all;
            color: #333;
        }
        .folder-stats {
            color: #666;
            font-size: 14px;
            margin-bottom: 15px;
        }
        .folder-link {
            display: inline-block;
            padding: 8px 16px;
            background: #007bff;
            color: white;
            text-decoration: none;
            border-radius: 5px;
            transition: background 0.3s;
        }
        .folder-link:hover {
            background: #0056b3;
        }
        .folder-delete-btn {
            display: inline-block;
            padding: 8px 16px;
            background: #dc3545;
            color: white;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 14px;
            margin-left: 10px;
            transition: background 0.3s;
        }
        .folder-delete-btn:hover {
            background: #c82333;
        }
        .stats {
            text-align: center;
            margin-bottom: 20px;
            color: #666;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>📸 图片库</h1>
        <div class="stats">
            共 ''' + str(len(all_folders)) + ''' 个文件夹 · 刷新于 ''' + time.strftime('%Y-%m-%d %H:%M:%S') + '''
        </div>
    </div>

    <div class="folder-container">
'''

        for folder_key, folder_info in all_folders.items():
            if folder_key == '':
                display_name = '根目录'
                folder_url = './index.html?t=' + str(int(time.time()))
            else:
                display_name = folder_key
                folder_url = f'./{folder_key}/index.html?t=' + str(int(time.time()))

            html += f'''
        <div class="folder-card" onclick="location.href='{folder_url}'">
            <div class="folder-icon">📁</div>
            <div class="folder-name">{display_name}</div>
            <div class="folder-stats">{folder_info['image_count']} 张图片</div>
            <span class="folder-link">查看图片</span>
'''
            if folder_key:
                html += f'''
            <button class="folder-delete-btn" onclick="deleteFolder('{folder_key}', event)">删除分类</button>
'''
            html += '''
        </div>
'''

        html += '''
    </div>
    <script>
        function deleteFolder(folderName, event) {
            event.stopPropagation();
            if (!confirm('确定要删除分类「' + folderName + '」吗？\\n这将删除该分类下的所有图片和数据库记录，不可恢复！')) {
                return;
            }
            fetch('/uppic/api/delete_folder', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({folder: folderName})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    alert(data.message);
                    location.reload();
                } else {
                    alert('删除失败: ' + data.message);
                }
            })
            .catch(err => alert('请求失败: ' + err));
        }
    </script>
</body>
</html>'''

        return html

    def generate_folder_html(self, folder_info, folder_path):
        """生成文件夹页面HTML"""
        html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>图片库 - {folder_path if folder_path else '根目录'}</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .header {{
            text-align: center;
            margin-bottom: 30px;
        }}
        .breadcrumb {{
            text-align: center;
            margin-bottom: 20px;
            color: #666;
        }}
        .breadcrumb a {{
            color: #007bff;
            text-decoration: none;
        }}
        .breadcrumb a:hover {{
            text-decoration: underline;
        }}
        .image-container {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 20px;
            max-width: 1200px;
            margin: 0 auto;
        }}
        .image-card {{
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
            transition: transform 0.3s, box-shadow 0.3s;
            position: relative;
        }}
        .image-card:hover {{
            transform: scale(1.02);
            box-shadow: 0 4px 20px rgba(0,0,0,0.15);
            z-index: 10;
        }}
        .image-preview {{
            width: 100%;
            height: 150px;
            object-fit: cover;
            display: block;
            cursor: pointer;
        }}
        .image-info {{
            padding: 10px;
        }}
        .image-name {{
            font-size: 12px;
            word-break: break-all;
            margin-bottom: 8px;
            color: #333;
        }}
        .image-actions {{
            display: flex;
            gap: 8px;
            justify-content: center;
        }}
        .image-link {{
            display: inline-block;
            padding: 5px 10px;
            background: #28a745;
            color: white;
            text-decoration: none;
            border-radius: 3px;
            font-size: 12px;
        }}
        .image-link:hover {{
            background: #218838;
        }}
        .delete-btn {{
            padding: 5px 10px;
            background: #dc3545;
            color: white;
            border: none;
            border-radius: 3px;
            cursor: pointer;
            font-size: 12px;
        }}
        .delete-btn:hover {{
            background: #c82333;
        }}
        .stats {{
            text-align: center;
            margin-bottom: 20px;
            color: #666;
        }}
        .subfolder-list {{
            margin: 20px 0;
            text-align: center;
        }}
        .subfolder-item {{
            display: inline-block;
            margin: 5px 10px;
            padding: 5px 15px;
            background: #17a2b8;
            color: white;
            text-decoration: none;
            border-radius: 20px;
            font-size: 12px;
        }}
        .subfolder-item:hover {{
            background: #138496;
        }}
    </style>
</head>
<body>
    <div class="header">
        <div class="breadcrumb">
            <a href="../index.html?t=' + str(int(time.time())) + '">← 首页</a>
            {' / ' + folder_path if folder_path else ''}
        </div>
        <h1>📁 {folder_path if folder_path else '根目录'}</h1>
        <div class="stats">
            共 {folder_info['image_count']} 张图片 · 刷新于 ''' + time.strftime('%Y-%m-%d %H:%M:%S') + '''
        </div>
'''

        if folder_info['subfolders']:
            html += '''
        <div class="subfolder-list">
            <strong>📂 子文件夹:</strong>
'''
            for subfolder in folder_info['subfolders']:
                if folder_path:
                    subfolder_url = f'./{subfolder}/index.html?t=' + str(int(time.time()))
                else:
                    subfolder_url = f'./{subfolder}/index.html?t=' + str(int(time.time()))

                html += f'            <a href="{subfolder_url}" class="subfolder-item">{subfolder}</a>\n'

            html += '''        </div>
'''

        html += '''
    </div>

    <div id="imageContainer" class="image-container">
        <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: #666;">加载中...</div>
    </div>

    <script>
        const folderPath = "''' + folder_path + '''";
        const cacheBuster = ''' + str(int(time.time())) + ''';
        
        function loadImages() {
            const container = document.getElementById('imageContainer');
            let images = ''' + str(folder_info['images']) + ''';
            
            if (!images || images.length === 0) {
                container.innerHTML = '<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: #666;">本文件夹暂无图片</div>';
                return;
            }
            
            container.innerHTML = '';
            images.forEach(imgName => {
                const imgUrl = '/uppic/img/' + (folderPath ? folderPath + '/' : '') + imgName + '?t=' + cacheBuster;
                const card = document.createElement('div');
                card.className = 'image-card';
                card.innerHTML = `
                    <img src="${imgUrl}" 
                         alt="${imgName}" 
                         class="image-preview"
                         onerror="this.parentElement.style.display='none'"
                         loading="lazy"
                         onclick="window.open('${imgUrl}', '_blank')">
                    <div class="image-info">
                        <div class="image-name" title="${imgName}">${imgName.length > 20 ? imgName.substring(0, 20) + '...' : imgName}</div>
                        <div class="image-actions">
                            <a href="${imgUrl}" target="_blank" class="image-link">查看</a>
                            <button class="delete-btn" onclick="deleteImage('${imgName}', event)">删除</button>
                        </div>
                    </div>
                `;
                container.appendChild(card);
            });
        }
        
        function deleteImage(imgName, event) {
            event.stopPropagation();
            if (!confirm(`确定要删除图片 "${imgName}" 吗？\\n此操作不可恢复！`)) {
                return;
            }
            
            fetch('/uppic/api/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    folder: folderPath,
                    filename: imgName
                })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    alert('删除成功！');
                    location.reload();
                } else {
                    alert('删除失败: ' + data.message);
                }
            })
            .catch(err => alert('请求失败: ' + err));
        }
        
        loadImages();
    </script>
</body>
</html>'''

        return html

    def generate_static_site(self, no_upload_oss_list):
        """生成静态网站（只生成HTML，图片直接引用源目录）"""
        all_folders = self.get_all_images_with_folders(no_upload_oss_list)
        
        # 清空并创建输出文件夹
        if self.output_folder.exists():
            shutil.rmtree(self.output_folder)
        self.output_folder.mkdir(parents=True, exist_ok=True)

        # 生成主页
        index_html = self.generate_index_html(all_folders)
        (self.output_folder / 'index.html').write_text(index_html, encoding='utf-8')

        # 为每个文件夹生成页面
        for folder_key, folder_info in all_folders.items():
            if folder_key == '':
                folder_dir = self.output_folder
            else:
                folder_dir = self.output_folder / folder_key
                folder_dir.mkdir(parents=True, exist_ok=True)

            folder_html = self.generate_folder_html(folder_info, folder_key)
            (folder_dir / 'index.html').write_text(folder_html, encoding='utf-8')

    def generate_command_html(self, folder_key: str, file_name: str, no_upload_oss_list):
        """为单个文件夹重新生成页面"""
        if folder_key in no_upload_oss_list:
            return
        
        # 重新生成所有页面
        self.generate_static_site(no_upload_oss_list)


if __name__ == "__main__":
    source_folder = "C:\\Users\\hu_pa\\Desktop\\randpic"
    output_folder = "C:\\Users\\hu_pa\\Desktop\\nonebot\\nonebot-plugin-randpic\\static"

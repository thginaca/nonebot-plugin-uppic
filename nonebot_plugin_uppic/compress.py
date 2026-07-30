import zipfile
import hashlib
from pathlib import Path
from nonebot.log import logger
from PIL import Image
import imagehash
import io


# 压缩缓存：记录已压缩文件的哈希，避免重复压缩
_compress_cache = {}


def _get_bytes_hash(image_bytes):
    """计算字节流的哈希，用于判断是否已压缩"""
    return hashlib.md5(image_bytes).hexdigest()


def _compress_gif_bytes(image_bytes, max_size_mb=1):
    """压缩动态GIF，优先保持质量。先减色 → 再缩放 → 最后跳帧。"""
    try:
        image_stream = io.BytesIO(image_bytes)
        img = Image.open(image_stream)
        loop = img.info.get('loop', 0)

        frames, durations = [], []
        try:
            while True:
                frames.append(img.copy().convert('RGBA'))
                durations.append(img.info.get('duration', 100))
                img.seek(img.tell() + 1)
        except (EOFError, Exception):
            pass

        if not frames:
            logger.warning("GIF帧读取失败，返回原始数据")
            return image_bytes

        original_size = frames[0].size
        n_frames = len(frames)

        # 逐步加大压缩力度的配置，按质量从高到低排序
        strategies = []

        # 第一阶段：只减色，不改帧和分辨率
        for n_colors in [256, 192, 128, 96, 64]:
            strategies.append({'frame_skip': 1, 'scale': 1.0, 'n_colors': n_colors})

        # 第二阶段：减色 + 适度缩放
        for scale in [0.85, 0.7]:
            for n_colors in [128, 96, 64]:
                strategies.append({'frame_skip': 1, 'scale': scale, 'n_colors': n_colors})

        # 第三阶段：减色 + 缩放 + 跳帧
        for frame_skip in [2, 3]:
            for scale in [0.85, 0.7, 0.55]:
                for n_colors in [96, 64]:
                    strategies.append({'frame_skip': frame_skip, 'scale': scale, 'n_colors': n_colors})

        # 第四阶段：极端压缩
        for frame_skip in [4, 5]:
            for scale in [0.5, 0.4]:
                for n_colors in [64, 48]:
                    strategies.append({'frame_skip': frame_skip, 'scale': scale, 'n_colors': n_colors})

        best_bytes = None
        best_config = None

        for cfg in strategies:
            frame_skip = cfg['frame_skip']
            scale = cfg['scale']
            n_colors = cfg['n_colors']

            sel_frames = frames[::frame_skip]
            sel_durations = [d * frame_skip for d in durations[::frame_skip]]

            if scale < 1.0:
                new_size = tuple(int(d * scale) for d in original_size)
                sel_frames = [f.resize(new_size, Image.Resampling.LANCZOS) for f in sel_frames]

            try:
                quantized = _quantize_gif_frames(sel_frames, n_colors)
                if quantized is None:
                    continue
            except Exception as e:
                logger.warning(f"GIF量化失败: {e}，尝试跳过此配置")
                continue

            try:
                out = io.BytesIO()
                quantized[0].save(
                    out, format='GIF', save_all=True,
                    append_images=quantized[1:],
                    loop=loop, duration=sel_durations, optimize=True,
                    disposal=2
                )

                result = out.getvalue()
                result_mb = len(result) / (1024 * 1024)

                if best_bytes is None or len(result) < len(best_bytes):
                    best_bytes = result
                    best_config = cfg

                if result_mb <= max_size_mb:
                    logger.info(
                        f"GIF压缩完成: 跳帧={frame_skip}, 缩放={scale:.2f}, "
                        f"颜色={n_colors}, 大小={result_mb:.2f}MB"
                    )
                    return result
            except Exception as e:
                logger.warning(f"GIF保存失败: {e}，尝试跳过此配置")
                continue

        if best_bytes is not None:
            logger.warning(
                f"GIF无法压缩到 {max_size_mb}MB 以内，"
                f"返回最小版本 (跳帧={best_config['frame_skip']}, "
                f"缩放={best_config['scale']:.2f}, 颜色={best_config['n_colors']}, "
                f"{len(best_bytes) / (1024 * 1024):.2f}MB)"
            )
            return best_bytes

        logger.warning("GIF压缩完全失败，返回原始数据")
        return image_bytes
    except Exception as e:
        logger.warning(f"GIF压缩过程发生错误: {e}，返回原始数据")
        return image_bytes


def _quantize_gif_frames(frames, n_colors):
    """
    量化GIF帧，优先使用支持RGBA的方法。
    返回量化后的帧列表，失败返回None。
    """
    quantize_methods = []

    if hasattr(Image.Quantize, 'FASTOCTREE'):
        quantize_methods.append(Image.Quantize.FASTOCTREE)
    if hasattr(Image.Quantize, 'LIBIMAGEQUANT'):
        quantize_methods.append(Image.Quantize.LIBIMAGEQUANT)

    quantize_methods.append('RGB_THEN_MEDIANCUT')

    for method in quantize_methods:
        try:
            if method == 'RGB_THEN_MEDIANCUT':
                quantized = [
                    f.convert('RGB').quantize(
                        colors=n_colors, method=Image.Quantize.MEDIANCUT
                    )
                    for f in frames
                ]
            else:
                quantized = [
                    f.quantize(colors=n_colors, method=method)
                    for f in frames
                ]
            return quantized
        except Exception:
            continue

    return None


def _compress_static_bytes(image_bytes, max_size_mb=1, target_quality=85):
    """压缩静态图片（JPEG/PNG 等），转为 JPEG 输出。"""
    image_stream = io.BytesIO(image_bytes)
    with Image.open(image_stream) as img:
        original_size = img.size

        if max(img.size) > 1920:
            scale = 1920 / max(img.size)
            new_size = tuple(int(dim * scale) for dim in img.size)
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            logger.info(f"  - 调整尺寸: {original_size} -> {img.size}")

        if img.mode in ('RGBA', 'LA', 'P'):
            if img.mode == 'RGBA':
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert('RGB')

        low, high = 10, 95
        best_quality = target_quality
        best_bytes = None

        while low <= high:
            mid = (low + high) // 2

            temp_stream = io.BytesIO()
            img.save(temp_stream, format='JPEG', quality=mid, optimize=True)
            current_bytes = temp_stream.getvalue()
            current_size_mb = len(current_bytes) / (1024 * 1024)

            if current_size_mb <= max_size_mb:
                best_quality = mid
                best_bytes = current_bytes
                if current_size_mb <= max_size_mb * 0.9:
                    low = mid + 1
                else:
                    break
            else:
                high = mid - 1

        if best_bytes is None:
            fallback_stream = io.BytesIO()
            img.save(fallback_stream, format='JPEG', quality=95, optimize=True)
            best_bytes = fallback_stream.getvalue()

        final_size_mb = len(best_bytes) / (1024 * 1024)
        logger.info(f"  - 最终质量: {best_quality}, 大小: {final_size_mb:.2f}MB")

        return best_bytes


def adaptive_compress_bytes(image_bytes, max_size_mb=1, target_quality=85, skip_cache=False):
    """
    自适应压缩，确保压缩后的大小不超过限制。
    动态 GIF 保持 GIF 格式输出，静态图片转为 JPEG 输出。
    
    参数:
        image_bytes: 图片字节流
        max_size_mb: 最大大小限制(MB)
        target_quality: 目标质量（仅用于静态图）
        skip_cache: 是否跳过缓存检查（用于强制压缩）
    """
    original_size_mb = len(image_bytes) / (1024 * 1024)

    if original_size_mb <= max_size_mb:
        return image_bytes

    # 检查缓存：如果已经压缩过，直接返回
    if not skip_cache:
        img_hash = _get_bytes_hash(image_bytes)
        if img_hash in _compress_cache:
            cached_result = _compress_cache[img_hash]
            if len(cached_result) > 0:
                logger.debug("使用压缩缓存，跳过重复压缩")
                return cached_result

    logger.info(f"图片大小: {original_size_mb:.2f}MB，超过限制 {max_size_mb}MB，开始自适应压缩...")

    image_stream = io.BytesIO(image_bytes)
    with Image.open(image_stream) as img:
        fmt = img.format
        is_animated = getattr(img, 'n_frames', 1) > 1

    if fmt == 'GIF' and is_animated:
        result = _compress_gif_bytes(image_bytes, max_size_mb)
    else:
        result = _compress_static_bytes(image_bytes, max_size_mb, target_quality)

    # 写入缓存
    if not skip_cache:
        img_hash = _get_bytes_hash(image_bytes)
        _compress_cache[img_hash] = result

    return result


def compress_image_from_bytes(image_bytes, skip_cache=False):
    """
    严格控制文件大小的压缩函数
    
    参数:
        image_bytes: 图片字节流
        skip_cache: 是否跳过缓存检查
    返回: 压缩后的图片字节流
    """
    return adaptive_compress_bytes(
        image_bytes=image_bytes,
        max_size_mb=1,
        target_quality=85,
        skip_cache=skip_cache
    )


def clear_compress_cache():
    """清空压缩缓存，释放内存"""
    global _compress_cache
    _compress_cache.clear()
    logger.debug("压缩缓存已清空")


def get_image_extension(image_bytes):
    """根据压缩后的字节内容返回正确的文件扩展名，避免依赖来源 URL 的扩展名"""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            fmt = img.format
            is_animated = getattr(img, 'n_frames', 1) > 1
        if fmt == 'GIF' and is_animated:
            return '.gif'
        return '.jpg'
    except Exception as e:
        logger.warning(f"获取图片扩展名失败: {e}，使用默认扩展名 .jpg")
        return '.jpg'


def compress_folder_basic(folder_path, zip_path, include_subfolders=True):
    """
    将文件夹压缩成zip文件

    参数:
    - folder_path: 要压缩的文件夹路径
    - zip_path: 输出的zip文件路径
    - include_subfolders: 是否包含子文件夹
    """
    folder = Path(folder_path)

    if not folder.exists():
        print(f"文件夹不存在: {folder_path}")
        return False

    if not folder.is_dir():
        print(f"路径不是文件夹: {folder_path}")
        return False

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path in folder.rglob("*") if include_subfolders else folder.iterdir():
                if file_path.is_file():
                    relative_path = file_path.relative_to(folder)
                    zipf.write(file_path, relative_path)
                    print(f"添加文件: {relative_path}")

        print(f"压缩完成: {zip_path}")
        return True
    except Exception as e:
        print(f"压缩失败: {e}")
        return False


def compute_phash(image_bytes: bytes):
    """计算图片感知哈希，返回十六进制字符串"""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            return str(imagehash.phash(img))
    except Exception as e:
        logger.warning(f"计算图片哈希失败: {e}，返回空字符串")
        return ""

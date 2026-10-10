"""图片生成器 — 基于 DashScope qwen-image-2.0 模型生成图片。

当图库为空时，根据文本内容自动生成配图。
图片只能使用一次，使用后从图库中删除。

依赖: dashscope SDK (pip install dashscope)
环境变量: DASHSCOPE_API_KEY
"""

from __future__ import annotations

import glob
import json
import logging
import os
import time

import dashscope
import requests
from dashscope import MultiModalConversation

# 设置 DashScope 国际版 API
dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"

logger = logging.getLogger(__name__)

# 支持的图片扩展名
_IMAGE_EXTENSIONS = {"*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.bmp"}

# 默认图库路径
_DEFAULT_IMAGE_DIR = os.path.join(
    os.path.expanduser("~"), ".openclaw", "workspace", "xhs-images"
)


def get_image_library_path(image_dir: str | None = None) -> str:
    """获取图库路径，确保目录存在。"""
    path = image_dir or _DEFAULT_IMAGE_DIR
    os.makedirs(path, exist_ok=True)
    return path


def list_images(image_dir: str | None = None) -> list[str]:
    """列出图库中所有图片文件。"""
    path = get_image_library_path(image_dir)
    images = []
    for ext in _IMAGE_EXTENSIONS:
        images.extend(glob.glob(os.path.join(path, ext)))
    images.sort(key=os.path.getmtime, reverse=True)
    return images


def has_images(image_dir: str | None = None) -> bool:
    """检查图库中是否有可用图片。"""
    return len(list_images(image_dir)) > 0


def pick_images(count: int = 1, image_dir: str | None = None) -> list[str]:
    """从图库中选取指定数量的图片（不删除，返回路径列表）。"""
    images = list_images(image_dir)
    return images[:count]


def delete_images(image_paths: list[str]) -> int:
    """删除指定的图片文件（单次使用后清理）。返回成功删除的数量。"""
    deleted = 0
    for path in image_paths:
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info("已删除图片: %s", path)
                deleted += 1
        except OSError as e:
            logger.warning("删除图片失败 %s: %s", path, e)
    return deleted


def generate_images(
    prompt: str,
    count: int = 3,
    image_dir: str | None = None,
    api_key: str | None = None,
    negative_prompt: str = "",
    size: str = "1440*1800",
    model: str = "qwen-image-2.0",
) -> list[str]:
    """调用 DashScope qwen-image-2.0 模型生成图片。

    使用 MultiModalConversation API，通过文本提示生成图片。
    每次调用生成 1 张图片，多张图片通过多次调用实现。

    Args:
        prompt: 图片描述文本
        count: 生成图片数量（1-4）
        image_dir: 图片保存目录
        api_key: DashScope API Key，不传则从环境变量 DASHSCOPE_API_KEY 获取
        negative_prompt: 反向提示词（qwen-image-2.0 通过 negative_prompt 参数支持）
        size: 图片尺寸，默认 1440*1800（3:4 竖版，适合小红书）
        model: 模型名称，默认 qwen-image-2.0

    Returns:
        生成的图片本地路径列表
    """
    key = api_key or os.getenv("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError(
            "未设置 DASHSCOPE_API_KEY 环境变量，无法调用图片生成模型。"
            "请设置: export DASHSCOPE_API_KEY=sk-xxx"
        )

    save_dir = get_image_library_path(image_dir)
    count = min(max(count, 1), 4)

    logger.info("正在调用 DashScope %s 生成 %d 张图片...", model, count)
    logger.info("提示词: %s", prompt[:100])

    messages = [
        {
            "role": "user",
            "content": [{"text": prompt}],
        }
    ]

    saved_paths = []
    for i in range(count):
        logger.info("正在生成第 %d/%d 张图片...", i + 1, count)
        try:
            response = MultiModalConversation.call(
                api_key=key,
                model=model,
                messages=messages,
                result_format="message",
                stream=False,
                n=1,
                negative_prompt=negative_prompt,
                size=size,
            )

            if response.get("status_code") != 200:
                error_msg = response.get("message", "未知错误")
                logger.warning("第 %d 张图片生成失败: %s", i + 1, error_msg)
                continue

            # 从响应中提取图片 URL
            choices = response.get("output", {}).get("choices", [])
            for choice in choices:
                content_list = choice.get("message", {}).get("content", [])
                for item in content_list:
                    image_url = item.get("image")
                    if image_url:
                        filepath = _download_single_image(image_url, save_dir, i)
                        if filepath:
                            saved_paths.append(filepath)

        except Exception as e:
            logger.warning("第 %d 张图片生成异常: %s", i + 1, e)
            continue

        # 多张图片之间稍作间隔
        if i < count - 1:
            time.sleep(1)

    if not saved_paths:
        raise RuntimeError("所有图片生成均失败")

    logger.info("成功生成 %d 张图片", len(saved_paths))
    return saved_paths


def _download_single_image(image_url: str, save_dir: str, index: int) -> str | None:
    """下载单张生成的图片到本地，自动转为 JPEG 压缩以控制文件大小。"""
    try:
        filename = f"gen_{int(time.time())}_{index}.jpg"
        filepath = os.path.join(save_dir, filename)

        img_resp = requests.get(image_url, timeout=60)
        if img_resp.status_code == 200:
            # 先保存原始数据为临时文件，再转 JPEG 压缩
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(img_resp.content))
            img = img.convert("RGB")
            img.save(filepath, "JPEG", quality=85, optimize=True)
            final_size = os.path.getsize(filepath)
            logger.info(
                "图片已保存: %s (%d KB, 原始 %d KB)",
                filepath,
                final_size // 1024,
                len(img_resp.content) // 1024,
            )
            return filepath
        else:
            logger.warning("下载图片失败 (status=%d): %s", img_resp.status_code, image_url)
            return None
    except Exception as e:
        logger.warning("下载图片异常: %s", e)
        return None


def ensure_images_for_publish(
    text_prompt: str,
    count: int = 3,
    image_dir: str | None = None,
    api_key: str | None = None,
) -> list[str]:
    """发布前确保有可用图片。如果图库为空则自动生成。

    Args:
        text_prompt: 用于生成图片的文本描述（通常是帖子标题+内容摘要）
        count: 需要的图片数量
        image_dir: 图库路径
        api_key: DashScope API Key

    Returns:
        可用的图片路径列表
    """
    existing = list_images(image_dir)
    if existing:
        selected = existing[:count]
        logger.info("图库中有 %d 张图片，选取 %d 张", len(existing), len(selected))
        return selected

    logger.info("图库为空，正在自动生成图片...")
    generated = generate_images(
        prompt=text_prompt,
        count=count,
        image_dir=image_dir,
        api_key=api_key,
    )

    if not generated:
        raise RuntimeError("图片生成失败，无法获取可用图片")

    logger.info("成功生成 %d 张图片", len(generated))
    return generated


# ─── CLI 入口 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="图片生成与图库管理")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="列出图库中的图片")
    p_list.add_argument("--image-dir", help="图库路径")

    p_gen = sub.add_parser("generate", help="生成图片")
    p_gen.add_argument("--prompt", required=True, help="图片描述")
    p_gen.add_argument("--count", type=int, default=3, help="生成数量")
    p_gen.add_argument("--image-dir", help="保存目录")
    p_gen.add_argument("--api-key", help="DashScope API Key")
    p_gen.add_argument("--size", default="1440*1800", help="图片尺寸（如 1440*1800）")

    p_del = sub.add_parser("delete", help="删除图片")
    p_del.add_argument("paths", nargs="+", help="要删除的图片路径")

    p_ensure = sub.add_parser("ensure", help="确保有可用图片（无则生成）")
    p_ensure.add_argument("--prompt", required=True, help="图片描述")
    p_ensure.add_argument("--count", type=int, default=3, help="需要数量")
    p_ensure.add_argument("--image-dir", help="图库路径")
    p_ensure.add_argument("--api-key", help="DashScope API Key")

    args = parser.parse_args()

    if args.command == "list":
        images = list_images(args.image_dir)
        print(json.dumps({
            "count": len(images),
            "images": images,
            "image_dir": get_image_library_path(args.image_dir),
        }, ensure_ascii=False, indent=2))

    elif args.command == "generate":
        paths = generate_images(
            prompt=args.prompt,
            count=args.count,
            image_dir=args.image_dir,
            api_key=args.api_key,
            size=args.size,
        )
        print(json.dumps({
            "success": True,
            "count": len(paths),
            "images": paths,
        }, ensure_ascii=False, indent=2))

    elif args.command == "delete":
        deleted = delete_images(args.paths)
        print(json.dumps({
            "success": True,
            "deleted": deleted,
            "total": len(args.paths),
        }, ensure_ascii=False, indent=2))

    elif args.command == "ensure":
        paths = ensure_images_for_publish(
            text_prompt=args.prompt,
            count=args.count,
            image_dir=args.image_dir,
            api_key=args.api_key,
        )
        print(json.dumps({
            "success": True,
            "count": len(paths),
            "images": paths,
        }, ensure_ascii=False, indent=2))

    else:
        parser.print_help()

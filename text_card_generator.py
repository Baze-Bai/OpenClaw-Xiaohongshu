"""文字卡片生成器 — 将标题+正文渲染为小红书风格的多页文字图片。

参考小红书知识博主排版：白底、蓝色左边条标题、正文分页。
图片只能使用一次，使用后从图库中删除。

依赖: Pillow>=10.0, fonts-noto-cjk (apt install fonts-noto-cjk)
"""

from __future__ import annotations

import glob
import logging
import os
import time

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ─── 布局常量 ─────────────────────────────────────────────────────────────────

IMAGE_WIDTH = 1080
IMAGE_HEIGHT = 1440  # 3:4 小红书最佳比例

BG_COLOR = "#FFFFFF"
ACCENT_COLOR = "#2B6CB0"  # 蓝色左边条
TITLE_COLOR = "#111111"
BODY_COLOR = "#1A1A1A"
PAGE_NUM_COLOR = "#999999"

MARGIN_LEFT = 100
MARGIN_RIGHT = 100
MARGIN_TOP_TITLE = 200  # 标题卡上边距
MARGIN_TOP_BODY = 140  # 正文卡上边距
MARGIN_BOTTOM = 120

ACCENT_BAR_WIDTH = 8
ACCENT_BAR_GAP = 24  # 蓝条与文字间距

TITLE_FONT_SIZE = 72
BODY_FONT_SIZE = 42
PAGE_NUM_FONT_SIZE = 32
LINE_HEIGHT_MULT = 1.8  # 行高倍数
PARA_SPACING = 40  # 段间距额外像素

# 正文可用区域
_BODY_USABLE_WIDTH = IMAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT  # 880px
_TITLE_USABLE_WIDTH = (
    IMAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT - ACCENT_BAR_WIDTH - ACCENT_BAR_GAP
)  # 848px

_BODY_LINE_HEIGHT = int(BODY_FONT_SIZE * LINE_HEIGHT_MULT)  # ~76px
_BODY_USABLE_HEIGHT = IMAGE_HEIGHT - MARGIN_TOP_BODY - MARGIN_BOTTOM - 60  # 页码区
_MAX_LINES_PER_CARD = _BODY_USABLE_HEIGHT // _BODY_LINE_HEIGHT  # ~14

# 默认图库路径
_DEFAULT_IMAGE_DIR = os.path.join(
    os.path.expanduser("~"), ".openclaw", "workspace", "xhs-images"
)

# ─── 字体查找 ─────────────────────────────────────────────────────────────────

_FONT_SEARCH_PATHS = {
    "regular": [
        # Linux Noto CJK
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Regular.otf",
        # Linux WQY
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        # Windows fonts via WSL
        "/mnt/c/Windows/Fonts/msyh.ttc",
        "/mnt/c/Windows/Fonts/simhei.ttf",
        "/mnt/c/Windows/Fonts/simsun.ttc",
    ],
    "bold": [
        # Linux Noto CJK
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Bold.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJKsc-Bold.otf",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Bold.otf",
        # Linux WQY (no separate bold, use regular)
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        # Windows fonts via WSL (bold variant)
        "/mnt/c/Windows/Fonts/msyhbd.ttc",
        "/mnt/c/Windows/Fonts/msyh.ttc",
        "/mnt/c/Windows/Fonts/simhei.ttf",
    ],
}


def _find_font(bold: bool = False) -> str:
    """查找系统中的中文字体路径。"""
    # 环境变量覆盖
    env_key = "XHS_FONT_BOLD_PATH" if bold else "XHS_FONT_PATH"
    env_path = os.getenv(env_key)
    if env_path and os.path.exists(env_path):
        return env_path

    variant = "bold" if bold else "regular"
    for path in _FONT_SEARCH_PATHS[variant]:
        if os.path.exists(path):
            return path

    # 尝试通过 fc-match 查找
    import subprocess

    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", f"NotoSansCJKsc:weight={'bold' if bold else 'regular'}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and os.path.exists(result.stdout.strip()):
            return result.stdout.strip()
    except Exception:
        pass

    # 最后回退：找任何 CJK 字体
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", ":lang=zh"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and os.path.exists(result.stdout.strip()):
            return result.stdout.strip()
    except Exception:
        pass

    raise RuntimeError(
        "未找到中文字体。请安装: sudo apt install -y fonts-noto-cjk"
    )


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """加载指定大小的字体。"""
    path = _find_font(bold)
    return ImageFont.truetype(path, size)


# ─── 文字处理 ─────────────────────────────────────────────────────────────────


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """CJK 逐字符换行。处理中英文混排。"""
    lines = []
    for raw_line in text.split("\n"):
        if not raw_line.strip():
            lines.append("")  # 空行保留（段落分隔）
            continue

        current = ""
        for char in raw_line:
            test = current + char
            w = draw.textlength(test, font=font)
            if w > max_width and current:
                lines.append(current)
                current = char
            else:
                current = test
        if current:
            lines.append(current)

    return lines


def _split_into_cards(lines: list[str], max_lines: int) -> list[list[str]]:
    """将行列表分组到多张卡片，尊重段落边界。"""
    cards: list[list[str]] = []
    current_card: list[str] = []

    for line in lines:
        if len(current_card) >= max_lines:
            cards.append(current_card)
            current_card = []
        current_card.append(line)

    if current_card:
        cards.append(current_card)

    return cards


# ─── 渲染函数 ─────────────────────────────────────────────────────────────────


def _render_page_number(
    draw: ImageDraw.ImageDraw,
    card_num: int,
    total: int,
    font: ImageFont.FreeTypeFont,
) -> None:
    """在右上角绘制页码。"""
    text = f"{card_num}/{total}"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    x = IMAGE_WIDTH - MARGIN_RIGHT - text_width
    y = 40
    draw.text((x, y), text, fill=PAGE_NUM_COLOR, font=font)


def _render_title_card(
    title: str,
    card_num: int,
    total: int,
    title_font: ImageFont.FreeTypeFont,
    page_font: ImageFont.FreeTypeFont,
) -> Image.Image:
    """渲染标题卡：蓝色左边条 + 大号粗体标题。"""
    img = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # 页码
    _render_page_number(draw, card_num, total, page_font)

    # 标题换行
    title_x = MARGIN_LEFT + ACCENT_BAR_WIDTH + ACCENT_BAR_GAP
    title_lines = _wrap_text(draw, title, title_font, _TITLE_USABLE_WIDTH)

    # 计算标题块高度，垂直居中偏上
    title_line_height = int(TITLE_FONT_SIZE * 1.5)
    block_height = len(title_lines) * title_line_height
    start_y = max(MARGIN_TOP_TITLE, (IMAGE_HEIGHT - block_height) // 3)

    # 绘制蓝色左边条
    bar_top = start_y
    bar_bottom = start_y + block_height
    draw.rectangle(
        [MARGIN_LEFT, bar_top, MARGIN_LEFT + ACCENT_BAR_WIDTH, bar_bottom],
        fill=ACCENT_COLOR,
    )

    # 绘制标题文字
    y = start_y
    for line in title_lines:
        draw.text((title_x, y), line, fill=TITLE_COLOR, font=title_font)
        y += title_line_height

    return img


def _render_body_card(
    lines: list[str],
    card_num: int,
    total: int,
    body_font: ImageFont.FreeTypeFont,
    page_font: ImageFont.FreeTypeFont,
) -> Image.Image:
    """渲染正文卡：普通字号正文。"""
    img = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # 页码
    _render_page_number(draw, card_num, total, page_font)

    # 绘制正文
    y = MARGIN_TOP_BODY
    for line in lines:
        if line == "":
            y += PARA_SPACING  # 段落间距
        else:
            draw.text((MARGIN_LEFT, y), line, fill=BODY_COLOR, font=body_font)
            y += _BODY_LINE_HEIGHT

    return img


# ─── 主入口 ───────────────────────────────────────────────────────────────────


def generate_text_cards(
    title: str,
    body: str,
    image_dir: str | None = None,
    subtitle: str = "",
) -> list[str]:
    """将标题+正文生成为小红书风格的文字卡片图片组。

    Args:
        title: 帖子标题
        body: 帖子正文（段落用空行分隔）
        image_dir: 图片保存目录
        subtitle: 可选副标题

    Returns:
        生成的图片路径列表（标题卡 + N 张正文卡）
    """
    save_dir = image_dir or _DEFAULT_IMAGE_DIR
    os.makedirs(save_dir, exist_ok=True)

    # 加载字体
    title_font = _load_font(TITLE_FONT_SIZE, bold=True)
    body_font = _load_font(BODY_FONT_SIZE, bold=False)
    page_font = _load_font(PAGE_NUM_FONT_SIZE, bold=False)

    # 用临时图片做文字测量
    tmp_img = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), BG_COLOR)
    tmp_draw = ImageDraw.Draw(tmp_img)

    # 正文换行
    body_lines = _wrap_text(tmp_draw, body, body_font, _BODY_USABLE_WIDTH)

    # 分页
    body_cards = _split_into_cards(body_lines, _MAX_LINES_PER_CARD)
    total_cards = 1 + len(body_cards)  # 标题卡 + 正文卡

    # 限制最多 18 张（小红书限制）
    if total_cards > 18:
        logger.warning("内容过长，截断为 17 页正文 (+ 1 标题页 = 18 页)")
        body_cards = body_cards[:17]
        total_cards = 18

    logger.info("生成文字卡片: 标题卡 1 张 + 正文卡 %d 张 = 共 %d 张", len(body_cards), total_cards)

    timestamp = int(time.time())
    saved_paths = []

    # 渲染标题卡
    title_img = _render_title_card(title, 1, total_cards, title_font, page_font)
    title_path = os.path.join(save_dir, f"textcard_{timestamp}_0.png")
    title_img.save(title_path, "PNG")
    saved_paths.append(title_path)
    logger.info("标题卡已保存: %s", title_path)

    # 渲染正文卡
    for i, card_lines in enumerate(body_cards):
        card_img = _render_body_card(
            card_lines, i + 2, total_cards, body_font, page_font
        )
        card_path = os.path.join(save_dir, f"textcard_{timestamp}_{i + 1}.png")
        card_img.save(card_path, "PNG")
        saved_paths.append(card_path)
        logger.info("正文卡 %d 已保存: %s", i + 1, card_path)

    logger.info("文字卡片生成完成，共 %d 张", len(saved_paths))
    return saved_paths


# ─── CLI 入口 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json
    import sys

    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="文字卡片图片生成器")
    parser.add_argument("--title", required=True, help="帖子标题")
    parser.add_argument("--body", required=True, help="帖子正文（支持 \\n 换行）")
    parser.add_argument("--image-dir", help="图片保存目录")
    parser.add_argument("--subtitle", default="", help="副标题（可选）")

    args = parser.parse_args()

    # 支持 \n 转义
    body = args.body.replace("\\n", "\n")

    paths = generate_text_cards(
        title=args.title,
        body=body,
        image_dir=args.image_dir,
        subtitle=args.subtitle,
    )

    print(json.dumps({
        "success": True,
        "count": len(paths),
        "images": paths,
    }, ensure_ascii=False, indent=2))

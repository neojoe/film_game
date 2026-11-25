"""开发工具:从演示视频里剪几段精华,生成 README 里内嵌播放的 GIF 预览。

为什么需要它:GitHub 的文件页面不会在线播放仓库里的 mp4(大文件只会提示"无法显示"),
而 GIF 可以在 README 里直接内嵌自动播放。所以 README 里放"GIF 精华预览",完整视频提供下载链接。

用法(项目根目录,需要 ffmpeg):
    python -m tools.make_preview_gif                          # 使用默认的三段
    python -m tools.make_preview_gif 22:32 58:64 70:76        # 自定义片段(单位:秒,相对 docs/demo.mp4)

默认三段对应:AI 旁白 + 自由行动 / 被锁住的选项 / 结局后日谈。重录视频后时间点会变,需要重新挑。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEGMENTS = ["22:32", "58:64", "70:76"]


def build_filter(segments: list[tuple[float, float]], width: int, fps: int, colors: int) -> str:
    n = len(segments)
    labels = "".join(f"[s{i}]" for i in range(n))
    parts = [f"[0:v]fps={fps},scale={width}:-1:flags=lanczos,split={n}{labels}"]
    for i, (start, end) in enumerate(segments):
        parts.append(f"[s{i}]trim={start}:{end},setpts=PTS-STARTPTS[v{i}]")
    joined = "".join(f"[v{i}]" for i in range(n))
    parts.append(f"{joined}concat=n={n}:v=1:a=0,split[x][y]")
    parts.append(f"[x]palettegen=max_colors={colors}:stats_mode=diff[p]")
    parts.append("[y][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
    return ";".join(parts)


def main(segments_arg: list[str], src: Path, out: Path, width: int, fps: int, colors: int) -> int:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("没找到 ffmpeg。", file=sys.stderr)
        return 1
    segments = []
    for item in segments_arg:
        start, end = item.split(":")
        segments.append((float(start), float(end)))
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
           "-filter_complex", build_filter(segments, width, fps, colors), "-loop", "0", str(out)]
    subprocess.run(cmd, check=True)
    print(f"已保存 {out.relative_to(ROOT)}  ({out.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("segments", nargs="*", default=DEFAULT_SEGMENTS, help="形如 22:32,单位秒")
    parser.add_argument("--src", type=Path, default=ROOT / "docs" / "demo.mp4")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "demo-preview.gif")
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--fps", type=int, default=6)
    parser.add_argument("--colors", type=int, default=64)
    args = parser.parse_args()
    sys.exit(main(args.segments, args.src, args.out, args.width, args.fps, args.colors))

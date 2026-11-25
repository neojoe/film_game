"""离线批量生成场景插画(智谱 CogView-3-Flash),并写出提示词清单 manifest.json 以便溯源。

用法(项目根目录,需要 .env 里的 ZHIPU_API_KEY):
    python -m tools.gen_assets                  # 只生成缺失的图片
    python -m tools.gen_assets --force          # 全部重新生成
    python -m tools.gen_assets prologue station # 只生成指定场景

为什么离线生成而不是运行时生成:
    文生图单张约 8-10 秒且会被限流。运行时生成会让每次切场景都卡顿;
    离线生成 + 缓存到 static/assets/scenes/,运行时零等待,没有 Key 的人也能看到完整画面。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone

import httpx

from app.config import SCENE_DIR, load_settings
from app.story import Story
from tools.art_prompts import SCENES, full_prompt

SIZE = "1344x768"
CONCURRENCY = 1  # 免费账户并发 > 1 容易触发 1302 限流
MAX_ATTEMPTS = 5
MIN_BYTES = 20_000
RATE_LIMIT_BACKOFF = 12  # 秒 × 第几次尝试


async def generate_one(client: httpx.AsyncClient, settings, key: str, sem: asyncio.Semaphore) -> dict | None:
    prompt = full_prompt(key)
    async with sem:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.monotonic()
            rate_limited = False
            try:
                resp = await client.post(
                    f"{settings.base_url}/images/generations",
                    headers={"Authorization": f"Bearer {settings.api_key}"},
                    json={"model": settings.image_model, "prompt": prompt, "size": SIZE},
                    timeout=120,
                )
                rate_limited = resp.status_code == 429
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
                url = resp.json()["data"][0]["url"]
                image = await client.get(url, timeout=60)
                image.raise_for_status()
                if len(image.content) < MIN_BYTES:
                    raise RuntimeError(f"image too small ({len(image.content)} bytes)")
                path = SCENE_DIR / f"{key}.png"
                path.write_bytes(image.content)
                print(f"  ok   {key:<11} {len(image.content)//1024:>4} KB  {time.monotonic()-started:.1f}s")
                return {
                    "key": key,
                    "file": path.name,
                    "model": settings.image_model,
                    "size": SIZE,
                    "prompt": prompt,
                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            except Exception as exc:  # noqa: BLE001 - 单张失败不应中断整批
                print(f"  fail {key:<11} attempt {attempt}/{MAX_ATTEMPTS}: {exc}", file=sys.stderr)
                await asyncio.sleep((RATE_LIMIT_BACKOFF if rate_limited else 2) * attempt)
    return None


async def main(keys: list[str], force: bool) -> int:
    settings = load_settings()
    if not settings.ai_enabled:
        print("未配置 ZHIPU_API_KEY(.env),无法生成。", file=sys.stderr)
        return 1

    story = Story.load()
    used = {n.image for n in story.nodes.values()}
    missing_prompts = used - set(SCENES)
    if missing_prompts:
        print(f"剧情引用了没有提示词的图片: {sorted(missing_prompts)}", file=sys.stderr)
        return 1

    SCENE_DIR.mkdir(parents=True, exist_ok=True)
    targets = keys or sorted(used)
    todo = [k for k in targets if force or not (SCENE_DIR / f"{k}.png").exists()]
    print(f"需要生成 {len(todo)} / {len(targets)} 张({settings.image_model}, {SIZE})")
    if not todo:
        return 0

    manifest_path = SCENE_DIR / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = {e["key"]: e for e in json.loads(manifest_path.read_text(encoding="utf-8"))}

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(generate_one(client, settings, k, sem) for k in todo))

    for entry in results:
        if entry:
            manifest[entry["key"]] = entry
    manifest_path.write_text(
        json.dumps(sorted(manifest.values(), key=lambda e: e["key"]), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    failed = [k for k, r in zip(todo, results) if r is None]
    if failed:
        print(f"失败: {failed}(可重跑本命令只补缺失的图)", file=sys.stderr)
        return 2
    print("全部完成。")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("keys", nargs="*", metavar="SCENE", help=f"可选场景: {', '.join(sorted(SCENES))}")
    parser.add_argument("--force", action="store_true", help="已存在的图片也重新生成")
    args = parser.parse_args()
    unknown = [k for k in args.keys if k not in SCENES]
    if unknown:
        parser.error(f"未知场景: {unknown}")
    raise SystemExit(asyncio.run(main(args.keys, args.force)))

"""Export only Douyin cookies locally. Never prints cookie contents."""
import json
from pathlib import Path


def main():
    source = Path("storage-state.json")
    state = json.loads(source.read_text(encoding="utf-8"))
    cookies = [c for c in state["cookies"] if c.get("domain", "").lstrip(".") == "douyin.com"
               or c.get("domain", "").lstrip(".").endswith(".douyin.com")]
    if not cookies:
        raise ValueError("没有找到抖音 Cookie，请先完成本人登录")
    Path("douyin-cookie.json").write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    print("已在本机生成 douyin-cookie.json。仅复制到自己的 GitHub Secret，不要上传或分享此文件。")


if __name__ == "__main__":
    main()

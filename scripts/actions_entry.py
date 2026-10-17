"""Environment values are read as data, never interpolated into shell commands."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.safe_main import main


def arguments():
    initialize = os.getenv("INITIALIZE_STATE") == "true"
    dry = os.getenv("REQUEST_DRY_RUN", "true") != "false"
    if initialize:
        if not dry:
            raise ValueError("初始化时必须勾选 Dry Run")
        return ["--initialize-state"]
    return ["--dry-run"] if dry else ["--send"]


if __name__ == "__main__":
    try:
        args = arguments()
    except ValueError:
        print("配置错误：初始化时必须勾选 Dry Run；未执行发送")
        raise SystemExit(2)
    raise SystemExit(main(args))

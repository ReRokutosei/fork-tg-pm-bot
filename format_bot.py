# format_bot.py
import subprocess
import sys
from pathlib import Path

def main():
    bot = Path("bot.py")
    if not bot.exists():
        print("❌ bot.py not found.", file=sys.stderr)
        sys.exit(1)

    # 显式使用当前 Python 环境调用 black
    try:
        result = subprocess.run(
            [sys.executable, "-m", "black", str(bot)],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent  # 确保工作目录正确
        )
        if result.returncode == 0:
            print("✅ Formatted bot.py")
        else:
            print("❌ black error:", result.stderr, file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print("❌ Failed to run black:", e, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
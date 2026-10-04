import argparse
import asyncio
import json
import time

from watcherobot.robot import _expression_custom_vector_path

from guess_animal.config import ROOT, Settings
from guess_animal.generator import AnimalGenerator


async def main() -> None:
    parser = argparse.ArgumentParser(description="调用所选 provider 验证真实动物图案生成")
    parser.add_argument("--count", type=int, default=3, help="连续生成的题目数量，默认三题")
    args = parser.parse_args()
    if not 1 <= args.count <= 10:
        parser.error("检查题目数量必须位于一至十之间")
    settings = Settings.from_environment()
    output = ROOT / ".artifacts" / "generation_check" / settings.drawing_provider
    generator = AnimalGenerator(settings, output / "history.json")
    report = []
    try:
        for index in range(args.count):
            start = time.monotonic()
            animals = await generator.generate(1)
            if len(animals) != 1:
                raise AssertionError("每次调用必须只生成一道题目")
            for animal in animals:
                for path in animal.drawing_steps():
                    _expression_custom_vector_path(path)
            seconds = round(time.monotonic() - start, 2)
            names = [animal.name for animal in animals]
            print(f"第 {index + 1} 次生成通过：{names}，耗时 {seconds} 秒", flush=True)
            report.append({"names": names, "seconds": seconds})
    finally:
        await generator.close()
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(main())

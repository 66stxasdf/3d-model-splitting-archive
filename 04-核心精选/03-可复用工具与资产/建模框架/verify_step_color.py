"""回读验证彩色 STEP（命令行工具）。

跑法：
    D:/Mangshe/python.exe colorparts/verify_step_color.py colorparts/out/MK02/MK02_colored.step

为什么不能数字符串（2026-10-28 踩坑）：
  早期用 txt.count('STYLED_ITEM')//2 判定带色零件数，MK-02 报 20/37，看着像失败。
  实际 STEP 里一个零件可能被写进多个 representation / 复用同一 style 实体，
  字符串出现次数和「带色零件数」根本不是一一对应。判据错了会让人去乱改几何。
  唯一可信的判据：用 STEPCAFControl_Reader 读回来，遍历标签逐个查颜色
  （实现见 cpkit.read_step_colors）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cpkit import read_step_colors


def main(path: str) -> int:
    n, nc, hist, bad = read_step_colors(path)
    print(f'文件: {path}')
    print(f'简单形状 {n} 件，带色 {nc} 件，唯一色 {len(hist)} 种')
    for k, v in sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f'  {k}  ×{v}')
    if bad:
        print(f'[!] 无色 {len(bad)} 件: {", ".join(bad[:20])}')
        return 2
    return 0 if nc == n else 2


if __name__ == '__main__':
    p = sys.argv[1] if len(sys.argv) > 1 else 'colorparts/out/MK02/MK02_colored.step'
    sys.exit(main(p))

# mmf2-image-patcher

在不重新编译游戏的前提下，**原地替换 Clickteam Fusion 2（MMF2）游戏可执行文件里的图像**。

面向的典型场景是像素风格的**民间汉化**：这类游戏的文字常常不是文本，而是**预先渲染进图片的字条/对话框**——没有 Shift-JIS 编码限制，却也没有现成的翻译手段。本工具解决的就是这一段。

```
普通汉化流程：  改文本 → 替换字符串 → 完成
MMF2 点阵游戏： 文字是图片 → 无文本可改 → ？？？
本工具：        解析图像库 → 重绘图片 → 原地写回 exe → 保持字节大小不变
```

---

## 它解决的核心问题

MMF2 的可执行文件在 PE 之后追加了游戏数据，图像库是一整块（样例中 7,136 张图 / 33 MB）。
改写它不是「找到图片、覆盖字节」这么简单，有三个硬性约束：

1. **每张图的压缩尺寸必须与原值完全一致**
   游戏内部有一张**逐图偏移表**（chunk 21845）。任何一张图尺寸变化，后面所有图的偏移全部平移，
   表随之失效——表现为**进程正常启动、窗口正常创建、画面全黑**。
2. **整文件字节数必须不变**
3. **像素布局不是 `W×H×4`**
   带 alpha 的图是「紧凑 BGR 平面 + 4 字节对齐的 alpha 平面」，两个平面行对齐规则不同。

详见 **[docs/FORMAT.md](docs/FORMAT.md)**——完整的容器、Chunk、ImageBank、像素布局规格。

---

## 它能做什么

- 导出全部图像，按 handle 索引，便于建立「文字图清单」
- 自动识别**文字图**（按尺寸分布 + 去重 + 接触表目视确认）
- 把重绘后的图片**原地写回 exe**，保持文件大小与所有偏移不变
- 放不下时**自动逐级量化调色板**直到塞进原字节预算
- **字节级回归验证**：解析改动前后的 exe，逐张比对，报告替换了几张、其余是否原封不动

## 它不做什么

- ❌ 不包含、不下载、不分发任何游戏本体或游戏素材
- ❌ 不提供任何成品汉化补丁
- ❌ 不绕过游戏的正版验证

**本仓库只有工具与格式文档。** 图片、文本、exe 一律由使用者用自己的游戏在本地生成。

---

## 环境要求

| 组件 | 用途 |
|---|---|
| Python 3.10+ | 主工具链 |
| `numpy`, `Pillow` | 图像解析与重绘 |
| [CTFAK 2.0](https://github.com/CTFAK/CTFAK2.0) | 解析游戏、导出图像（**需自行编译**，无预编译包） |
| .NET SDK 8 | 编译 CTFAK |
| `CTFAK-Native.dll` | CTFAK 的必需原生依赖，可从 [CTFAK-UnEx](https://github.com/AITYunivers/CTFAK-UnEx) 的 `.resources/` 取预编译 x64 版 |
| 中文点阵字体（如 [Zpix](https://github.com/SolidZORO/zpix-pixel-font)，MIT） | 重绘文字用 |

> 编译 CTFAK 时会踩到三个坑（无控制台崩溃、静态字段为 null、ASP.NET 运行时版本），
> 已在 [docs/SETUP.md](docs/SETUP.md) 中列出具体改法。

---

## 快速上手

```bash
# 1) 用 CTFAK 列出所有 chunk，确认图像库的位置与 Flag
python src/walk_chunks.py  /path/to/game.exe

# 2) 导出图像库明文（CTFAK 打补丁后支持 CTFAK_DUMP_CHUNKS 环境变量）
#    确认 26214 的 Flag 是 0（明文）

# 3) 按 handle 导出全部图像，建立 handle ↔ 图片 的映射
#    CTFAK 的 NormalImage.Read 在 build >= 284 时会做 Handle--
#    —— 这个「减一」是最容易踩的坑，详见 FORMAT.md

# 4) 统计尺寸分布，找出疑似文字图（宽扁长条 / 微型字模）
python src/inventory.py /path/to/dumpdir

# 5) 目视确认接触表，建立翻译清单，重绘
python src/redraw.py

# 6) 原地写回（严格保持每张图的 CompressedSize）
python src/patch.py --exe /path/to/game.exe --bank bank.bin --redrawn ./redrawn

# 7) 字节级回归验证
python src/verify.py --orig game.exe --patched game_zh.exe
```

---

## 实战记录

一个完整的真实案例（含全部踩坑与定位过程）见 **[docs/CASE-STUDY.md](docs/CASE-STUDY.md)**。

摘要：

```
引擎        Clickteam Fusion 2.5，build 293，PAMU（Unicode）头
图像库      7,136 张 / 32.9 MB，Flag = 0（完全明文）
文字图      310 张唯一（132×15 标签 / 426×66 文本框 / 80×16 按钮 / 31×7 调试标签）
最终替换    294 张，exe 大小与原版一字节不差
验证        内容变化 294 张，其余 6,842 张原封不动，游戏正常渲染
```

---

## 已知限制

- 仅在 **build 293 + `PAMU` 头**上验证过
- Android 打包的游戏（GraphicMode 0–5）使用另一套像素布局，未验证
- Fusion 3（`CRUF` 头）未验证
- 图像库 Flag ≠ 0（加密或压缩）时，需要额外处理加密层
- 不同游戏的 `Transparent` 语义、调色板习惯、尺寸分布都可能不同——**先全量导出再动手**

---

## 法律与伦理

- 本仓库**不含任何游戏素材**，也不提供成品补丁
- 请使用**你自己合法拥有**的游戏副本
- 格式文档属于互操作性研究；请遵守你所在地区的法律
- 如果你是游戏作者并希望本项目移除对某款游戏的引用，请开 issue

## 致谢

- [CTFAK](https://github.com/CTFAK/CTFAK2.0) / [CTFAK-UnEx](https://github.com/AITYunivers/CTFAK-UnEx)
  ——没有它们就没有这个项目；本文档中的若干结论也是在阅读其源码后验证的
- [Zpix 最像素字体](https://github.com/SolidZORO/zpix-pixel-font)（MIT）

## 许可

MIT

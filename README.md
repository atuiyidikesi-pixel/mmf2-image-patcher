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
| Python 3.10+ | 全部工具 |
| `numpy`, `Pillow` | 图像处理 |

```bash
pip install numpy Pillow
```

**核心流程不需要 CTFAK、不需要 .NET、不需要 Windows。**
`src/` 里的四个脚本是纯 Python，直接读 exe、自己解析图像库。

只有一项可选依赖：[CTFAK](https://github.com/CTFAK/CTFAK2.0)，用于**交叉验证**
你的解析结果（本项目的解码器就是用它逐字节比对验证的）。如果你要用它，
编译时有三处必须打的补丁，见 [docs/SETUP.md](docs/SETUP.md)。

---

## 快速上手

```bash
# 1) 先看看这个 exe 是什么结构，图像库在哪、是否加密
python src/walk_chunks.py /path/to/game.exe

# 2) 导出全部图像（按 handle 命名），同时导出图像库原始数据
python src/extract.py /path/to/game.exe -o dump/
#    -> dump/<handle>.png      每张图一个文件
#       dump/bank.bin          图像库原始字节，后面 patch 要用
#       dump/index.json        每张图的尺寸/格式/压缩后大小

# 3) 找出哪些图含文字（这正是"这游戏有多少文本"的答案）
python src/inventory.py dump/ -o inventory/
#    -> inventory/summary.txt   按"像文字的程度"排名
#       inventory/catalog.json  每个尺寸组的唯一图片清单
#       inventory/<W>x<H>.png   接触表，用来肉眼确认

# 4) 翻译并重绘 —— 这一步是你的工作
#    把改好的图放进 redrawn/，文件名保持 <handle>.png 不变
#    只放你真正改过的图即可

# 5) 原地写回
python src/patch.py --exe /path/to/game.exe --bank dump/bank.bin \
                    --images redrawn/ -o game_zh.exe

# 6) 字节级回归验证（不要只看"游戏没崩"）
python src/verify.py /path/to/game.exe game_zh.exe
```

### 第 6 步为什么不能省

一个改错了的补丁**照样能启动、照样有窗口**，只是画面全黑；一个写错 handle 的补丁
**照样能玩**，只是你改的那张图根本不是你想改的那张。
`verify.py` 会明确告诉你：

```
images            : 7,136
payloads changed  : 450
payloads identical: 6,686
comp_size preserved: 7,136 / 7,136   (all images keep their original offset)

RESULT: OK
```

`comp_size preserved` 必须是满分。只要有一张图的压缩后大小变了，
它在文件里的偏移就会移动，游戏内部那张**逐图偏移表**（chunk 21845）随之失效。

### 放不下怎么办

`patch.py` 会自动处理：先尝试多种 zlib 策略，仍超出原字节数就**逐级量化调色板**
（128 → 96 → … → 4 色）直到塞进去。实在放不下的会保留原图并报告出来，
**绝不会**为了让你的图进去而改动文件大小。

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

## 正确性验证

解码器不是"看起来能跑"，而是**与 CTFAK 逐字节比对过**：

```
解码结果与 CTFAK 一致    7,136 / 7,136   100.00%
编码往返字节一致          7,136 / 7,136   100.00%
整个图像库重新序列化       与原文件字节完全相同
```

> CTFAK 的像素转换在原生 DLL（`CTFAK-Native.dll`）里，源码不可见，
> 所以它很适合当参照物。上面这个比对过程中揪出了两个真实 bug：
> 透明色按 RGBA 而非 BGRA 读（准确率 97%）、以及多加了"黑色跳过"的判断（98.6%）。

端到端流水线也在同一个样本上跑通过：

```
extract.py   7,136 张全部解码，0 失败
patch.py     替换 3 张，exe 大小不变
verify.py    变化 3 张 / 其余 7,133 张原样 / comp_size preserved 7,136 of 7,136 / RESULT: OK
```

---

## 已知限制

- 仅在 **build 293 + `PAMU` 头**上做过完整验证
- Android 打包的游戏（GraphicMode 0–5）使用另一套像素布局，未验证
- Fusion 3（`CRUF` 头）未验证
- 图像库 Flag ≠ 0（加密或压缩）时，`extract.py` 会明确拒绝并要求你先解密
- `inventory.py` 的文字图排名是**启发式**，只用来缩小范围——一定要看接触表确认
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

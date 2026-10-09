# Clickteam Fusion 2 (MMF2) — 运行时容器与图像格式规格

> 本文是黑盒逆向的产物：没有官方文档，全部结论来自对真实游戏可执行文件的解析、
> 对照实验与字节级验证。所有数字都经过实测确认。

---

## 1. 可执行文件布局

MMF2 编译出的 `.exe` 是「一个很小的 PE + 追加在后面的游戏数据」。

```
┌─────────────────────────────┐ 0x000000
│  PE 头 + 各 section          │  .text .rdata .data .rsrc .reloc
├─────────────────────────────┤ ← 游戏数据起点
│  PackData（打包的扩展 DLL）   │  以 0x7777 开头，内含 N 个扩展文件
├─────────────────────────────┤
│  CCN 数据流（游戏本体）       │  一串自描述的 Chunk
└─────────────────────────────┘ EOF
```

### 游戏数据起点的计算方式

与 CTFAK 的 `ExeFileReader.CalculateEntryPoint` 一致：

1. 读 `e_lfanew`(0x3C) → PE 头
2. 读 section 数量、可选头大小
3. 遍历 section：
   - 若名字为 `.extra` → 数据起点 = 该 section 的 `PointerToRawData`
   - 否则若已是最后一个 section → 数据起点 = `PointerToRawData + SizeOfRawData`

### PackData

紧跟在数据起点，以 `0x7777` 开头，记录被打包的扩展模块
（`mmfs2.dll`、各 `.mfx` / `.mvx` / `.sft`）。解析完 PackData 后，紧接着就是 CCN 数据流。

---

## 2. Chunk 格式

CCN 数据流由一连串自描述的 chunk 组成：

```
int16  Id
int16  Flag        0=不压缩  1=压缩  2=加密  3=压缩+加密
int32  Size        后面 Data 的字节数
byte[] Data
```

- **Flag = 0**：`Data` 就是明文，直接可用。
- **Flag = 2**：`Data` 经过 XOR 流密码变换。
- **Flag = 3**：先压缩再加密。
- **Flag = 1**：仅压缩。

遍历方式：从数据起点开始，按 `8 + Size` 步进，直到读到 `Id = 32639`（LastChunk）
或越过文件末尾。

### 关键 chunk Id

| Id | 含义 |
|---|---|
| 8739 | Header（以 `PAMU` 开头表示 Unicode 版本） |
| 8740 / 8741 / 8763 | 名称 / 作者 / 版权 |
| 8755 | Global Strings（全局字符串表） |
| 8742 | Menu（应用程序菜单） |
| **26214** | **Images（图像库）** ← 汉化的主战场 |
| 26215 | Fonts |
| 26216 | Sounds |
| 13107 | Frame |
| 13117 | Frame Events |
| **21845** | **Image Handles / 图像偏移表** ← 见第 5 节，极其重要 |
| 32639 | LastChunk |

---

## 3. 加密

MMF2 用的是 **XOR 流密码**，密钥由游戏元数据推导：

```
key = MakeKey(name, copyright, editorFilename)   // build > 284
key = MakeKey(editorFilename, name, copyright)   // build ≤ 284
```

变换是对称的（异或自反），所以**加密与解密是同一个操作**。

> 实测：`26214`（图像库）和 `26216`（音效库）在本样例中 **Flag = 0**，即完全明文。
> 图像库可以直接原地改写，不需要处理加密。

---

## 4. ImageBank（chunk 26214）格式

```
int32  图像数量 count
重复 count 次：
    int32  Handle
    int32  DecompressedSize      // 下面这份 payload 解压后的长度
    int32  CompressedSize
    byte[] zlib(payload)         // CompressedSize 字节
```

### payload 结构（头部固定 32 字节）

```
偏移  类型    字段
 0    int32   Checksum          // 运行时似乎不校验（实测改像素后仍正常运行）
 4    int32   References
 8    int32   DataSize          // 像素数据字节数，决定下面的布局
12    int16   Width
14    int16   Height
16    uint8   GraphicMode
17    uint8   Flags             // bit0 RLE, bit1 RLEW, bit2 RLET, bit3 LZX,
                                // bit4 Alpha, bit5 ACE, bit6 Mac, bit7 RGBA
18    int16   (padding)
20    int16   HotspotX
22    int16   HotspotY
24    int16   ActionX
26    int16   ActionY
28    4 bytes Transparent       // BGRA，透明色（第 4 字节为 255 时生效）
32    byte[]  像素数据
```

### Handle 有一个「减一」

```csharp
Handle = reader.ReadInt32();
if (Settings.Build >= 284) Handle--;      // ← 磁盘上的值比逻辑 handle 大 1
```

**这是最容易踩的坑**：如果按磁盘上的原始值去索引，每张图都会错位到相邻槽位；
而相邻图往往尺寸相同，尺寸校验根本拦不住。作者本人就在这里浪费了一轮完整调试。

### 像素布局（GraphicMode = 4）

**一条规则解释全部情况**：

```
BGR 平面   stride = align2(宽) × 3          宽度向上取偶
alpha 平面 stride = align4(宽)              仅当 Flags 含 Alpha 位时追加
DataSize = align2(w)*3*h  [+ align4(w)*h]
```

`align2(x) = (x+1) & ~1`，`align4(x) = (x+3) & ~3`。

两个平面是**分开存储**的（先全部 BGR 行，再全部 alpha 行），**不是逐行交错**。

#### 为什么容易搞错

看起来像"四种布局"，其实是同一条规则的四个特例：

| 宽度 | 表面现象 | 实际 |
|---|---|---|
| 偶数宽、无 alpha | `DataSize == w*h*3`（"紧凑"） | `align2(w)==w`，规则退化 |
| 奇数宽、无 alpha | `DataSize == align4(w*3)*h`（"补齐"） | 多数奇数宽下两者恰好相等 |
| 任意宽、有 alpha | 上面再加 `align4(w)*h` | |

实测样本（本样例 7,136 张全部命中）：

```
5×7     align2(5)=6   →  6*3*7            =    126   ✓
31×7    align2(31)=32 → 32*3*7            =    672   ✓
9×10    align2(9)=10  → 10*3*10 + 12*10   =    420   ✓
189×132 align2=190    → 190*3*132         =  75240   ✓
426×66  align2=426    → 426*3*66 + 428*66 = 112596   ✓
```

> **验证方式**：拿本条规则解码全部图像，与 CTFAK（其像素转换在原生 DLL 里）的
> 导出结果**逐字节比对**，7,136 / 7,136 完全一致；编码往返同样 100% 一致；
> 整个图像库重新序列化后与原文件字节相同。

### 透明色的字节序

偏移 28 处的 4 字节是 **BGRA**，即 `[0]=B, [1]=G, [2]=R`。

**按 RGBA 读会静默地以另一个颜色作为透明键**，把不相干的像素变成透明——
本样例中这个错误让 7,136 张里 215 张的 alpha 通道出错（准确率从 100% 掉到 97%）。

还有一个反直觉的点：**透明键是无条件应用的，即使它是纯黑**。
加"黑色就跳过"的判断会再错 104 张。


---

## 5. ⚠️ 最重要的约束：图像偏移表

chunk **21845** 是一张 **每张图一项的偏移表**（本样例 29,024 字节 ≈ 7,136 项）。

**运行时靠它定位图像，而 CTFAK 之类的解析器是顺序读取的，所以解析没问题——
但一旦你改变了任何一张图的 compressed size，后面所有图的偏移全部平移，表就失效了。**

症状：**进程正常启动、窗口正常创建、但画面全黑**（渲染器读不到合法图像数据）。

### 因此的硬性规则

> **每张图的 `CompressedSize` 必须与原值完全一致，偏移一个字节都不能动。**

放不下的图片，有两条出路：

1. **逐级降低调色板**（128 → 96 → … → 8 色）重新压缩，直到塞进原字节数
2. 实在塞不下就保留原图

### 尾部填充的正确做法

zlib 解压到 `ZLIB_STREAM_END` 即停止，**流尾部的填充字节会被忽略**。
所以压缩结果比原值小时，直接补零到原长度即可：

```python
nc = zlib.compress(payload, 9)
if len(nc) < csize:
    nc += b"\x00" * (csize - len(nc))
```

**不要**把填充放在 chunk / 数据流末尾——实测那样做同样会黑屏。

---

## 6. 原地替换流程

```python
# 1. 定位图像库 chunk
off = exe.index(bank_bytes) - 8          # flag=0 时 dump 的字节 == exe 中的字节
cid, flag, size = struct.unpack_from("<hhI", exe, off)

# 2. 重建整个 ImageBank（每张图保持原 CompressedSize）
new_bank = struct.pack("<i", count) + b"".join(records)

# 3. 拼接：chunk 头 + 新 bank + 原有后续数据
assert len(new_bank) == size             # 硬性要求
chunk = struct.pack("<hhI", 26214, 0, len(new_bank)) + new_bank
new_exe = exe[:off] + chunk + exe[off + 8 + size:]

# 4. 整文件大小必须不变
assert len(new_exe) == len(exe)
```

---

## 7. 验证方法

改完之后不要只靠「游戏没崩」判断，做**字节级回归**：

1. 用 CTFAK 分别解析原版与改动版，按 handle 导出全部图像
2. 逐张比对哈希，统计「内容被替换的张数」与「保持原样的张数」

预期结果形如：

```
原版 7136 张 / 改动版 7136 张
内容被替换的图: 294 张  (期望 294)
其余保持原样: 6842 张
```

---

## 8. 环境要求

**核心流程是纯 Python**，不需要 CTFAK、不需要 .NET、不需要 Windows：

```bash
pip install numpy Pillow
```

| 组件 | 用途 | 是否必需 |
|---|---|---|
| Python 3.10+ / numpy / Pillow | 全部工具 | ✅ |
| [CTFAK 2.0](https://github.com/CTFAK/CTFAK2.0) | 交叉验证你的解析结果 | ❌ 可选 |

本文档中"与 CTFAK 逐字节比对 100% 一致"这个结论，就是用 CTFAK 当参照物得到的——
它的像素转换在原生 DLL 里，是个很好的独立实现。

如果你要用 CTFAK，**编译时有三处必须打的补丁**：

1. `ASCIIArt.DrawArt2()` 读取 `Console.WindowWidth`——**重定向 I/O 时抛「句柄无效」**，
   而那行 `coeff` 变量赋值后从未被使用（纯死代码）。加 try/catch 或删掉。
2. `CTFAKCore.parameters` 与 `CTFAKCore.path` 是**不可为 null 的静态字段**，
   解析器无条件解引用。自己写工具时必须先赋值。
3. 程序声明依赖 `Microsoft.AspNetCore.App 6.0.0`（因为 Core 用了 `Microsoft.NET.Sdk.Web`），
   若只有 8.x 运行时，改 `runtimeconfig.json` 的版本号并加 `"rollForward": "LatestMajor"`。

另外 `CTFAKCore.Init()` 会加载 `x64\CTFAK-Native.dll`（主仓库不含预编译版），
可从 [CTFAK-UnEx](https://github.com/AITYunivers/CTFAK-UnEx) 的 `.resources/` 取得。


---

## 9. 适用范围与已知限制

**已验证**：Clickteam Fusion 2.5 编译、build 293、`PAMU`（Unicode）头的游戏。

**未验证**：
- 其他 build 号（`Handle--` 的阈值是 284）
- Android 打包的游戏（GraphicMode 0–5 是另一套布局）
- Fusion 3（`CRUF` 头）
- Flag ≠ 0 的图像库（加密/压缩）

**不要假设**：不同游戏的 `Transparent` 语义、调色板习惯、图像尺寸分布都可能不同，
先做一次全量导出再动手。

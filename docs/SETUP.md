# 环境搭建

## 1. .NET SDK（无需管理员）

```powershell
# 查询最新 SDK 版本
$idx = Invoke-RestMethod "https://dotnetcli.blob.core.windows.net/dotnet/release-metadata/8.0/releases.json"
$ver = $idx.'latest-sdk'

# 下载并解压（约 270 MB，解压后约 1 GB）
$url = "https://builds.dotnet.microsoft.com/dotnet/Sdk/$ver/dotnet-sdk-$ver-win-x64.zip"
Invoke-WebRequest $url -OutFile dotnet-sdk.zip
Expand-Archive dotnet-sdk.zip -DestinationPath C:\tools\dotnet-sdk

# 使用
$env:DOTNET_ROOT = 'C:\tools\dotnet-sdk'
C:\tools\dotnet-sdk\dotnet.exe --list-sdks
```

---

## 2. 编译 CTFAK 2.0

仓库**没有预编译包**，必须自己编译。

```powershell
git clone https://github.com/CTFAK/CTFAK2.0
cd CTFAK2.0

$env:DOTNET_ROOT = 'C:\tools\dotnet-sdk'
$env:PATH = "$env:DOTNET_ROOT;$env:PATH"

dotnet build Interface\CTFAK.Cli\CTFAK.Cli.csproj -c Release
dotnet build Plugins\Dumper\Dumper.csproj -c Release
dotnet build Plugins\CTFAK.Decompiler\CTFAK.Decompiler.csproj -c Release
```

### 编译前必须打的三个补丁

#### ① `Interface/CTFAK.Cli/ASCIIArt.cs` — 无控制台时崩溃

```csharp
// 原代码：stdout 被重定向时 Console.WindowWidth 抛 "句柄无效"
// 而且 coeff 赋值后从未被使用，是死代码
var coeff = Console.WindowWidth / Console.WindowHeight;

// 改为（直接删掉那行），并给 Console.Title 加保护
public static void SetStatus(string msg)
{
    try { Console.Title = $"{version}. Status: {msg}"; } catch { }
}
```

同样处理 `Console.ForegroundColor`（重定向时也会抛）。

#### ② `Interface/CTFAK.Cli/Program.cs` — `Console.Clear()` / `Console.ReadKey()`

```csharp
try { Console.Clear(); } catch { }
try { Console.ReadKey(); } catch { }
```

#### ③ 自己写工具时：初始化静态字段

如果你在 CTFAK.Core 之上写自己的工具，**必须**在 `LoadGame` 之前设置：

```csharp
Directory.SetCurrentDirectory(
    Path.GetDirectoryName(Process.GetCurrentProcess().MainModule.FileName));
CTFAK.CTFAKCore.Init();                    // 加载 x64\zlibwapi.dll 与 x64\CTFAK-Native.dll
CTFAK.CTFAKCore.parameters = string.Empty; // 不可为 null，解析器无条件解引用
CTFAK.CTFAKCore.path = gamePath;           // 同上
```

漏掉后两个 → `NullReferenceException at GameData.Read`。

---

## 3. 补齐运行时依赖

### 3.1 `CTFAK-Native.dll`（必需）

`CTFAKCore.Init()` 会加载 `x64\CTFAK-Native.dll`（P/Invoke 原生库）。
CTFAK 2.0 的主仓库里没有预编译版本，但 **CTFAK-UnEx 的 `.resources/` 里有**：

```
CTFAK-UnEx-master/.resources/CTFAK-Native.dll   (x64, 26 KB)
CTFAK-UnEx-master/.resources/zlibwapi.dll       (x64)
```

复制到输出目录的 `x64/` 子目录下：

```
bin\Release\net6.0-windows\
├── CTFAK.Cli.exe
├── Plugins\
│   ├── Dumper.dll
│   └── CTFAK.Decompiler.dll
└── x64\
    ├── CTFAK-Native.dll
    └── zlibwapi.dll
```

### 3.2 运行时版本前滚

产物声明依赖 `Microsoft.AspNetCore.App 6.0.0`（因为 `CTFAK.Core.csproj` 用了
`Microsoft.NET.Sdk.Web`）。若只有 8.x 运行时：

```json
{
  "runtimeOptions": {
    "tfm": "net6.0",
    "frameworks": [
      { "name": "Microsoft.NETCore.App",    "version": "8.0.0" },
      { "name": "Microsoft.AspNetCore.App", "version": "8.0.0" }
    ],
    "rollForward": "LatestMajor"
  }
}
```

改 `CTFAK.Cli.runtimeconfig.json`（以及你自己项目的同名文件）。

---

## 4. 让 CTFAK dump 出解密后的 chunk

本工具需要每个 chunk 的**明文**。给 `Core/CTFAK.Core/CCN/Chunks/Chunk.cs` 的
`Read()` 返回处加一段：

```csharp
static int _dumpIndex = 0;
static void DumpChunk(short id, ChunkFlags flag, byte[] data)
{
    if (Environment.GetEnvironmentVariable("CTFAK_DUMP_CHUNKS") == null) return;
    try
    {
        var dir = Path.Combine(AppContext.BaseDirectory, "chunkdump");
        Directory.CreateDirectory(dir);
        File.WriteAllBytes(
            Path.Combine(dir, $"{_dumpIndex++:0000}_id{id}_flag{(int)flag}_{(data?.Length ?? 0)}.bin"),
            data ?? Array.Empty<byte>());
    }
    catch { }
}

// 在 return ChunkData; 之前：
DumpChunk(Id, Flag, ChunkData);
```

设环境变量 `CTFAK_DUMP_CHUNKS=1` 后运行，即可得到：
- `<idx>_id26214_flag0_*.bin` → **图像库明文**
- `<idx>_id21845_flag1_*.bin` → **图像偏移表**

---

## 5. 按 handle 导出全部图像（建立映射）

`SortedImageDumper` 按「帧/对象/动画」命名，**丢失了 handle 信息**。
而原地写回必须知道 handle。给自己的工具加一段：

```csharp
if (Environment.GetEnvironmentVariable("CTFAK_DUMP_IMAGES") != null)
{
    var dir = Path.Combine(AppContext.BaseDirectory, "imgbyhandle");
    Directory.CreateDirectory(dir);
    var bank  = typeof(GameData).GetField("Images").GetValue(gd);
    var items = (IDictionary)bank.GetType().GetField("Items").GetValue(bank);
    foreach (DictionaryEntry de in items)
    {
        var prop = de.Value.GetType().GetProperty("bitmap");
        var bmp  = (Bitmap)prop.GetValue(de.Value);
        bmp.Save(Path.Combine(dir, de.Key + ".png"), ImageFormat.Png);
    }
}
```

**注意**：`Items` 的 key 是 CTFAK 的逻辑 handle（已经减过 1）；
你在 `src/imagebank.py` 里直接读磁盘得到的 handle 要 **+1** 才能对齐。
见 [FORMAT.md](FORMAT.md) 第 4 节。

---

## 6. 中文字体

点阵游戏请用**点阵中文字体**，矢量字体在这个尺寸下会糊。

推荐 [Zpix 最像素](https://github.com/SolidZORO/zpix-pixel-font)（MIT，12px 点阵，覆盖简繁）：

```bash
curl -L -o zpix.ttf \
  https://github.com/SolidZORO/zpix-pixel-font/releases/latest/download/zpix.ttf
```

---

## 7. Python 依赖

```bash
pip install numpy Pillow
```

---

## 8. 常见问题

| 症状 | 原因 |
|---|---|
| `System.IO.IOException: 句柄无效` at `DrawArt2` | 没打补丁 ①，stdout 被重定向 |
| `NullReferenceException` at `GameData.Read` | 没设 `CTFAKCore.parameters` / `.path` |
| `You must install or update .NET ... Microsoft.AspNetCore.App 6.0.0` | 没做运行时前滚 |
| `FileNotFoundException: x64\CTFAK-Native.dll` | 工作目录不对，或缺原生库 |
| 进程活着但画面全黑 | **改动了图片尺寸导致偏移表失效**，见 FORMAT.md 第 5 节 |
| 图片全部错位一张 | **handle 减一**，见 FORMAT.md 第 4 节 |
| 中文糊成一团 | 用了矢量字体，或描边方向过多吃掉了单像素笔画 |

# ComfyUI-SW-H3MultiRef

海螺 **MiniMax H3** 多图参考生视频 —— 把官方 `MiniMaxH3ReferenceToVideo` 那一大串"条件编码 → 采样 → 解码 → 合成"全部收进**一个节点**，
素材用一个**点开式大面板**录入：每段 = **1 段参考视频 + 最多 9 张参考图 + 最多 3 条参考音频 + 1 段提示词 + 使用时长**。

原来 31 个节点的工作流，现在 **13 个节点**。

---

## 两个节点

### 1. `SW 海螺H3·多图参考素材输入`（`SWH3MultiRefSegments`）

画布上是个小节点，只显示「N 段 · M 段就绪 · 预计总时长」和一个按钮：

```
┌──────────────────────────────────────────┐
│ SW 海螺H3 · 多图参考素材输入              │
│ 2 段 · 1 段就绪 · 预计总时长 6.0s         │
│            [ 打开素材面板 ]              │
│ input 目录：135 视频 / 863 图片 / 115 音频 │
└──────────────────────────────────────────┘
```

点开是一个大面板，每段一张卡片，四列：

| 列 | 内容 |
| --- | --- |
| **参考视频（1 段）** | 视频预览 + 已有文件下拉 + 上传 + **折叠的「取帧设置」**（强制帧率 / 取帧上限 / 跳过片头 / 隔帧取样 / 宽 / 高 / 格式 / 用视频原声当参考音频） |
| **参考图（最多 9 张）** | 3×3 缩略图网格，点「+」上传，或从下拉里选已有图；每格可单独清除 |
| **参考音频（最多 3 条）** | 3 行，上传 / 下拉 / 清除 |
| **提示词 + 使用时长** | 一段提示词 + 该段使用多少秒（**填 0 = 有参考视频就跟视频长度**） |

顶部工具条：`➕ 新增段位`、`批量上传视频`、`批量上传图片`、`清空`、`完成`。
每张卡片右上角：`上移` / `下移` / `复制` / `删除`。

输出 `SW_H3_MULTIREF`，接给下面的一体节点。

### 2. `SW 海螺H3·多图参考生视频（一体）`（`SWH3RefToVideo`）

内置了（**顺序与 04-7 工作流逐节点对齐**）：

- 官方 `MiniMaxH3ReferenceToVideo` 的参考编码（**内部直调官方节点**，行为与原生工作流一致）
- CLIP 卸载（等价 `SW_H3FreeTextEncoder`，每段编码完就把 Qwen3-VL 卸出显存）
- 条件强度（等价 `SW_H3ConditionStrength`）
- 无 CFG 采样（等价 `RandomNoise + KSamplerSelect + BasicScheduler + BasicGuider + SamplerCustomAdvanced`）
- `VAEDecode` + `VAEDecodeAudio` 解码
- 段间音轨拼接 + `CreateVideo`

逐段生成后按帧拼成一条完整视频。

**与 04-7 的逐项对齐表**

| 04-7 工作流里的节点 | 本节点内部 | 说明 |
| --- | --- | --- |
| `136 MiniMaxH3ReferenceToVideo` | 直调官方 `.execute()` | 同一个类、同一份参数，不走任何中间封装 |
| `196 SW_H3FreeTextEncoder` | `_free_text_encoder(clip)` | 同样是定向 `clip.patcher` 卸载，失败才 `unload_all_models` |
| `197 SW_H3ConditionStrength` | `conditioning_set_values` | visual 0.999 / audio 1.0 |
| `123/124/126/129/125` 采样五件套 | `_basic_sample()` | 同样是 euler / beta / 6 步 / 降噪 1.0，同样喂 `125` 的第一个输出 |
| `181 SW_DocKr_VRAMCleanup` | `_vram_cleanup()` | **无条件执行**，参数照抄：`offload_model=False`、`offload_cache=True`、危险阈值 7000MB |
| `122 VAEDecode` / `121 VAEDecodeAudio` | `_decode_latent()` | 同一个 latent 分别喂两个 VAE，与 04-7 一致 |
| `130 CreateVideo` | `_build_video()` | fps 24 / **bit_depth 8** / sRGB / codec none |

执行顺序也一致：`ref2va → 卸CLIP → 设强度 → 采样 → 显存清理 → 解码 → CreateVideo`。
每一段跑完会在控制台打一行分段计时（素材 / 编码 / 卸CLIP / 采样 / 清理 / 解码），慢在哪一眼能看到。

**输入**

| 端口 | 类型 | 说明 |
| --- | --- | --- |
| `model` | MODEL | 接 LoRA / SageAttention / BlockSparse / LoRA 堆叠之后 |
| `clip` | CLIP | Qwen3-VL 文本编码器 |
| `vae` | VAE | H3 video VAE |
| `audio_vae` | VAE | H3 audio VAE（不接 → 参考音频无法编码、成片静音） |
| `段位数据` | SW_H3_MULTIREF | 接素材面板节点 |

**输出**

| 端口 | 类型 | 说明 |
| --- | --- | --- |
| `video` | VIDEO | 成片（直接接 SaveVideo） |
| `画面` | IMAGE | 全部帧 |
| `音轨` | AUDIO | 拼接后的音轨 |
| `段数` | INT | |
| `成片帧数` | INT | |
| `成片秒数` | FLOAT | |
| `报告` | STRING | 每段的帧数 / 时长来源 / 素材数 / seed |

**关键控件**（默认值对齐 04-7 武戏版）

节点上只有 5 个真连线端口（`model` / `clip` / `vae` / `audio_vae` / `段位数据`），
其余全是控件 —— 素材一律在「多图参考素材输入」面板里配，节点上不再有视频/图/音频的输入口。

| 控件 | 默认 | 这是什么 | 怎么调 |
| --- | --- | --- | --- |
| 宽 / 高 | `736` / `1280` | 成片分辨率（自动按 32 对齐） | 04-7 就是 9:16 竖屏 736×1280。想更清晰可上 832×1472，显存与耗时同步涨 |
| 默认时长 | `7.3` | 某段**既没填「使用时长」、也没参考视频**时的兜底秒数 | 会被吸附到 H3 的 `5+17k` 帧网格；7.3s = 175 帧，与 04-7 原工作流的 `ComfyMathExpression` 同值 |
| 参考图尺寸 | `match` | 参考图送进模型前缩到多大 | `match` = 按成片面积等比缩（省显存，推荐）；`max` = 短边 2048，人物更保真但显存翻几倍 |
| 参考强度 | `0.999` | 参考图 / 参考视频对画面的约束力（`SW_H3ConditionStrength` 的 visual） | `1.0` = 完全听参考；调低会让模型更自由（容易跑形）。换人 / 武戏这类要求像的任务别动 |
| 音频强度 | `1.0` | 参考音频对生成音轨的约束力（同上的 audio） | 一般保持 `1.0` |
| 种子 | `0` | 第 1 段的采样随机种子 | 换个数字就换一版 |
| 递增种子 | `true` | 第 N 段用 `种子 + (N-1)` | 开着各段有变化又同风格；想每段完全一致就关掉 |
| 步数 | `6` | 采样步数 | 配 **4 步 Turbo LoRA** 用 6；若换成非 Turbo 权重需 20~30 |
| 采样器 | `euler` | 采样算法 | H3 官方用 `euler`，别动 |
| 调度器 | `beta` | 噪声调度曲线 | H3 官方 `BasicScheduler` 就是 `beta`，别动 |
| 降噪 | `1.0` | 重绘强度 | `1.0` = 从纯噪声全新生成；只有做「参考视频转绘」时才调低 |
| 帧率 | `24.0` | 成片帧率 | H3 固定 24 |
| 卸载CLIP | `true` | 每段编码完把 Qwen3-VL 卸出显存（`SW_H3FreeTextEncoder`） | 显存紧就开着 |
| **只生成第几段** | `0` | `0` = 全部段；填 `N` = 只跑第 N 段 | 调提示词时用，出片快很多 |
| **参考视频短边** | `352` | 参考视频帧的**短边像素**，只缩不放、保持宽高比 | **性能开关，见下一节**。352 = 你 04-7 工作流里 `KJNodes Resize Image v2` 的同款设置；填 `0` = 不限制（用原始尺寸，慢） |

> 采样后、解码前会**无条件**执行一次 04-7 里 181 号节点的显存清理
> （`SW_DocKr_VRAMCleanup`，`offload_model=False` / `offload_cache=True` / 危险阈值 7000MB）：
> 只 gc + 清 CUDA 缓存，不卸载模型，所以下一段不用重读 25GB DiT。

---

## ★ 为什么这个插件曾比 04-7 慢 3 倍（603s vs 191s）

**结论先说：根因是参考视频喂进去的分辨率。** 04-7 在 `SW-loadvideo` 后面挂了一个
`KJNodes Resize Image v2` 压到 **352×640**；本插件早期版本直接把参考视频缩到 H3 标称画布
**768×1344**。官方 `MiniMaxH3ReferenceToVideo.execute` 对参考视频的处理是：

1. `adapt_canvas` 算标称画布（短边 768、面积上限 768×1344）；
2. **输入比标称画布小时，保持输入原尺寸**（`if vw*vh < cw*ch` 就不动）；
3. `_resize`（lanczos）→ 截断到 `frame_count` → 对齐 `5+17k` → **整段 `vae.encode(frames)`**。

于是参考视频的 VAE 编码量 = 每帧像素 × 帧数：

| | 每帧像素 | 帧数 | 编码量 |
| --- | --- | --- | --- |
| 04-7（352×640） | 225,280 | 243 | 1× |
| 本插件旧版（768×1344） | 1,032,192 | 243 | **4.58×** |

两次成片都是 **243 帧 / 10.1s**（生成长度完全相同，排除时长因素）；用户那段成片甚至是
1280×2304（比本插件的 736×1280 像素还多 3.1 倍）却只要 191s —— 差距不在生成分辨率，
就在参考视频编码。总耗时 3.16 倍与编码量 4.58 倍方向一致。

**修复 = 新增「参考视频短边」控件（默认 352）**：取帧时按短边等比缩（只缩不放），
720×1280 的源 → 352×640，与 04-7 完全同款；填 `0` 才回到旧行为。

> 优先级：**面板里填了自定义宽/高 > 短边 > 像素上限**。
> 报告行会显示实际喂入的 `宽x高·帧数`，慢不慢一眼可证。

---

## 提示词里怎么引用素材

沿用 H3 官方标签：

- `<Picture 1>` … `<Picture 9>` —— 第 1…9 张参考图
- `<Video 1>` —— 参考视频（每段最多 1 段，所以固定是 `<Video 1>`）
- `<Audio 1>` … `<Audio 3>` —— 第 1…3 条参考音频

> 注意：如果某段勾了「用视频原声当参考音频」，视频原声会**占用一个 `<Audio j>` 编号**，
> 而且是排在 `<Video 1>` 前面出现的。写提示词时按面板里 `音频 1/2/3` 的顺序对号入座。

---

## 关于视频取帧：不需要再挂 `SW-loadvideo`

H3 的参考视频本质是「一串 IMAGE 帧 + 可选的原声音轨」，正是 `SW-loadvideo` 的两个输出。
所以这个包把取帧逻辑内置了（同一套 `VHSLoadFormats` 语义）：

| 取帧参数 | 默认 | 说明 |
| --- | --- | --- |
| 强制帧率 | `24` | `0` = 保持原帧率 |
| 取帧上限 | `0` | `0` = 不限（但仍受 720 帧上限与内存守卫约束） |
| 跳过片头 | `0` | 跳过前 N 个输出帧 |
| 隔帧取样 | `1` | 每 N 帧取 1 帧 |
| 宽 / 高 | `0` | `0` = 交给一体节点的「参考视频短边」（默认 352）；填了就以面板为准（优先级最高，且不套任何上限） |
| 格式 | `H3` | H3 / AnimateDiff / Wan / LTXV / Mochi / Hunyuan / Cosmos / None |
| 用视频原声当参考音频 | ✅ | 把视频音轨一起送进 `<Audio j>` |

**画布上不用再放 `SW-loadvideo` 了。**

另外做了三处省内存/省时间的处理（画质无损，因为官方节点本来也会压到同一尺寸再截帧）：

1. 源分辨率高于 H3 上限（`768×1344`）时**先压再送**，不会把 4K 素材整段铺进内存；
2. 填了使用时长就**只取该时长对应的帧数**，没填则最多取 720 帧（30s @24fps）——
   官方 `execute` 里本来就会 `frames[:length]` 截掉，多取纯属浪费；
3. **逐帧缩放、边解边缩**：解码循环里每取一帧就立刻缩到目标尺寸再入列。
   旧写法是先按原尺寸堆栈（1080p×243 帧 ≈ 12GB）再整体 `common_upscale`，
   又慢又容易爆内存；现在峰值只有几十 MB。

---

## 安装

把整个 `ComfyUI-SW-H3MultiRef` 文件夹放进：

```
ComfyUI/custom_nodes/ComfyUI-SW-H3MultiRef
```

重启 ComfyUI。节点在 **`SW/海螺H3`** 分类下。

依赖只有 ComfyUI 自带的 `PyAV`（`av`）和 `Pillow`，不用额外装东西。

配套工作流：

```
04-7多图参考生视频SW一体版01.json
```

（31 节点 → 13 节点：模型区 + 加速区留外面，其余全融进一体节点）

---

## 工作流结构

```
① 模型区（留外面）    UNETLoader → LoraLoaderModelOnly
                     CLIPLoader / VAELoader(视频) / VAELoader(音频)
                     ModelAttentionBackend（默认未接线，可选）
        ↓
② 加速区（留外面）    MiniMaxH3MemoryEfficientSageAttentionPatch
                     → BlockSparseAttention
                     → SW_LoraStackLoader
        ↓
③ 素材面板  ──段位数据──┐
                        ↓
   一体节点（条件编码 + 卸载CLIP + 采样 + 解码 + 音轨拼接）
                        ↓
                    SaveVideo
```

模型区和加速区故意留在外面，方便随时换 LoRA、换注意力后端，不用进节点里翻。

---

## 踩坑记录

这一版做下来踩到的几个坑，都写在代码注释里了，这里也记一笔：

1. **`addDOMWidget(name, "div", root, { serialize:false })` 的第 4 个参数只写进 `options`**，
   还必须再显式设一次 `widget.serialize = false`，否则 `widgets_values` 里会多出一个 `null`。
   （`options.serialize` 控制的是"是否进 API prompt"，`widget.serialize` 控制的是"是否写进工作流 JSON"，两回事。）
2. **`class` 上的 `display:grid` 会盖掉 `hidden` 属性的默认 `display:none`** ——
   「取帧设置」看着是折叠的、其实一直展开着。必须显式补 `.swh3-opts[hidden] { display:none; }`。
3. **面板 grid 要写 `align-items:start`** —— 否则某一列（比如展开取帧设置后的视频列）变高，
   其它列会被 flex 压缩，参考图 3×3 的正方格子就被压扁了。
4. **前端文件必须放在 `web/` 顶层**，不能放 `web/js/`，否则相对 import 404、扩展静默不加载；
   用绝对路径 `import { app } from "/scripts/app.js"` 最稳。
5. 测试脚本里**不要把插件目录插进 `sys.path`** —— 会让官方 `comfy_extras` 里的 `import nodes`
   解析到插件自己的 `nodes.py`，报一个看起来很像 bug 的循环导入。
6. **`widgets_values` 必须与当前控件严格一一对应，隐藏 widget 也占一位** ——
   `INT` 上开了 `control_after_generate` 会额外多出一个 combo widget。
   手写工作流 JSON 时少写这一位，从那里开始整列错位，界面表现为
   「步数 `NaN`、采样器 `beta`、调度器 `1`、降噪 `24.00`」这种匪夷所思的值。
   本版直接**不在 `种子` 上开 `control_after_generate`**（节点自带「递增种子」），
   少一个隐藏 widget 就少一次错位机会。
7. **`optional` 输入口在画布上就是一个端口** —— 哪怕前端不主动 `addInput`，
   ComfyUI 也会把 `INPUT_TYPES` 里的 `optional` 渲染成一排空圆点。
   素材既然全在面板里配，本版把 `optional` 整块删掉，节点上只剩 5 个真连线端口。
8. **`VAEDecode` 是全 ComfyUI 唯一没有 `classmethod execute` 的节点** ——
   它是老式节点，`FUNCTION = "decode"`，只有实例方法 `.decode(self, vae, samples)`。
   （`BasicScheduler` / `KSamplerSelect` / `BasicGuider` / `RandomNoise` /
   `SamplerCustomAdvanced` / `VAEDecodeAudio` / `CreateVideo` / `MiniMaxH3ReferenceToVideo`
   都经 `io.Schema` 包装过，一律有 `.execute`。）
   在节点里写 `VAEDecode.execute(...)` 会在采样跑完 463 秒之后才崩，白等一场。
9. **参考视频帧必须除 255** —— 官方 `MiniMaxH3ReferenceToVideo` 的 `_resize` 和 `vae.encode`
   都不做数值归一，喂 0-255 的张量进去，参考视频会以"全白画面"的形式进 VAE 和 Qwen。
   ComfyUI 的 IMAGE 惯例是 0-1，`PyAV` 解出来的是 uint8 0-255，中间必须补一刀。
10. **官方节点对参考视频"保持小输入原尺寸"，所以喂小 = 直接变快**（2026-10-16，603s vs 191s 实证）——
    `adapt_canvas` 只在输入**大于**标称画布时才缩；小于时原样整段 `vae.encode`。
    参考视频是逐帧进 VAE 的，每帧像素直接决定编码量：352 短边比 768 短边少 4.58 倍像素。
    用户原工作流在 `SW-loadvideo` 后面挂 `Resize Image v2 352×640` 不是随手加的，那就是性能开关。
11. **报告行要显示实际喂入尺寸** —— 性能问题最难缠的是"看不见"。每段报告现在打
    `视频 352x640·73帧`，用户自己能对账，不用来回截图猜。

---

## 版权

代码公开，**但保留所有权利**（未授予开源许可）。

---
name: dazzle-deck
description: 生成单 HTML 高视觉冲击演示 deck（1280×720 / 16:9，键盘翻页，沉浸动效与跨页过渡），按"规划 → 骨架 → 逐页填充 → 逐页渲染自检 → 全局复审"的增量流程构建
---

# dazzle-deck：单 HTML 炫彩演示 deck

你要交付的不是一份"幻灯片"，而是一场在浏览器里上演的**动态视觉演出**：持续运行的动态背景、每页的入场编排、页间的过渡特效、键盘可交互——同时排版严谨、内容真实、零控制台报错。

> **你是「会写代码的设计师」,不是炫技的前端。** 克制是高级感的来源:每个特效都为信息服务,**绚得其所**——宁可少一个特效,也不要多一处拥挤 / 喧宾夺主 / 排版失序。动手前先在阶段 1 把设计决策**写死成契约**,全程照契约走,别即兴加料。

## 1. 产物与边界

- 最终产物：工作区根目录的 **`deck.html`**（如用了生成图则外加 `assets/`，图片用相对路径 `assets/img_NN.png` 引用；没有生成图时 deck.html 就是唯一交付物）。
- 画布 **1280×720（16:9）**，固定设计画布 + JS 等比缩放适配屏幕（见 §4 契约）。
- CSS / JS 全部内联在 `<style>` / `<script>`。允许 CDN：**Three.js r128**、GSAP、ECharts、D3、Google Fonts。**禁止外链图片 URL**（图片只能来自 assets/ 下的本地文件）。
- 工作区文件：`plan.md`（你的规划）、`deck.html`、`assets/`（可选）、`shots/`（渲染截图输出）。

## 2. 工作流（六阶段）

**必须先渲后看：只读代码不算自检。** 渲染统一用 bash 执行 skill 自带脚本：

```bash
python skills/dazzle-deck/scripts/render_deck.py deck.html shots/ --page N   # 渲染第 N 页
python skills/dazzle-deck/scripts/render_deck.py deck.html shots/ --all     # 全部页 + contact_sheet.png
```

成功时 stdout 每行一个 PNG 路径（告警以 `[console]/[static]/[blank]/[nav]` 前缀打在路径之前）；渲染元数据写 `shots/render.json`（console_errors / static_pages / blank_pages / 页数 / 导航方式）。截图后用 `vision_analyze` 查看。

### 阶段 0 —— 理解 query 与定调

query 通常只有一两句话，页数、语言、受众、风格大多由你定夺：

**第一步——把 query 扩成「重度创作简报」再动手**（query 越短越要扩）：像创意总监接 brief 那样补成详细规格——主题野心 + 一句话视觉母题、整体构图设想、**该上的技术就点名上**（Three.js 3D / shader / 粒子 / GSAP / ECharts 动效……按主题选）、每页 fancy 手法草案、要营造的氛围与情绪。**输入信息密度决定产出上限**——别拿一句话硬做，先把它在 plan.md 顶部补成一份你自己都想动手的丰富简报，再据此定调。

- **先定「招牌视觉」，别走安全套路**：每个 deck 必须有一个配得上题材的**招牌视觉手法**（signature visual）。自由档 / 题材档、尤其题材含空间主体（建筑、文旅、天文、自然生态、解剖、产品机械、地图…）时，**优先高天花板模式**：代码搭 Three.js 3D 场景（翻页驱动相机巡游）、自定义 shader 背景、每页专属多场景沉浸、极繁拼贴。**「canvas 2D 粒子背景 + 编辑式排版」只是选项之一、不是默认答案**——它便宜好过检，但全场退回它就是 fancy 的失败。能上更有野心的招牌就上。

- 页数：query 给了就遵从；没给则按内容量定 **8–12 页**。
- 语言：跟 query 的语言走，整页语言自然统一。
- **先判使用场景的庄重度**（§3 场景适配），据此圈定风格候选域与动画密度档。
- **风格必须点名**：`read skills/dazzle-deck/references/style-families.md`，选定一个与主题、场景都契合的 family（query 带了风格倾向则在其方向内落到具体 family）。
- `read skills/dazzle-deck/references/fancy-cookbook.md`，为本 deck 挑 2–3 个技法配方（全局背景选一种、入场编排选一种、过渡/彩蛋按需）。

### 阶段 1 —— 写 plan.md + 设计契约（DESIGN.md / tokens.json）

把设计决策**先写死成契约**再动手。写 `plan.md`，并落两份 deck 级契约文件（不跨 deck）：

1. **brief 自答清单**（读完 query 自己回答，写进 plan.md 头部，不问人）：受众是谁、语气基调、**版型轴**（信息密度 / 图文比 / 网格策略）、庄重度档（庄重 / 自由 / 题材）、动画密度档、风格 family + 为何契合、**明确不要什么**（列 2–4 条本 deck 禁区，如"不要满屏特效 / 不要排版拥挤 / 不要花哨配色"）。
2. **设计系统**：family 名；精确 palette（`--bg / --ink / --accent / --accent-2` 的 hex）；display + body 字体（含中文字体兜底）；全局背景层方案（**代码搭 3D 场景 / shader 噪声场 / 每页多场景 / 粒子流场 / 程序化纹理，选一——按招牌视觉来定，别条件反射选粒子**）；通用过渡方案。
2b. **招牌视觉（必填一行）**：本 deck 的招牌手法是什么、什么野心级别、为何契合题材与场景；若选 3D/沉浸/shader，写明用什么搭（如「Three.js 参数化几何体搭古建场景 + 翻页驱动相机弧线巡游」）。**定了就要真做出来**（见 §5「招牌兑现」）。
3. **落 `tokens.json`（机器可读，供管线统一取用）**：`{"palette":{"bg","ink","accent","accent2"},"fonts":{"display","body"},"motion":"restrained|standard|dazzle","image_style_prefix":"<色系词> palette, <质感/媒介词>, no text, no watermark"}`。其中 **`image_style_prefix` 是本 deck 所有生成图的统一风格契约**——`image_generate` 会自动把它拼到每条 prompt 末尾，**锁死全 deck 图片风格一致**（治"风格不统一"）。
4. **落 `DESIGN.md`（散文契约，3–6 句）**：视觉主题 / 配色角色 / 排版 / **Do's & Don'ts**——把"为什么这么定 + 明确不要什么"写清，作为全程复审基准。
5. **页序表**：每页一行 `NN | 页型 | 一句话内容 | 本页 fancy 手法`。页型从：封面 / 议程 / 观点 / 数据 / 对比 / 流程 / 案例 / 架构 / 时间线 / 总结 中选，相邻页不重复同一页型；fancy 手法不要超过 2 页雷同。
6. **素材清单**（如需）：逐页判断是否"摄影刚需"（§6），需要的写出生图 prompt 草稿与用途。

规划自检：palette 是不是精确 hex？有没有滑回暖米黄/奶油底？页型有变化吗？内容是真实可信的吗（真实主体、真实数据，不是占位词）？

### 阶段 2 —— 生成素材（仅当确需真实图片）

按 §6 的纪律生图：生成 → `vision_analyze` 核对（内容对不对 / 色调搭不搭 / 有无文字水印）→ 不合格改 prompt 重生，**绝不硬用**；确切路径记入 plan.md。没有摄影刚需的 deck 跳过本阶段（多数 deck 应该跳过）。

### 阶段 3 —— 写全局骨架 shell（只写骨架，不写页面内容）

一次 write 写出**完整可运行但页面为空**的 `deck.html` 骨架（结构契约见 §4）：

- head（Google Fonts / CDN）→ `:root` design tokens → 全部基础 CSS（.deck/.slide/.active/导航 UI/通用 keyframes 库/通用过渡）→ `#bg-layer` 全局动态背景（**完整实现并运行**，不是占位）→ N 个空 `<section class="slide">`（仅含 `<!-- SLIDE NN: 页型 + 一句话 -->` 注释，**不写任何真实内容**；封面页可先放最小标题文字保证 smoke 有内容可看，其余页一律保持空）→ 导航控制器 JS → 页码 + 进度条。

**硬性纪律：骨架这一次 write 里，除封面外的 section 必须是空的（只有注释）。不要在骨架阶段就把多页内容一次性写满**——那会让一整批未经单页验证的页面同时落地，错误跨页累积、后面更难收拾，也丢掉了逐页"渲染即验证"的全部价值。

然后 smoke 验证：`render --page 1` + 看图——封面可见、页码/进度条在、背景层在动（看 stdout 的 [static] 与 [console]）、零报错。骨架不过关先修骨架，再进入逐页填充。

### 阶段 4 —— 逐页填充（一页一个循环，不要跳步）

按页序**一页一页**来，每页走完一个完整的"填充→渲染→看图"小循环再进入下一页：

1. `edit` 把**该一页**的空 section 替换为完整内容（页内样式可放 section 内 `<style>` 或按 `#sNN` 前缀写进全局 style；页内 JS 一律注册到 `window.slideInits[N]`，见 §4）。**一次 edit 只填一页**，不要一次 edit 灌进多页内容。
2. `render --page N` → `vision_analyze` 该页截图，按 §5 清单自检。**先渲后看是硬要求**——不渲染就接着填下一页是不允许的。
3. 有硬伤 → 最小修改 → 重渲重看，**每页最多 3 轮**。通过的页不再回看，进入下一页。

为什么坚持逐页：每页在落地的当下就被单独看过、确认无溢出/遮挡/报错，问题在最便宜的时候被发现和修掉；这也是这条技能要教给模型的核心节奏——增量构建 + 即时验证，而非一次写完赌它对。

### 阶段 5 —— 全局复审

`render --all` → 先看 **contact_sheet.png** 一图扫全局：风格有无漂移、相邻页有无雷同、页码连续性、背景全套统一；再核对 render.json：**console_errors 必须为空**；static_pages 逐页核对是否"静得有理由"（§5）。**再跑 a11y 校验**（用与 render 同一个 python）：`skills/dazzle-deck/scripts/check_a11y.py deck.html` —— **fontsize fail（文字 <14px 过小）必修**（放大或精简文案，别靠堆小字塞信息）；contrast manual（文字压图/渐变）逐条**人眼确认可读**，不够就压暗 scrim + 文字安全区；结果留 a11y.json。发现问题：跨页问题改 `:root` token 或骨架，单页问题 edit 该 section；只看有问题的页的大图。行有余力，此时可给关键页加页面级专属过渡/彩蛋增强。

### 阶段 6 —— 收尾

一段简短文字总结：页数、风格 family、最得意的 1–2 个 fancy 点、**如实报告遗留问题**（还有哪页有什么没修掉）。**谎报"全部通过"而实际有硬伤 = 整条产出作废。** 禁止"谢谢聆听/感谢观看"式收尾页套话。

## 3. 场景适配：fancy 的边界

**fancy 是执行质量与视觉冲击的拉满，不是风格的出格。** 给毕业答辩配赛博朋克霓虹，和给发布会配灰白模板，是同一种失败。阶段 0 先把场景庄重度判清楚：

| 档位 | 典型场景 | 风格候选域 | 动画密度 |
|---|---|---|---|
| **庄重档** | 学术答辩、政企汇报、医疗/金融/法务报告 | 深色商务、编辑杂志、瑞士网格、数据仪表盘等沉稳系 | **演示级**：克制深邃的背景微动效 + 精致入场编排 + 数据动画（count-up / draw-in），页面落定后静帧为常态；禁霓虹故障、粒子轰炸、强干扰特效 |
| **自由档** | 产品发布会、创意提案、科技分享、个人作品 | dazzle 簇放开：赛博、沉浸 3D、极繁、孟菲斯…… | **秀场级**：跨页过渡特效、粒子 / 3D / shader、戏剧化编排放手做 |
| **题材档** | 文旅、儿童教育、文化艺术、美食 | 跟着题材气质走（文化→东方美学、儿童→明快插画……） | 介于两档之间，动效形式贴合题材隐喻 |

- 庄重档的 fancy 发力点：排印张力（大字号对比、精确字距）、数据可视化做满（真实数据 + 动画入场）、深邃的背景层次（缓慢的渐变流动、低调的几何微动）、页间过渡干净利落。**克制 ≠ 平庸**——庄重档同样要有记忆点。
- plan.md 必须写明档位与理由；query 里的场景词（"答辩""给投资人路演""下周组会"）是最重要的判断依据。

## 4. deck 架构契约（骨架必须满足）

```
<body>
  <div id="bg-layer"></div>            ← 全局动态背景层（canvas/Three.js/CSS 动画）
  <div class="stage">                  ← 居中容器，position:fixed; inset:0; flex 居中
    <div class="deck" id="deck">       ← 固定 1280×720 设计画布，overflow:hidden
      <section class="slide active" data-slide="1">…</section>
      <section class="slide" data-slide="2">…</section>
      …
      <div class="hud">页码 + 进度条</div>
    </div>
  </div>
</body>
```

1. **design tokens**：所有颜色 / 字体在 `:root` 定义（`--bg/--ink/--accent/--accent-2/--font-display/--font-body`），全文只用 `var()` 引用，不写散落的裸 hex。
2. **固定画布 + 等比缩放**（根治溢出/裁切/比例失真）：`.deck{position:relative;width:1280px;height:720px;overflow:hidden}`，内容一律按 1280×720 用 px 布局与定字号；JS `fitDeck()` 按 `Math.min(innerWidth/1280, innerHeight/720)` 缩放 deck，监听 resize。**绝不混用单位**（最忌 deck 按 vh 定尺寸、字号按 vw 定——letterbox 屏下字体撑爆画布）。
3. **一次只显示一页**：`.slide{position:absolute;inset:0;opacity:0;visibility:hidden;pointer-events:none}`，`.slide.active` 三项全开。**非当前页必须彻底不可见**（只切 opacity 不切 visibility 会互相透叠）；任一时刻有且仅有一页 active；给单页加自定义样式时**绝不要改 position**（脱离堆叠会把该页压成 0 高、canvas 黑屏）。
4. **导航控制器**（骨架期写完整）：
   - 键盘 ArrowRight/Left、Space、PageDown/Up 翻页；`window.addEventListener('load', () => window.focus())` 抢焦点（iframe 内键盘可用的前提）。
   - 翻页时把 `.active` 切到目标页（渲染脚本靠 `.active` 类识别当前页，这是硬契约）、更新页码与进度条、对目标页调用 `window.slideInits[N]`（见第 6 条）、派发 `slidechange` 事件。
   - **切页重播入场动画（CSS）**：同一元素再 add 一次类**不会重启**已结束的动画。激活目标页时，对其入场元素（`.reveal` 等）做「移除入场动画类 → 强制重排 `void el.offsetWidth` → 重新加类」让 keyframes 重新触发（**别用 remove `.active` 来重启**——会闪一下不可见）；JS 动画按第 6 条每次重跑。来回切页时每页动画都应重新演一遍。
   - **必须支持 URL 参数直跳**：`new URLSearchParams(location.search).get('slide')` → 初始化时直接跳到第 N 页（1-based）。渲染脚本靠它做单页 O(1) 渲染，缺失会显著拖慢自检并在 stderr 告警。
5. **全局背景层**：`#bg-layer{position:fixed;inset:0;z-index:0;pointer-events:none}`，deck 容器 `z-index:1` 且背景透明或半透明遮罩（内容页遮罩偏深保可读，封面/结尾可放开露出完整背景）。背景动画**骨架期完整实现**；复审期只准调参（颜色/密度/速度），不要重写。事件监听挂 `window`（背景层不可交互），尺寸用 `window.innerWidth/Height` + resize 监听。
6. **per-slide JS 惰性注册**：页内脚本一律写成 `window.slideInits = window.slideInits || {}; slideInits[N] = () => {…}`，由导航控制器在该页激活时调用。**每次进页都重播动画**——把「建资源」与「播动画」分开：重资源（canvas / Three 场景 / 事件监听）只建一次（幂等），但**入场 / 数据动画每次激活都重跑**（count-up 归零重数、ECharts 重 `setOption`（带 animation）/ `dispatchAction`、GSAP `timeline.restart()`、CSS 入场见第 4 条重启法）——这样来回切页动画会重新播放、不是只看一次。**禁止顶层立即执行的代码去摸其他页（或尚未填充的空 section）的 DOM**——这是空骨架阶段渲染不报错、逐页填充互不踩踏的关键。
7. **入场动画契约**（动画与截图自检不再矛盾的官方解法）：每页入场编排总时长 **≤2s**，全部入场动画必须 `animation-fill-mode: forwards`（或 both），**最终态 = 完整内容**（渲染脚本在 ~2.6s 截图，截到的就是最终态）。持续型动画（背景粒子、呼吸光效、HUD 滚动）不受 2s 限制。禁止停在 boot/loading/打字机未完成态。
8. **通用过渡**归导航控制器、骨架期写好：class 驱动（`.slide.leaving` / `.slide.active` + transition/keyframes），默认给一套有戏剧性的编排（位移 + 透明度 + 模糊/裁切的组合，配 cubic-bezier 缓动）。页面级专属过渡（粒子重构、shader wipe 等）是复审期的可选增强。
9. **性能预算**：动画只动 transform / opacity / filter（不动 layout 属性）；canvas 粒子总数与 Three.js draw call 克制（见 §8 粒子参数表）；持续动画用 requestAnimationFrame 且页面不可见时无须暂停（渲染检查依赖动态可见）。

## 5. 渲染自检清单

每页 `render --page N` + `vision_analyze` 后对照：

**硬伤（必须修干净才算过）：**
1. 溢出 / 裁切——内容越过 1280×720 画布或容器边；巨字被裁半截；固定小盒文字撑破。
2. 遮挡压盖——装饰元素 / 大数字 / 浮层压住正文；负字距导致字形粘连（中文大标题尤其）。
3. 占位残留 / 空壳——空图表容器、KPI 全 0、只有表头没有数据行、坐标轴没曲线。
4. 破图破表——img / canvas / Three.js / ECharts 没真正渲出来（黑块、空白区）。
5. 中文豆腐块——字体没加载（中文必须 Google Fonts 引入并放进所有显示中文的 font-family 链）。
6. 对比不足——文字在其背景上看不清；深色风格同样要保证可读。
7. 配色出界——palette 之外的颜色乱入（ECharts 默认色、生成图跑色未调和）。
8. **console error ≠ 0**——看 stdout 的 `[console]` 行与 render.json；JS 报错常意味着后续动画/翻页全挂。
9. 入场动画未到最终态——截图里文字半透明 / 位移中途（说明动画 >2s 或缺 fill-mode: forwards）。

**[static] 是信号不是硬伤**：stdout 出现 `[static] page N` 表示该页在采样窗口内无可见动态。自问：这是**有意识的设计**（庄重档的数据密集页、节奏上的安静页），还是偷懒？静得有理由 → 忽略并继续；说不出理由 → 按本页的密度档位补恰当的动态（背景微动 / 数据动画），**不是无脑堆粒子**。

**软伤（有余量再修）**：对齐与网格、留白节奏、层级清晰度。

**deck 级自评（全局复审时）**：**招牌视觉野心**——有没有配得上题材的招牌视觉？还是退回了「通用粒子背景 + 编辑排版」的安全套路？题材本可沉浸 3D / shader / 多场景却选了最省事的粒子，**fancy 维度算不合格**，复审有余量就升级。整套至少有 **2–3 个高光记忆页**（戏剧性的封面、惊艳的数据页、出人意料的过渡……）；允许安静页存在制造节奏——**页页高潮 = 没有高潮**；风格 / 配色 / 字体全套一致不漂移。

**招牌兑现（硬纪律）**：plan 第 2b 条定的招牌视觉必须在 deck 里**真正实现**，**严禁降级充数**——定了「代码 3D 场景 / 沉浸」就必须真有 Three.js 3D 场景（几何体 + 光照 + 相机运动），用 2D 粒子假装沉浸不合格；要么补成真 3D，要么诚实把 plan 改成你真做出来的模式（别谎报）。

**修复纪律**：每页最多 3 轮 edit→render→看图，每轮只做能消除硬伤的最小修改；**禁止用"删掉动画 / 背景 / 特效"换取过检**——修复是修 bug 不是降级，确需移除某特效必须在收尾总结里说明理由；3 轮后仍有残留就保留最好一版并如实记录。

## 6. 配图与生图（image_generate 可用时）

**默认代码绘制。** 图表 / 数据可视化 / 抽象概念 / 几何装饰 / 图标 / UI 元素**严禁生图**——这些用 SVG / Canvas / CSS 画，这是本 skill 的看家本领。生图只用于**摄影刚需**：真实实物、地点建筑、自然风光、食物、文物艺术品、人物场景氛围。多数 deck 一张都不需要；需要时通常 1–4 张，在阶段 2 一次性生成。

- **prompt 只写主体，风格由契约统一加**：`image_generate` 自动把 `tokens.json` 的 `image_style_prefix` 拼到每条 prompt 末尾，**锁死全 deck 图片风格一致**——所以你只写 `"<主体描述>, <构图/视角>, <光线/情绪词>"`，**不用每张手写 palette/质感词**（前缀在 tokens.json 里集中定义、改一处全 deck 生效）。宽高比按用途选（全页背景 16:9，半幅配图 4:3 / 3:4）。
- **复杂示意可改生成图**：生物过程分期、机制示意等**用 SVG 画不好看时，改用 `image_generate` 出风格统一的插画**（契约前缀已保证各张一致），别硬用粗糙 SVG。
- **整图背景 + 文字叠加 → 生图时就为文字留位**（关键反 badcase）：要做整页背景、上面又压文字的图，**生成侧先规划留白**——prompt 指定主体偏一侧、另一侧留**大面积低细节 / 虚化 / 暗部空间**给文字（如 `主体在画面右侧，左侧大片低细节深色空间，浅景深` / `subject on the right third, generous clean negative space on the left, shallow depth of field`）。文字**只落在那片留白处，绝不压主体焦点**；scrim / 渐变蒙版也叠在文字这一侧（与下条的文字侧深蒙版一致）。render 后用 `vision_analyze` **专核一条**：**文字有没有盖住图的主体？** 盖住了就把文字挪到留白侧；挪不开就换构图重生图——别靠加深 scrim 硬糊主体。
- **生成后必核对**：`vision_analyze` 看内容是否对、色调是否贴 palette、有无文字/水印/多余人物；跑色就改 prompt 重生，救不回再用 CSS 压色罩（duotone / 半透明主色叠层）。
- **嵌入时二次调和**：`filter: saturate(.85) contrast(1.05)` 微调 + 用 `--bg` 的 rgba 做渐变蒙版（文字侧深、画面侧浅）+ 暗角，把图收进 deck 色域；文字区必须保证可读。
- **鼓励 fancy 化用法**：生成图不只是 `<img>` 平铺——clip-path 异形裁切、mask-image 做 reveal 入场（遵守 ≤2s + forwards）、mix-blend-mode 与背景层融合、作为 Three.js 纹理。让真实图片也成为视觉特效的一部分。

（若工具列表里没有 image_generate，跳过本节，一切视觉均代码绘制。）

## 7. 美学底线（反 AI slop）与绚哲学

每一份 deck 都必须有性格、有立场，绝不能是套路化的 AI 设计：

1. **字体**：禁止默认 Inter / Roboto / Arial / Open Sans / Lato / Space Grotesk / 系统字体；display + body 搭配、与主题契合；不要言必复古衬线、不要反复用 Fraunces / Cormorant / Playfair。font-family 末尾必须带通用兜底（sans-serif / serif / monospace）；数字 / 数据读数用确含数字字形的等宽字体（JetBrains Mono / IBM Plex Mono 等）。
2. **配色**：明确主色 + 尖锐重音、hex 精确、非平均分布；禁紫粉渐变白底 / 通用蓝绿渐变 / 无聊灰阶。
3. **反米黄**：除非 query 明确要求，禁止暖米黄 / 奶油 / 宣纸 / 象牙（#FBF7F0 #F5F1EA #ECE3D0 这类 R>G>B 暖白）做主背景，禁止做旧纸张 / 茶渍噪点 / 复古手稿基调。干净的纯深色 / 冷白 / 饱和品牌色都完全 OK。
4. **中文字体覆盖**：含中文必须 Google Fonts 引入中文字体（Noto Sans SC / Noto Serif SC / ZCOOL XiaoWei / Smiley Sans / LXGW WenKai 等按风格选）并放进所有显示中文的元素的 font-family 链；禁止只依赖 PingFang SC / Microsoft YaHei。整页语言自然统一，不做中英并行两套文本。
5. **形态贴合**：deck 是幻灯片不是网页应用——禁止浏览器式顶栏 / 侧边导航 / 贯穿全场的常驻控制台外壳（LIVE FEED / SESSION / VERSION / 脉冲指示灯这类 app 状态件）；元数据只放页脚或封面。
6. **内容真正画出来**：图表用真实数值直接画出图元；首屏即呈现真实内容；任何装饰不得用 z-index 压盖正文；hero / 封面严禁"巨幅纯色块只配一两个字"——要有清晰主标题 + 说明文案 + 视觉焦点。内容要有真实主体与真实数据（编合理的具体数字），不是占位词。
7. **布局完整性**：固定容器文字不溢出（overflow:hidden + 子 min-width:0，或 fit-content / 允许换行，预留 ≥15% 余量）；超大 display 标题设 max-width 允许换行，禁 white-space:nowrap 硬撑；`line-height<1` 的巨字按墨迹盒实算高度、与邻块留 ≥24px；满屏 flex 列用 justify-content 摊匀纵向余量，禁止单个 margin-top:auto 钉底塌出死白。
8. **覆盖层与光标**：canvas 拖尾淡出用 `clearRect` 或 `destination-out`，严禁低 alpha `fillRect` 整屏铺（累积成不透明遮罩盖住正文）；自定义光标 z-index 不高于正文且 `pointer-events:none`。
9. **不可遗忘点**：整套 deck 至少 2–3 处让人记住的细节——戏剧性开场、独特的数据动画、出人意料的过渡或彩蛋。

- **禁网格底纹背景**：方格纸 / 蓝图网格 / 透视网格地面 / 规则点阵 / Three.js `GridHelper` **一律不做背景**（被严重滥用的 AI 套路，哪怕低透明度单层也不行）；要纵深质感请用渐变光晕 / shader 噪声 / 粒子流场 / 径向暗角。（只禁「当背景平铺的视觉网格」；CSS Grid 布局、瑞士排版结构性分栏不受影响。）
- **不许走安全套路充数**：别不分题材地把「canvas 2D 粒子背景 + 编辑排版」当万能答案——题材本可承载更有野心的招牌（3D / shader / 多场景 / 极繁）时退回粒子就是偷懒。

**绚哲学——尽可能绚，但绚得其所**：在场景档位允许的密度内做满视觉表现力——持续微动的背景、视差 / 磁吸 / 扰动的鼠标响应、形变 / 光晕的 hover、backdrop-filter / mix-blend-mode / mask-image / clip-path、preserve-3d 空间深度、粒子与程序化纹理、staggered reveal、大字号排印张力（字距不得让字形粘连）。**敢上重型手法**：代码搭 Three.js 3D 场景、自定义 GLSL shader 背景、每页专属多场景沉浸、极繁拼贴——场景允许时这些是加分项不是禁区，「代码搭出可巡游的 3D 世界」（如紫禁城 3D 漫游级别）是值得追的标杆，别因更难调试就回避。绚 ≠ 杂乱：再密集也要层级清晰、配色有立场、贴合场景档位。

**图表也要动起来（ECharts / 数据可视化）**：能加动画的图表一律加上**贴合数据语义的入场动效**——柱状图柱子从 0 生长、折线图沿线 draw-in、数据点逐个浮现、环图 / 饼图扫描展开、散点渐显；关键数字配 count-up。**动画在 `slideInits[N]` 里随翻页触发**（进页才 setOption / dispatchAction，别一上来就把图静态画好）；遵守入场 ≤2s + forwards，别用无限循环抖动（防 anim-jank / 卡顿）。一张静态图表 = 浪费了动态 deck 的主场。

## 8. 工程避坑（常见 bug，写代码前过一遍）

1. **JS 函数引用顺序**：顶层立即执行的主控函数不要直接调用尚未注册的全局函数；对可能未定义的加 `if (typeof fn === 'function')` 防御——首行 ReferenceError 会杀掉后续全部脚本（翻页/动画全挂）。
2. **Three.js 锁 r128**：`https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js`（UMD 全局最稳）；OrbitControls 用 `three@0.128.0/examples/js/`，不要 r150+ 的路径（已废弃）。`ShaderMaterial({vertexColors:true})` 会自动注入 `attribute vec3 color`，vertexShader 里不要再声明一遍（redefinition → 粒子全黑）。
3. **Canvas / Three.js 尺寸与 DPI**：`renderer.setPixelRatio(devicePixelRatio)`；`setSize(w, h)` 不传第三参数；取尺寸用父元素 `getBoundingClientRect()`（canvas 自身初始可能为 0）并用 ResizeObserver 监听。
4. **粒子参数速查**（球面 starfield、相机 z≈400、AdditiveBlending）：偏暗 3000 粒 / sz 0.5–2.5 / 投影系数 300 / alpha 1.0 / 遮罩 0.85；适中 7000 / 1.2–4 / 550 / 1.3 / 0.78；偏亮 12000+ / 2–6 / 700 / 1.8 / 0.4。原则：封面要震撼（透明背景 + 多彩 + 高 alpha），内容页要背景化（半透明遮罩 + 冷色单调）。
5. **ECharts 配色**：所有系列色从 `:root` token 取，禁用 ECharts 默认色板；图表初始化放进 `slideInits[N]`，容器要有确定尺寸。
6. **键盘事件**：监听挂 `window`；`load` 时 `window.focus()` 抢焦点（deck 常被嵌 iframe 预览）。
7. **幂等 vs 重播**：`slideInits[N]` **只对「建资源」幂等**（canvas / Three 场景 / 监听只建一次，防泄漏卡顿）；**「播动画」不要幂等**——入场 / 数据动画每次激活都要重跑（见 §4 第 4、6 条），否则来回切页动画只播一次。
8. **代码搭 3D 场景**（选了沉浸 3D 招牌时）：参数化几何体（Box/Plane/Cone/Torus/Sphere 组合）拼场景 + Ambient/Directional/Point 三类光；**全 deck 共用一个常驻场景、每页一个相机机位**，翻页时相机沿弧线 lerp 巡游（注视点同步插值）。骨架期就把场景与相机搭起来跑通，逐页只调机位与该页 HTML。范例见 `fancy-cookbook.md`「代码搭建 3D 场景」+「单一 3D 世界相机航点运镜」。

## 9. references

- `references/style-families.md` —— 风格家族库：气质 / 适用与禁用场景 / palette 示例 / 字体方向 / 该风格下 fancy 怎么发力。阶段 0 必读。
- `references/fancy-cookbook.md` —— 技法配方：入场动画 / 跨页过渡 / 全局背景 / 交互彩蛋，含核心代码模式与参数安全范围。阶段 0 选型时读。

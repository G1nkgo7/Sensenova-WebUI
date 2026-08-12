// pptagent_static_web_demo — “舞台暗房”工作台:创作简报 → PowerPoint 式编辑器(胶片条 + 画布,实时显影)。
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const pad2 = (n) => String(n).padStart(2, "0");

const STATUS_LABEL = {
  waiting: "等待上一版", queued: "排队中", running: "生成中", completed: "已完成", failed: "失败", rejected: "未通过",
  error: "失败", stopped: "已中断", interrupted: "已中断", max_turns: "已达轮次上限", idle: "待继续",
  not_started: "未开始",
};
const isStaticActiveStatus = (status) => ["waiting", "queued", "running"].includes(status);
const isStaticGeneratingStatus = (status) => ["queued", "running"].includes(status);
const PHASE_LABEL = {
  starting: "启动中", planning: "规划大纲", researching: "检索资料", designing: "配图设计",
  delegating: "页面制作", rendering: "页面制作", verifying: "成稿校验", done: "完成", failed: "失败",
  stopped: "已中断", not_started: "未开始",
};
const TOOL_LABEL = {
  read: "研读资料", write: "撰写页面", edit: "修改页面", bash: "渲染画面",
  read_file: "研读资料", write_file: "撰写页面", edit_file: "修改页面",
  patch: "调整页面", terminal: "运行制作工具",
  vision_analyze: "检查画面", web_search: "搜索资料", web_fetch: "查阅网页", web_extract: "提取网页要点",
  image_generate: "绘制配图", fetch_image: "下载真实图片", delegate_task: "分派任务",
};
function actText(p) {
  const a = p && p.activity;
  if (!a) return "";
  const key = canonicalOrchestrationAgentKey(a.agent);
  const slide = key.match(/^slide_0?(\d+)/);
  let who = "页面制作";
  if (key === "orch") who = "整体策划";
  else if (key.startsWith("research")) who = "资料研究";
  else if (key.startsWith("material")) who = "素材整理";
  else if (key.startsWith("image")) who = "视觉素材";
  else if (key.startsWith("review")) who = "整稿校验";
  else if (key.startsWith("slide_group_")) who = "页面组制作";
  else if (slide) who = `第 ${Number(slide[1])} 页制作`;
  const action = {
    read: "整理资料", read_file: "整理资料", write: "设计页面", write_file: "设计页面",
    edit: "优化页面", edit_file: "优化页面", patch: "优化页面", terminal: "生成页面预览",
    bash: "生成页面预览", vision_analyze: "检查页面效果", image_generate: "生成视觉素材", fetch_image: "下载真实图片",
    web_search: "查找参考资料", web_fetch: "整理参考资料", web_extract: "提取网页要点", delegate_task: "安排页面制作",
  }[a.tool] || TOOL_LABEL[a.tool] || "推进制作";
  return `${who} · ${action}`;
}
// phase -> stepper key;STEP_ORDER 用于把之前的步标记为已完成
const PHASE_STEP = {
  starting: "plan", planning: "plan", researching: "research", designing: "research",
  delegating: "render", rendering: "render", verifying: "verify", done: "done", failed: null,
};
const STEP_ORDER = ["plan", "research", "render", "verify", "done"];

function runErrorText(value) {
  const message = String(value || "生成失败");
  if (message.includes("empty_giveup")) {
    return "模型连续返回空响应，自愈重试后仍未恢复。请重新发起任务（empty_giveup）。";
  }
  return message;
}

function ensureStudioNoticeStack() {
  let stack = $("#studio-notice-stack");
  if (stack) return stack;
  stack = document.createElement("div");
  stack.id = "studio-notice-stack";
  stack.className = "studio-notice-stack";
  stack.setAttribute("aria-live", "polite");
  stack.setAttribute("aria-atomic", "false");
  document.body.append(stack);
  return stack;
}

function dismissStudioNotice(notice) {
  if (!notice || notice.classList.contains("leaving")) return;
  clearTimeout(notice.__dismissTimer);
  notice.classList.add("leaving");
  setTimeout(() => notice.remove(), 220);
}

function showStudioNotice(message, { type = "error", duration = 7000 } = {}) {
  const text = String(message || "").trim();
  if (!text) return;
  const stack = ensureStudioNoticeStack();
  const duplicate = [...stack.children].find((item) => item.dataset.message === text && !item.classList.contains("leaving"));
  if (duplicate) {
    duplicate.classList.remove("notice-bump");
    requestAnimationFrame(() => duplicate.classList.add("notice-bump"));
    return duplicate;
  }
  const notice = document.createElement("div");
  notice.className = `studio-notice ${type}`;
  notice.dataset.message = text;
  notice.setAttribute("role", type === "error" ? "alert" : "status");
  notice.innerHTML = `<span class="studio-notice-mark" aria-hidden="true"></span><span class="studio-notice-copy">${escapeHtml(text)}</span><button type="button" aria-label="关闭通知">×</button>`;
  notice.querySelector("button").onclick = () => dismissStudioNotice(notice);
  stack.prepend(notice);
  requestAnimationFrame(() => notice.classList.add("shown"));
  if (duration > 0) notice.__dismissTimer = setTimeout(() => dismissStudioNotice(notice), duration);
  return notice;
}

function clearStudioNotices() {
  const stack = $("#studio-notice-stack");
  if (!stack) return;
  [...stack.children].forEach((notice) => dismissStudioNotice(notice));
}

function notifyRunFailure(value, fallback = "生成未能完成") {
  const message = runErrorText(value || fallback);
  const cancelled = /已取消|取消|已中断|中断|已停止|停止|cancel(?:led|ed)?/i.test(message);
  showStudioNotice(cancelled ? "生成已中断" : message, {
    type: cancelled ? "info" : "error",
    duration: cancelled ? 2800 : 9000,
  });
}

const ed = {                 // 编辑器状态
  id: null, kind: "static", sse: null, timer: null,
  total: 0, rendered: new Set(), sel: null, follow: true,
  status: null, startedElapsed: null, elapsedBase: 0, elapsedAt: 0,
  agentTimings: {}, overallTiming: null,
  fullQuery: "",
};
let isAuthenticated = document.querySelector(".layout")?.dataset.authenticated === "1";
const dynamicEnabled = document.querySelector(".layout")?.dataset.dynamicEnabled !== "0";
const trajectoryMode = document.querySelector(".layout")?.dataset.trajectoryMode === "1";
const trajectoryDeckApi = (id, suffix = "") => `/api/trajectory-monitor/decks/${encodeURIComponent(id)}${suffix}`;
let resumeGenerationAfterAuth = false;
let composerLaunchMorph = null;
let pageViewTransitionToken = 0;
let pageViewTransitionTarget = null;
const taskComposerDrafts = new Map();

function taskComposerKey(kind = ed.kind, id = ed.id) {
  return `${kind || "static"}:${id || "new"}`;
}

function beginComposerLaunch(query) {
  // Query 会在新工作区中直接成为右侧消息。这里只保存一次轻量转场状态，
  // 不再复制并放大输入框，避免长 Query 形成遮挡页面的悬浮层。
  composerLaunchMorph = { query: String(query || "").trim() };
  $("#composer")?.classList.add("is-sending-query");
  const send = $("#send");
  if (send) {
    send.dataset.idleLabel ||= send.textContent;
    send.textContent = creationMode === "dynamic" ? "正在启动动态演示…" : "正在启动静态演示…";
    send.setAttribute("aria-busy", "true");
  }
}

function clearComposerLaunchMorph({ restore = false } = {}) {
  composerLaunchMorph = null;
  $("#composer")?.classList.remove("is-sending-query");
  const send = $("#send");
  if (send) {
    send.textContent = send.dataset.idleLabel || uiText("generate");
    send.removeAttribute("aria-busy");
  }
}

function finishComposerLaunchMorph() {
  const query = $("#outline-user-query")?.closest(".outline-user-turn");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  clearComposerLaunchMorph();
  if (reduced || typeof query?.animate !== "function") return;
  query.animate(
    [{ opacity: 0, transform: "translate3d(18px,10px,0) scale(.985)" }, { opacity: 1, transform: "none" }],
    { duration: 460, delay: 80, easing: "cubic-bezier(.16,1,.3,1)", fill: "backwards" },
  );
}

async function transitionComposerToEditor() {
  const main = $("#main");
  const composer = $("#composer");
  const editor = $("#editor");
  if (!main || !composer || !editor) return;
  if (composer.hidden || !composerLaunchMorph || matchMedia("(prefers-reduced-motion: reduce)").matches
      || typeof composer.animate !== "function") {
    composer.hidden = true;
    editor.hidden = false;
    return;
  }

  main.classList.add("workspace-launch-transition");
  composer.classList.add("workspace-launch-source");
  editor.classList.add("workspace-launch-target");
  editor.hidden = false;

  const sourceAnimation = composer.animate([
    { opacity: 1, transform: "translate3d(0,0,0) scale(1)", filter: "blur(0)" },
    { opacity: .72, offset: .38 },
    { opacity: 0, transform: "translate3d(0,-10px,0) scale(.992)", filter: "blur(2px)" },
  ], { duration: 430, easing: "cubic-bezier(.4,0,.2,1)", fill: "both" });
  const targetAnimation = editor.animate([
    { opacity: 0, transform: "translate3d(0,10px,0) scale(.994)" },
    { opacity: 0, offset: .16 },
    { opacity: 1, transform: "translate3d(0,0,0) scale(1)" },
  ], { duration: 500, easing: "cubic-bezier(.16,1,.3,1)", fill: "both" });

  await Promise.allSettled([sourceAnimation.finished, targetAnimation.finished]);
  composer.hidden = true;
  sourceAnimation.cancel();
  targetAnimation.cancel();
  composer.classList.remove("workspace-launch-source");
  editor.classList.remove("workspace-launch-target");
  main.classList.remove("workspace-launch-transition");
}

function refreshDeckListWhenIdle(active) {
  const refresh = () => loadDecks(active);
  if (typeof requestIdleCallback === "function") requestIdleCallback(refresh, { timeout: 900 });
  else setTimeout(refresh, 80);
}

const SUGGESTION_POOL = [
  {
    label: "城市户外品牌 · 中庭快闪提案",
    query: `下周我们要去跟商场的招商总监谈一个核心中庭的快闪店位置。我们是一个全新的城市户外品牌，要让商场相信我们能吸引最高净值的年轻客流。请帮我做一份20页左右的入驻提案PPT。
第一部分不讲常规的品牌故事，直接放满墙的穿搭图，讲山系机能风怎么成了现在一线城市新中产的标配。
第二部分讲我们的空间策展概念。不要普通货架，我们要在商场中庭搭一个带溪流的室内原始森林，并把空间效果图放上去。
第三部分讲客群和社群运营。我们不仅卖冲锋衣，周末还会组织飞盘、露营和溯溪，重点强调超高复购率和高客单价。最后给商场展示商业回报，测算一个月快闪能为商场带来的全网曝光量、小红书打卡数和预估GMV。
排版采用最前沿的山系机能潮流风，抛弃传统PPT的规矩感。底色用岩石灰或水泥灰，搭配极具视觉冲击力的荧光橙或荧光绿作为点缀。图片要混搭：一半是粗犷的高清雪山、森林摄影，一半是极具工业感的反光面料微距图。字体使用极其粗犷的黑体，字号要特别大，像街头潮流海报的大字报排版。整体要有强烈的野性质感。`,
  },
  {
    label: "山系度假民宿 · 招商与 OTA",
    query: `我新开了一家山系度假民宿，需要一份25页左右的产品介绍资料PPT，用于招商洽谈和OTA平台上架资料参考。
内容上包括民宿招商该有的板块：选址与环境卖点、房型配置与定价策略、配套设施与体验活动、目标客群画像、淡旺季运营思路、合规资质与安全保障，其他你判断该补的也一并补齐。
配色定一个主色搭配一个辅助色，字体走山系度假那种松弛、自然、有呼吸感的调性。整体要精致、有设计感，排版要留得开、透得出山野气息，可以适度用大图叙事、房型信息卡片、简洁的数据图或时间线来讲清楚运营节奏。`,
  },
  {
    label: "《沙丘》· 电影美学拆解",
    query: `下周我要给影迷社群做一次《沙丘》电影美学与世界观设定的视觉拆解分享，25到35页之间，页数你根据内容节奏定。
内容层面希望做扎实，别停留在剧情复述。围绕几个维度展开：厄拉科斯星球的地理与生态设定、香料经济与政治权力结构、弗雷曼人的文化与宗教符号、各大家族的视觉识别体系（服装、纹章、建筑语言）、导演维伦纽瓦和摄影师 Greig Fraser 的镜头语言与构图逻辑、汉斯·季默配乐与声音设计的美学取向、以及影片对粗野主义建筑和中东—北非文化的借鉴。每一块都要给出可以拿来跟朋友聊的具体观察和例证，涉及的设定、幕后信息、参考来源请核实准确。
视觉做成一本顶级好莱坞概念艺术图册的质感，追求巨物压迫感和粗野主义的沉重体量。整体色板用废土黄、香料橘、沙岩灰。大量使用宽画幅满版剧照或概念图，让画面自己说话。字体选硬朗、字重偏轻的现代无衬线，营造干燥、灼热的空气感。页面节奏要有电影分镜的呼吸——极空的留白页、纯黑过场页和铺满巨物的震撼页交替出现。`,
  },
  {
    label: "乡村振兴 · 产业路线汇报",
    query: `我们驻村工作队下周要给镇里做乡村振兴项目进展汇报，报告体量控制在25页左右。整体叙事要先压住情绪再把希望托起来：前面讲村子现状和困境要平实记录，用真实数据慢慢加压；第15页左右的转折页必须做出极强的视觉冲击，色调瞬间从深沉切到暖橘色带来希望感；结尾落点要靠事实升华。
中间核心是四条产业路线（果蔬、民宿、电商、加工）的深度横评。请按“投入、带动户数、周期、风险、可持续”五个统一维度，设计一页极具记忆点的 4×5 打分矩阵（使用高对比度热力色块或多边形雷达图组合，严禁默认表格），并据此明确给出要押注哪条路线的量化理由。请自行调研核实真实的行业参考数据。
视觉美术走“大地纪实与破晓旭日”的杂志风。前半段使用深炭黑、泥土褐与粗糙夯土肌理，转折后引入大面积破晓暖光与自然图像；封面、转场及核心页对标《国家地理》的排版质感，用大画幅真实乡村影像结合不规则网格，拒绝同一版式反复套用。`,
  },
  {
    label: "山区山洪 · 应急预案",
    query: `下周要去给文旅局汇报一份山区农家乐的山洪应急预案，帮我出一份22页左右的对比分析方案。核心是详细拆解“沿河预警广播”与“手机短信推送”两种预警方式的实战优劣。
第一部分调研并引用真实可查的山洪气象灾害数据；第二部分对比两种方案的响应延迟、信号死角覆盖率和断网断电极端天气下的可靠性；第三部分给出一套针对农家乐店主和游客的极简疏散 SOP。结论必须是可直接落地的实操建议。
视觉采用“生态地貌与数字预警”融合风：森林苔藓绿、岩石灰配应急霓虹橙，贯穿地形等高线与气象雷达热力图，用半透明毛玻璃科技 UI 承载信息。核心页设计成上帝视角的 3D 微缩山水地貌模型；两种预警的对比使用多维雷达图和强反差左右分屏，避免默认表格和柱状图。`,
  },
  {
    label: "开源软件 · 像素 RPG 科普",
    query: `马上要在高校社团给大学生做一场关于开源软件的科普讲座，制作一份25页左右的演示文稿，内容必须实用且接地气。
第一部分讲透什么是开源，并核查、引用学生每天都在使用却不知道是开源的真实软件案例；第二部分重点拆解 MIT、Apache、GPL 等常见许可证的核心区别，结合真实的开源维权或商业冲突案例，给出新手避坑指南，说明哪些能自由使用、哪些使用后必须公开自己的代码。
视觉使用“8-bit 复古像素 RPG 游戏”风格，设置一个像素极客 NPC 作为向导，背景采用街机深渊黑、CRT 扫描线和粗颗粒像素网格，配色用高亮青、电光黄和品红。封面与章节页做成游戏启动界面和关卡地图；许可证区别做成 RPG 技能树或阵营九宫格，信息框统一为复古游戏对话框。`,
  },
  {
    label: "农业卫星 · 太空老农历",
    query: `我要给省农业厅和农业合作社农户代表做一份15页的农业监测低轨卫星星座组网发射科普与宣介PPT。为了让农户和基层工作者感受到技术价值，整体采用温馨质朴的现代手绘绘本风，把高精尖航天科技化作亲切的“天上新农具”，主色为阳光暖黄、沃野泥土棕和嫩芽初绿。
内容按太空老农历“春耕、夏长、秋收、冬藏”推进：星座组网总览；卫星载荷与光谱成像原理；火箭发射与入轨步骤；终端数据接收；经济效益与助农政策落地。用太空稻草人、农作物体检报告、太空医生听诊器和智能老农历手机界面等比喻讲清复杂技术。
排版大量使用手绘麦穗、小拖拉机和笑脸云朵点缀，进度条做成种子发芽到结出硕果的插画演变，让硬核航天项目充满接地气的人情味。`,
  },
  {
    label: "家庭收纳 · 小白实用课程",
    query: `想做一套家庭收纳整理的实用课程，面向完全没经验的小白，控制在24页左右。重点讲衣柜、厨房和玄关三个常见场景，用真实家庭案例展示整理前后对比和具体操作步骤。整体用轻松明快的卡通插画风，排版干净有设计感，让人看完就能照着动手整理。`,
  },
  {
    label: "《霸王龙的晚餐》· 睡前科普",
    query: `我想制作一份20页左右、面向4—8岁小朋友的睡前交互式科普课件PPT，主题叫《霸王龙的晚餐》。不要任何血腥撕咬或吓人的画面，要温馨幽默、充满童趣：以一只肚子咕咕叫但很友善的小霸王龙寻找晚饭为故事主线，带领小朋友认识不同恐龙的身体特征与生活环境。
内容包括小霸王龙登场与恐龙世界地图、猜脚印和小短手互动；逐页探索梁龙、甲龙、三角龙与副栉龙；展示白垩纪密林、火山湖畔与蕨类植物森林；结尾反转为恐龙伙伴们举办树叶与野果晚宴，并自然引导小朋友准备睡觉。每页配大字号恐龙名片与极简互动知识。
视觉采用温馨治愈的暖心绘本插画风，以燕麦奶黄和天空蓝为底，搭配暖阳黄、薄荷绿、珊瑚粉与小橘红。每页都有圆滚滚大眼睛的拟人化恐龙，字体使用超大字号软萌字体，版式采用翻翻书、卡通对话框与手绘点缀，让整套课件像一本温馨睡前童话。`,
  },
];

const NOVA_DOT_FONT = {
  A: ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
  E: ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
  N: ["10001", "11001", "11001", "10101", "10011", "10011", "10001"],
  O: ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
  P: ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
  R: ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
  S: ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
  T: ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
  V: ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
};

function initNovaDoodle() {
  const host = $("#nova-doodle");
  const canvas = $("#nova-doodle-canvas");
  const context = canvas?.getContext("2d");
  if (!host || !canvas || !context) return;

  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const dots = [];
  const brandDots = [];
  const brandLetters = "SENSENOVA";
  const letters = "PRESENT";
  const centers = [53, 102, 151, 200, 249, 298, 347];
  const brandCenters = [38, 78.5, 119, 159.5, 200, 240.5, 281, 321.5, 362];
  const accentMap = [
    { row: 2, col: 1, color: "blue" }, null,
    { row: 6, col: 3, color: "purple" }, null,
    { row: 5, col: 4, color: "mint" }, null,
    { row: 3, col: 4, color: "coral" },
  ];
  let visible = true;
  let frame = 0;
  let startedAt = performance.now();
  let pointerX = .5;
  let pointerY = .5;

  function abstractStrength(tile, row, col, letterOn) {
    const dx = col - 3;
    const dy = row - 4;
    const radius = Math.hypot(dx * 1.08, dy);
    const orbit = Math.max(0, 1 - Math.abs(radius - (2.2 + (tile % 3) * .24)) / .68);
    const wave = Math.max(0, 1 - Math.abs(dx - Math.sin((dy + tile) * .75) * .64) / .78);
    // The word never disappears: only its surrounding field reorganizes.
    return Math.max(letterOn ? .78 : .11, (tile % 2 ? orbit : wave) * (letterOn ? .42 : .58));
  }

  centers.forEach((centerX, tile) => {
    const glyph = NOVA_DOT_FONT[letters[tile]];
    for (let row = 0; row < 9; row += 1) {
      for (let col = 0; col < 7; col += 1) {
        const dx = col - 3;
        const dy = row - 4;
        // Seven compact rounded fields show PRESENT together in one frame.
        if (Math.hypot(dx * 1.14, dy) > 4.72) continue;
        const glyphRow = row - 1;
        const glyphCol = col - 1;
        const letterOn = glyphRow >= 0 && glyphRow < 7 && glyphCol >= 0 && glyphCol < 5
          && glyph[glyphRow][glyphCol] === "1";
        const accent = accentMap[tile];
        dots.push({
          tile, row, col,
          x: centerX + dx * 5.35,
          y: 78 + dy * 6.15,
          letterStrength: letterOn ? 1 : .10 + ((row * 13 + col * 7 + tile * 5) % 5) * .045,
          abstractStrength: abstractStrength(tile, row, col, letterOn),
          accent: accent && row === accent.row && col === accent.col ? accent.color : "",
          seed: (tile * 97 + row * 19 + col * 31) % 113,
          delay: tile * 62 + Math.hypot(dx, dy) * 28 + ((row + col) % 3) * 20,
        });
      }
    }
  });

  brandCenters.forEach((centerX, tile) => {
    const glyph = NOVA_DOT_FONT[brandLetters[tile]];
    for (let row = 0; row < 9; row += 1) {
      for (let col = 0; col < 7; col += 1) {
        const dx = col - 3;
        const dy = row - 4;
        if (Math.hypot(dx * 1.14, dy) > 4.72) continue;
        const glyphRow = row - 1;
        const glyphCol = col - 1;
        const letterOn = glyphRow >= 0 && glyphRow < 7 && glyphCol >= 0 && glyphCol < 5
          && glyph[glyphRow][glyphCol] === "1";
        brandDots.push({
          tile, row, col,
          x: centerX + dx * 4.35,
          y: 78 + dy * 5.55,
          letterStrength: letterOn ? 1 : .09 + ((row * 7 + col * 11 + tile * 3) % 5) * .04,
          abstractStrength: abstractStrength(tile, row, col, letterOn),
          seed: (tile * 83 + row * 23 + col * 37) % 127,
          delay: tile * 48 + Math.hypot(dx, dy) * 25 + ((row + col) % 3) * 17,
        });
      }
    }
  });

  function palette() {
    return document.documentElement.dataset.theme === "dark"
      ? {
        base: [61, 62, 67], mid: [123, 124, 132], bright: [229, 229, 234],
        blue: [130, 205, 255], purple: [229, 125, 224], mint: [128, 223, 135], coral: [255, 141, 147],
        yellow: [242, 211, 88], lime: [116, 229, 126],
        brandPurple: [179, 145, 255], brandGreen: [29, 226, 171],
      }
      : {
        base: [211, 209, 217], mid: [134, 130, 143], bright: [64, 60, 72],
        blue: [53, 154, 226], purple: [184, 53, 173], mint: [35, 167, 92], coral: [225, 77, 88],
        yellow: [196, 153, 25], lime: [31, 166, 87],
        brandPurple: [100, 55, 220], brandGreen: [0, 184, 132],
      };
  }

  function colorMix(a, b, amount, alpha = 1) {
    const mix = a.map((value, index) => Math.round(value + (b[index] - value) * amount));
    return `rgba(${mix[0]},${mix[1]},${mix[2]},${alpha})`;
  }

  function brandPointColor(colors, position, alpha = 1) {
    // Keep SENSE unmistakably purple and let NOVA resolve into mint through a
    // continuous blend instead of a hard color seam between two letters.
    const blend = easeState(Math.max(0, Math.min(1, (position - .30) / .58)));
    return colorMix(colors.brandPurple, colors.brandGreen, blend, alpha);
  }

  function easeState(value) {
    const t = Math.max(0, Math.min(1, value));
    return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function abstractAmount(elapsed) {
    if (motion.matches) return 0;
    const cycle = elapsed % 10400;
    if (cycle < 3300) return 0;
    if (cycle < 4500) return easeState((cycle - 3300) / 1200);
    if (cycle < 6500) return 1;
    if (cycle < 7700) return 1 - easeState((cycle - 6500) / 1200);
    return 0;
  }

  function brandState(elapsed) {
    if (motion.matches) return { brand: 0, active: false, direction: 0, progress: 0 };
    const cycle = elapsed % 11200;
    if (cycle < 3900) return { brand: 0, active: false, direction: 0, progress: 0 };
    if (cycle < 5200) {
      const progress = (cycle - 3900) / 1300;
      return { brand: easeState(progress), active: true, direction: 1, progress };
    }
    if (cycle < 7600) return { brand: 1, active: false, direction: 0, progress: 0 };
    if (cycle < 8900) {
      const progress = (cycle - 7600) / 1300;
      return { brand: 1 - easeState(progress), active: true, direction: -1, progress };
    }
    return { brand: 0, active: false, direction: 0, progress: 0 };
  }

  function brandAmount(elapsed) {
    return brandState(elapsed).brand;
  }

  function dotSwitchState(dot, transition, isBrand) {
    if (!transition.active) {
      return {
        alpha: isBrand ? transition.brand : 1 - transition.brand,
        scatter: 0,
        burst: 0,
        entering: false,
      };
    }
    const entering = transition.direction > 0 ? isBrand : !isBrand;
    const jitter = ((dot.seed * 37 + dot.tile * 17 + dot.row * 11 + dot.col * 7) % 101) / 100;
    const local = entering
      ? Math.max(0, Math.min(1, (transition.progress - .12 - jitter * .22) / .66))
      : Math.max(0, Math.min(1, (transition.progress - jitter * .20) / .68));
    const eased = easeState(local);
    return {
      alpha: entering ? eased : 1 - eased,
      scatter: entering ? 1 - eased : eased,
      burst: Math.sin(Math.PI * local),
      entering,
    };
  }

  function scatterVector(dot, state, direction) {
    const angle = dot.seed * .31 + dot.tile * 1.47 + dot.row * .22 - dot.col * .19;
    const spin = state.burst * (state.entering ? -1 : 1) * direction * 1.08;
    const distance = (16 + (dot.seed % 19) * 1.65) * state.scatter;
    return {
      x: Math.cos(angle + spin) * distance + Math.sin(dot.row + dot.seed) * state.burst * 5.5,
      y: Math.sin(angle + spin) * distance * .64 + Math.cos(dot.col * .8 + dot.seed) * state.burst * 4.5,
    };
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(devicePixelRatio || 1, 2.5);
    const width = Math.max(1, Math.round(rect.width * dpr));
    const height = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    draw(performance.now());
  }

  function draw(now) {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const sx = rect.width / 400;
    const sy = rect.height / 154;
    const dpr = canvas.width / rect.width;
    const colors = palette();
    const elapsed = now - startedAt;
    const abstract = abstractAmount(elapsed);
    const transition = brandState(elapsed);
    const transitionEnergy = transition.active ? Math.sin(Math.PI * transition.progress) : 0;
    const sweep = ((elapsed / 4300) % 1) * 380 + 10;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, rect.width, rect.height);

    context.save();
    for (const dot of dots) {
      const switchState = dotSwitchState(dot, transition, false);
      if (switchState.alpha <= .002) continue;
      context.globalAlpha = switchState.alpha;
      const reveal = motion.matches ? 1 : Math.max(0, Math.min(1, (elapsed - dot.delay) / 460));
      if (!reveal) continue;
      const revealEase = 1 - Math.pow(1 - reveal, 3);
      const localDelay = motion.matches ? 0 : Math.max(0, Math.min(1, abstract * 1.48 - dot.tile * .028 - dot.seed * .0008));
      const state = easeState(localDelay);
      const strength = dot.letterStrength + (dot.abstractStrength - dot.letterStrength) * state;
      const shimmer = motion.matches ? 0 : Math.max(0, 1 - Math.abs(dot.x - sweep) / 25) * .26;
      const pointerDistance = Math.hypot(dot.x / 400 - pointerX, dot.y / 154 - pointerY);
      const pointerPower = motion.matches ? 0 : Math.max(0, 1 - pointerDistance / .16);
      const breath = motion.matches ? 0 : Math.sin(elapsed * .0019 + dot.seed * .19 + dot.tile) * .06;
      const orbit = transitionEnergy * Math.sin(dot.seed * .33 + dot.tile) * 1.35;
      const scatter = scatterVector(dot, switchState, transition.direction);
      const hoverOffset = pointerPower * 1.55;
      const px = (dot.x + scatter.x + Math.cos(dot.seed) * orbit + (dot.x / 400 - pointerX) * hoverOffset) * sx;
      const py = (dot.y + scatter.y + Math.sin(dot.seed) * orbit + (dot.y / 154 - pointerY) * hoverOffset) * sy;
      const intensity = Math.max(0, Math.min(1, strength + shimmer + pointerPower * .24 + breath));
      const radius = (2.0 + intensity * .56 + breath * .28) * Math.min(sx, sy) * revealEase;
      const accentColor = dot.accent ? colors[dot.accent] : null;
      const fill = accentColor
        ? colorMix(colors.mid, accentColor, .82, .93)
        : colorMix(colors.base, colors.bright, intensity, .84 + intensity * .15);

      if (dot.accent || intensity > .86) {
        context.beginPath();
        context.arc(px, py, radius * (dot.accent ? 2.25 : 1.75), 0, Math.PI * 2);
        context.fillStyle = dot.accent
          ? colorMix(accentColor, accentColor, 1, .12)
          : colorMix(colors.bright, colors.bright, 1, .035);
        context.fill();
      }
      context.beginPath();
      context.arc(px, py, Math.max(.55, radius), 0, Math.PI * 2);
      context.fillStyle = fill;
      context.fill();
    }
    context.restore();

    context.save();
    for (const dot of brandDots) {
      const switchState = dotSwitchState(dot, transition, true);
      if (switchState.alpha <= .002) continue;
      context.globalAlpha = switchState.alpha;
      const reveal = motion.matches ? 1 : Math.max(0, Math.min(1, (elapsed - dot.delay) / 420));
      if (!reveal) continue;
      const revealEase = 1 - Math.pow(1 - reveal, 3);
      const localDelay = motion.matches ? 0 : Math.max(0, Math.min(1, abstract * 1.45 - dot.tile * .022 - dot.seed * .0007));
      const state = easeState(localDelay);
      const strength = dot.letterStrength + (dot.abstractStrength - dot.letterStrength) * state;
      const shimmer = motion.matches ? 0 : Math.max(0, 1 - Math.abs(dot.x - sweep) / 22) * .30;
      const pointerDistance = Math.hypot(dot.x / 400 - pointerX, dot.y / 154 - pointerY);
      const pointerPower = motion.matches ? 0 : Math.max(0, 1 - pointerDistance / .15);
      const breath = motion.matches ? 0 : Math.sin(elapsed * .0021 + dot.seed * .18 + dot.tile) * .055;
      const orbit = transitionEnergy * Math.sin(dot.seed * .27 + dot.tile) * 1.15;
      const scatter = scatterVector(dot, switchState, transition.direction);
      const px = (dot.x + scatter.x + Math.cos(dot.seed) * orbit) * sx;
      const py = (dot.y + scatter.y + Math.sin(dot.seed) * orbit) * sy;
      const intensity = Math.max(0, Math.min(1, strength + shimmer + pointerPower * .22 + breath));
      const isLetterDot = dot.letterStrength > .8;
      const radius = ((isLetterDot ? 2.08 : 1.56) + intensity * (isLetterDot ? .62 : .42) + breath * .24)
        * Math.min(sx, sy) * revealEase;
      const gradient = Math.max(0, Math.min(1, (dot.x - 24) / 352));
      const letterColor = brandPointColor(colors, gradient, .96);
      const fill = isLetterDot
        ? letterColor
        : colorMix(colors.base, colors.mid, intensity * .72, .82 + intensity * .12);
      if (isLetterDot && shimmer > .30) {
        context.beginPath();
        context.arc(px, py, radius * 2.05, 0, Math.PI * 2);
        context.fillStyle = brandPointColor(colors, gradient, .085 * shimmer);
        context.fill();
      }
      context.beginPath();
      context.arc(px, py, Math.max(.5, radius), 0, Math.PI * 2);
      context.fillStyle = fill;
      context.fill();
    }
    context.restore();
    host.classList.add("doodle-ready");
  }

  function animate(now) {
    draw(now);
    frame = visible && !motion.matches && !document.hidden ? requestAnimationFrame(animate) : 0;
  }

  function resume() {
    if (visible && !motion.matches && !document.hidden && !frame) frame = requestAnimationFrame(animate);
    else draw(performance.now());
  }

  const observer = new IntersectionObserver((entries) => {
    visible = entries[0]?.isIntersecting !== false;
    if (!visible && frame) { cancelAnimationFrame(frame); frame = 0; }
    resume();
  }, { rootMargin: "80px" });
  observer.observe(host);
  new ResizeObserver(resize).observe(canvas);
  document.addEventListener("visibilitychange", resume);
  motion.addEventListener?.("change", resume);
  new MutationObserver(() => draw(performance.now())).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  canvas.addEventListener("pointermove", (event) => {
    const rect = canvas.getBoundingClientRect();
    pointerX = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    pointerY = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
  });
  canvas.addEventListener("pointerleave", () => { pointerX = .5; pointerY = .5; });
  resize();
  resume();
}

// app.js is deferred, so the DOM is ready here. Draw synchronously to avoid a
// one-frame flash of the legacy fallback logo on first load or hard refresh.
initNovaDoodle();

function renderSuggestions() {
  const list = $("#suggestion-list");
  if (!list) return;
  const picks = [...SUGGESTION_POOL].sort(() => Math.random() - 0.5).slice(0, 4);
  list.innerHTML = picks.map((item, index) =>
    `<button type="button" class="suggestion-chip" style="--chip-index:${index}" data-query="${escapeHtml(item.query)}">${escapeHtml(item.label)}</button>`
  ).join("");
}

const STYLE_LABEL = {
  "": "智慧匹配", "商务正式": "商务正式", "学术严谨": "学术严谨",
  "科技感": "科技感", "活泼明快": "彩色创意", "极简": "极简留白",
};

function syncStylePicker(value = "") {
  const select = $("#style-sel");
  const safeValue = [...select.options].some((option) => option.value === value) ? value : "";
  select.value = safeValue;
  $("#style-current").textContent = STYLE_LABEL[safeValue] || safeValue || "智慧匹配";
  $$(".style-card").forEach((card) => {
    const selected = card.dataset.style === safeValue;
    card.classList.toggle("selected", selected);
    card.setAttribute("aria-selected", selected ? "true" : "false");
  });
}

function setStyleGallery(open) {
  const gallery = $("#style-gallery");
  gallery.hidden = !open;
  $("#style-trigger").classList.toggle("active", open);
  $("#style-trigger").setAttribute("aria-expanded", open ? "true" : "false");
}

const LENGTH_LABEL = { "0": "智慧推荐", "4": "精简", "8": "适中", "14": "详细" };

function syncLengthPicker(value = "0") {
  const select = $("#length-sel");
  const safeValue = [...select.options].some((option) => option.value === String(value)) ? String(value) : "0";
  select.value = safeValue;
  $("#length-current").textContent = LENGTH_LABEL[safeValue] || "智慧推荐";
  $$("#length-menu [data-length]").forEach((item) => {
    const selected = item.dataset.length === safeValue;
    item.classList.toggle("selected", selected);
    item.setAttribute("aria-pressed", selected ? "true" : "false");
  });
}

function setLengthMenu(open) {
  const menu = $("#length-menu");
  const trigger = $("#length-trigger");
  menu.hidden = !open;
  trigger.classList.toggle("active", open);
  trigger.setAttribute("aria-expanded", open ? "true" : "false");
}

function syncModelPicker() {
  const select = $("#model-sel");
  const option = select?.selectedOptions?.[0];
  if (!select) return;
  $("#model-current").textContent = option?.textContent.trim() || "配置模型";
  $("#model-trigger")?.classList.toggle("model-empty", !option);
  $$("#model-menu [data-model]").forEach((item) => {
    const selected = item.dataset.model === select.value;
    item.classList.toggle("selected", selected);
    item.setAttribute("aria-pressed", selected ? "true" : "false");
  });
  syncThinkingControl();
}

function renderModelMenu() {
  const menu = $("#model-menu");
  const select = $("#model-sel");
  if (!menu || !select) return;
  const options = [...select.options];
  menu.innerHTML = `<div class="model-menu-title">模型</div>${options.length ? options.map((option) => {
    const custom = option.dataset.custom === "1";
    return `<div class="model-menu-row"${custom ? ` data-custom-model="1"` : ""}>
      <button type="button" class="model-menu-option" data-model="${escapeHtml(option.value)}"${option.disabled ? " disabled" : ""}>
        <span>${custom ? "✦" : "◇"}</span><strong>${escapeHtml(option.textContent.trim())}</strong><small>${custom ? "自定义" : "部署配置"}</small><i>✓</i>
      </button>
      ${custom ? `<button type="button" class="model-menu-delete" data-delete-model="${escapeHtml(option.value)}" aria-label="删除 ${escapeHtml(option.textContent.trim())}" title="删除模型">×</button>` : ""}
    </div>`;
  }).join("") : `<div class="model-menu-empty">尚未配置模型</div>`}
    <div class="model-menu-separator"></div>
    <button type="button" class="model-menu-manage" data-configure-model>
      <span>＋</span><strong>${options.length ? "管理模型" : "添加模型"}</strong><i>›</i>
    </button>`;
  syncModelPicker();
}

function setModelMenu(open) {
  const menu = $("#model-menu");
  const trigger = $("#model-trigger");
  if (!menu || !trigger) return;
  if (open) renderModelMenu();
  menu.hidden = !open;
  trigger.classList.toggle("active", open);
  trigger.setAttribute("aria-expanded", open ? "true" : "false");
}

function syncSkillPicker() {
  const select = $("#skill-sel");
  const current = $("#skill-current");
  const control = $("#skill-control");
  const trigger = $("#skill-trigger");
  if (!select || !current || !control || !trigger) return;
  const dynamic = creationMode === "dynamic";
  control.classList.remove("fixed");
  trigger.setAttribute("aria-disabled", "false");
  current.textContent = dynamic ? "SenseNova Dynamic HTML" : (select.selectedOptions[0]?.textContent.trim() || "Auto（自动选择）");
  $$("#skill-menu [data-skill]").forEach((item) => {
    const selected = dynamic ? item.dataset.skill === dynamicSkill : item.dataset.skill === select.value;
    item.classList.toggle("selected", selected);
    item.setAttribute("aria-pressed", selected ? "true" : "false");
  });
}

function renderSkillMenu() {
  const menu = $("#skill-menu");
  const select = $("#skill-sel");
  if (!menu || !select) return;
  const options = creationMode === "dynamic"
    ? [{ value: "sense-present-dazzle", label: "SenseNova Dynamic HTML", disabled: false, title: "sn-ppt-dazzle + 专属动态 Harness" }]
    : [...select.options]
        .filter((option) => option.value !== "long-horizon")
        .map((option) => ({
          value: option.value, label: option.textContent.trim(), disabled: option.disabled, title: option.title || "",
        }));
  menu.innerHTML = `<div class="skill-menu-title">Skill</div>${options.map((option) => `
    <button type="button" data-skill="${escapeHtml(option.value)}"${option.disabled ? " disabled" : ""} title="${escapeHtml(option.title)}">
      <span>✦</span><strong>${escapeHtml(option.label)}</strong><i>✓</i>
    </button>`).join("")}`;
  syncSkillPicker();
}

function setSkillMenu(open) {
  const menu = $("#skill-menu");
  const trigger = $("#skill-trigger");
  if (!menu || !trigger) return;
  const next = !!open;
  if (next) renderSkillMenu();
  menu.hidden = !next;
  trigger.classList.toggle("active", next);
  trigger.setAttribute("aria-expanded", next ? "true" : "false");
}

function setUserMenu(open) {
  const menu = $("#user-menu");
  const trigger = $("#user-menu-trigger");
  if (!menu || !trigger) return;
  if (!open) setFontPanel(false);
  menu.hidden = !open;
  trigger.classList.toggle("active", open);
  trigger.setAttribute("aria-expanded", open ? "true" : "false");
}

let authTabTransitionToken = 0;

function setAuthTab(tab = "login", { animate = true, focus = true } = {}) {
  const next = tab === "register" ? "register" : "login";
  const tabs = $(".auth-tabs");
  const stage = $("#auth-form-stage");
  const loginForm = $("#auth-login-form");
  const registerForm = $("#auth-register-form");
  const current = stage?.dataset.active === "register" ? "register" : "login";
  const outgoing = current === "login" ? loginForm : registerForm;
  const incoming = next === "login" ? loginForm : registerForm;
  const focusInput = () => {
    if (!focus) return;
    requestAnimationFrame(() => $(next === "login" ? "#auth-login-username" : '#auth-register-form input[name="username"]')?.focus());
  };

  if (tabs) tabs.dataset.active = next;
  $$("[data-auth-tab]").forEach((button) => {
    const selected = button.dataset.authTab === next;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-selected", selected ? "true" : "false");
  });
  const gateError = $("#auth-gate-error");
  if (gateError) gateError.hidden = true;

  if (!stage || !loginForm || !registerForm || current === next) {
    if (loginForm) loginForm.hidden = next !== "login";
    if (registerForm) registerForm.hidden = next !== "register";
    if (stage) stage.dataset.active = next;
    focusInput();
    return;
  }

  const token = ++authTabTransitionToken;
  const reducedMotion = !animate || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  stage.getAnimations().forEach((animation) => animation.cancel());
  loginForm.getAnimations().forEach((animation) => animation.cancel());
  registerForm.getAnimations().forEach((animation) => animation.cancel());

  if (reducedMotion) {
    outgoing.hidden = true;
    incoming.hidden = false;
    stage.dataset.active = next;
    stage.classList.remove("is-switching");
    stage.style.height = "";
    focusInput();
    return;
  }

  const direction = next === "register" ? 1 : -1;
  const startHeight = stage.getBoundingClientRect().height;
  incoming.hidden = false;
  stage.style.height = `${startHeight}px`;
  stage.classList.add("is-switching");
  const endHeight = incoming.scrollHeight;
  stage.dataset.active = next;

  const timing = { duration: 380, easing: "cubic-bezier(.22,.78,.2,1)", fill: "both" };
  const heightAnimation = stage.animate(
    [{ height: `${startHeight}px` }, { height: `${endHeight}px` }],
    { duration: 420, easing: "cubic-bezier(.22,.78,.2,1)", fill: "both" },
  );
  const outgoingAnimation = outgoing.animate(
    [
      { opacity: 1, transform: "translateX(0) scale(1)", filter: "blur(0)" },
      { opacity: 0, transform: `translateX(${-direction * 22}px) scale(.985)`, filter: "blur(2px)" },
    ],
    timing,
  );
  const incomingAnimation = incoming.animate(
    [
      { opacity: 0, transform: `translateX(${direction * 26}px) scale(.985)`, filter: "blur(2px)" },
      { opacity: 1, transform: "translateX(0) scale(1)", filter: "blur(0)" },
    ],
    timing,
  );

  Promise.allSettled([heightAnimation.finished, outgoingAnimation.finished, incomingAnimation.finished]).then(() => {
    if (token !== authTabTransitionToken) return;
    outgoing.hidden = true;
    incoming.hidden = false;
    stage.classList.remove("is-switching");
    stage.style.height = "";
    focusInput();
  });
}

function setAuthGate(open, { tab = "login", resume = false } = {}) {
  const gate = $("#auth-gate");
  if (!gate) return;
  if (resume) resumeGenerationAfterAuth = true;
  gate.hidden = !open;
  document.body.classList.toggle("modal-open", open);
  if (open) setAuthTab(tab, { animate: false });
  else {
    resumeGenerationAfterAuth = false;
    const url = new URL(location.href);
    if (url.searchParams.has("auth")) {
      url.searchParams.delete("auth");
      history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }
  }
}

function requireAuthentication({ resume = false, tab = "login" } = {}) {
  if (isAuthenticated) return true;
  setAuthGate(true, { tab, resume });
  return false;
}

function applyAuthenticatedUser(payload) {
  isAuthenticated = true;
  const layout = document.querySelector(".layout");
  if (layout) layout.dataset.authenticated = "1";
  $$(".auth-only").forEach((element) => (element.hidden = false));
  $$(".guest-only").forEach((element) => (element.hidden = true));
  const name = payload.display_name || payload.username || "SenseNova 用户";
  const initial = Array.from(name)[0]?.toUpperCase() || "S";
  const menuName = $(".user-menu-account strong");
  const menuMeta = $(".user-menu-account small");
  const profileName = $(".user-profile-copy strong");
  const profileMeta = $(".user-profile-copy small");
  if (menuName) menuName.textContent = name;
  if (menuMeta) menuMeta.textContent = "SenseNova Studio 用户";
  if (profileName) profileName.textContent = name;
  if (profileMeta) profileMeta.textContent = "个人账号";
  const menuAvatar = $(".user-menu-account .user-avatar");
  const profileAvatar = $("#user-menu-trigger .user-avatar");
  if (menuAvatar) menuAvatar.textContent = initial;
  if (profileAvatar) profileAvatar.textContent = initial;
}

async function switchAccount() {
  const button = $('[data-user-action="switch"]');
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      redirect: "follow",
    });
    if (!response.ok) throw new Error("切换账号失败，请稍后重试");
    // 重新载入游客态页面，既清除上一账号的界面数据，也直接打开登录弹窗。
    location.assign("/?auth=login");
  } catch (error) {
    if (button) button.disabled = false;
    showStudioNotice(error?.message || "切换账号失败，请稍后重试");
  }
}

async function submitAuthForm(form, endpoint) {
  const submit = form.querySelector('button[type="submit"]');
  const errorBox = $("#auth-gate-error");
  errorBox.hidden = true;
  submit.disabled = true;
  try {
    const response = await fetch(endpoint, { method: "POST", body: new FormData(form) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || payload.detail || "认证失败，请重试");
    const shouldResume = resumeGenerationAfterAuth;
    applyAuthenticatedUser(payload);
    setAuthGate(false);
    form.reset();
    await Promise.all([loadDecks(), loadCustomModels().catch(() => {})]);
    if (shouldResume) await send();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    submit.disabled = false;
  }
}

function setTopbar(title = "新对话", subtitle = "AI 生成内容可能存在偏差，请注意核验") {
  const titleEl = $("#topbar-title");
  const subtitleEl = $("#topbar-subtitle");
  if (titleEl) titleEl.textContent = title || "新对话";
  if (subtitleEl) subtitleEl.textContent = subtitle;
}

function setSidebarCollapsed(collapsed, { remember = true } = {}) {
  const layout = document.querySelector(".layout");
  const button = $("#sidebar-toggle");
  const expandButton = $("#sidebar-expand");
  const nextCollapsed = !!collapsed;
  if (nextCollapsed) setUserMenu(false);
  if (!remember) layout?.classList.add("sidebar-transition-suppressed");
  layout?.classList.toggle("sidebar-collapsed", nextCollapsed);
  if (!remember) requestAnimationFrame(() => requestAnimationFrame(() => layout?.classList.remove("sidebar-transition-suppressed")));
  if (button) {
    button.setAttribute("aria-expanded", nextCollapsed ? "false" : "true");
    button.setAttribute("aria-label", nextCollapsed ? uiText("expand_sidebar") : uiText("collapse_sidebar"));
    button.title = nextCollapsed ? uiText("expand_sidebar") : uiText("collapse_sidebar");
  }
  if (expandButton) {
    expandButton.setAttribute("aria-expanded", nextCollapsed ? "false" : "true");
    expandButton.setAttribute("aria-label", uiText("expand_sidebar"));
    expandButton.title = uiText("expand_sidebar");
  }
  if (remember) localStorage.setItem("studio_sidebar_collapsed", nextCollapsed ? "1" : "0");
}

function setProcRailOpen(open) {
  const editor = $("#editor");
  const button = $("#proc-toggle");
  const nextOpen = !!open;
  editor?.classList.toggle("proc-open", nextOpen);
  button?.setAttribute("aria-expanded", nextOpen ? "true" : "false");
}

function setProcRailCollapsed(collapsed, { remember = true } = {}) {
  const editor = $("#editor");
  const collapseButton = $("#proc-rail-collapse");
  const expandButton = $("#proc-rail-expand");
  const nextCollapsed = !!collapsed;
  if (nextCollapsed) setProcRailOpen(false);
  editor?.classList.toggle("proc-rail-collapsed", nextCollapsed);
  collapseButton?.setAttribute("aria-expanded", nextCollapsed ? "false" : "true");
  expandButton?.setAttribute("aria-expanded", nextCollapsed ? "false" : "true");
  if (remember) localStorage.setItem("studio_proc_rail_collapsed", nextCollapsed ? "1" : "0");
  requestAnimationFrame(() => scheduleDeckViewportFit());
  setTimeout(() => scheduleDeckViewportFit(), 180);
  setTimeout(() => scheduleDeckViewportFit(), 430);
}

function setTheme(theme, { remember = true } = {}) {
  const safeTheme = theme === "dark" ? "dark" : "light";
  const root = document.documentElement;
  const button = $("#theme-toggle");
  root.dataset.theme = safeTheme;
  root.style.colorScheme = safeTheme;
  if (button) {
    const dark = safeTheme === "dark";
    button.classList.toggle("dark-active", dark);
    button.setAttribute("aria-pressed", dark ? "true" : "false");
    button.setAttribute("aria-label", dark ? "切换亮色主题" : "切换深色主题");
    button.title = dark ? "切换亮色主题" : "切换深色主题";
    const icon = button.querySelector("span");
    if (icon) icon.textContent = dark ? "☀" : "☾";
  }
  $$('[data-theme-choice]').forEach((choice) => {
    const selected = choice.dataset.themeChoice === safeTheme;
    choice.classList.toggle("selected", selected);
    choice.setAttribute("aria-pressed", selected ? "true" : "false");
  });
  if (remember) localStorage.setItem("studio_theme", safeTheme);
}

const UI_COPY = {
  zh: {
    new_chat: "新对话", history: "历史记录", theme: "主题", login_register: "登录 / 注册", new_presentation: "新建演示",
    batch: "批量生成与下载", add_model: "添加模型", static_presentation: "静态演示",
    dynamic_presentation: "动态演示", switch_account: "切换账号", logout: "退出登录",
    hero_title: "今天，你想展示什么？", start_inspiration: "从一个灵感开始", refresh: "换一批", prompt_placeholder: "描述你的主题、受众和想要的效果…",
    generate: "开始生成", upload_files: "上传文件或图片", upload_limit: "最多 8 个，单个不超过 20MB",
    length: "篇幅", style: "风格", model: "模型", skill: "Skill", appearance: "外观",
    appearance_note: "选择适合当前环境的界面主题", light: "浅色", dark: "深色", language: "语言",
    language_note: "设置 Studio 的界面语言", expand_sidebar: "展开侧栏", collapse_sidebar: "收起侧栏",
    mode_static: "已选择静态演示", mode_dynamic: "已选择动态演示",
    static_hint: "静态演示约需 20–35 分钟 · 期间可关闭页面 · ⌘/Ctrl + Enter 发送",
    dynamic_hint: "动态演示会逐页编排动效并渲染检查 · 右侧实时预览 · ⌘/Ctrl + Enter 发送",
  },
  en: {
    new_chat: "New chat", history: "History", theme: "Theme", login_register: "Sign in / Register", new_presentation: "New presentation",
    batch: "Batch generation & download", add_model: "Add model", static_presentation: "Static presentation",
    dynamic_presentation: "Dynamic presentation", switch_account: "Switch account", logout: "Sign out",
    hero_title: "What would you like to present today?", start_inspiration: "Start with an idea", refresh: "Refresh", prompt_placeholder: "Describe your topic, audience, and desired outcome…",
    generate: "Generate", upload_files: "Upload files or images", upload_limit: "Up to 8 files, 20MB each",
    length: "Length", style: "Style", model: "Model", skill: "Skill", appearance: "Appearance",
    appearance_note: "Choose a comfortable interface theme", light: "Light", dark: "Dark", language: "Language",
    language_note: "Set the Studio interface language", expand_sidebar: "Expand sidebar", collapse_sidebar: "Collapse sidebar",
    mode_static: "Static presentation selected", mode_dynamic: "Dynamic presentation selected",
    static_hint: "Static generation takes about 20–35 minutes · You may close this page · ⌘/Ctrl + Enter to send",
    dynamic_hint: "Dynamic generation choreographs and checks each slide · Live preview on the right · ⌘/Ctrl + Enter to send",
  },
};
const storedLanguage = localStorage.getItem("studio_language");
const serverDefaultLanguage = document.querySelector(".layout")?.dataset.defaultLanguage === "en" ? "en" : "zh";
let currentLanguage = storedLanguage === "en" || storedLanguage === "zh"
  ? storedLanguage
  : serverDefaultLanguage;
function uiText(key) { return UI_COPY[currentLanguage]?.[key] || UI_COPY.zh[key] || key; }
function setLanguage(language, { remember = true } = {}) {
  currentLanguage = language === "en" ? "en" : "zh";
  document.documentElement.lang = currentLanguage;
  $$('[data-i18n]').forEach((element) => {
    const value = UI_COPY[currentLanguage][element.dataset.i18n];
    if (value) element.textContent = value;
  });
  $$('[data-i18n-placeholder]').forEach((element) => {
    const value = UI_COPY[currentLanguage][element.dataset.i18nPlaceholder];
    if (value) element.placeholder = value;
  });
  const selector = $("#settings-language");
  if (selector) selector.value = currentLanguage;
  setCreationMode(creationMode, { remember: false });
  setSidebarCollapsed(document.querySelector(".layout")?.classList.contains("sidebar-collapsed"), { remember: false });
  if (remember) localStorage.setItem("studio_language", currentLanguage);
}

let creationMode = dynamicEnabled && localStorage.getItem("studio_creation_mode") === "dynamic" ? "dynamic" : "static";
let dynamicSkill = "sense-present-dazzle";
function setCreationMode(mode, { remember = true } = {}) {
  const nextMode = dynamicEnabled && mode === "dynamic" ? "dynamic" : "static";
  const changed = nextMode !== creationMode;
  creationMode = nextMode;
  const modeOptions = $(".creation-mode-options");
  if (modeOptions) modeOptions.dataset.active = creationMode;
  const dock = $(".composer-dock");
  if (dock) dock.dataset.mode = creationMode;
  $$(".creation-mode-card").forEach((card) => {
    const on = card.dataset.mode === creationMode;
    card.classList.toggle("selected", on);
    card.setAttribute("aria-pressed", on ? "true" : "false");
  });
  // 两条生成线共用风格、模型和全部偏好；仅 Skill 随类型切换。
  $("#common-settings").hidden = false;
  setSkillMenu(false);
  syncSkillPicker();
  if ($("#mode-summary")) $("#mode-summary").textContent = uiText(creationMode === "static" ? "mode_static" : "mode_dynamic");
  if ($("#generation-hint")) $("#generation-hint").textContent = uiText(creationMode === "static" ? "static_hint" : "dynamic_hint");
  syncAttachZone();
  syncFontControl();
  if (changed && remember) {
    dock?.classList.remove("mode-switching");
    void dock?.offsetWidth;
    dock?.classList.add("mode-switching");
    clearTimeout(window.__studioModeAnimation);
    window.__studioModeAnimation = setTimeout(() => dock?.classList.remove("mode-switching"), 680);
  }
  if (remember) localStorage.setItem("studio_creation_mode", creationMode);
}

function showComposer() {
  $("#editor").hidden = true;
  $("#composer").hidden = false;
  setTopbar();
  renderSuggestions();
  setCreationMode(creationMode, { remember: false });
}

function syncDynamicModels(payload = {}) {
  // /models 探活只作观测，不能作为禁用模型的依据：部分兼容端点不开放
  // /models，且短暂网络抖动也不应阻止用户发出真实生成请求。
  if (!Array.isArray(payload.models)) return;
  const states = Object.fromEntries(payload.models.map((model) => [model.key, model.ok]));
  window.__dynamicModelState = states;
  const select = $("#model-sel");
  if (!select) return;
  [...select.options].forEach((option) => {
    option.dataset.baseLabel ||= option.textContent.replace(/ · 不可达$/, "");
    option.disabled = false;
    option.textContent = option.dataset.baseLabel;
    if (option.value in states) option.dataset.health = states[option.value] ? "ok" : "unknown";
  });
  renderModelMenu();
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
async function jget(u) { const r = await fetch(u); if (!r.ok) throw new Error(await r.text()); return r.json(); }
async function jpatch(u, body) {
  const r = await fetch(u, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let message = await r.text();
    try { message = JSON.parse(message).detail || message; } catch {}
    throw new Error(message);
  }
  return r.json();
}
let customModelItems = [];

function upsertCustomModelOption(model, { select = false } = {}) {
  const main = $("#model-sel");
  const defaultPipeline = main.options[0]?.dataset.defaultPipeline || $("#pipeline-sel")?.value || "";
  [main, $("#batch-model")].filter(Boolean).forEach((target) => {
    let option = [...target.options].find((item) => item.value === model.key);
    if (!option) {
      option = document.createElement("option");
      option.value = model.key;
      target.append(option);
    }
    option.textContent = model.name;
    option.dataset.backend = "openai";
    option.dataset.defaultPipeline = defaultPipeline;
    option.dataset.custom = "1";
    option.dataset.thinkingToggle = "0";
  });
  if (select) {
    main.value = model.key;
    normalizeVersionSelection();
  }
  renderModelMenu();
}

function removeCustomModelOption(key) {
  const main = $("#model-sel");
  const wasSelected = main?.value === key;
  [$("#model-sel"), $("#batch-model")].filter(Boolean).forEach((target) => {
    const option = [...target.options].find((item) => item.value === key);
    if (option) option.remove();
  });
  if (wasSelected || !main.value) {
    main.selectedIndex = main.options.length ? 0 : -1;
    normalizeVersionSelection();
  }
  if (localStorage.getItem("studio_model") === key) localStorage.removeItem("studio_model");
  renderModelMenu();
}

async function deleteCustomModel(key, button = null) {
  const model = customModelItems.find((item) => item.key === key);
  if (!model || !confirm(`删除自定义模型“${model.name}”？历史任务仍会保留。`)) return false;
  if (button) button.disabled = true;
  try {
    const id = key.split(":")[1];
    const response = await fetch(`/api/models/custom/${encodeURIComponent(id)}`, { method: "DELETE" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "删除失败");
    customModelItems = customModelItems.filter((item) => item.key !== key);
    removeCustomModelOption(key);
    renderCustomModelList();
    showStudioNotice(`已删除模型“${model.name}”`, { type: "success", duration: 3500 });
    return true;
  } catch (error) {
    showStudioNotice(`模型删除失败：${error.message}`, { type: "error", duration: 7000 });
    if (button) button.disabled = false;
    return false;
  }
}

function renderCustomModelList() {
  const section = $("#custom-models-section");
  const list = $("#custom-model-list");
  section.hidden = !customModelItems.length;
  list.innerHTML = customModelItems.map((model) => `
    <div class="custom-model-item" data-model-key="${escapeHtml(model.key)}">
      <span class="custom-model-item-copy"><strong>${escapeHtml(model.name)}</strong><small>${escapeHtml(model.model_id)} · ${escapeHtml(model.base_url)}${model.has_api_key ? " · Key 已保存" : ""}</small></span>
      <button type="button" data-delete-model="${escapeHtml(model.key)}" aria-label="删除 ${escapeHtml(model.name)}">删除</button>
    </div>`).join("");
}

async function loadCustomModels() {
  const payload = await jget("/api/models/custom");
  customModelItems = payload.models || [];
  customModelItems.forEach((model) => upsertCustomModelOption(model));
  renderCustomModelList();
}

async function setModelDialog(open) {
  if (open && !requireAuthentication()) return;
  const modal = $("#model-modal");
  modal.hidden = !open;
  document.body.classList.toggle("modal-open", open);
  $("#model-form-error").hidden = true;
  if (open) {
    try { await loadCustomModels(); } catch (error) {
      const box = $("#model-form-error"); box.textContent = "模型列表加载失败：" + error.message; box.hidden = false;
    }
    $("#custom-model-name").focus();
  }
}

let serviceSettings = null;

async function guideOptionalMaterialServices() {
  if (localStorage.getItem("studio_optional_material_services_hint") === "1") return;
  try {
    const settings = serviceSettings || await jget("/api/settings/services");
    const missing = [];
    if (!settings.image_generation?.available) missing.push("AI 生图");
    if (!settings.web_search?.available) missing.push("联网搜图/搜索");
    if (!missing.length) return;
    localStorage.setItem("studio_optional_material_services_hint", "1");
    showStudioNotice(
      `${missing.join("和")}尚未配置；不影响发起任务，Skill 会按现有能力降级。可在左下角“用户 → 设置”中补充。`,
      { type: "info", duration: 10000 },
    );
  } catch (_) {
    // Optional guidance must never block generation.
  }
}

function syncServiceCards() {
  ["image", "search"].forEach((name) => {
    const enabled = $(`#service-${name}-enabled`)?.checked;
    $(`[data-service-card="${name}"]`)?.classList.toggle("is-enabled", !!enabled);
  });
}

function syncImageProviderFields({ applyDefaults = false } = {}) {
  const provider = $("#service-image-provider")?.value || "openai_images";
  const isSenseNova = provider === "sensenova_u1";
  const url = $("#service-image-url");
  const model = $("#service-image-model");
  const note = $("#service-image-provider-note");
  if (note) note.textContent = isSenseNova
    ? "SenseNova U1 · 原生 Images API"
    : "OpenAI Images API 兼容服务";
  if (url) url.placeholder = isSenseNova
    ? "https://token.sensenova.cn/v1"
    : "https://example.com/v1";
  if (model) model.placeholder = isSenseNova ? "sensenova-u1-fast" : "gpt-image-2";
  if (applyDefaults && isSenseNova && !url.value.trim()) url.value = "https://token.sensenova.cn/v1";
  if (applyDefaults && isSenseNova && model && !model.value.trim()) model.value = "sensenova-u1-fast";
}

function applyServiceSettings(payload) {
  serviceSettings = payload || {};
  const image = serviceSettings.image_generation || {};
  const search = serviceSettings.web_search || {};
  const generation = serviceSettings.generation || {};
  $("#service-max-tokens").value = generation.max_tokens || 40960;
  $("#service-streaming-enabled").checked = generation.streaming_enabled !== false;
  $("#service-static-max-turns").value = generation.static_max_turns ?? 4096;
  $("#service-static-subagent-max-turns").value = generation.static_subagent_max_turns ?? 200;
  $("#service-dynamic-max-turns").value = generation.dynamic_max_turns || 4096;
  $("#service-image-enabled").checked = !!image.enabled;
  $("#service-image-provider").value = image.provider || "openai_images";
  $("#service-image-url").value = image.base_url || "";
  $("#service-image-model").value = image.model || "";
  $("#service-image-key").value = "";
  $("#service-image-key-state").textContent = image.has_api_key ? "已安全保存" : "未配置";
  $("#service-image-clear-wrap").hidden = !image.has_api_key;
  $("#service-image-clear").checked = false;
  $("#service-search-enabled").checked = !!search.enabled;
  $("#service-search-url").value = search.base_url || "";
  $("#service-search-key").value = "";
  $("#service-search-key-state").textContent = search.has_api_key ? "已安全保存" : "未配置";
  $("#service-search-clear-wrap").hidden = !search.has_api_key;
  $("#service-search-clear").checked = false;
  syncImageProviderFields();
  syncServiceCards();
}

async function setServiceDialog(open) {
  if (open && !requireAuthentication()) return;
  const modal = $("#service-modal");
  modal.hidden = !open;
  document.body.classList.toggle("modal-open", open);
  const errorBox = $("#service-form-error");
  errorBox.hidden = true;
  if (!open) return;
  try {
    applyServiceSettings(await jget("/api/settings/services"));
  } catch (error) {
    errorBox.textContent = "服务配置加载失败：" + error.message;
    errorBox.hidden = false;
  }
}

function fmtDur(s) {
  if (s == null || !Number.isFinite(Number(s))) return "";
  const total = Math.max(0, Math.round(Number(s)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  if (h) return `${h}小时${m}分${sec}秒`;
  return m ? `${m}分${sec}秒` : `${sec}秒`;
}
// ``ed.kind`` describes the backend transport (legacy dynamic API versus the
// unified Deck API).  The new direct sn-ppt-dazzle route intentionally uses the
// Deck API, so visual identity must come from the produced presentation type.
function editorPresentationKind() {
  return ed.kind === "dynamic" || ed.pptOutput === "dynamic_html" ? "dynamic" : "static";
}
function fileUrl(n) {
  if (ed.kind === "dynamic") return `/dynamic/files/${ed.id}/shots/page_${pad2(n)}.png?t=${ed.dynamicStamp || 0}`;
  const t = (ed.rtimes && ed.rtimes[String(n)]) || 0;   // mtime 作缓存戳:重渲后换新图
  if (ed.pptOutput === "dynamic_html") {
    return `/api/decks/${ed.id}/file?rel=shots/page_${pad2(n)}.png&t=${t}`;
  }
  return trajectoryMode
    ? `${trajectoryDeckApi(ed.id, "/file")}?rel=renders/slide_${pad2(n)}.png&t=${t}`
    : `/api/decks/${ed.id}/file?rel=renders/slide_${pad2(n)}.png&t=${t}`;
}

/* ---------------- 侧栏列表 ---------------- */
const HISTORY_GROUPS = [
  { key: "pinned", label: "置顶", icon: "pin" },
  { key: "static", label: trajectoryMode ? "合成数据" : "静态演示", icon: "static" },
  { key: "dynamic", label: "动态演示", icon: "dynamic" },
].filter((group) => dynamicEnabled || group.key !== "dynamic");
let historyContextTarget = null;

function historyGroupState() {
  try { return JSON.parse(localStorage.getItem("studio_history_groups") || "{}"); }
  catch { return {}; }
}

function setHistoryGroupCollapsed(key, collapsed) {
  const state = historyGroupState();
  state[key] = !!collapsed;
  localStorage.setItem("studio_history_groups", JSON.stringify(state));
}

function historyRow(d) {
  const visualKind = d.groupKind || d.kind;
  const kindLabel = visualKind === "dynamic" ? uiText("dynamic_presentation") : uiText("static_presentation");
  const statusLabel = STATUS_LABEL[d.status] || d.status;
  const markerLabel = `${kindLabel} · ${statusLabel}`;
  const title = d.display_title || d.title || "未命名";
  const active = String(d.id) === String(ed.id) && d.kind === ed.kind;
  return `
    <a class="convo deck-item ${active ? "active" : ""}" data-id="${escapeHtml(String(d.id))}" data-kind="${d.kind}"
      data-title="${escapeHtml(title)}" data-status="${escapeHtml(String(d.status || ""))}"
      data-pinned="${d.pinned ? "1" : "0"}" href="#${d.kind}-${encodeURIComponent(d.id)}">
      <span class="kind-mark ${visualKind} s-${d.status}" role="img" aria-label="${escapeHtml(markerLabel)}" title="${escapeHtml(markerLabel)}"></span>
      <span class="deck-t" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
      <span class="st muted small">${escapeHtml(statusLabel)}</span>
      <button class="del-btn" title="删除这条记录" data-id="${escapeHtml(String(d.id))}" data-kind="${d.kind}">✕</button>
    </a>`;
}

function historyGroupMarkup(group, rows, collapsed) {
  const empty = group.key === "pinned" ? "右键演示可置顶" : `暂无${group.label}`;
  return `<section class="history-group${collapsed ? " collapsed" : ""}" data-history-group="${group.key}">
    <button type="button" class="history-group-head" aria-expanded="${collapsed ? "false" : "true"}">
      <span class="history-group-icon ${group.icon}" aria-hidden="true"></span>
      <span class="history-group-title">${group.label}</span>
      <span class="history-group-count">${rows.length}</span>
      <span class="history-group-chevron" aria-hidden="true"></span>
    </button>
    <div class="history-group-panel"><div class="history-group-items">
      ${rows.length ? rows.map(historyRow).join("") : `<div class="history-group-empty">${empty}</div>`}
    </div></div>
  </section>`;
}

function ensureHistoryContextUi() {
  let menu = $("#history-context-menu");
  if (!menu) {
    menu = document.createElement("div");
    menu.id = "history-context-menu";
    menu.className = "history-context-menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    menu.innerHTML = `
      <button type="button" data-history-action="rename" role="menuitem"><span class="history-menu-icon rename" aria-hidden="true"></span><span>重命名</span></button>
      <button type="button" data-history-action="pin" role="menuitem"><span class="history-menu-icon pin" aria-hidden="true"></span><span class="history-pin-label">置顶</span></button>
      <button type="button" data-history-action="regenerate" role="menuitem"><span class="history-menu-icon regenerate" aria-hidden="true"></span><span>重新生成</span></button>`;
    document.body.append(menu);
    menu.addEventListener("click", async (event) => {
      const action = event.target.closest("[data-history-action]")?.dataset.historyAction;
      if (!action || !historyContextTarget) return;
      const target = historyContextTarget;
      closeHistoryContextMenu();
      if (action === "rename") openHistoryRenameDialog(target);
      if (action === "pin") await updateHistoryItem(target, { pinned: !target.pinned });
      if (action === "regenerate") await regenerateHistoryItem(target);
    });
  }
  let modal = $("#history-rename-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "history-rename-modal";
    modal.className = "history-rename-modal";
    modal.hidden = true;
    modal.innerHTML = `<form class="history-rename-card">
      <div class="history-rename-heading"><span>重命名演示</span><button type="button" data-history-rename-close aria-label="关闭">×</button></div>
      <label for="history-rename-input">名称</label>
      <input id="history-rename-input" maxlength="80" autocomplete="off">
      <div class="history-rename-error" role="alert" hidden></div>
      <div class="history-rename-actions"><button type="button" data-history-rename-close>取消</button><button type="submit">保存</button></div>
    </form>`;
    document.body.append(modal);
    modal.addEventListener("click", (event) => {
      if (event.target === modal || event.target.closest("[data-history-rename-close]")) closeHistoryRenameDialog();
    });
    modal.querySelector("form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!historyContextTarget) return;
      const input = $("#history-rename-input");
      const title = input.value.trim();
      const error = modal.querySelector(".history-rename-error");
      if (!title) { error.textContent = "名称不能为空"; error.hidden = false; input.focus(); return; }
      const submit = modal.querySelector('[type="submit"]');
      submit.disabled = true; submit.textContent = "保存中…";
      const ok = await updateHistoryItem(historyContextTarget, { title }, { quiet: true });
      submit.disabled = false; submit.textContent = "保存";
      if (ok) closeHistoryRenameDialog();
      else { error.textContent = "保存失败，请稍后重试"; error.hidden = false; }
    });
  }
  return menu;
}

function openHistoryContextMenu(item, x, y) {
  historyContextTarget = {
    id: item.dataset.id,
    kind: item.dataset.kind,
    title: item.dataset.title || "未命名",
    status: item.dataset.status || "",
    pinned: item.dataset.pinned === "1",
  };
  const menu = ensureHistoryContextUi();
  menu.querySelector(".history-pin-label").textContent = historyContextTarget.pinned ? "取消置顶" : "置顶";
  menu.querySelector('[data-history-action="regenerate"]').hidden = historyContextTarget.status !== "completed";
  menu.hidden = false;
  menu.style.left = `${Math.min(x, innerWidth - menu.offsetWidth - 10)}px`;
  menu.style.top = `${Math.min(y, innerHeight - menu.offsetHeight - 10)}px`;
  requestAnimationFrame(() => menu.classList.add("open"));
}

function closeHistoryContextMenu() {
  const menu = $("#history-context-menu");
  if (!menu) return;
  menu.classList.remove("open");
  menu.hidden = true;
}

function openHistoryRenameDialog(target) {
  historyContextTarget = target;
  ensureHistoryContextUi();
  const modal = $("#history-rename-modal");
  const input = $("#history-rename-input");
  modal.querySelector(".history-rename-error").hidden = true;
  input.value = target.title;
  modal.hidden = false;
  requestAnimationFrame(() => { modal.classList.add("open"); input.focus(); input.select(); });
}

function closeHistoryRenameDialog() {
  const modal = $("#history-rename-modal");
  if (!modal) return;
  modal.classList.remove("open");
  modal.hidden = true;
  historyContextTarget = null;
}

async function updateHistoryItem(target, changes, { quiet = false } = {}) {
  try {
    await jpatch(`/api/history/${target.kind}/${encodeURIComponent(target.id)}`, changes);
    if (changes.title && String(ed.id) === String(target.id) && ed.kind === target.kind) {
      $("#ed-title").textContent = changes.title;
    }
    await loadDecks(ed.id);
    return true;
  } catch (error) {
    if (!quiet) alert(`更新失败：${error.message}`);
    return false;
  }
}

async function regenerateHistoryItem(target) {
  try {
    const response = target.kind === "dynamic"
      ? await fetch("/api/dynamic/regenerate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conv_id: target.id }),
        })
      : await fetch(`/api/decks/${encodeURIComponent(target.id)}/regenerate`, { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.error || "重新生成失败");
    if (target.kind === "dynamic") await openDynamic(payload.conv_id, { subscribeNow: true });
    else await openDeck(payload.deck_id);
    refreshDeckListWhenIdle(target.kind === "dynamic" ? payload.conv_id : payload.deck_id);
  } catch (error) {
    alert(`重新生成失败：${error.message}`);
  }
}

async function loadDecks(active) {
  const box = $("#deck-list");
  if (!isAuthenticated) {
    box.innerHTML = '<button type="button" class="guest-history" data-open-auth>登录后查看历史演示</button>';
    return;
  }
  try {
    const [staticResult, dynamicResult] = await Promise.all([
      jget(trajectoryMode ? "/api/trajectory-monitor/decks" : "/api/decks").catch(() => ({ decks: [] })),
      dynamicEnabled
        ? jget("/api/dynamic/conversations").catch(() => ({ items: [] }))
        : Promise.resolve({ items: [] }),
    ]);
    syncDynamicModels(dynamicResult);
    const rows = [
      ...(staticResult.decks || [])
        .filter((d) => dynamicEnabled || (d.presentation_kind || "static") === "static")
        .map((d) => ({
        ...d, kind: "static", groupKind: d.presentation_kind || "static",
        sortAt: d.created_at || d.updated_at || 0,
      })),
      ...(dynamicResult.items || []).map((d) => ({
        ...d, id: d.conv_id, kind: "dynamic", groupKind: "dynamic",
        sortAt: d.updated || d.created || "",
      })),
    ];
    if (!trajectoryMode) rows.sort((a, b) => String(b.sortAt).localeCompare(String(a.sortAt)));
    const state = historyGroupState();
    const grouped = {
      pinned: rows.filter((d) => d.pinned),
      static: rows.filter((d) => !d.pinned && d.groupKind === "static"),
      dynamic: dynamicEnabled ? rows.filter((d) => !d.pinned && d.groupKind === "dynamic") : [],
    };
    box.innerHTML = HISTORY_GROUPS.map((group) => historyGroupMarkup(group, grouped[group.key], !!state[group.key])).join("");
    $$("#deck-list .history-group-head").forEach((button) => (button.onclick = () => {
      const section = button.closest(".history-group");
      const collapsed = !section.classList.contains("collapsed");
      section.classList.toggle("collapsed", collapsed);
      button.setAttribute("aria-expanded", collapsed ? "false" : "true");
      setHistoryGroupCollapsed(section.dataset.historyGroup, collapsed);
    }));
    $$("#deck-list .deck-item").forEach((a) =>
      (a.onclick = (e) => {
        if (e.target.classList.contains("del-btn")) return;
        e.preventDefault(); a.dataset.kind === "dynamic" ? openDynamic(a.dataset.id) : openDeck(+a.dataset.id);
      }));
    $$("#deck-list .deck-item").forEach((a) => (a.oncontextmenu = trajectoryMode ? null : (event) => {
      event.preventDefault(); event.stopPropagation(); openHistoryContextMenu(a, event.clientX, event.clientY);
    }));
    $$("#deck-list .del-btn").forEach((b) => {
      if (trajectoryMode) { b.hidden = true; return; }
      b.onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      askDelete(b.closest(".deck-item"), b.dataset.id, b.dataset.kind);
      };
    });
  } catch { box.innerHTML = '<div class="muted small pad">加载失败</div>'; }
}

function openDeckFromLocationHash() {
  if (ed.id) return;
  const match = location.hash.match(dynamicEnabled ? /^#(static|dynamic)-(.+)$/ : /^#(static)-(.+)$/);
  let requestedId = "";
  try { requestedId = match ? decodeURIComponent(match[2]) : ""; } catch { requestedId = ""; }
  const requested = match && $$("#deck-list .deck-item").find((item) =>
    item.dataset.kind === match[1] && item.dataset.id === requestedId);
  if (requested) requested.click();
  else if (trajectoryMode) $("#deck-list .deck-item")?.click();
}

function askDelete(row, id, kind = "static") {
  if (!row || row.querySelector(".confirm-box")) return;
  row.classList.add("confirming");
  const box = document.createElement("span");
  box.className = "confirm-box";
  box.innerHTML = `<span class="cf-txt">不可恢复</span>
    <button class="cf-yes">删除</button><button class="cf-no">取消</button>`;
  row.appendChild(box);
  const dismiss = () => {
    box.remove(); row.classList.remove("confirming");
    document.removeEventListener("click", onDocClick, true);
  };
  const onDocClick = (ev) => { if (!box.contains(ev.target)) dismiss(); };
  box.querySelector(".cf-no").onclick = (e) => { e.preventDefault(); e.stopPropagation(); dismiss(); };
  box.querySelector(".cf-yes").onclick = async (e) => {
    e.preventDefault(); e.stopPropagation();
    const btn = e.target; btn.disabled = true; btn.textContent = "删除中…";
    await removeDeck(id, kind);
    dismiss();
  };
  setTimeout(() => document.addEventListener("click", onDocClick, true), 0);
}

async function removeDeck(id, kind = "static") {
  try {
    const r = kind === "dynamic"
      ? await fetch("/api/dynamic/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ conv_id: id }) })
      : await fetch(`/api/decks/${id}`, { method: "DELETE" });
    if (!r.ok) throw new Error(await r.text());
  } catch (e) { alert("删除失败: " + e.message); return; }
  if (String(ed.id) === String(id)) closeEditor();
  loadDecks(ed.id);
}

/* ---------------- 提交简报 ---------------- */
const MODEL_LABEL = {
  "sensenova-flash-lite-v39": "SenseNova Flash Lite v39 (1)",
  "sensenova-flash-lite-v39-2": "SenseNova Flash Lite v39 (2)",
  "sensenova-flash-lite-v39-3": "SenseNova Flash Lite v39 (3)",
  "sensenova-flash-lite-v39-4": "SenseNova Flash Lite v39 (4)",
  "gpt-5.6-sol": "GPT-5.6 SOL (tokenhub)",
  "gpt-5.6-luna": "GPT-5.6 LUNA (tokenhub)",
  "gpt-5.6-terra": "GPT-5.6 TERRA (tokenhub)",
  "kimi-k3": "Kimi K3 (DashScope)",
};
function modelLabel(key) {
  return MODEL_LABEL[key] || key;
}
const PIPELINE_LABEL = {
  infer: "Clean infer harness",
  "visual-craft-harness": "Visual Craft Harness",
  "sn-ppt-web-harness": "sn-ppt-web harness",
  "mural-presenter-harness": "sn-ppt-web harness",
  "sense-present-standard-harness": "SenseNova Static HTML Harness",
  "sense-present-dazzle-harness": "SenseNova Dynamic HTML Harness",
};
const SKILL_LABEL = {
  "sense-present-standard": "SenseNova Static HTML",
  "sense-present-dazzle": "SenseNova Dynamic HTML",
  auto: "Auto（自动选择）",
  zh: "中文 Skill",
  en: "English Skill",
  "long-horizon": "Long-horizon HTML PPT",
  "long-horizon-grouped": "Long-horizon HTML PPT Grouped",
  "long-horizon-grouped-inline-image": "Long-horizon Grouped · Inline Image",
  "visual-craft": "Visual Craft HTML PPT",
  "sn-ppt-web": "sn-ppt-web",
  "mural-presenter": "sn-ppt-web",
};

/* ---------------- 附件(随所选管线能力联动) ---------------- */
let attachFiles = [];               // composer 已选附件(File);仅所选管线 caps 含 attachments 时可用
let fontFiles = [];                 // 用户授权上传的字体；与普通材料分开提交和挂载
const FONT_ROLE_OPTIONS = {
  title: ["Noto Sans SC", "Noto Serif SC", "Smiley Sans", "ZCOOL XiaoWei", "Ma Shan Zheng", "Bebas Neue", "Playfair Display", "Space Grotesk", "Sora"],
  body: ["Noto Sans SC", "Noto Serif SC", "LXGW WenKai", "DM Sans", "Manrope"],
  number: ["IBM Plex Mono", "Archivo", "Montserrat", "Oswald", "Bebas Neue", "Sora"],
  annotation: ["LXGW WenKai", "Zhi Mang Xing", "Dancing Script", "Kalam"],
};
const attachmentObjectUrls = new Map();
let attachmentViewerRequestId = 0;
function pipelineHasCap(cap) {
  const opt = $("#pipeline-sel").selectedOptions && $("#pipeline-sel").selectedOptions[0];
  return (opt?.dataset.caps || "").split(",").includes(cap);
}
function attachmentExtension(name = "") {
  const clean = String(name).split(/[?#]/, 1)[0];
  return clean.includes(".") ? clean.split(".").pop().toLowerCase() : "file";
}
function attachmentPreviewKind(name = "", type = "") {
  const mime = String(type || "").toLowerCase();
  const ext = attachmentExtension(name);
  if ((mime.startsWith("image/") && mime !== "image/svg+xml") || ["png", "jpg", "jpeg", "gif", "webp", "bmp"].includes(ext)) return "image";
  if (mime === "application/pdf" || ext === "pdf") return "pdf";
  if (mime === "text/markdown" || ["md", "markdown"].includes(ext)) return "markdown";
  if (mime.startsWith("text/") || ["txt", "csv", "json", "yaml", "yml", "xml", "html", "htm"].includes(ext)) return "text";
  if (mime.startsWith("audio/") || ["mp3", "wav", "m4a", "aac", "ogg", "flac"].includes(ext)) return "audio";
  if (mime.startsWith("video/") || ["mp4", "webm", "mov", "m4v", "avi"].includes(ext)) return "video";
  if (["doc", "docx", "ppt", "pptx", "xls", "xlsx", "xlsm"].includes(ext)) return "office";
  return "file";
}
function attachmentTypeLabel(name = "", type = "") {
  const ext = attachmentExtension(name);
  const kind = attachmentPreviewKind(name, type);
  if (kind === "image") return `${ext === "file" ? "图片" : ext.toUpperCase()} 图片`;
  if (kind === "pdf") return "PDF 文档";
  if (kind === "markdown") return "Markdown 文档";
  if (kind === "text") return `${ext === "file" ? "文本" : ext.toUpperCase()} 文本`;
  if (kind === "audio") return `${ext === "file" ? "音频" : ext.toUpperCase()} 音频`;
  if (kind === "video") return `${ext === "file" ? "视频" : ext.toUpperCase()} 视频`;
  if (kind === "office") return `${ext.toUpperCase()} 文档`;
  return ext === "file" ? "附件" : `${ext.toUpperCase()} 文件`;
}
function attachmentSizeLabel(size = 0) {
  const bytes = Number(size) || 0;
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}
function deckAttachmentPreviewUrls(item = {}, index = 0) {
  if (!/^\d+$/.test(String(ed.id || ""))) return { url: item.url || "", fallbackUrl: "", previewUrl: "" };
  const dedicatedUrl = `/api/decks/${ed.id}/attachments/${index}`;
  let rel = String(item.preview_rel || "").replace(/^\/+/, "");
  // Only use a workspace path when the backend explicitly exposed one.
  // Parsed PDF/Office/text inputs are not copied into attachments/raw, so
  // synthesising that path produced a 404 that an iframe could not recover from.
  const workspaceUrl = rel
    ? `/api/decks/${ed.id}/file?rel=${encodeURIComponent(rel)}`
    : "";
  return {
    url: item.url || workspaceUrl || dedicatedUrl,
    fallbackUrl: item.url ? (workspaceUrl || dedicatedUrl) : dedicatedUrl,
    previewUrl: `/api/decks/${ed.id}/attachments/${index}/preview`,
  };
}
function attachmentObjectUrl(file) {
  if (!attachmentObjectUrls.has(file)) attachmentObjectUrls.set(file, URL.createObjectURL(file));
  return attachmentObjectUrls.get(file);
}
function releaseAttachmentObjectUrl(file) {
  const url = attachmentObjectUrls.get(file);
  if (url) URL.revokeObjectURL(url);
  attachmentObjectUrls.delete(file);
}
function clearAttachFiles() {
  attachFiles.forEach(releaseAttachmentObjectUrl);
  attachFiles = [];
  renderAttachList();
}
function closeAttachmentViewer() {
  attachmentViewerRequestId += 1;
  const viewer = $("#attachment-viewer");
  if (!viewer) return;
  viewer.hidden = true;
  $("#attachment-viewer-body").replaceChildren();
  if ($("#model-modal")?.hidden !== false && $("#service-modal")?.hidden !== false) {
    document.body.classList.remove("modal-open");
  }
}
async function fetchAttachmentSource(url, fallbackUrl = "") {
  let sourceUrl = url;
  let response = await fetch(sourceUrl, { credentials: "same-origin" });
  if (!response.ok && fallbackUrl && fallbackUrl !== sourceUrl) {
    sourceUrl = fallbackUrl;
    response = await fetch(sourceUrl, { credentials: "same-origin" });
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return { sourceUrl, response };
}
function attachmentPreviewError(message = "读取附件失败") {
  const empty = document.createElement("div");
  empty.className = "attachment-viewer-file error";
  empty.innerHTML = `<span>!</span><strong>暂时无法预览</strong><p>${escapeHtml(message)}</p>`;
  return empty;
}
async function openAttachmentViewer({ name = "附件", type = "", size = 0, url = "", fallbackUrl = "", previewUrl = "", downloadUrl = "" } = {}) {
  if (!url) return;
  const requestId = ++attachmentViewerRequestId;
  const viewer = $("#attachment-viewer");
  const body = $("#attachment-viewer-body");
  if (!viewer || !body) return;
  const kind = attachmentPreviewKind(name, type);
  $("#attachment-viewer-title").textContent = name;
  $("#attachment-viewer-meta").textContent = [attachmentTypeLabel(name, type), attachmentSizeLabel(size)].filter(Boolean).join(" · ");
  const marks = { image: "▧", pdf: "PDF", markdown: "MD", text: "TXT", audio: "♪", video: "▶", office: attachmentExtension(name).slice(0, 4).toUpperCase() };
  $("#attachment-viewer-mark").textContent = marks[kind] || "↥";
  body.replaceChildren();
  const open = $("#attachment-viewer-open");
  const download = $("#attachment-viewer-download");
  open.href = url;
  download.href = downloadUrl || url;
  download.download = name;
  viewer.hidden = false;
  document.body.classList.add("modal-open");
  if (kind === "image") {
    const image = document.createElement("img");
    image.src = url;
    image.alt = name;
    if (fallbackUrl && fallbackUrl !== url) {
      image.onerror = () => {
        image.onerror = null;
        image.src = fallbackUrl;
        open.href = fallbackUrl;
        download.href = `${fallbackUrl}${fallbackUrl.includes("?") ? "&" : "?"}download=1`;
      };
    }
    body.append(image);
    $("#attachment-viewer-tip").textContent = "点击外部区域可关闭预览";
  } else if (kind === "markdown") {
    const article = document.createElement("article");
    article.className = "attachment-markdown agent-markdown loading";
    article.innerHTML = "<p>正在整理 Markdown 内容…</p>";
    body.append(article);
    $("#attachment-viewer-tip").textContent = "Markdown 预览";
    try {
      const { sourceUrl, response } = await fetchAttachmentSource(url, fallbackUrl);
      const markdown = await response.text();
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      article.classList.remove("loading");
      article.innerHTML = agentMarkdownHtml(markdown) || "<p>这个 Markdown 文件暂时没有内容。</p>";
      body.scrollTop = 0;
      if (sourceUrl !== url) open.href = sourceUrl;
    } catch (error) {
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      article.classList.remove("loading");
      article.classList.add("error");
      article.innerHTML = `<h2>暂时无法预览</h2><p>${escapeHtml(error?.message || "读取 Markdown 文件失败")}</p>`;
      $("#attachment-viewer-tip").textContent = "可在新窗口打开或下载文件";
    }
  } else if (kind === "pdf") {
    const frame = document.createElement("iframe");
    frame.src = `${url}${url.includes("#") ? "&" : "#"}view=FitH&toolbar=1&navpanes=0`;
    frame.title = `${name} 预览`;
    frame.className = "attachment-pdf-frame";
    body.append(frame);
    $("#attachment-viewer-tip").textContent = "PDF 预览 · 支持翻页与缩放";
  } else if (kind === "text") {
    const pre = document.createElement("pre");
    pre.className = "attachment-text loading";
    pre.textContent = "正在读取文本内容…";
    body.append(pre);
    $("#attachment-viewer-tip").textContent = "文本内容预览";
    try {
      const { sourceUrl, response } = await fetchAttachmentSource(url, fallbackUrl);
      let content = await response.text();
      if (attachmentExtension(name) === "json") {
        try { content = JSON.stringify(JSON.parse(content), null, 2); } catch (_) { /* keep source */ }
      }
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      pre.classList.remove("loading");
      pre.textContent = content || "这个文件暂时没有文本内容。";
      body.scrollTop = 0;
      if (sourceUrl !== url) open.href = sourceUrl;
    } catch (error) {
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      pre.replaceWith(attachmentPreviewError(error?.message || "读取文本文件失败"));
      $("#attachment-viewer-tip").textContent = "可在新窗口打开或下载文件";
    }
  } else if (kind === "audio" || kind === "video") {
    const media = document.createElement(kind);
    media.src = url;
    media.controls = true;
    media.preload = "metadata";
    if (kind === "video") media.playsInline = true;
    body.append(media);
    $("#attachment-viewer-tip").textContent = kind === "audio" ? "音频预览" : "视频预览";
  } else if (kind === "office" && previewUrl) {
    const article = document.createElement("article");
    article.className = "attachment-document loading";
    article.innerHTML = "<p>正在整理文档内容…</p>";
    body.append(article);
    $("#attachment-viewer-tip").textContent = "文档内容预览";
    try {
      const response = await fetch(previewUrl, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      const gallery = (data.assets || []).length ? `<div class="attachment-document-pages">${data.assets.map((asset) => `<img src="${escapeHtml(asset.url)}" alt="${escapeHtml(asset.label || name)}" loading="lazy">`).join("")}</div>` : "";
      const text = String(data.text || "").trim();
      const notes = (data.notes || []).filter(Boolean);
      article.classList.remove("loading");
      article.innerHTML = `${gallery}${text ? `<pre>${escapeHtml(text)}</pre>` : ""}${notes.length ? `<div class="attachment-document-notes">${notes.map((note) => `<p>${escapeHtml(note)}</p>`).join("")}</div>` : ""}${!gallery && !text ? "<p>没有可提取的网页预览内容，请下载后使用对应应用打开。</p>" : ""}`;
      body.scrollTop = 0;
    } catch (error) {
      if (requestId !== attachmentViewerRequestId || viewer.hidden) return;
      article.replaceWith(attachmentPreviewError(error?.message || "读取文档预览失败"));
      $("#attachment-viewer-tip").textContent = "可在新窗口打开或下载文件";
    }
  } else {
    const empty = document.createElement("div");
    empty.className = "attachment-viewer-file";
    empty.innerHTML = `<span>${escapeHtml(attachmentExtension(name).slice(0, 5).toUpperCase())}</span><strong>${escapeHtml(name)}</strong><p>浏览器无法直接预览此格式，可以下载后使用对应应用打开。</p>`;
    body.append(empty);
    $("#attachment-viewer-tip").textContent = "该格式暂不支持网页内预览";
  }
}
function renderAttachList() {
  const box = $("#attach-list"); if (!box) return;
  box.hidden = attachFiles.length === 0;
  box.innerHTML = attachFiles.map((file, index) => {
    const kind = attachmentPreviewKind(file.name, file.type);
    const url = attachmentObjectUrl(file);
    const visual = kind === "image"
      ? `<img src="${escapeHtml(url)}" alt="">`
      : `<span>${kind === "pdf" ? "PDF" : attachmentExtension(file.name).slice(0, 4).toUpperCase()}</span>`;
    const meta = [attachmentTypeLabel(file.name, file.type), attachmentSizeLabel(file.size)].filter(Boolean).join(" · ");
    return `<article class="attach-item" data-i="${index}">
      <button type="button" class="attach-open" data-local-attachment="${index}" title="查看 ${escapeHtml(file.name)}">
        <span class="attach-thumb ${kind}">${visual}</span>
        <span class="attach-copy"><strong>${escapeHtml(file.name)}</strong><small>${escapeHtml(meta)}</small></span>
      </button>
      <button type="button" data-i="${index}" class="attach-x" title="移除 ${escapeHtml(file.name)}" aria-label="移除 ${escapeHtml(file.name)}">×</button>
    </article>`;
  }).join("");
  const count = $("#attach-count");
  if (count) {
    count.textContent = String(attachFiles.length);
    count.hidden = attachFiles.length === 0;
  }
}
function setAttachMenu(open) {
  const zone = $("#attach-zone");
  const menu = $("#attach-menu");
  const trigger = $("#attach-btn");
  if (!zone || !menu || !trigger) return;
  menu.hidden = !open;
  zone.classList.toggle("menu-open", open);
  trigger.classList.toggle("active", open);
  trigger.setAttribute("aria-expanded", open ? "true" : "false");
}
function syncAttachZone() {          // 附件仅开放给静态管线；动态演示不展示入口，也不保留待提交附件
  const on = creationMode === "static" && pipelineHasCap("attachments");
  const zone = $("#attach-zone"); if (zone) zone.hidden = !on;
  if (!on) setAttachMenu(false);
  if (!on && attachFiles.length) clearAttachFiles();
}
let fontPanelPositionFrame = 0;

function positionFloatingFontPanel() {
  const panel = $("#font-panel");
  const trigger = $("#font-trigger");
  if (!panel || !trigger || panel.hidden || !panel.classList.contains("font-panel-floating")) return;

  const margin = 12;
  const gap = 12;
  const triggerRect = trigger.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  const viewportWidth = window.visualViewport?.width || window.innerWidth;
  const viewportHeight = window.visualViewport?.height || window.innerHeight;
  const preferredLeft = triggerRect.right + gap;
  const maxLeft = Math.max(margin, viewportWidth - panelRect.width - margin);
  const left = Math.max(margin, Math.min(preferredLeft, maxLeft));
  const preferredTop = triggerRect.bottom - panelRect.height;
  const maxTop = Math.max(margin, viewportHeight - panelRect.height - margin);
  const top = Math.max(margin, Math.min(preferredTop, maxTop));

  panel.style.left = `${Math.round(left)}px`;
  panel.style.top = `${Math.round(top)}px`;
}

function queueFontPanelPosition() {
  cancelAnimationFrame(fontPanelPositionFrame);
  fontPanelPositionFrame = requestAnimationFrame(positionFloatingFontPanel);
}

function setFontPanel(open) {
  const panel = $("#font-panel"); const trigger = $("#font-trigger");
  const home = $("#font-control");
  if (!panel || !trigger || !home) return;
  const next = !!open;

  if (next) {
    if (panel.parentElement !== document.body) document.body.appendChild(panel);
    panel.classList.add("font-panel-floating");
    panel.hidden = false;
    positionFloatingFontPanel();
    queueFontPanelPosition();
  } else {
    panel.hidden = true;
    panel.classList.remove("font-panel-floating");
    panel.removeAttribute("style");
    if (panel.parentElement !== home) home.appendChild(panel);
  }
  trigger.classList.toggle("active", next);
  trigger.setAttribute("aria-expanded", next ? "true" : "false");
}

window.addEventListener("resize", queueFontPanelPosition);
window.visualViewport?.addEventListener("resize", queueFontPanelPosition);
function renderFontControls() {
  $$('[data-font-role]').forEach((select) => {
    const previous = select.value || "auto";
    const role = select.dataset.fontRole;
    const options = ['<option value="auto">智慧匹配</option>'];
    (FONT_ROLE_OPTIONS[role] || []).forEach((family) => options.push(
      `<option value="builtin:${escapeHtml(family)}">${escapeHtml(family)}</option>`
    ));
    fontFiles.forEach((file) => options.push(
      `<option value="custom:${escapeHtml(file.name)}">上传 · ${escapeHtml(file.name)}</option>`
    ));
    select.innerHTML = options.join("");
    select.value = [...select.options].some((item) => item.value === previous) ? previous : "auto";
  });
  const list = $("#font-file-list");
  if (list) {
    list.hidden = fontFiles.length === 0;
    list.innerHTML = fontFiles.map((file, index) =>
      `<span class="font-file-chip"><span>${escapeHtml(file.name)}</span><button type="button" data-font-remove="${index}" aria-label="移除字体">×</button></span>`
    ).join("");
  }
  if ($("#font-license")) $("#font-license").hidden = fontFiles.length === 0;
  const chosen = $$('[data-font-role]').filter((select) => select.value !== "auto").length;
  if ($("#font-current")) $("#font-current").textContent = chosen || fontFiles.length ? `${chosen} 个角色` : "智慧匹配";
}
function clearFontFiles() {
  fontFiles = [];
  if ($("#font-license-ack")) $("#font-license-ack").checked = false;
  renderFontControls();
}
function fontRoleConfig() {
  return Object.fromEntries(
    $$('[data-font-role]').map((select) => [select.dataset.fontRole, select.value])
      .filter(([, value]) => value && value !== "auto")
  );
}
function syncFontControl() {
  const enabled = creationMode === "static" && pipelineHasCap("custom_fonts");
  const control = $("#font-control");
  if (control) control.hidden = !enabled;
  if (!enabled) setFontPanel(false);
  if (!enabled && fontFiles.length) clearFontFiles();
}
function attachmentMode() {
  const el = $("#attachment-mode");
  return el ? el.value : "web_parse";
}

function thinkingEnabled() {
  return modelSupportsThinking() && !!$("#thinking-sel")?.checked;
}

function modelSupportsThinking() {
  return $("#model-sel")?.selectedOptions?.[0]?.dataset.thinkingToggle === "1";
}

function syncThinkingControl() {
  const control = $("#thinking-control");
  const input = $("#thinking-sel");
  if (!control || !input) return;
  const supported = modelSupportsThinking();
  control.hidden = !supported;
  input.disabled = !supported;
  const saved = localStorage.getItem("studio_thinking");
  setThinkingEnabled(
    supported && (saved === null ? true : saved === "1"),
    { remember: false },
  );
}

function setThinkingEnabled(enabled, { remember = true } = {}) {
  const input = $("#thinking-sel");
  const trigger = $("#thinking-toggle");
  if (!input || !trigger) return;
  input.checked = !!enabled;
  trigger.setAttribute("aria-checked", enabled ? "true" : "false");
  if (remember) localStorage.setItem("studio_thinking", enabled ? "1" : "0");
}

async function send() {
  const q = $("#q").value.trim();
  if (!q) { $("#q").focus(); return; }
  if (!$("#model-sel").value) {
    await setModelDialog(true);
    return;
  }
  if (fontFiles.length && !$("#font-license-ack")?.checked) {
    alert("请先确认拥有所上传字体用于演示与嵌入交付的权利");
    setFontPanel(true);
    return;
  }
  if (!requireAuthentication({ resume: true })) return;
  void guideOptionalMaterialServices();
  beginComposerLaunch(q);
  if (creationMode === "dynamic") {
    return sendSensePresentDazzle(q);
  }
  const slideCount = currentSlideCount();
  const body = new FormData();        // FormData:同时带表单字段 + 附件(multipart);FastAPI Form/File 都收
  body.set("query", q);
  body.set("model", $("#model-sel").value);
  body.set("pipeline", $("#pipeline-sel").value);
  body.set("skill", $("#skill-sel").value);
  body.set("ppt_output", "static_html");
  body.set("slide_count", slideCount);   // 0 = 自由发挥;>0 = 目标页数(上限 18)
  body.set("theme", $("#theme-sel") ? $("#theme-sel").value : "");
  body.set("style", $("#style-sel") ? $("#style-sel").value : "");
  body.set("scheme", $("#scheme-sel") ? $("#scheme-sel").value : "");   // 主题/风格/色系 = 生成偏好(空=自动)
  body.set("thinking", thinkingEnabled() ? "1" : "0");
  body.set("attachment_mode", attachmentMode());
  body.set("font_roles", JSON.stringify(fontRoleConfig()));
  body.set("font_license_ack", $("#font-license-ack")?.checked ? "1" : "0");
  if (pipelineHasCap("attachments")) attachFiles.forEach((f) => body.append("files", f));  // 仅支持附件的管线才带
  if (pipelineHasCap("custom_fonts")) fontFiles.forEach((f) => body.append("font_files", f));
  $("#send").disabled = true;
  try {
    const r = await fetch("/api/decks", { method: "POST", body });
    if (r.status === 401) {
      isAuthenticated = false;
      clearComposerLaunchMorph({ restore: true });
      setAuthGate(true, { resume: true });
      return;
    }
    if (!r.ok) {
      let msg = await r.text();
      try { msg = JSON.parse(msg).detail || msg; } catch {}
      throw new Error(msg);
    }
    const j = await r.json();
    $("#q").value = ""; resetGrowingTextarea($("#q"));
    clearAttachFiles();
    clearFontFiles();
    await openDeck(j.deck_id);          // 输入台平滑收拢到 00 会话,右侧过程栏全程直播
    refreshDeckListWhenIdle(j.deck_id);
  } catch (e) {
    clearComposerLaunchMorph({ restore: true });
    alert("提交失败: " + e.message);
  }
  finally { $("#send").disabled = false; }
}

async function sendSensePresentDazzle(query) {
  const button = $("#send");
  const body = new FormData();
  body.set("query", query);
  body.set("model", $("#model-sel").value);
  body.set("skill", "sense-present-dazzle");
  body.set("ppt_output", "dynamic_html");
  body.set("slide_count", currentSlideCount());
  body.set("theme", $("#theme-sel")?.value || "");
  body.set("style", $("#style-sel")?.value || "");
  body.set("scheme", $("#scheme-sel")?.value || "");
  body.set("thinking", thinkingEnabled() ? "1" : "0");
  button.disabled = true;
  try {
    const response = await fetch("/api/decks", { method: "POST", body });
    if (response.status === 401) {
      isAuthenticated = false;
      clearComposerLaunchMorph({ restore: true });
      setAuthGate(true, { resume: true });
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || payload.error || "动态生成提交失败");
    $("#q").value = ""; resetGrowingTextarea($("#q"));
    clearAttachFiles();
    await openDeck(payload.deck_id);
    refreshDeckListWhenIdle(payload.deck_id);
  } catch (error) {
    clearComposerLaunchMorph({ restore: true });
    alert("提交失败: " + error.message);
  } finally {
    button.disabled = false;
  }
}

async function sendDynamic(query) {
  const button = $("#send");
  const model = $("#model-sel").value;
  if (!model) { clearComposerLaunchMorph({ restore: true }); alert("当前没有可用的动态模型"); return; }
  button.disabled = true;
  try {
    const r = await fetch("/api/dynamic/send", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: query,
        model,
        skill: "dazzle-deck",
        slide_count: currentSlideCount(),
        theme: $("#theme-sel")?.value || "",
        style: $("#style-sel")?.value || "",
        scheme: $("#scheme-sel")?.value || "",
        thinking: thinkingEnabled(),
      }),
    });
    if (r.status === 401) {
      isAuthenticated = false;
      clearComposerLaunchMorph({ restore: true });
      setAuthGate(true, { resume: true });
      return;
    }
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || j.error || "动态生成提交失败");
    $("#q").value = ""; resetGrowingTextarea($("#q"));
    await openDynamic(j.conv_id, { subscribeNow: true });
    refreshDeckListWhenIdle(j.conv_id);
  } catch (e) {
    clearComposerLaunchMorph({ restore: true });
    alert("提交失败: " + e.message);
  }
  finally { button.disabled = false; }
}

function currentSlideCount() {
  const lv = $("#length-sel") ? $("#length-sel").value : "0";
  return lv === "custom"
    ? Math.min(18, Math.max(0, parseInt($("#length-num").value) || 0))
    : (parseInt(lv) || 0);
}

/* ---------------- 批量生成 ---------------- */
const BATCH_STATUS_LABEL = {
  queued: "排队中", running: "生成中", completed: "全部完成",
  partial: "部分完成", failed: "全部失败",
};
let selectedBatchId = null;
let batchTimer = null;

function batchStatusText(batch) {
  return BATCH_STATUS_LABEL[batch.status] || batch.status;
}

async function loadBatches(selectId = selectedBatchId) {
  const box = $("#batch-history");
  try {
    const { batches } = await jget("/api/batches");
    if (!batches.length) {
      box.innerHTML = '<div class="muted small">还没有批次</div>';
      $("#batch-detail").hidden = true;
      return;
    }
    box.innerHTML = batches.map((batch) => `
      <button type="button" class="batch-history-item ${batch.id === selectId ? "active" : ""}" data-id="${batch.id}">
        <span><strong>${escapeHtml(batch.name || `批次 ${batch.id}`)}</strong>
          <small>${escapeHtml(batch.model_label || batch.model)} · ${batch.total} 条</small></span>
        <em class="s-${batch.status}">${batchStatusText(batch)}</em>
      </button>`).join("");
    $$(".batch-history-item", box).forEach((button) => {
      button.onclick = () => selectBatch(+button.dataset.id);
    });
    const target = selectId || batches[0].id;
    if (target) await selectBatch(target);
  } catch (e) {
    box.innerHTML = `<div class="batch-error">批次加载失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderBatchDetail(batch) {
  selectedBatchId = batch.id;
  $$(".batch-history-item").forEach((item) =>
    item.classList.toggle("active", +item.dataset.id === batch.id));
  $("#batch-detail").hidden = false;
  $("#batch-detail-name").textContent = batch.name || `批次 ${batch.id}`;
  $("#batch-detail-meta").textContent =
    `${batch.model_label || modelLabel(batch.model)} · ${SKILL_LABEL[batch.skill_version] || batch.skill_version} · ${batch.total} 条`;
  const c = batch.counts;
  const finished = c.completed + c.failed;
  $("#batch-meter-bar").style.width = `${batch.total ? Math.round(finished / batch.total * 100) : 0}%`;
  $("#batch-counts").innerHTML =
    `<span>完成 <b>${c.completed}</b></span><span>生成中 <b>${c.running}</b></span>` +
    `<span>排队 <b>${c.queued}</b></span><span>失败 <b>${c.failed}</b></span>`;
  $("#batch-decks").innerHTML = (batch.decks || []).map((deck) => `
    <button type="button" class="batch-deck" data-id="${deck.id}">
      <span class="batch-deck-index">${String(deck.batch_index || 0).padStart(2, "0")}</span>
      <span class="batch-deck-title">${escapeHtml(deck.title || "未命名")}</span>
      <span class="batch-deck-status s-${deck.status}">${SKILL_LABEL[deck.skill_version] || deck.skill_version || ""} · ${STATUS_LABEL[deck.status] || deck.status}</span>
    </button>`).join("");
  $$(".batch-deck", $("#batch-decks")).forEach((button) => {
    button.onclick = () => {
      closeBatchModal();
      openDeck(+button.dataset.id);
    };
  });
  const download = $("#batch-download");
  download.hidden = !batch.download_ready;
  download.href = batch.download_ready ? `/api/batches/${batch.id}/download` : "#";
}

async function selectBatch(id) {
  if (batchTimer) { clearTimeout(batchTimer); batchTimer = null; }
  selectedBatchId = id;
  try {
    const batch = await jget(`/api/batches/${id}`);
    renderBatchDetail(batch);
    if (!batch.terminal && !$("#batch-modal").hidden) {
      batchTimer = setTimeout(() => selectBatch(id), 3000);
    }
  } catch (e) {
    $("#batch-error").hidden = false;
    $("#batch-error").textContent = "批次详情加载失败：" + e.message;
  }
}

async function openBatchModal() {
  if (!requireAuthentication()) return;
  if (!$("#model-sel").value) {
    await setModelDialog(true);
    return;
  }
  $("#batch-error").hidden = true;
  const mainModel = $("#model-sel").value;
  if ($(`#batch-model option[value="${mainModel}"]`)) $("#batch-model").value = mainModel;
  const mainSkill = $("#skill-sel").value;
  if ($(`#batch-skill option[value="${mainSkill}"]`)) $("#batch-skill").value = mainSkill;
  $("#batch-attachment-mode").value = attachmentMode();
  $("#batch-modal").hidden = false;
  await loadBatches();
}

function closeBatchModal() {
  $("#batch-modal").hidden = true;
  if (batchTimer) { clearTimeout(batchTimer); batchTimer = null; }
}

async function submitBatch(event) {
  event.preventDefault();
  if (!$("#batch-model").value) {
    await setModelDialog(true);
    return;
  }
  const file = $("#batch-file").files[0];
  if (!file) { $("#batch-file").focus(); return; }
  const body = new FormData($("#batch-form"));
  body.set("model", $("#batch-model").value);
  body.set("skill", $("#batch-skill").value);
  body.set("attachment_mode", $("#batch-attachment-mode").value);
  body.set("slide_count", currentSlideCount());
  body.set("theme", $("#theme-sel")?.value || "");
  body.set("style", $("#style-sel")?.value || "");
  body.set("scheme", $("#scheme-sel")?.value || "");
  body.set("thinking", thinkingEnabled() ? "1" : "0");
  const button = $("#batch-submit");
  button.disabled = true;
  button.textContent = "正在创建…";
  $("#batch-error").hidden = true;
  try {
    const response = await fetch("/api/batches", { method: "POST", body });
    if (!response.ok) {
      let message = await response.text();
      try { message = JSON.parse(message).detail || message; } catch {}
      throw new Error(message);
    }
    const batch = await response.json();
    $("#batch-form").reset();
    $("#batch-attachment-mode").value = attachmentMode();
    renderBatchDetail(batch);
    await loadDecks();
    await loadBatches(batch.id);
  } catch (e) {
    $("#batch-error").hidden = false;
    $("#batch-error").textContent = "提交失败：" + e.message;
  } finally {
    button.disabled = false;
    button.textContent = "开始批量生成";
  }
}

/* ---------------- 编辑器 ---------------- */
function closeEditor({ showComposerView = true } = {}) {
  setProcRailOpen(false);
  if (ed.sse) { ed.sse.close(); ed.sse = null; }
  if (ed.timer) { clearInterval(ed.timer); ed.timer = null; }
  if (ed.dynamicPollTimer) { clearTimeout(ed.dynamicPollTimer); ed.dynamicPollTimer = null; }
  stopLive();
  ed.id = null;
  const frame = $("#canvas-deck");
  frame.hidden = true; frame.src = "about:blank";
  delete frame.dataset.deckKind;
  delete frame.dataset.deckId;
  delete frame.dataset.pendingSlide;
  if (location.hash.match(/^#(?:static|dynamic)-/)) history.replaceState(null, "", location.pathname + location.search);
  if (showComposerView) showComposer();
}

async function openDeck(id) {
  clearStudioNotices();
  closeEditor({ showComposerView: false });
  history.replaceState(null, "", `#static-${encodeURIComponent(id)}`);
  $$("#deck-list .deck-item").forEach((a) => a.classList.toggle("active", a.dataset.id == id));
  stopLive();
  Object.assign(ed, { id, kind: "static", total: 0, rendered: new Set(), sel: null, follow: true, status: null,
                      lastPhase: null, feedSeen: new Set(), finalized: false, planCache: {},
                      ppCollapsed: true,
                      feed: {}, pageAgents: {}, workspaceView: "process", viewMode: "ppt",
                      specialistArtifacts: {}, agentTimings: {}, overallTiming: null,
                      staticDeckUrl: trajectoryMode ? trajectoryDeckApi(id, "/files/present.html") : `/api/decks/${id}/files/present.html`, staticDeckReady: false,
                      staticDeckFinal: false, staticHtmlStamp: 0, staticStamp: Date.now(),
                      fullQuery: "",
                      briefMeta: {}, attachments: [], staticFollowups: [], staticConversationTurns: [],
                      conKey: null, conLines: [],
                      rtimes: {}, staticDirtyPages: new Set(), phLines: {}, historyRequest: 0,
                      pageHistoryCache: new Map(), pageHistoryPending: new Map(),
                      historyScopePage: null, historyRenderKey: "", speechRenderKey: "",
                      speechScopePage: null, speechRequest: 0, speechPages: new Map(), hasAnySpeech: false, stopRequested: false });
  $("#editor").dataset.kind = "static";
  setRailLabels("static");
  $("#plan-panel").hidden = true;
  $("#pr-feed").innerHTML = "";
  $("#pr-act").textContent = "";
  $("#pr-count").textContent = "–";
  $("#pr-bar").style.width = "0%";
  $$("#pr-steps li").forEach((li) => (li.className = ""));
  buildFilmstrip();                       // 含 00 · 过程帧(total 未知时也先有它)
  $("#canvas-img").hidden = true;
  $("#canvas-deck").hidden = true;
  $("#canvas-empty").hidden = false;
  $("#canvas-empty .ce-text").textContent = "正在筹备…";
  $("#follow-btn").classList.add("on");
  const progressPromise = jget(trajectoryMode ? trajectoryDeckApi(id, "/progress") : `/api/decks/${id}/progress`);
  const workspaceTransition = transitionComposerToEditor();
  let p;
  try { p = await progressPromise; }
  catch {
    await workspaceTransition;
    showStudioNotice("演示加载失败，请稍后重试", { type: "error" });
    finishComposerLaunchMorph();
    return;
  }
  $("#ed-title").textContent = p.title || "演示文稿";
  apply(p);
  setTopbar(
    p.title || "演示文稿",
    `${editorPresentationKind() === "dynamic" ? "动态" : "静态"}演示 · AI 生成内容请注意核验`,
  );
  if (isStaticActiveStatus(p.status)) {
    if (trajectoryMode) {
      ed.timer = setInterval(async () => {
        if (ed.id !== id) return;
        try { apply(await jget(trajectoryDeckApi(id, "/progress"))); } catch {}
      }, 2000);
    } else {
      subscribe(id);
      ed.timer = setInterval(tick, 1000);
    }
    startLive();
  }
  await workspaceTransition;
  finishComposerLaunchMorph();
}

/* ---------------- 动态演示：复用同一套编辑器外壳 ---------------- */
function setRailLabels(kind) {
  const labels = kind === "dynamic"
    ? [["理解与规划", "明确叙事结构与动态策略"], ["素材与风格", "准备事实、图像与视觉方向"], ["页面制作", "逐页编排布局、动效与交互"], ["质量检查", "渲染复审与全局一致性检查"], ["完成", ""]]
    : [["理解与规划", "明确叙事结构与页面目标"], ["素材与风格", "准备事实、图像与视觉方向"], ["页面制作", "逐页完成内容、排版与渲染"], ["质量检查", "成稿复审与全局一致性检查"], ["完成", ""]];
  $$("#pr-steps li").forEach((li, i) => {
    li.querySelector("b").textContent = labels[i][0];
    li.querySelector("i").textContent = labels[i][1];
  });
  $("#pr-countlabel").textContent = "页面已完成";
  const runtime = $("#pr-runtime");
  if (runtime) {
    runtime.dataset.kind = kind;
    runtime.querySelector("b").textContent = kind === "dynamic" ? "连续动态创作" : "多页面同步制作";
    runtime.querySelector("i").textContent = kind === "dynamic"
      ? "从整体规划到逐页动效，由同一创作流程持续推进"
      : "完成整体规划后，多页内容与视觉会同步推进";
  }
}

async function openDynamic(convId, { subscribeNow = false } = {}) {
  clearStudioNotices();
  closeEditor({ showComposerView: false });
  history.replaceState(null, "", `#dynamic-${encodeURIComponent(convId)}`);
  $$("#deck-list .deck-item").forEach((a) => a.classList.toggle("active", a.dataset.kind === "dynamic" && a.dataset.id === convId));
  Object.assign(ed, {
    id: convId, kind: "dynamic", total: 0, rendered: new Set(), sel: null, follow: true,
    status: "running", fullQuery: "", dynamicDeckUrl: "", dynamicLogs: [], dynamicSeq: new Set(),
    dynamicPhase: "plan", dynamicStamp: Date.now(), dynamicStartedAt: Date.now() / 1000,
    elapsedBase: 0, elapsedAt: Date.now(), planCache: {}, ppCollapsed: true,
    workspaceView: "process", viewMode: "ppt",
    dynamicModelLabel: "动态模型", dynamicFeedSeen: new Set(), dynamicError: "",
    briefMeta: {}, attachments: [], pageAgents: {}, dynamicFollowups: [], dynamicChatTurns: [],
    agentTimings: {}, overallTiming: null,
    speechRenderKey: "", speechScopePage: null, speechRequest: 0, speechPages: new Map(), hasAnySpeech: false, stopRequested: false,
  });
  $("#editor").dataset.kind = "dynamic";
  setRailLabels("dynamic");
  $("#plan-panel").hidden = true;
  $("#pr-feed").innerHTML = ""; $("#pr-act").textContent = "正在启动动态 Agent…"; $("#pr-elapsed").textContent = "";
  $("#pr-count").textContent = "–"; $("#pr-bar").style.width = "4%";
  $$("#pr-steps li").forEach((li) => (li.className = ""));
  $("#canvas-img").hidden = true; $("#canvas-deck").hidden = true;
  buildFilmstrip(); select(0, { byUser: false });
  const conversationPromise = jget(`/api/dynamic/conversation?conv_id=${encodeURIComponent(convId)}`);
  const workspaceTransition = transitionComposerToEditor();
  let data;
  try { data = await conversationPromise; }
  catch (e) {
    await workspaceTransition;
    showStudioNotice(`演示加载失败：${e.message}`, { type: "error" });
    finishComposerLaunchMorph();
    return;
  }
  $("#ed-title").textContent = data.meta?.title || "动态演示";
  setTopbar(data.meta?.title || "动态演示", "动态演示 · AI 生成内容请注意核验");
  ed.dynamicModelLabel = data.meta?.model_label || data.meta?.model_key || "动态模型";
  ed.briefMeta = { ...(data.meta || {}), skill_version: "dazzle-deck", thinking: data.meta?.thinking === true };
  if (data.meta?.user_query) ed.fullQuery = data.meta.user_query;
  const events = data.events || [];
  const eventTimes = events.map((event) => Number(event.ts)).filter(Number.isFinite);
  if (eventTimes.length) ed.dynamicStartedAt = eventTimes[0];
  const terminalEvent = [...events].reverse().find((event) => event.kind === "done" || event.kind === "error");
  const dynamicEndedAt = data.active
    ? Date.now() / 1000
    : Number(terminalEvent?.ts || eventTimes.at(-1) || ed.dynamicStartedAt);
  ed.elapsedBase = Math.max(0, Math.round(dynamicEndedAt - ed.dynamicStartedAt));
  ed.elapsedAt = Date.now();
  ed.dynamicHydrating = true;
  try {
    events.forEach((event) => applyDynamicEvent(event, { historical: true }));
  } finally {
    ed.dynamicHydrating = false;
  }
  syncOutlineBrief();
  syncOutlineConversationTurns();
  if (!data.active && ed.status === "running") ed.status = data.meta?.status || "interrupted";
  if (data.active || subscribeNow) {
    ed.status = "running"; subscribeDynamic(convId); ed.timer = setInterval(tick, 1000);
  }
  renderDynamicChrome();
  renderDynamicConsole();
  await workspaceTransition;
  finishComposerLaunchMorph();
}

function dynamicLog(text, kind = "text", timestamp = null) {
  if (!text) return;
  const entry = { text: String(text).replace(/\s+/g, " ").trim().slice(0, 260), kind, created_at: timestamp };
  ed.dynamicLogs.push(entry);
  ed.dynamicChatTurns ||= [];
  if (kind === "user") {
    ed.dynamicChatTurns.push({ role: "user", content: entry.text, created_at: timestamp });
    ed.dynamicChatTurns.push({ role: "assistant", logs: [], current: true, status: "running", created_at: timestamp });
    ed.dynamicChatTurns.slice(0, -1).forEach((turn) => {
      if (turn.role === "assistant") turn.current = false;
    });
  } else {
    let assistant = [...ed.dynamicChatTurns].reverse().find((turn) => turn.role === "assistant");
    if (!assistant) {
      assistant = { role: "assistant", logs: [], current: true, status: "running", created_at: timestamp };
      ed.dynamicChatTurns.push(assistant);
    }
    assistant.logs.push(entry);
  }
  if (ed.kind === "dynamic" && !ed.dynamicHydrating) {
    if (kind === "user") syncOutlineConversationTurns();
    renderDynamicConsole();
  }
}

function dynamicPhaseForTool(name, args = {}) {
  if (["read_file"].includes(name)) return "plan";
  if (["image_generate", "web_search", "web_fetch"].includes(name)) return "research";
  if (name === "vision_analyze" || (name === "bash" && /--all/.test(args.command || ""))) return "verify";
  return "render";
}

function requestedSlideCount(text) {
  const value = String(text || "");
  const preferred = value.match(/目标篇幅[：:]\s*(?:约\s*)?(\d+)\s*页/);
  const natural = value.match(/(?:做|制作|生成|需要|共|约|一套)?\s*(\d+)\s*页(?:的|PPT|ppt|演示|幻灯片)?/);
  const count = Number(preferred?.[1] || natural?.[1] || 0);
  return Number.isInteger(count) && count > 0 && count <= 40 ? count : 0;
}

function advanceDynamicPhase(nextPhase) {
  if (nextPhase === "done") { ed.dynamicPhase = "done"; return; }
  const current = STEP_ORDER.indexOf(ed.dynamicPhase);
  const next = STEP_ORDER.indexOf(nextPhase);
  if (next >= 0 && next >= current) ed.dynamicPhase = nextPhase;
}

function applyDynamicEvent(event, { historical = false } = {}) {
  if (ed.kind !== "dynamic" || (event.conv_id && event.conv_id !== ed.id)) return;
  if (event.seq && ed.dynamicSeq.has(event.seq)) return;
  if (event.seq) ed.dynamicSeq.add(event.seq);
  const k = event.kind;
  if (k === "user") {
    ed.fullQuery = ed.briefMeta?.user_query || event.text || ed.fullQuery;
    const requested = requestedSlideCount(event.text);
    if (requested && !ed.total) { ed.total = requested; buildFilmstrip(); }
    if (event.first !== false) {
      $("#ed-title").textContent = (event.text || "动态演示").slice(0, 56);
      setTopbar((event.text || "动态演示").slice(0, 56), "动态演示 · AI 生成内容请注意核验");
    } else if (event.text && !ed.dynamicFollowups?.includes(event.text)) {
      ed.dynamicFollowups ||= [];
      ed.dynamicFollowups.push(event.text);
    }
    dynamicLog(event.text, "user", event.ts);
    syncOutlineBrief();
    syncOutlineConversationTurns();
  } else if (k === "assistant_text") {
    dynamicLog(event.text, "text", event.ts);
  } else if (k === "tool_call") {
    advanceDynamicPhase(dynamicPhaseForTool(event.name, event.args || {}));
    const action = event.label || TOOL_LABEL[event.name] || "推进制作任务";
    const source = event.args?.path || event.args?.prompt || "";
    const detail = source ? compactActivityHint(source) : "";
    dynamicLog(`${action}${detail ? " · " + detail : ""}`, "tool", event.ts);
    if (!historical) feedAdd(`<span class="pr-txt">${escapeHtml(action)}</span>`);
  } else if (k === "tool_result") {
    if (!event.ok && !historical) feedAdd(`<span class="pr-txt pr-bad">${escapeHtml(event.label || event.name)}未通过，正在自动调整</span>`);
    dynamicLog(event.ok ? "当前步骤已完成" : "发现问题，正在自动修正", event.ok ? "result" : "error", event.ts);
  } else if (k === "vision") {
    advanceDynamicPhase("verify"); dynamicLog(`检查画面 · ${event.path || "渲染图"}`, "tool", event.ts);
  } else if (k === "deck_update" || k === "page_rendered") {
    if (event.deck_url) ed.dynamicDeckUrl = event.deck_url;
    if (event.n_pages) ed.total = Math.max(ed.total, +event.n_pages || 0);
    if (event.slide) ed.rendered.add(+event.slide);
    else if (k === "page_rendered" && event.n_pages) for (let i = 1; i <= event.n_pages; i++) ed.rendered.add(i);
    ed.dynamicStamp = Date.now(); advanceDynamicPhase(k === "page_rendered" ? "verify" : "render");
    buildFilmstrip();
    if (event.slide && ed.follow && ed.workspaceView === "ppt" && ed.viewMode !== "console") select(+event.slide, { byUser: false });
    if (k === "page_rendered") {
      const pages = event.slide
        ? [+event.slide]
        : (event.n_pages ? Array.from({ length: +event.n_pages }, (_, index) => index + 1) : []);
      pages.forEach((n) => {
        const key = `page:${n}`;
        if (ed.dynamicFeedSeen.has(key)) return;
        ed.dynamicFeedSeen.add(key);
        feedAdd(`<img class="pr-thumb" src="${fileUrl(n)}" alt=""><span class="pr-txt">第 ${n} 页渲染完成</span>`, "slide", event.ts);
      });
    }
  } else if (k === "note") {
    dynamicLog(event.text, "note", event.ts);
  } else if (k === "error") {
    dynamicLog(event.message, "error", event.ts);
    ed.dynamicError = event.message || "动态生成失败";
    ed.status = "error";
    const assistant = [...(ed.dynamicChatTurns || [])].reverse().find((turn) => turn.role === "assistant");
    if (assistant) { assistant.status = "error"; assistant.responded_at = event.ts; }
    notifyRunFailure(ed.dynamicError, "动态演示生成失败");
    if (ed.timer) { clearInterval(ed.timer); ed.timer = null; }
  } else if (k === "final") {
    dynamicLog(event.text, "final", event.ts);
  } else if (k === "done") {
    const eventStatus = event.status === "completed" ? "completed" : (event.status || "failed");
    ed.status = ed.stopRequested ? "stopped" : eventStatus;
    if (ed.status === "completed") {
      advanceDynamicPhase("done");
      for (let n = 1; n <= ed.total; n++) ed.rendered.add(n);
      buildFilmstrip();
    }
    const endedAt = Number(event.ts);
    if (Number.isFinite(endedAt)) {
      ed.elapsedBase = Math.max(0, Math.round(endedAt - ed.dynamicStartedAt));
      ed.elapsedAt = Date.now();
    }
    if (!ed.dynamicFeedSeen.has("done")) {
      ed.dynamicFeedSeen.add("done");
      const duration = ed.elapsedBase != null ? ` · 用时 ${fmtDur(ed.elapsedBase)}` : "";
      if (ed.status === "completed")
        feedAdd(`<span class="pr-txt pr-ok">✓ 全部完成 · 共 ${ed.total || ed.rendered.size} 页${duration}</span>`, "", event.ts);
    }
    if (ed.status !== "completed") {
      const failureText = ed.dynamicError || (ed.status === "stopped" ? "生成已中断" : `生成未完成 · ${STATUS_LABEL[ed.status] || ed.status}`);
      notifyRunFailure(failureText, ed.status === "stopped" ? "生成已中断" : "动态演示生成失败");
    }
    if (ed.timer) { clearInterval(ed.timer); ed.timer = null; }
    const assistant = [...(ed.dynamicChatTurns || [])].reverse().find((turn) => turn.role === "assistant");
    if (assistant) { assistant.status = ed.status; assistant.responded_at = event.ts; }
    if (ed.status === "completed" && Number(ed.sel) > 0) {
      // speech.md is often written during final packaging, after the last page
      // render event. Probe once more; the drawer stays absent when none exists.
      setTimeout(() => loadDynamicPageSpeech(Number(ed.sel), { quiet: true }), 0);
    }
    loadDecks(ed.id);
  }
  if (!historical) renderDynamicChrome();
}

function renderDynamicChrome() {
  if (ed.kind !== "dynamic") return;
  const completed = ed.status === "completed";
  const running = ed.status === "running";
  const failed = !running && !completed;
  const phaseLabel = { plan: "理解需求", research: "风格与素材", render: "逐页编排", verify: "渲染复审", done: "完成" }[ed.dynamicPhase] || "制作中";
  const elapsedSeconds = currentElapsedSeconds();
  const elapsed = elapsedSeconds != null ? fmtDur(elapsedSeconds) : "";
  const status = $("#ed-status");
  const chromeKey = JSON.stringify([ed.id, ed.dynamicModelLabel, ed.status, ed.dynamicPhase]);
  if (status.dataset.dynamicChromeKey !== chromeKey) {
    status.dataset.dynamicChromeKey = chromeKey;
    status.innerHTML = `<span class="badge ${completed ? "ph-done" : (failed ? "ph-failed" : "")}">${completed ? "完成" : (failed ? STATUS_LABEL[ed.status] || ed.status : phaseLabel)}</span>`;
  }
  if ($("#pr-elapsed").textContent !== elapsed) $("#pr-elapsed").textContent = elapsed;
  const idx = STEP_ORDER.indexOf(ed.dynamicPhase);
  $$("#pr-steps li").forEach((li) => { const i = STEP_ORDER.indexOf(li.dataset.k); li.className = i < idx || completed ? "done" : (i === idx ? "now" : ""); });
  $("#pr-count").innerHTML = ed.total ? `${ed.rendered.size}<small>/${ed.total}</small>` : "–";
  const pct = ed.total ? Math.round(ed.rendered.size / ed.total * 100) : (completed ? 100 : 6);
  $("#pr-bar").style.width = `${completed ? 100 : pct}%`; $("#ed-bar").style.width = `${completed ? 100 : Math.max(6, pct)}%`;
  $("#pr-act").textContent = running ? (ed.dynamicLogs.at(-1)?.text || "正在准备…") : "";
  renderActions(ed.status);
  updateNav();
  syncTaskComposerState();
  syncTaskConfig();
  syncViewToggle();
}

function subscribeDynamic(convId) {
  if (ed.sse) ed.sse.close();
  const es = new EventSource(`/api/dynamic/stream?conv_id=${encodeURIComponent(convId)}`); ed.sse = es;
  es.onmessage = (message) => {
    if (ed.kind !== "dynamic" || ed.id !== convId) { es.close(); return; }
    try { applyDynamicEvent(JSON.parse(message.data)); } catch {}
  };
  es.addEventListener("end", () => { es.close(); if (ed.sse === es) ed.sse = null; });
}

function startDynamicConversationPoll(convId) {
  if (ed.dynamicPollTimer) clearTimeout(ed.dynamicPollTimer);
  const poll = async () => {
    if (ed.kind !== "dynamic" || String(ed.id) !== String(convId)) return;
    try {
      const data = await jget(`/api/dynamic/conversation?conv_id=${encodeURIComponent(convId)}`);
      (data.events || []).forEach((event) => applyDynamicEvent(event));
      if (!data.active) {
        if (ed.status === "running") ed.status = data.meta?.status || "interrupted";
        renderDynamicChrome();
        ed.dynamicPollTimer = null;
        return;
      }
    } catch {}
    ed.dynamicPollTimer = setTimeout(poll, 1400);
  };
  ed.dynamicPollTimer = setTimeout(poll, 320);
}

function renderDynamicConsole() {
  if (ed.kind !== "dynamic" || ed.dynamicHydrating || !$("#gen-console")) return;
  const body = $("#agent-progress-console") || $("#gen-console");
  const currentTurn = [...(ed.dynamicChatTurns || [])].reverse().find((turn) => turn.role === "assistant");
  const events = (currentTurn?.logs || ed.dynamicLogs).flatMap((entry) => {
    if (entry.kind === "user") return [];
    if (entry.kind === "tool") return [{ k: "tool", tool: "dynamic_action", hint: entry.text }];
    if (entry.kind === "result") return [];
    return [{ k: "text", s: entry.text }];
  });
  renderAgentProgress(body, events, {
    running: ed.status === "running",
    rendered: ed.sel > 0 && ed.rendered.has(ed.sel),
    n: ed.sel || 0,
    source: `dynamic:${ed.sel || 0}`,
  });
}

function apply(p) {
  if (ed.id == null) return;
  if (ed.stopRequested) p = { ...p, status: "interrupted", error: "用户已停止" };
  ed.status = p.status;
  // 正式 present.html 或任意单页 HTML 均可进入实时 PPT。生成期由后端
  // 即时拼装临时播放器，finalize 后同一路径无缝切换到正式文件。
  const nextHtmlStamp = Number(p.html_stamp) || 0;
  const htmlPreviewChanged = !!nextHtmlStamp && nextHtmlStamp !== ed.staticHtmlStamp;
  ed.staticDeckReady = !!p.html_ready;
  ed.staticDeckFinal = !!p.html_final;
  ed.pptOutput = p.ppt_output || ed.pptOutput || "static_html";
  if (p.html_entry) ed.staticDeckUrl = trajectoryMode
    ? trajectoryDeckApi(ed.id, `/files/${p.html_entry}`)
    : `/api/decks/${ed.id}/files/${p.html_entry}`;
  const presentationKind = editorPresentationKind();
  $("#editor").dataset.output = presentationKind;
  setRailLabels(presentationKind);
  if (nextHtmlStamp) {
    ed.staticHtmlStamp = nextHtmlStamp;
    ed.staticStamp = nextHtmlStamp;
  }
  if (typeof p.query === "string") ed.fullQuery = p.query;
  ed.briefMeta = {
    ...(ed.briefMeta || {}), model: p.model, pipeline: p.pipeline,
    skill_version: p.skill_version, slide_count: p.slide_count,
    theme: p.theme, style: p.style, scheme: p.scheme, user_query: p.user_query,
    runtime_limits: p.runtime_limits || ed.briefMeta?.runtime_limits,
    thinking: p.thinking === true,
    parent_deck_id: p.parent_deck_id, revision_no: p.revision_no,
    revision_supported: p.revision_supported,
  };
  if (typeof p.revision_instruction === "string" && p.revision_instruction.trim()) {
    ed.staticFollowups = [p.revision_instruction.trim()];
  }
  if (Array.isArray(p.conversation_turns)) ed.staticConversationTurns = p.conversation_turns;
  if (Array.isArray(p.attachments)) ed.attachments = p.attachments;
  syncOutlineBrief();
  syncOutlineConversationTurns();
  ed.elapsedBase = p.elapsed_s ?? null; ed.elapsedAt = Date.now();

  // 总页数(计划出来前未知,先用已渲数撑起胶片条)
  const known = p.slides_total || 0;
  const maxR = p.rendered.length ? Math.max(...p.rendered) : 0;
  const total = Math.max(known, maxR, p.slides_authored || 0);
  if (total !== ed.total) { ed.total = total; buildFilmstrip(); }

  // 新渲染好的页:替换占位 → 显影;已显示的页被重渲覆盖 → 换新图
  const rtimes = p.rtimes || {};
  for (const n of p.rendered) {
    const k = String(n);
    if (!ed.rendered.has(n)) {
      ed.rendered.add(n);
      ed.rtimes[k] = rtimes[k] || 0;
      ed.staticDirtyPages?.add(n);
      fillFrame(n);
      if (ed.follow && ed.workspaceView === "ppt" && ed.viewMode !== "console") select(n, { byUser: false });
    } else if (rtimes[k] && rtimes[k] !== ed.rtimes[k]) {
      ed.rtimes[k] = rtimes[k];
      ed.staticDirtyPages?.add(n);
      refreshFrame(n);
    }
  }

  // html_stamp belongs to the whole provisional player: another page being
  // authored also changes it. Keep the selected iframe stable unless that
  // selected page itself has a new successful render. Dirty background pages
  // are picked up lazily when the user navigates to them.
  if (htmlPreviewChanged && ed.staticDirtyPages?.has(Number(ed.sel))
      && ed.workspaceView === "ppt" && ed.viewMode !== "console"
      && ed.sel > 0 && ed.rendered.has(ed.sel)) {
    showStaticDeck(ed.sel);
  }

  // 顶栏状态 + 右侧过程栏 + 进度线
  const phase = p.phase || "starting";
  renderStatus(p, phase);
  railApply(p, phase);
  const pct = ed.total ? Math.round((p.slides_rendered / ed.total) * 100)
    : (p.status === "completed" ? 100 : 4);
  $("#ed-bar").style.width = (p.status === "completed" ? 100 : pct) + "%";

  // 画布空态文案:阶段 + 引擎此刻在做什么
  if (!ed.rendered.size && !$(".workstream-head", $("#canvas-empty"))) {
    const act = actText(p);
    $("#canvas-empty .ce-text").textContent =
      p.status === "not_started" ? "尚未开始生成"
        : p.status === "waiting" ? "修订已排队，等待上一版完成…"
        : p.status === "queued" ? "排队中…"
        : (PHASE_LABEL[phase] || "筹备中") + "…" + (act ? `  ${act}` : "");
  }

  // 选中页的规划 md 可能刚写出来 → 补拉(含 00=deck.md)
  if (ed.sel != null && !ed.planCache[ed.sel]) loadPlan(ed.sel);

  // 默认落在 00 · 过程帧(开始生成的第一眼就是编排器直播)
  if (ed.sel == null) select(0, { byUser: false });

  // 终态
  if (["completed", "failed", "rejected", "interrupted"].includes(p.status)) {
    if (ed.timer) { clearInterval(ed.timer); ed.timer = null; }
    stopLive();
    if (p.status !== "completed") {
      notifyRunFailure(
        p.error,
        p.status === "interrupted" ? "生成已中断" : (p.status === "rejected" ? "成稿校验未通过" : "静态演示生成失败"),
      );
      if (!ed.rendered.size) $("#canvas-empty .ce-text").textContent = "未能生成成稿";
      const ring = $(".develop-ring");           // 终态视图可能没有加载环
      if (ring) ring.style.display = "none";
    }
    if (p.status === "completed" && ed.workspaceView === "ppt" && ed.sel > 0 && ed.viewMode !== "console") {
      select(ed.sel, { byUser: false });
    }
    loadDecks(ed.id);
  }
  renderActions(p.status);
  updateNav();
  syncTaskConfig();
  syncViewToggle();
}

function taskConfigData() {
  const model = ed.kind === "dynamic"
    ? (ed.dynamicModelLabel || "动态模型")
    : (ed.briefMeta?.model ? modelLabel(ed.briefMeta.model) : "—");
  const skillKey = ed.kind === "dynamic" ? "dazzle-deck" : ed.briefMeta?.skill_version;
  const skill = SKILL_LABEL[skillKey] || (skillKey === "dazzle-deck" ? "Dazzle Deck" : skillKey) || "—";
  const preferences = [ed.briefMeta?.slide_count ? `约 ${ed.briefMeta.slide_count} 页` : "", ed.briefMeta?.theme,
    ed.briefMeta?.style, ed.briefMeta?.scheme].filter(Boolean).join(" · ");
  const thinking = ed.briefMeta?.thinking === true ? "开启" : "关闭";
  const limits = ed.briefMeta?.runtime_limits || {};
  const maxTokens = Number(limits.max_tokens) || 40960;
  const configuredMain = ed.kind === "dynamic"
    ? Number(limits.dynamic_max_turns)
    : Number(limits.static_max_turns);
  const mainTurns = configuredMain > 0 ? configuredMain : 4096;
  const childRaw = Number(limits.static_subagent_max_turns);
  let runtime;
  if (ed.kind === "dynamic") {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent ${mainTurns.toLocaleString()} 轮`;
  } else if (childRaw > 0) {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent ${mainTurns.toLocaleString()} 轮 · 子 Agent ${childRaw.toLocaleString()} 轮`;
  } else if (skillKey === "sn-ppt-web" || skillKey === "mural-presenter") {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent ${mainTurns.toLocaleString()} 轮 · 页面子 Agent 36–120 轮 · 其他子 Agent ${mainTurns.toLocaleString()} 轮`;
  } else if (skillKey === "visual-craft") {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent ${mainTurns.toLocaleString()} 轮 · 页面子 Agent 28 轮 · 其他子 Agent ${mainTurns.toLocaleString()} 轮`;
  } else if (skillKey === "sense-present-standard") {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent 240 轮 · 子 Agent 240 轮`;
  } else {
    runtime = `${maxTokens.toLocaleString()} Tokens/轮 · 主 Agent ${mainTurns.toLocaleString()} 轮 · 子 Agent ${mainTurns.toLocaleString()} 轮`;
  }
  return { model, skill, thinking, preferences, runtime };
}

function syncTaskConfig() {
  const { model, skill, thinking, preferences, runtime } = taskConfigData();
  const summaryNode = $("#pr-config-summary");
  const modelNode = $("#pr-config-model");
  const skillNode = $("#pr-config-skill");
  const thinkingNode = $("#pr-config-thinking");
  const runtimeNode = $("#pr-config-runtime");
  const prefNode = $("#pr-config-pref");
  const prefRow = $("#pr-config-pref-row");
  if (summaryNode) {
    summaryNode.textContent = `深度思考：${thinking === "开启" ? "已开启" : "未开启"}`;
    summaryNode.dataset.enabled = thinking === "开启" ? "1" : "0";
  }
  if (modelNode) modelNode.textContent = model;
  if (skillNode) skillNode.textContent = skill;
  if (thinkingNode) thinkingNode.textContent = thinking;
  if (runtimeNode) runtimeNode.textContent = runtime;
  if (prefNode) prefNode.textContent = preferences || "—";
  if (prefRow) prefRow.hidden = !preferences;
  const outlineModel = $("#outline-config-model");
  const outlineSkill = $("#outline-config-skill");
  const outlineThinking = $("#outline-config-thinking");
  const outlineRuntime = $("#outline-config-runtime");
  const outlinePref = $("#outline-config-pref");
  const outlinePrefRow = $("#outline-config-pref-row");
  const outlineSummary = $("#outline-config-summary");
  if (outlineModel) outlineModel.textContent = model;
  if (outlineSkill) outlineSkill.textContent = skill;
  if (outlineThinking) outlineThinking.textContent = thinking;
  if (outlineRuntime) outlineRuntime.textContent = runtime;
  if (outlinePref) outlinePref.textContent = preferences || "—";
  if (outlinePrefRow) outlinePrefRow.hidden = !preferences;
  if (outlineSummary) outlineSummary.textContent = `${model} · ${skill} · 深度思考：${thinking === "开启" ? "已开启" : "未开启"}`;
}

function renderStatus(p, phase) {
  delete $("#ed-status").dataset.dynamicChromeKey;
  const cls = p.status === "completed" ? "ph-done" : (["failed", "rejected", "interrupted"].includes(p.status) ? "ph-failed" : "");
  const act = (!["completed", "failed", "rejected", "interrupted"].includes(p.status) && actText(p)) || "";
  const badgeLabel = p.status === "interrupted"
    ? STATUS_LABEL.interrupted
    : (PHASE_LABEL[phase] || phase);
  const phaseBadge = ["delegating", "rendering"].includes(phase)
    ? ""
    : `<span class="badge ${cls}">${badgeLabel}</span>`;
  $("#ed-status").innerHTML =
    phaseBadge +
    (act ? `<span class="ed-act">${escapeHtml(act)}</span>` : "");
  syncTaskComposerState();
}

function currentElapsedSeconds() {
  if (ed.elapsedBase == null) return;
  const live = isStaticActiveStatus(ed.status);
  return Math.max(0, Math.round(ed.elapsedBase + (live ? (Date.now() - ed.elapsedAt) / 1000 : 0)));
}

function tick() {   // SSE 间隙本地走秒
  const s = currentElapsedSeconds();
  if (s == null) return;
  const el = $("#ed-elapsed");
  const statusText = `${STATUS_LABEL[ed.status] || ed.status}` +
    (ed.total ? ` · ${ed.rendered.size}/${ed.total} 页` : "");
  if (el && el.textContent !== statusText) el.textContent = statusText;
  if ($("#pr-elapsed").textContent !== fmtDur(s)) $("#pr-elapsed").textContent = fmtDur(s);
}

/* ----- 右侧生成过程栏 ----- */
function feedAdd(html, cls = "", timestamp = null) {
  const item = document.createElement("div");
  item.className = "pr-item " + cls;
  const raw = Number(timestamp);
  const t = Number.isFinite(raw) ? new Date(raw < 1e12 ? raw * 1000 : raw) : new Date();
  item.innerHTML = `<span class="pr-time">${pad2(t.getHours())}:${pad2(t.getMinutes())}</span>${html}`;
  $("#pr-feed").prepend(item);
}

function railDisclosureClosedHeight(details, summary) {
  const style = getComputedStyle(details);
  const border = (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
  return summary.offsetHeight + border;
}

function setRailDisclosureOpen(details, open) {
  const summary = details.querySelector(":scope > summary");
  const body = details.querySelector(":scope > .pr-rail-body");
  if (!summary || !body || details.classList.contains("is-animating")) return;
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reducedMotion || typeof details.animate !== "function") {
    details.open = open;
    return;
  }

  const startHeight = details.offsetHeight;
  if (open) details.open = true;
  const endHeight = open
    ? details.scrollHeight + (parseFloat(getComputedStyle(details).borderTopWidth) || 0)
      + (parseFloat(getComputedStyle(details).borderBottomWidth) || 0)
    : railDisclosureClosedHeight(details, summary);

  details.classList.add("is-animating");
  details.style.height = `${startHeight}px`;
  const shellAnimation = details.animate(
    [{ height: `${startHeight}px` }, { height: `${endHeight}px` }],
    { duration: open ? 380 : 340, easing: "cubic-bezier(.22,.8,.3,1)", fill: "both" },
  );
  const bodyAnimation = body.animate(
    open
      ? [{ opacity: 0, transform: "translateY(-5px) scaleY(.985)" }, { opacity: 1, transform: "none" }]
      : [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(-4px) scaleY(.985)" }],
    { duration: open ? 320 : 250, easing: "cubic-bezier(.22,.8,.3,1)", fill: "both" },
  );
  shellAnimation.finished.catch(() => {}).then(() => {
    if (!open) details.open = false;
    details.classList.remove("is-animating");
    details.style.height = "";
    shellAnimation.cancel();
    bodyAnimation.cancel();
  });
}

function initRailDisclosureAnimations() {
  $$(".pr-rail-section").forEach((details) => {
    const summary = details.querySelector(":scope > summary");
    summary?.addEventListener("click", (event) => {
      event.preventDefault();
      setRailDisclosureOpen(details, !details.open);
    });
  });
}

function syncPageSpeechDrawer(n = Number(ed.sel) || 0) {
  const drawer = $("#pr-speech-section");
  const toggle = $("#page-speech-toggle");
  if (!drawer || !toggle) return;
  // Dynamic decks normally do not ship speech.md, and static Skills can omit
  // it too. Once any page proves that this deck has notes, however, keep the
  // bottom drawer mounted. Page changes then update only its contents instead
  // of making the whole control jump in and out of the preview layout.
  const hasAnySpeech = !!ed.hasAnySpeech
    || [...(ed.speechPages?.values?.() || [])].some(Boolean);
  const visible = !!ed.id && ed.workspaceView === "ppt" && n > 0
    && hasAnySpeech;
  drawer.hidden = !visible;
  // Default to a compact footer. An explicit "0" is written only after the
  // user opens it, so returning users still keep their own preference.
  const collapsed = localStorage.getItem("studio_speech_collapsed") !== "0";
  drawer.classList.toggle("collapsed", collapsed);
  toggle.setAttribute("aria-expanded", String(!collapsed));
}

function setPageSpeechScope(n) {
  syncPageSpeechDrawer(n);
  if (n <= 0) {
    ed.speechScopePage = null;
    ed.speechRenderKey = "";
    return;
  }
  if (ed.speechScopePage === n) return;
  ed.speechScopePage = n;
  ed.speechRenderKey = "";
  const sourceToggle = $("#page-speech-source-toggle");
  if (sourceToggle) {
    sourceToggle.hidden = true;
    sourceToggle.setAttribute("aria-expanded", "false");
  }
  $("#pr-speech-state").textContent = "正在读取本页内容…";
  const content = $("#pr-speech-content");
  if (ed.hasAnySpeech && content.childElementCount) {
    content.classList.add("is-updating");
  } else {
    content.innerHTML = '<div class="pr-speech-empty">正在整理本页讲稿</div>';
  }
}

function setRailPageScope(n) {
  setPageSpeechScope(n);
  const history = $("#pr-page-history");
  const feed = $("#pr-feed");
  const rail = $("#proc-rail");
  const pageMode = ed.kind === "static" && Number(n) > 0;
  rail?.classList.toggle("page-history-mode", pageMode);
  if (history) history.hidden = !pageMode;
  if (feed) feed.hidden = pageMode;
  if (!pageMode) {
    ed.historyScopePage = null;
    ed.historyRenderKey = "";
    return;
  }
  // select()/livefeed polling may revisit the same page many times. Do not
  // replace the rail with loading placeholders unless the selected page
  // actually changed; replacing it collapses details and restarts animations.
  if (ed.historyScopePage === n) return;
  ed.historyScopePage = n;
  ed.historyRenderKey = "";
  $("#pr-history-title").textContent = `第 ${pad2(n)} 页 · 页面详情`;
  const cached = ed.pageHistoryCache?.get(Number(n));
  if (cached?.data) {
    renderPageHistory(cached.data, Number(n));
    return;
  }
  $("#pr-history-count").textContent = "正在读取真实渲染轨迹…";
  $("#pr-history-list").innerHTML = '<div class="pr-history-empty"><i></i><span>正在整理本页的渲染与视觉检查记录</span></div>';
}

function historyVerdict(item) {
  const judgment = String(item.judgment || "").trim();
  if (judgment.length >= 18) return judgment;
  return String(item.prompt || judgment || "该版本已完成视觉检查。请展开查看检查维度。").trim();
}

function speechMarkdownBlocks(value) {
  const blocks = String(value || "").trim().split(/\n\s*\n/).filter(Boolean);
  return blocks.map((block) => {
    const lines = block.split("\n").map((line) => line.trim()).filter(Boolean);
    if (lines.length && lines.every((line) => /^[-*+]\s+/.test(line))) {
      return `<ul>${lines.map((line) => `<li>${escapeHtml(line.replace(/^[-*+]\s+/, ""))}</li>`).join("")}</ul>`;
    }
    return `<p>${lines.map((line) => escapeHtml(line)).join("<br>")}</p>`;
  }).join("");
}

function cleanSpeechNotes(value, title = "") {
  const expectedTitle = String(title || "").replace(/^#+\s*/, "").trim();
  const lines = String(value || "").split("\n");
  while (lines.length) {
    const first = lines[0].trim();
    const plain = first.replace(/^#{1,6}\s*/, "").trim();
    if (!first) { lines.shift(); continue; }
    if (/^(?:slide|page)\s*0*\d+(?:\s*[：:·|｜—–-].*)?$/i.test(plain)) {
      lines.shift();
      continue;
    }
    if (expectedTitle && plain === expectedTitle) {
      lines.shift();
      continue;
    }
    break;
  }
  return lines.join("\n").trim();
}

function renderPageSpeech(speech, n) {
  const state = $("#pr-speech-state");
  const content = $("#pr-speech-content");
  const sourceToggle = $("#page-speech-source-toggle");
  const hasSpeech = !!speech?.exists && [speech.notes, speech.evidence]
    .some((value) => String(value || "").trim());
  const hasEvidence = !!String(speech?.evidence || "").trim();
  if (sourceToggle) {
    sourceToggle.hidden = !hasEvidence;
    if (!hasEvidence) sourceToggle.setAttribute("aria-expanded", "false");
  }
  ed.speechPages ||= new Map();
  ed.speechPages.set(Number(n), hasSpeech);
  if (hasSpeech) ed.hasAnySpeech = true;
  syncPageSpeechDrawer(n);
  const renderKey = JSON.stringify({ n, status: ed.status, speech: speech || null });
  if (ed.speechRenderKey === renderKey) return;
  ed.speechRenderKey = renderKey;
  content.classList.remove("is-updating");
  if (!hasSpeech) {
    const active = ed.kind === "dynamic" ? ed.status === "running" : isStaticActiveStatus(ed.status);
    state.textContent = active ? "等待本页讲稿" : "本页暂无讲稿";
    content.innerHTML = `<div class="pr-speech-empty">${isStaticActiveStatus(ed.status)
      || (ed.kind === "dynamic" && ed.status === "running")
      ? "讲稿生成后会自动显示在这里"
      : "这页暂时没有讲稿"}</div>`;
    return;
  }
  state.textContent = "讲述内容与来源";
  const notes = speechMarkdownBlocks(cleanSpeechNotes(speech.notes, speech.title));
  const evidence = speechMarkdownBlocks(speech.evidence);
  const sourceOpen = hasEvidence && sourceToggle?.getAttribute("aria-expanded") === "true";
  content.innerHTML = `
    <div class="pr-speech-block">${notes || "<p>暂无讲述内容</p>"}</div>
    ${hasEvidence ? `<div class="pr-speech-evidence-body" id="page-speech-evidence"${sourceOpen ? "" : " hidden"}>${evidence || "<p>暂无来源信息</p>"}</div>` : ""}`;
}

function normalizeVisionTechnicalDetail(value) {
  let text = String(value || "").trim()
    .replace(/^---+\s*/, "")
    .replace(/``(?:text)?\s*/gi, "\n")
    .replace(/\s*``\s*$/g, "")
    .replace(/([。；])\s*(?=(?:身份(?:声明)?|产出)\s*[:：])/g, "$1\n")
    .replace(/\s+(?=(?:身份(?:声明)?|产出)\s*[:：])/g, "\n")
    .replace(/\s+(?=(?:group|status|pages|renders|hard_issues|summary)\s*:)/gi, "\n");
  text = text.split("\n").map((line) => {
    const trimmed = line.trim();
    const field = trimmed.match(/^(身份(?:声明)?|产出|group|status|pages|renders|hard_issues|summary)\s*[:：]\s*(.*)$/i);
    if (!field) return trimmed;
    const labels = {
      "身份": "执行角色", "身份声明": "执行角色", "产出": "交付文件",
      group: "页面组", status: "状态", pages: "页码", renders: "渲染文件",
      hard_issues: "严重问题", summary: "完整说明",
    };
    const key = labels[field[1]] || labels[field[1].toLowerCase()] || field[1];
    return `- **${key}：** ${field[2]}`;
  }).filter(Boolean).join("\n");
  return text;
}

function visionJudgmentPresentation(value) {
  const text = cleanAgentText(value);
  if (!text) return { summary: "", detail: "" };
  const markers = [/\s+---+\s+/, /\s+身份(?:声明)?\s*[:：]/i, /\s+``(?:text)?\s*/i, /\s+```(?:text)?\s*/i];
  let splitAt = -1;
  markers.forEach((pattern) => {
    const match = pattern.exec(text);
    if (match && (splitAt < 0 || match.index < splitAt)) splitAt = match.index;
  });
  if (splitAt < 0) return { summary: text, detail: "" };
  return {
    summary: text.slice(0, splitAt).trim(),
    detail: normalizeVisionTechnicalDetail(text.slice(splitAt).trim()),
  };
}

function renderPageHistory(data, n) {
  if (ed.kind !== "static" || ed.sel !== n) return;
  const items = data.items || [];
  renderPageSpeech(data.speech || {}, n);
  const countText = items.length
    ? `${data.total || items.length} 次真实视觉检查`
    : (isStaticActiveStatus(ed.status) ? "等待首次视觉检查" : "暂无视觉检查记录");
  if ($("#pr-history-count").textContent !== countText) $("#pr-history-count").textContent = countText;
  const list = $("#pr-history-list");
  const renderKey = JSON.stringify({ n, status: ed.status, total: data.total || 0, items });
  if (ed.historyRenderKey === renderKey) return;
  const scroll = list.closest(".pr-history-scroll");
  const previousTop = scroll?.scrollTop || 0;
  const promptOpen = $$(".pr-history-prompt", list).map((details) => details.open);
  const judgmentOpen = $$(".pr-history-judgment-details", list).map((details) => details.open);
  ed.historyRenderKey = renderKey;
  if (!items.length) {
    list.innerHTML = `<div class="pr-history-empty"><i></i><span>${isStaticActiveStatus(ed.status)
      ? "页面生成后，渲染图与 Vision 判断会实时出现在这里"
      : "这页没有保存独立的 Vision 检查快照"}</span></div>`;
    return;
  }
  list.innerHTML = items.map((item) => {
    const review = item.stage === "review";
    const revisionNo = Number(item.revision_no || 0);
    const roundLabel = revisionNo > 0 ? `第 ${revisionNo} 轮修改` : "初版";
    const hasJudgment = String(item.judgment || "").trim().length >= 18;
    const verdict = hasJudgment
      ? historyVerdict(item)
      : "本次 Vision 检查未返回可见判断，仅保存了检查问题与当时的页面快照。";
    const judgment = visionJudgmentPresentation(verdict);
    const prompt = String(item.prompt || "").trim();
    const changed = Number(item.changes || 0);
    return `<article class="pr-history-card${review ? " review" : ""}">
      <div class="pr-history-meta">
        <span class="pr-history-stage">${roundLabel} · ${review ? (ed.status === "completed" ? "整册 Review" : "整册 Review · 未完成") : `页面自检 · V${item.version || 1}`}</span>
        <time>${escapeHtml(item.time || "")}</time>
      </div>
      ${item.image_url ? `<button type="button" class="pr-history-shot" aria-label="查看第 ${item.version || 1} 版渲染大图"><img src="${escapeHtml(item.image_url)}" alt="第 ${n} 页第 ${item.version || 1} 版渲染"></button>` : ""}
      <div class="pr-history-verdict${hasJudgment ? "" : " is-missing"}"><span>Vision 检查结果</span><div class="pr-history-markdown agent-markdown">${agentMarkdownHtml(judgment.summary || verdict)}</div></div>
      ${judgment.detail ? `<details class="pr-history-judgment-details"><summary>查看完整判断<i aria-hidden="true"></i></summary><div class="pr-history-markdown agent-markdown">${agentMarkdownHtml(judgment.detail)}</div></details>` : ""}
      ${changed ? `<div class="pr-history-change"><i>↻</i><span><b>修改记录 · ${changed} 次</b>${(item.change_notes || []).length
        ? `<ul>${item.change_notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("")}</ul>`
        : `<em>根据视觉检查结果完成页面调整</em>`}${item.version < items.length ? `<em>完成后重新渲染并进入下一轮检查</em>` : ""}</span></div>` : ""}
      ${prompt ? `<details class="pr-history-prompt"><summary>查看检查项</summary><div class="pr-history-markdown agent-markdown">${agentMarkdownHtml(prompt)}</div></details>` : ""}
    </article>`;
  }).join("");
  $$(".pr-history-prompt", list).forEach((details, index) => {
    if (promptOpen[index]) details.open = true;
  });
  $$(".pr-history-judgment-details", list).forEach((details, index) => {
    if (judgmentOpen[index]) details.open = true;
  });
  if (scroll) requestAnimationFrame(() => { scroll.scrollTop = previousTop; });
}

async function loadPageHistory(n, { quiet = false, prefetch = false, force = false } = {}) {
  if (ed.kind !== "static" || !ed.id || n <= 0) return;
  ed.pageHistoryCache ||= new Map();
  ed.pageHistoryPending ||= new Map();
  const page = Number(n);
  const now = Date.now();
  const active = isStaticActiveStatus(ed.status);
  const cached = ed.pageHistoryCache.get(page);
  const cacheTtl = active ? 5000 : 30000;
  if (!prefetch && cached?.data && ed.sel === page) renderPageHistory(cached.data, page);
  if (!force && cached?.data && cached.status === ed.status && now - cached.fetchedAt < cacheTtl) {
    return cached.data;
  }
  const request = prefetch ? ed.historyRequest : ++ed.historyRequest;
  let pending = ed.pageHistoryPending.get(page);
  if (!pending) {
    pending = jget(`${trajectoryMode ? trajectoryDeckApi(ed.id, "/page-history") : `/api/decks/${ed.id}/page-history`}?n=${page}`);
    ed.pageHistoryPending.set(page, pending);
  }
  try {
    const data = await pending;
    ed.pageHistoryCache.set(page, { data, status: ed.status, fetchedAt: Date.now() });
    if (!prefetch && (request !== ed.historyRequest || ed.kind !== "static" || ed.sel !== page)) return data;
    if (!prefetch) renderPageHistory(data, page);

    // Completed decks are immutable most of the time. Warm the neighboring
    // pages only after the visible page is ready, so the next click can render
    // immediately without competing with live generation requests.
    if (!prefetch && !active) {
      const warm = () => [page - 1, page + 1]
        .filter((candidate) => candidate > 0 && candidate <= Number(ed.total || 0))
        .forEach((candidate) => {
          if (!ed.pageHistoryCache.has(candidate)) loadPageHistory(candidate, { quiet: true, prefetch: true });
        });
      if (typeof requestIdleCallback === "function") requestIdleCallback(warm, { timeout: 1200 });
      else setTimeout(warm, 120);
    }
    return data;
  } catch (error) {
    if (!prefetch && (request !== ed.historyRequest || ed.kind !== "static" || ed.sel !== page || quiet)) return;
    if (prefetch || quiet) return;
    $("#pr-history-count").textContent = "记录读取失败";
    $("#pr-history-list").innerHTML = `<div class="pr-history-empty"><span>${escapeHtml(error.message)}</span></div>`;
    $("#pr-speech-state").textContent = "讲稿读取失败";
    $("#pr-speech-content").innerHTML = `<div class="pr-speech-empty">${escapeHtml(error.message)}</div>`;
  } finally {
    if (ed.pageHistoryPending.get(page) === pending) ed.pageHistoryPending.delete(page);
  }
}

function speechFromMarkdown(text, n, source = "speech.md") {
  const value = String(text || "");
  const headingPattern = /^#{1,3}\s+(?:(?:slide|page)\s*0*(\d+)|第\s*0*(\d+)\s*页)(?:\s*[—–:：|｜·-]\s*([^\n]+))?\s*$/gim;
  const headings = [...value.matchAll(headingPattern)];
  const index = headings.findIndex((match) => Number(match[1] || match[2]) === Number(n));
  if (index < 0) return { exists: false, page: n, source };
  const heading = headings[index];
  const start = Number(heading.index) + heading[0].length;
  const end = index + 1 < headings.length ? Number(headings[index + 1].index) : value.length;
  let body = value.slice(start, end).trim();
  const evidencePattern = /^#{2,4}\s+(?:evidence\s+and\s+sources|证据(?:与来源)?|来源(?:与证据)?|参考资料(?:[（(]不朗读[）)])?)\s*$/im;
  const evidenceMatch = evidencePattern.exec(body);
  const notes = (evidenceMatch ? body.slice(0, evidenceMatch.index) : body)
    .replace(/^#{2,4}\s+(?:讲述内容|演讲稿|speaker\s+notes?)\s*$/im, "").trim();
  const evidence = evidenceMatch ? body.slice(evidenceMatch.index + evidenceMatch[0].length).trim() : "";
  return { exists: !!(notes || evidence), page: n, title: String(heading[3] || "").trim(), notes, evidence, source };
}

async function loadDynamicSpeechFile(n) {
  for (const rel of ["speech.md", "plan/speech.md"]) {
    const response = await fetch(`/dynamic/files/${encodeURIComponent(ed.id)}/${rel}`, { credentials: "same-origin" });
    if (response.ok) return speechFromMarkdown(await response.text(), n, rel);
    if (response.status !== 404) throw new Error(`讲稿读取失败 (${response.status})`);
  }
  return { exists: false, page: n };
}

async function loadDynamicPageSpeech(n, { quiet = false } = {}) {
  if (ed.kind !== "dynamic" || !ed.id || n <= 0) return;
  const request = ++ed.speechRequest;
  try {
    const data = await jget(`/api/dynamic/page-speech?conv_id=${encodeURIComponent(ed.id)}&n=${n}`);
    if (request !== ed.speechRequest || ed.kind !== "dynamic" || ed.sel !== n) return;
    renderPageSpeech(data || {}, n);
  } catch (error) {
    if (request !== ed.speechRequest || ed.kind !== "dynamic" || ed.sel !== n) return;
    try {
      const speech = await loadDynamicSpeechFile(n);
      if (request !== ed.speechRequest || ed.kind !== "dynamic" || ed.sel !== n) return;
      renderPageSpeech(speech, n);
      return;
    } catch {}
    ed.speechPages ||= new Map();
    if (!ed.speechPages.has(Number(n))) ed.speechPages.set(Number(n), false);
    syncPageSpeechDrawer(n);
    if (quiet) return;
    $("#pr-speech-state").textContent = "本页暂无讲稿";
    $("#pr-speech-content").innerHTML = '<div class="pr-speech-empty">这页暂时没有讲稿</div>';
  }
}

function railApply(p, phase) {
  const terminal = ["completed", "failed", "rejected", "interrupted", "not_started"].includes(p.status);

  $("#pr-elapsed").textContent = p.elapsed_s != null ? fmtDur(p.elapsed_s) : "";
  $("#pr-count").innerHTML = ed.total
    ? `${ed.rendered.size}<small>/${ed.total}</small>` : (p.status === "completed" ? "✓" : "–");
  const pct = ed.total ? Math.round((ed.rendered.size / ed.total) * 100)
    : (p.status === "completed" ? 100 : 4);
  $("#pr-bar").style.width = (p.status === "completed" ? 100 : pct) + "%";

  // 阶段时间线
  const cur = p.status === "completed" ? "done" : PHASE_STEP[phase];
  const idx = STEP_ORDER.indexOf(cur);
  $$("#pr-steps li").forEach((li) => {
    const i = STEP_ORDER.indexOf(li.dataset.k);
    li.className = i < idx || p.status === "completed" ? "done" : (i === idx ? "now" : "");
  });

  // 实时动作
  const act = !terminal ? actText(p) : "";
  $("#pr-act").textContent = act || (terminal ? "" : (
    p.status === "waiting" ? "等待上一版完成…"
      : (p.status === "queued" ? "排队中…" : "正在启动引擎…")
  ));

  // 事件流:阶段切换(按显示名去重,delegating/rendering 同属「并行生成」只记一次)+ 每页渲染完成
  const lbl = PHASE_LABEL[phase];
  if (!terminal && lbl && lbl !== ed.lastPhase) {
    if (ed.lastPhase !== null || phase !== "starting")
      feedAdd(`<span class="pr-txt">进入「${lbl}」</span>`);
    ed.lastPhase = lbl;
  }
  (p.rendered || []).forEach((n) => {
    if (ed.feedSeen.has(n)) return;
    ed.feedSeen.add(n);
    const src = fileUrl(n);
    feedAdd(`<img class="pr-thumb" src="${src}" alt=""><span class="pr-txt">第 ${n} 页渲染完成</span>`, "slide");
  });
  if (terminal && !ed.finalized) {
    ed.finalized = true;
    if (p.status === "completed")
      feedAdd(`<span class="pr-txt pr-ok">✓ 全部完成 · 共 ${ed.rendered.size} 页${p.elapsed_s != null ? " · 用时 " + fmtDur(p.elapsed_s) : ""}</span>`);
  }
}

function renderActions(status) {
  if (ed.kind === "dynamic") {
    const running = status === "running";
    const ready = status === "completed" && !!ed.dynamicDeckUrl;
    const deckUrl = ed.dynamicDeckUrl || "";
    const actions = $("#ed-actions");
    delete actions.dataset.staticRenderKey;
    const renderKey = JSON.stringify([ed.id, status, ready, deckUrl]);
    if (actions.dataset.dynamicRenderKey === renderKey) return;
    actions.dataset.dynamicRenderKey = renderKey;
    actions.innerHTML =
      (running ? `<button class="btn-cancel" id="cancel-btn">停止生成</button>` : "") +
      (ready
        ? `<button class="btn-ghost" id="overview-btn" type="button">预览</button>
           <button class="btn-ghost" id="fullscreen-btn" type="button">播放</button>
           <div class="export-menu-wrap">
             <button class="btn-dl export-trigger" id="export-trigger" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="export-menu">
               <span>导出</span><span class="export-chevron" aria-hidden="true"></span>
             </button>
             <div class="export-menu" id="export-menu" role="menu" hidden>
               <button class="export-menu-item utility-action" id="share-btn" type="button" role="menuitem">
                 <span class="export-menu-icon" aria-hidden="true">↗</span><span class="export-menu-copy"><b>分享</b><small>复制或调用系统分享</small></span>
               </button>
               <button class="export-menu-item utility-action" id="copy-output-btn" type="button" role="menuitem">
                 <span class="export-menu-icon" aria-hidden="true">⌘</span><span class="export-menu-copy"><b>复制输出路径</b><small>复制服务器上的产物目录</small></span>
               </button>
               <span class="export-menu-separator" aria-hidden="true"></span>
               <a class="export-menu-item" href="/api/dynamic/download?conv_id=${encodeURIComponent(ed.id)}" role="menuitem">
                 <span class="export-menu-icon" aria-hidden="true">↓</span><span class="export-menu-copy"><b>下载 .zip</b><small>包含动态 HTML、图片与本地资源</small></span>
               </a>
             </div>
           </div>`
        : "");
    bindUtilityActions();
    bindExportMenu();
    bindPresentationFullscreen();
    const ob = $("#overview-btn");
    if (ob) ob.onclick = openOverviewModal;
    const cb = $("#cancel-btn");
    if (cb) cb.onclick = async () => {
      cb.disabled = true; cb.textContent = "停止中…";
      ed.stopRequested = true;
      ed.status = "stopped";
      renderDynamicChrome();
      renderActions("stopped");
      showStudioNotice("生成已中断", { type: "info", duration: 2800 });
      try {
        await fetch("/api/dynamic/stop", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conv_id: ed.id }),
        });
      } catch (error) {
        showStudioNotice(`停止请求发送失败：${error.message}`, { type: "error" });
      }
    };
    return;
  }
  const actions = $("#ed-actions");
  delete actions.dataset.dynamicRenderKey;
  const terminal = ["completed", "failed", "rejected", "interrupted", "not_started"].includes(status);
  const failed = status === "failed" || status === "rejected" || status === "interrupted";
  const renderKey = JSON.stringify([ed.id, status, ed.pptOutput, ed.staticDeckUrl]);
  if (actions.dataset.staticRenderKey === renderKey) return;
  actions.dataset.staticRenderKey = renderKey;
  const dynamicHtml = ed.pptOutput === "dynamic_html";
  actions.innerHTML =
    (!terminal ? `<button class="btn-cancel" id="cancel-btn">取消</button>` : "") +
    (failed ? `<button class="btn-retry" id="retry-btn">↻ 重新生成</button>` : "") +
    (status === "completed"
      ? `<button class="btn-ghost" id="overview-btn">预览</button>
         <button class="btn-ghost" id="fullscreen-btn" type="button">播放</button>
         <div class="export-menu-wrap">
           <button class="btn-dl export-trigger" id="export-trigger" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="export-menu">
             <span>导出</span><span class="export-chevron" aria-hidden="true"></span>
           </button>
           <div class="export-menu" id="export-menu" role="menu" hidden>
             <button class="export-menu-item utility-action" id="share-btn" type="button" role="menuitem">
               <span class="export-menu-icon" aria-hidden="true">↗</span><span class="export-menu-copy"><b>分享</b><small>复制或调用系统分享</small></span>
             </button>
             <button class="export-menu-item utility-action" id="copy-output-btn" type="button" role="menuitem">
               <span class="export-menu-icon" aria-hidden="true">⌘</span><span class="export-menu-copy"><b>复制输出路径</b><small>复制服务器上的产物目录</small></span>
             </button>
             <span class="export-menu-separator" aria-hidden="true"></span>
             ${dynamicHtml
               ? ""
               : `<a class="export-menu-item" href="/api/decks/${ed.id}/pptx" role="menuitem">
                    <span class="export-menu-icon export-file-icon" aria-hidden="true">P</span><span class="export-menu-copy"><b>导出 .pptx</b><small>下载 PowerPoint 文件</small></span>
                  </a>`}
             <a class="export-menu-item" href="/api/decks/${ed.id}/download" role="menuitem">
               <span class="export-menu-icon" aria-hidden="true">↓</span><span class="export-menu-copy"><b>下载 .zip</b><small>下载完整工程与资源</small></span>
             </a>
           </div>
         </div>` : "");
  bindUtilityActions();
  bindExportMenu();
  bindPresentationFullscreen();
  const ob = $("#overview-btn");
  if (ob) ob.onclick = openOverviewModal;
  const cb = $("#cancel-btn");
  if (cb) cb.onclick = async () => {
    cb.disabled = true;
    ed.stopRequested = true;
    ed.status = "interrupted";
    renderStatus({ status: "interrupted" }, "failed");
    renderActions("interrupted");
    showStudioNotice("生成已中断", { type: "info", duration: 2800 });
    try {
      await fetch(`/api/decks/${ed.id}/cancel`, { method: "POST" });
    } catch (error) {
      showStudioNotice(`停止请求发送失败：${error.message}`, { type: "error" });
    }
  };
  const rb = $("#retry-btn");
  if (rb) rb.onclick = async () => {
    rb.disabled = true; rb.textContent = "排队中…";
    try {
      const r = await fetch(`/api/decks/${ed.id}/retry`, { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail || "失败");
      await loadDecks(ed.id);
      openDeck(ed.id);                       // 重新进入直播
    } catch (e) { alert("重新生成失败: " + e.message); rb.disabled = false; rb.textContent = "↻ 重新生成"; }
  };
}

function closeExportMenu() {
  const trigger = $("#export-trigger");
  const menu = $("#export-menu");
  if (!trigger || !menu) return;
  menu.hidden = true;
  trigger.classList.remove("open");
  trigger.setAttribute("aria-expanded", "false");
}

function bindExportMenu() {
  const trigger = $("#export-trigger");
  const menu = $("#export-menu");
  if (!trigger || !menu) return;
  trigger.onclick = (event) => {
    event.stopPropagation();
    const open = menu.hidden;
    menu.hidden = !open;
    trigger.classList.toggle("open", open);
    trigger.setAttribute("aria-expanded", String(open));
  };
  menu.onclick = (event) => {
    event.stopPropagation();
    if (event.target.closest(".export-menu-item")) closeExportMenu();
  };
}

function bindPresentationFullscreen() {
  const button = $("#fullscreen-btn");
  if (!button) return;
  button.onclick = () => {
    const stage = $("#canvas");
    const deckUrl = ed.kind === "dynamic" ? ed.dynamicDeckUrl : ed.staticDeckUrl;
    if (!stage || !deckUrl) return;

    // Fullscreen must be requested synchronously from the click gesture. Move
    // from the 00 progress surface to a real slide first, then promote the
    // existing preview stage to the monitor's fullscreen top layer.
    ed.workspaceView = "ppt";
    ed.viewMode = "ppt";
    localStorage.setItem("studio_page_view", ed.viewMode);
    select(Math.max(1, Number(ed.sel) || 1), { byUser: true });

    const request = stage.requestFullscreen || stage.webkitRequestFullscreen;
    if (!request) {
      alert("当前浏览器不支持显示器全屏，请使用最新版 Chrome 或 Edge。");
      return;
    }
    try {
      const pending = request.call(stage);
      if (pending?.then) {
        pending.then(() => {
          scheduleDeckViewportFit();
          setTimeout(() => scheduleDeckViewportFit(), 180);
        }).catch(() => alert("浏览器阻止了全屏，请允许该页面进入全屏后重试。"));
      }
    } catch {
      alert("浏览器阻止了全屏，请允许该页面进入全屏后重试。");
    }
  };
}

function legacyCopyText(text) {
  const active = document.activeElement;
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.cssText = "position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0;pointer-events:none";
  document.body.appendChild(textarea);
  textarea.focus({ preventScroll: true });
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  let copied = false;
  try { copied = document.execCommand("copy"); }
  finally {
    textarea.remove();
    active?.focus?.({ preventScroll: true });
  }
  if (!copied) throw new Error("legacy clipboard copy was rejected");
}

async function writeClipboardText(text) {
  if (window.isSecureContext && navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
  else legacyCopyText(text);
}

function currentShareUrl() {
  const url = new URL(location.href);
  url.search = "";
  url.hash = `${ed.kind}-${encodeURIComponent(ed.id)}`;
  return url.toString();
}

function setTemporaryButtonText(button, text, fallback, delay = 1500) {
  if (!button) return;
  if (!button._fallbackHtml) button._fallbackHtml = button.innerHTML;
  button.textContent = text;
  clearTimeout(button._labelTimer);
  button._labelTimer = setTimeout(() => {
    if (!button.isConnected) return;
    if (button._fallbackHtml) button.innerHTML = button._fallbackHtml;
    else button.textContent = fallback;
  }, delay);
}

async function shareCurrentDeck() {
  const button = $("#share-btn");
  const url = currentShareUrl();
  if (navigator.share) {
    try {
      await navigator.share({ title: $("#ed-title")?.textContent || "SenseNova Present", text: "查看这份 SenseNova Present 演示", url });
      setTemporaryButtonText(button, "已分享", "↗ 分享");
      return;
    } catch (error) {
      if (error?.name === "AbortError") return;
    }
  }
  try {
    await writeClipboardText(url);
    setTemporaryButtonText(button, "链接已复制", "↗ 分享");
  } catch (error) {
    console.warn("Share link copy failed", error);
    setTemporaryButtonText(button, "复制失败", "↗ 分享", 1900);
  }
}

async function copyOutputLocation() {
  const button = $("#copy-output-btn");
  if (!button || !ed.id) return;
  button.disabled = true;
  button.textContent = "读取中…";
  try {
    const endpoint = ed.kind === "dynamic"
      ? `/api/dynamic/output-location?conv_id=${encodeURIComponent(ed.id)}`
      : `/api/decks/${encodeURIComponent(ed.id)}/output-location`;
    const payload = await jget(endpoint);
    await writeClipboardText(payload.path || "");
    setTemporaryButtonText(button, "目录已复制", "⌘ 复制输出目录");
  } catch (error) {
    console.warn("Output location copy failed", error);
    setTemporaryButtonText(button, "复制失败", "⌘ 复制输出目录", 1900);
  } finally {
    button.disabled = false;
  }
}

function bindUtilityActions() {
  const share = $("#share-btn");
  const output = $("#copy-output-btn");
  if (share) share.onclick = shareCurrentDeck;
  if (output) output.onclick = copyOutputLocation;
}

async function copyOutlineQuery(button) {
  const text = button?.dataset.copyMessage || $("#outline-user-query")?.textContent?.trim() || outlineBriefData().query;
  if (!text) return;
  try {
    await writeClipboardText(text);
    setTemporaryButtonText(button, "已复制", "复制", 1200);
  } catch (error) {
    console.warn("Query copy failed", error);
    setTemporaryButtonText(button, "复制失败", "复制", 1800);
  }
}

function renderOverviewGrid() {
  const total = ed.total || ed.rendered.size || 0;
  const grid = $("#overview-grid");
  $("#overview-count").textContent = total ? `${total} 页` : "";
  if (!total) {
    grid.innerHTML = '<div class="overview-empty">暂无可预览页面</div>';
    return;
  }
  grid.innerHTML = Array.from({ length: total }, (_, i) => {
    const n = i + 1;
    const ready = ed.rendered.has(n);
    return `<button type="button" class="overview-card${ready ? "" : " pending"}" data-n="${n}" ${ready ? "" : "disabled"}>
      <span class="overview-no">${pad2(n)}</span>
      ${ready ? `<img src="${fileUrl(n)}" alt="第 ${n} 页预览">` : '<span class="overview-wait">未渲染</span>'}
    </button>`;
  }).join("");
}

function openOverviewModal() {
  renderOverviewGrid();
  $("#overview-modal").hidden = false;
}

function closeOverviewModal() {
  $("#overview-modal").hidden = true;
}

/* ----- 胶片条 ----- */
function buildFilmstrip() {
  const fs = $("#filmstrip");
  fs.innerHTML = "";
  ed.phLines = {};                              // 帧重建,占位帧微日志状态清零
  // 00 · 过程帧:两条产线都在这里展示 AI 制作进度
  const of = document.createElement("div");
  of.className = "frame frame-outline"; of.dataset.n = 0;
  of.innerHTML = `<span class="no">00</span>
    <div class="ocard"><span class="oc-title">过程</span><span class="oc-sub">PROCESS</span></div>`;
  of.onclick = () => setWorkspaceView("process");
  fs.appendChild(of);
  for (let n = 1; n <= ed.total; n++) {
    const f = document.createElement("div");
    f.className = "frame"; f.dataset.n = n;
    f.innerHTML = `<span class="no">${pad2(n)}</span>
      <div class="ph"><i class="dots tl"><b></b><b></b><b></b></i><div class="ph-log"></div></div>`;
    f.onclick = () => select(n, { byUser: true });
    fs.appendChild(f);
    if (ed.rendered.has(n)) fillFrame(n);
  }
  if (ed.sel != null) markSel();
}

function wantUrl(n) { return new URL(fileUrl(n), location.origin).href; }

function fillFrame(n) {
  const f = $(`#filmstrip .frame[data-n="${n}"]`);
  if (!f) return;
  const img = document.createElement("img");
  // 注意:不能用 loading="lazy" —— 未挂载进 DOM 的 lazy 图片不会发起请求,onload 永远不触发。
  img.src = fileUrl(n);
  img.onload = () => {
    if (img.src !== wantUrl(n)) return;        // 加载期间又重渲了:丢弃过期图
    const ph = f.querySelector(".ph");
    const cur = f.querySelector("img");
    if (ph) ph.replaceWith(img);
    else if (cur && cur.src !== img.src) cur.src = img.src;
  };
  img.onerror = () => {};
}

// 某页被设计师自检重渲覆盖:刷新胶片条小图;若正选中,大画布预加载后无闪切换
function refreshFrame(n) {
  const url = fileUrl(n);
  const f = $(`#filmstrip .frame[data-n="${n}"] img`);
  if (f) f.src = url;
  else fillFrame(n);                            // 初图仍在加载:重走 fillFrame(其 onload 会校验最新 URL)
  if (ed.sel === n) {
    const big = $("#canvas-img");
    const pre = new Image();
    pre.onload = () => { if (ed.sel === n && pre.src === wantUrl(n)) big.src = pre.src; };
    pre.src = url;
  }
}

function markSel() {
  $$("#filmstrip .frame").forEach((f) => f.classList.toggle("sel", +f.dataset.n === ed.sel));
  const f = $(`#filmstrip .frame[data-n="${ed.sel}"]`);
  if (f) f.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// 顶部控制「过程 ⇄ 实时 PPT」工作区；底部控制实时 PPT 内当前页的
// 「页面预览 ⇄ 子 Agent 消息」。两层状态必须独立，避免切换本页消息时跳回 00。
function syncViewToggle() {
  const vt = $("#view-toggle");
  const previewReady = ed.kind === "dynamic" ? !!ed.dynamicDeckUrl : !!ed.staticDeckReady;
  const isOutline = ed.sel === 0;
  const hasPreview = !isOutline && ed.rendered.has(ed.sel);
  const processMode = ed.workspaceView !== "ppt";
  const pageConsole = (pageViewTransitionTarget || ed.viewMode) === "console";
  const onConsole = processMode || !hasPreview || pageConsole;
  const editor = $("#editor");
  // 00 is a conversation/progress surface, not a 16:9 slide. Its card is
  // content-sized (with a viewport cap) so large monitors do not turn a short
  // conversation into a mostly empty full-height canvas.
  editor?.classList.toggle("outline-layout", isOutline);
  editor?.classList.toggle("preview-layout", !onConsole);
  editor?.classList.toggle("progress-layout", onConsole);
  editor?.classList.toggle("workspace-process", processMode);
  editor?.classList.toggle("workspace-ppt", !processMode);
  const effectiveConsole = !hasPreview || pageConsole;
  if (vt) vt.dataset.active = effectiveConsole ? "console" : "ppt";
  vt?.querySelectorAll("button").forEach((b) => {
    const isConsole = b.dataset.view === "console";
    b.classList.toggle("on", isConsole ? effectiveConsole : !effectiveConsole);
    b.disabled = !isConsole && !hasPreview;
  });
  const workspaceSwitch = $("#workspace-view-switch");
  if (workspaceSwitch) workspaceSwitch.dataset.active = processMode ? "process" : "ppt";
  workspaceSwitch?.querySelectorAll("button").forEach((button) => {
    const active = button.dataset.workspaceView === (processMode ? "process" : "ppt");
    const disabled = button.dataset.workspaceView === "ppt" && !previewReady;
    button.classList.toggle("on", active);
    button.setAttribute("aria-pressed", String(active));
    button.disabled = disabled;
    button.title = disabled ? "HTML 产出后即可查看实时 PPT" : "";
  });
  syncPageSpeechDrawer();
}

function setWorkspaceView(view) {
  if (!ed.id) return;
  const processMode = view !== "ppt";
  const previewReady = ed.kind === "dynamic" ? !!ed.dynamicDeckUrl : !!ed.staticDeckReady;
  if (!processMode && !previewReady) return;
  ed.workspaceView = processMode ? "process" : "ppt";
  setProcRailOpen(false);
  if (processMode) {
    select(0, { byUser: false });
    return;
  }
  setFollow(true);
  const currentReady = Number(ed.sel) > 0 && ed.rendered.has(Number(ed.sel));
  const target = currentReady
    ? Number(ed.sel)
    : (ed.rendered.size ? Math.max(...ed.rendered) : 1);
  select(target, { byUser: false });
}

async function setPageView(view) {
  if (!ed.id || ed.workspaceView !== "ppt" || ed.sel === 0) return;
  if (view === "ppt" && !ed.rendered.has(Number(ed.sel))) return;
  const nextView = view === "console" ? "console" : "ppt";
  if (ed.viewMode === nextView && pageViewTransitionTarget == null) return;
  const token = ++pageViewTransitionToken;
  pageViewTransitionTarget = nextView;
  const canvas = $("#canvas");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const direction = nextView === "console" ? 1 : -1;
  const toggle = $("#view-toggle");
  let outAnimation = null;
  if (toggle) toggle.dataset.active = nextView;

  if (!reduced && typeof canvas?.animate === "function") {
    outAnimation = canvas.animate([
      { opacity: 1, transform: "translate3d(0,0,0) scale(1)" },
      { opacity: .18, transform: `translate3d(${direction * -7}px,0,0) scale(.997)` },
    ], { duration: 115, easing: "cubic-bezier(.4,0,1,1)", fill: "both" });
    await outAnimation.finished.catch(() => {});
  }
  if (token !== pageViewTransitionToken) { outAnimation?.cancel(); return; }

  ed.viewMode = nextView;
  pageViewTransitionTarget = null;
  localStorage.setItem("studio_page_view", ed.viewMode);
  select(Number(ed.sel), { byUser: false });
  outAnimation?.cancel();

  if (!reduced && typeof canvas?.animate === "function") {
    canvas.animate([
      { opacity: .12, transform: `translate3d(${direction * 9}px,0,0) scale(.997)` },
      { opacity: 1, transform: "translate3d(0,0,0) scale(1)" },
    ], { duration: 300, easing: "cubic-bezier(.16,1,.3,1)" });
  }
}

/* ----- 画布 ----- */
let deckViewportFitRaf = 0;
let stableStaticImageToken = 0;
let dynamicDeckRevealToken = 0;

// The preview column contains both the 16:9 player and a persistent bottom
// dock. Sizing the player from width alone can make it taller than the actual
// remaining row on wide/short screens, where the sticky dock then covers the
// slide footer. Fit against both dimensions so the complete HTML canvas is
// always visible, while still consuming every available pixel on its limiting
// axis. Static present.html and dynamic deck.html share this outer geometry.
function fitWorkspaceCanvas() {
  const editor = $("#editor");
  const wrap = editor?.querySelector(".canvas-wrap");
  const canvas = $("#canvas");
  if (!editor || !wrap || !canvas) return;

  const fullscreenElement = document.fullscreenElement || document.webkitFullscreenElement;
  const shouldFit = !editor.hidden
    && editor.classList.contains("workspace-ppt")
    && fullscreenElement !== canvas;

  if (!shouldFit) {
    wrap.style.removeProperty("--workspace-canvas-width");
    canvas.style.removeProperty("width");
    canvas.style.removeProperty("height");
    canvas.style.removeProperty("max-width");
    canvas.style.removeProperty("max-height");
    canvas.style.removeProperty("align-self");
    return;
  }

  const wrapStyle = getComputedStyle(wrap);
  const horizontalPadding = parseFloat(wrapStyle.paddingLeft || 0) + parseFloat(wrapStyle.paddingRight || 0);
  const verticalPadding = parseFloat(wrapStyle.paddingTop || 0) + parseFloat(wrapStyle.paddingBottom || 0);
  const dock = wrap.querySelector(".canvas-bottom-dock");
  // The dock is allowed to stretch through the unused lower half of a tall
  // preview column so the speech drawer can stay pinned to its bottom edge.
  // Only reserve the dock's visible children when fitting the 16:9 player;
  // subtracting the stretched outer box would collapse the player to ~1px.
  let dockHeight = 0;
  if (dock && getComputedStyle(dock).display !== "none") {
    const dockStyle = getComputedStyle(dock);
    dockHeight = parseFloat(dockStyle.paddingTop || 0) + parseFloat(dockStyle.paddingBottom || 0);
    for (const child of dock.children) {
      const childStyle = getComputedStyle(child);
      if (childStyle.display === "none" || child.hidden) continue;
      // Speaker notes occupy their own bottom area. Reserve their current
      // height so expanding the drawer scales the slide instead of covering it.
      dockHeight += child.getBoundingClientRect().height;
    }
  }
  const availableWidth = Math.max(1, wrap.clientWidth - horizontalPadding);
  const availableHeight = Math.max(1, wrap.clientHeight - verticalPadding - dockHeight);
  const fittedWidth = Math.min(availableWidth, availableHeight * 16 / 9);
  const fittedHeight = fittedWidth * 9 / 16;

  canvas.style.width = `${Math.floor(fittedWidth * 10) / 10}px`;
  canvas.style.height = `${Math.floor(fittedHeight * 10) / 10}px`;
  canvas.style.maxWidth = "100%";
  canvas.style.maxHeight = `${Math.floor(availableHeight * 10) / 10}px`;
  canvas.style.alignSelf = "center";
  wrap.style.setProperty("--workspace-canvas-width", `${Math.floor(fittedWidth * 10) / 10}px`);
}

function guardSlideDocument(slideFrame) {
  if (!slideFrame) return;
  slideFrame.setAttribute("scrolling", "no");
  try {
    const doc = slideFrame.contentDocument;
    if (!doc?.documentElement || !doc.body) return;
    let style = doc.getElementById("studio-slide-viewport-guard");
    if (!style) {
      style = doc.createElement("style");
      style.id = "studio-slide-viewport-guard";
      style.textContent = `
        html, body {
          width: 100% !important;
          height: 100% !important;
          min-width: 0 !important;
          min-height: 0 !important;
          margin: 0 !important;
          padding: 0 !important;
          overflow: hidden !important;
          overscroll-behavior: none !important;
          scrollbar-width: none !important;
        }
        html::-webkit-scrollbar, body::-webkit-scrollbar { display: none !important; }
        body > .slide, body > [data-slide] {
          position: absolute !important;
          inset: 0 auto auto 0 !important;
          margin: 0 !important;
          max-width: none !important;
          max-height: none !important;
        }
      `;
      doc.head?.appendChild(style);
    }
    slideFrame.contentWindow?.scrollTo(0, 0);
  } catch {
    // Older exported pages can be cross-origin; those keep their native player.
  }
}

function stabilizeStaticDeckPlayer(frame) {
  try {
    const doc = frame.contentDocument;
    if (!doc?.documentElement || !doc.body) return;
    frame.setAttribute("scrolling", "no");
    doc.documentElement.style.setProperty("overflow", "hidden", "important");
    doc.body.style.setProperty("overflow", "hidden", "important");
    let playerGuard = doc.getElementById("studio-static-player-viewport-guard");
    if (!playerGuard) {
      playerGuard = doc.createElement("style");
      playerGuard.id = "studio-static-player-viewport-guard";
      playerGuard.textContent = `
        #stage { overflow: hidden !important; }
        #wrap {
          flex: 0 0 auto !important;
          flex-shrink: 0 !important;
          max-width: none !important;
          max-height: none !important;
        }
        #wrap iframe {
          overflow: hidden !important;
          scrollbar-width: none !important;
        }
      `;
      doc.head?.appendChild(playerGuard);
    }
    const nestedFrames = [...doc.querySelectorAll("#wrap iframe, iframe[data-slide]")];
    nestedFrames.forEach((slideFrame) => {
      if (!slideFrame.dataset.studioViewportGuard) {
        slideFrame.dataset.studioViewportGuard = "1";
        slideFrame.addEventListener("load", () => {
          guardSlideDocument(slideFrame);
          scheduleDeckViewportFit(frame);
        });
      }
      guardSlideDocument(slideFrame);
    });
  } catch {
    // Same-origin is expected for Studio exports; legacy decks retain native behavior.
  }
}

function stabilizeDynamicDeckPlayer(frame) {
  try {
    const doc = frame.contentDocument;
    if (!doc?.documentElement || !doc.body) return;
    let playerGuard = doc.getElementById("studio-dynamic-player-viewport-guard");
    if (!playerGuard) {
      playerGuard = doc.createElement("style");
      playerGuard.id = "studio-dynamic-player-viewport-guard";
      playerGuard.textContent = `
        .stage { overflow: hidden !important; }
        #deck, .stage > .deck {
          flex: 0 0 auto !important;
          flex-shrink: 0 !important;
          max-width: none !important;
          max-height: none !important;
        }
      `;
      doc.head?.appendChild(playerGuard);
    }
  } catch {
    // Same-origin is expected; third-party players keep their own fit logic.
  }
}

function fitDynamicDeckAfterLoad(frame, deckId) {
  let settled = false;
  let attempts = 0;
  const fit = () => {
    if (settled) return;
    if (frame.dataset.deckKind !== "dynamic" || frame.dataset.deckId !== String(deckId)) {
      settled = true;
      frame.removeEventListener("load", fit);
      return;
    }
    try {
      if (!frame.contentDocument?.querySelector("#deck, .deck")) return;
    } catch {
      return;
    }
    settled = true;
    frame.removeEventListener("load", fit);
    stabilizeDynamicDeckPlayer(frame);
    scheduleDeckViewportFit(frame);
    setTimeout(() => scheduleDeckViewportFit(frame), 180);
  };
  frame.addEventListener("load", fit);
  const retry = () => {
    fit();
    attempts += 1;
    if (!settled && attempts < 60) setTimeout(retry, 100);
  };
  retry();
}

function fitDeckViewport(frame = $("#canvas-deck")) {
  if (!frame || frame.hidden || !frame.contentWindow) return;
  frame.setAttribute("scrolling", "no");
  try {
    const doc = frame.contentDocument;
    if (!doc?.documentElement || !doc.body) return;
    doc.documentElement.style.setProperty("overflow", "hidden", "important");
    doc.body.style.setProperty("overflow", "hidden", "important");
    frame.contentWindow.scrollTo(0, 0);
    if (frame.dataset.deckKind === "static") {
      if (editorPresentationKind() === "dynamic") stabilizeDynamicDeckPlayer(frame);
      else stabilizeStaticDeckPlayer(frame);
    }
    if (frame.dataset.deckKind === "dynamic") stabilizeDynamicDeckPlayer(frame);
    // Both canonical static decks and Dazzle decks recalculate their fixed
    // 16:9 stage from innerWidth/innerHeight on resize. Re-dispatch after the
    // iframe has actually acquired its preview/fullscreen dimensions.
    frame.contentWindow.dispatchEvent(new Event("resize"));
  } catch {
    // Cross-origin/legacy output: the iframe's own responsive rules remain active.
  }
}

function scheduleDeckViewportFit(frame = $("#canvas-deck")) {
  cancelAnimationFrame(deckViewportFitRaf);
  deckViewportFitRaf = requestAnimationFrame(() => {
    fitWorkspaceCanvas();
    fitDeckViewport(frame);
    requestAnimationFrame(() => {
      fitWorkspaceCanvas();
      fitDeckViewport(frame);
    });
  });
}

function prepareStaticDeckMotion(frame) {
  try {
    const doc = frame.contentDocument;
    if (!doc?.documentElement || doc.getElementById("studio-static-motion")) return;
    const style = doc.createElement("style");
    style.id = "studio-static-motion";
    style.textContent = `
      html.studio-embedded-player {
        --motion-page-duration: 540ms !important;
        --motion-enter-duration: 660ms !important;
        --motion-page-shift: var(--studio-page-shift, 42px) !important;
        --motion-content-shift: 18px !important;
      }
      html.studio-embedded-player .slide {
        will-change: opacity, transform, filter;
        backface-visibility: hidden;
      }
    `;
    doc.head.appendChild(style);
    doc.documentElement.classList.add("studio-embedded-player");
    stabilizeStaticDeckPlayer(frame);
  } catch {
    // Same-origin is expected; legacy outputs can still fall back to their own player.
  }
}

function playStaticSlide(frame, n) {
  try {
    const deckWindow = frame.contentWindow;
    const player = deckWindow?.cleanDeck;
    if (!player?.go || (player.provisional && Number(player.count) < 1)) return false;
    const dynamicHtml = editorPresentationKind() === "dynamic";
    if (dynamicHtml) stabilizeDynamicDeckPlayer(frame);
    else prepareStaticDeckMotion(frame);
    const current = dynamicDeckActiveSlide(frame);
    const direction = current && n < current ? -1 : 1;
    frame.contentDocument?.documentElement?.style.setProperty("--studio-page-shift", `${direction * 42}px`);
    player.go(n);
    // Provisional players are assembled from whatever fragments exist while a
    // deck is being authored. Some legacy skills emit complete HTML documents
    // instead of embeddable .slide[data-slide] fragments; in that case the
    // provisional shell exists but contains zero playable slides. Never let an
    // empty shell replace the stable rendered PNG.
    if (player.provisional && dynamicDeckActiveSlide(frame) !== Number(n)) return false;
    if (dynamicHtml) stabilizeDynamicDeckPlayer(frame);
    else stabilizeStaticDeckPlayer(frame);
    scheduleDeckViewportFit(frame);
    // A neighboring page can finish lazy-loading during a fast filmstrip jump.
    // Confirm that the canonical player kept the latest requested page instead
    // of exposing a stale nested iframe.
    setTimeout(() => {
      if (frame.hidden || frame.dataset.deckKind !== "static" || Number(ed.sel) !== Number(n)) return;
      const active = dynamicDeckActiveSlide(frame);
      if (active && active !== Number(n)) deckWindow.cleanDeck.go(n);
      if (dynamicHtml) stabilizeDynamicDeckPlayer(frame);
      else stabilizeStaticDeckPlayer(frame);
      scheduleDeckViewportFit(frame);
    }, 80);
    return true;
  } catch {
    return false;
  }
}

function keepStaticPng(n) {
  const img = $("#canvas-img"), frame = $("#canvas-deck"), empty = $("#canvas-empty");
  frame.hidden = true;
  const url = fileUrl(n);
  const token = ++stableStaticImageToken;
  const currentIsUsable = img.complete && img.naturalWidth > 0;
  if (img.src.endsWith(url) && currentIsUsable) {
    img.hidden = false;
    empty.hidden = true;
    return;
  }

  // Keep the last successfully rendered page visible while the requested page
  // is fetched. Directly replacing img.src clears the old bitmap immediately;
  // a stale id, a transient 404 or a slow AFS read would therefore turn the
  // whole preview into an empty canvas. Decode in a detached image first and
  // swap only after the new bitmap is ready.
  if (currentIsUsable) {
    img.hidden = false;
    empty.hidden = true;
  }
  const probe = new Image();
  probe.decoding = "async";
  probe.onload = async () => {
    try { await probe.decode?.(); } catch {}
    if (token !== stableStaticImageToken || ed.kind !== "static" || Number(ed.sel) !== Number(n)
        || ed.workspaceView !== "ppt" || ed.viewMode === "console" || !frame.hidden) return;
    img.src = url;
    img.hidden = false;
    empty.hidden = true;
  };
  probe.onerror = () => {
    if (token !== stableStaticImageToken || ed.kind !== "static" || Number(ed.sel) !== Number(n)) return;
    if (currentIsUsable) {
      img.hidden = false;
      empty.hidden = true;
    }
  };
  probe.src = url;
}

async function revealStaticDeckWhenFontsReady(frame, deckWindow) {
  const deckId = frame.dataset.deckId;
  const dynamicHtml = editorPresentationKind() === "dynamic";
  if (!deckWindow.cleanDeck && typeof deckWindow.__deckGo === "function") {
    const count = frame.contentDocument?.querySelectorAll(".slide").length || 0;
    deckWindow.cleanDeck = {
      go: (slide) => deckWindow.__deckGo(Math.max(0, Number(slide) - 1)),
      count,
      fontsReady: frame.contentDocument?.fonts?.ready || Promise.resolve(),
      senseNovaV2: true,
    };
  }
  // Static decks wait for their bundled fonts so the canonical HTML and PNG
  // remain pixel-consistent. Dynamic HTML must stay live even when a remote
  // webfont is blocked: browser fallback fonts are preferable to silently
  // replacing the deck (and all of its transitions) with a screenshot.
  if (!dynamicHtml) {
    try {
      const ready = deckWindow.cleanDeck?.fontsReady || frame.contentDocument?.fonts?.ready;
      if (ready) await ready;
    } catch {
      // A failed FontFace is checked below; preserve the stable PNG on failure.
    }
  }
  if (frame.dataset.deckKind !== "static" || frame.dataset.deckId !== deckId || frame.contentWindow !== deckWindow) return;
  const failed = (() => {
    try { return [...(frame.contentDocument?.fonts || [])].some((face) => face.status === "error"); }
    catch { return true; }
  })();
  const player = deckWindow.cleanDeck;
  if ((!dynamicHtml && failed) || !player?.go || (player.provisional && Number(player.count) < 1)) {
    frame.dataset.fontsReady = "0";
    keepStaticPng(ed.sel);
    return;
  }
  frame.dataset.fontsReady = "1";
  const pending = Number(frame.dataset.pendingSlide) || ed.sel;
  if (!playStaticSlide(frame, pending)) {
    frame.dataset.fontsReady = "0";
    keepStaticPng(pending);
    return;
  }
  delete frame.dataset.pendingSlide;
  frame.hidden = false;
  scheduleDeckViewportFit(frame);
  frame.classList.add("font-pending");
  frame.classList.remove("font-ready");
  requestAnimationFrame(() => {
    frame.classList.remove("font-pending");
    frame.classList.add("font-ready");
    setTimeout(() => {
      if (frame.dataset.deckKind === "static" && frame.dataset.deckId === deckId) {
        $("#canvas-img").hidden = true;
      }
    }, 220);
  });
}

function showStaticDeck(n) {
  const frame = $("#canvas-deck");
  const deckId = String(ed.id);
  const sameDeck = frame.dataset.deckKind === "static" && frame.dataset.deckId === deckId;
  const requestedStamp = String(ed.staticStamp || 0);
  const targetIsDirty = !!ed.staticDirtyPages?.has(Number(n));
  const mustReload = sameDeck && targetIsDirty && frame.dataset.deckStamp !== requestedStamp;
  frame.dataset.pendingSlide = String(n);
  $("#canvas-empty").hidden = true;
  if (sameDeck && !mustReload && frame.dataset.fontsReady === "1") {
    frame.hidden = false;
    $("#canvas-img").hidden = true;
    if (playStaticSlide(frame, n)) delete frame.dataset.pendingSlide;
    return;
  }
  keepStaticPng(n);
  if (sameDeck && !mustReload) return;
  frame.dataset.deckKind = "static";
  frame.dataset.deckId = deckId;
  frame.dataset.deckStamp = requestedStamp;
  frame.dataset.fontsReady = "0";
  frame.classList.add("font-pending");
  frame.classList.remove("font-ready");
  // This request contains every page revision known at requestedStamp. Changes
  // arriving while it loads will be added to the set again by apply().
  ed.staticDirtyPages?.clear();
  frame.src = `${ed.staticDeckUrl}?slide=${n}&t=${ed.staticStamp}`;
}

function outlineBriefData() {
  let query = String(ed.briefMeta?.user_query || ed.fullQuery || "").trim();
  const preferenceMarker = "\n\n本次演示偏好：";
  if (query.includes(preferenceMarker)) {
    [query] = query.split(preferenceMarker, 1);
  }
  const attachmentMarkers = ["\n\n【附件资料】", "\n\n【附件处理模式:pipeline_agent】"];
  let attachmentBlock = "";
  for (const marker of attachmentMarkers) {
    if (!query.includes(marker)) continue;
    [query, attachmentBlock] = query.split(marker, 2);
    break;
  }
  const attachments = (ed.attachments || []).map((item, fallbackIndex) => {
    const index = Number.isInteger(Number(item.index)) ? Number(item.index) : fallbackIndex;
    const preview = deckAttachmentPreviewUrls(item, index);
    return {
      index,
      name: item.name || item.stored_name || "附件",
      stored_name: item.stored_name || "",
      type: item.type || "file",
      size: Number(item.size) || 0,
      url: preview.url,
      fallbackUrl: preview.fallbackUrl,
      previewUrl: preview.previewUrl,
    };
  });
  if (!attachments.length && attachmentBlock) {
    const names = [...attachmentBlock.matchAll(/^\s*\d+\.\s*(?:文件名:\s*)?(.+?)(?:\s+\([^\n]+\)\s*->.*)?$/gm)]
      .map((match) => match[1].trim())
      .filter((name) => name && !name.startsWith("本请求"));
    names.forEach((name) => attachments.push({ name, type: name.split(".").pop() || "file", size: 0 }));
  }
  return { query: query.trim(), attachments };
}

function outlineBriefMetaHtml(data) {
  const attachments = data.attachments.length ? `<div class="outline-attachments" aria-label="本次上传的附件">
    ${data.attachments.map((item) => {
      const kind = attachmentPreviewKind(item.name, item.type);
      const icon = kind === "image" ? "▧" : (kind === "pdf" ? "PDF" : "↥");
      const meta = [attachmentTypeLabel(item.name, item.type), attachmentSizeLabel(item.size)].filter(Boolean).join(" · ");
      const visual = kind === "image" && item.url
        ? `<i><img src="${escapeHtml(item.url)}" alt=""${item.fallbackUrl ? ` data-attachment-image-fallback="${escapeHtml(item.fallbackUrl)}"` : ""}></i>`
        : `<i>${icon}</i>`;
      const attrs = item.url
        ? ` data-attachment-preview="1" data-attachment-url="${escapeHtml(item.url)}" data-attachment-fallback-url="${escapeHtml(item.fallbackUrl || "")}" data-attachment-preview-url="${escapeHtml(item.previewUrl || "")}" data-attachment-name="${escapeHtml(item.name)}" data-attachment-type="${escapeHtml(item.type)}" data-attachment-size="${item.size}"`
        : " disabled";
      return `<button type="button" class="outline-attachment ${kind}" title="查看 ${escapeHtml(item.name)}"${attrs}>${visual}<span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(meta)}</small></span></button>`;
    }).join("")}
  </div>` : "";
  return attachments;
}

function taskComposerCopy(kind = ed.kind, status = ed.status) {
  if (kind === "dynamic" || ed.pptOutput === "dynamic_html") return {
    placeholder: "继续修改这份演示，例如：把第 3 页改得更有冲击力…",
    helper: status === "running" ? "动态演示生成中" : "继续描述修改意见",
    disabled: status === "running",
  };
  if (ed.briefMeta?.revision_supported === false) return {
    placeholder: "当前生成版本暂不支持连续编辑…",
    helper: "请使用支持静态续编的 Long-horizon / Clean 生成版本",
    disabled: true,
  };
  if (status === "waiting") return {
    placeholder: "已有一条修改要求在等待执行…",
    helper: "当前修订已排队，上一版完成后自动开始",
    disabled: true,
  };
  if (isStaticGeneratingStatus(status)) return {
    placeholder: "当前修改正在执行，完成后可继续提出要求…",
    helper: "每轮修改都会直接更新当前演示",
    disabled: true,
  };
  if (status !== "completed") return {
    placeholder: "当前版本未成功完成，无法继续修改…",
    helper: "请先重新生成一份可用成稿",
    disabled: true,
  };
  return {
    placeholder: "继续修改这份演示，例如：精简第 3 页并增强配图…",
    helper: "基于当前成稿创建可回溯的修订版本",
    disabled: false,
  };
}

function conversationDate(value) {
  if (value == null || value === "") return null;
  const numeric = Number(value);
  const date = Number.isFinite(numeric)
    ? new Date(Math.abs(numeric) < 1e12 ? numeric * 1000 : numeric)
    : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function conversationDayLabel(date) {
  if (!date) return "";
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const target = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const days = Math.round((start - target) / 86400000);
  if (days === 0) return "今天";
  if (days === 1) return "昨天";
  if (date.getFullYear() === today.getFullYear()) return `${date.getMonth() + 1}月${date.getDate()}日`;
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;
}

function conversationTime(value, { withDay = false } = {}) {
  const date = conversationDate(value);
  if (!date) return null;
  const clock = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
  return { date, iso: date.toISOString(), text: withDay ? `${conversationDayLabel(date)} ${clock}` : clock };
}

function outlineMessageTimeMarkup(value, className = "") {
  const time = conversationTime(value);
  const full = conversationTime(value, { withDay: true });
  return time && full
    ? `<time class="outline-message-time${className ? ` ${className}` : ""}" datetime="${escapeHtml(time.iso)}" title="${escapeHtml(full.text)}">${escapeHtml(time.text)}</time>`
    : "";
}

function outlineTimeDividerMarkup(value, round) {
  const time = conversationTime(value, { withDay: true });
  if (!time) return "";
  const stage = round <= 1 ? "开始对话" : `第 ${round} 轮`;
  return `<div class="outline-time-divider" role="separator" aria-label="${escapeHtml(`${stage}，${time.text}`)}"><span></span><time datetime="${escapeHtml(time.iso)}">${escapeHtml(stage)} · ${escapeHtml(time.text)}</time><span></span></div>`;
}

function outlineUserTurnMarkup(content, { initial = false, createdAt = null } = {}) {
  const text = String(content || "").trim() || (initial ? "正在读取你的演示需求…" : "");
  const displayName = $(".user-profile-copy strong")?.textContent?.trim() || "用户";
  const v1User = document.querySelector(".layout")?.dataset.authEnabled === "0";
  const avatarContent = v1User
    ? '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="rgba(255,255,255,.94)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8.2" r="3.25"/><path d="M5.8 19c.55-3.45 2.55-5.2 6.2-5.2s5.65 1.75 6.2 5.2"/></svg>'
    : escapeHtml($("#user-menu-trigger .user-avatar")?.textContent?.trim()
      || Array.from(displayName)[0]?.toUpperCase() || "U");
  const copy = `<button type="button" class="outline-query-copy"${initial ? ' id="outline-query-copy"' : ""} data-copy-message="${escapeHtml(text)}" title="复制用户消息" aria-label="复制用户消息">复制</button>`;
  const meta = initial ? `<div id="outline-query-meta">${outlineBriefMetaHtml(outlineBriefData())}</div>` : "";
  return `<section class="outline-user-turn${initial ? " initial" : " followup"}">
    <div class="outline-user-identity"><span class="outline-turn-label">${escapeHtml(displayName)}</span><span class="outline-user-avatar${v1User ? " outline-user-avatar-v1" : ""}" aria-hidden="true">${avatarContent}</span></div>
    <div class="outline-user-bubble"><p${initial ? ' id="outline-user-query"' : ""}>${escapeHtml(text)}</p>${meta}</div>
    <div class="outline-user-actions">${outlineMessageTimeMarkup(createdAt)}${copy}</div>
  </section>`;
}

function outlineTaskConfigMarkup(kind, config) {
  return `<details class="outline-task-config" id="outline-task-config" data-kind="${kind}">
    <summary>
      <span class="outline-config-mark" aria-hidden="true">${agentIdentityGlyph(kind, "outline-config-glyph")}</span>
      <b>本次生成配置</b>
      <i id="outline-config-summary">${escapeHtml(`${config.model} · ${config.skill} · 深度思考：${config.thinking === "开启" ? "已开启" : "未开启"}`)}</i>
      <em aria-hidden="true">⌄</em>
    </summary>
    <div class="outline-config-body">
      <div><span>模型</span><b id="outline-config-model">${escapeHtml(config.model)}</b></div>
      <div><span>Skill</span><b id="outline-config-skill">${escapeHtml(config.skill)}</b></div>
      <div><span>深度思考</span><b id="outline-config-thinking">${escapeHtml(config.thinking)}</b></div>
      <div><span>Agent 上限</span><b id="outline-config-runtime">${escapeHtml(config.runtime)}</b></div>
      <div id="outline-config-pref-row"${config.preferences ? "" : " hidden"}><span>偏好</span><b id="outline-config-pref">${escapeHtml(config.preferences || "—")}</b></div>
    </div>
  </details>`;
}

function outlineAssistantSummary(turn) {
  const revision = Number(turn?.revision_no) || 0;
  const pages = Number(turn?.slide_count) || 0;
  const status = String(turn?.status || "completed");
  if (status === "completed") return {
    title: revision ? "已按要求完成修改" : "初版演示已经完成",
    detail: pages ? `已完成 ${pages} 页演示，可以继续提出修改意见。` : "演示已经完成，可以继续提出修改意见。",
    state: "completed",
  };
  if (["failed", "rejected", "error"].includes(status)) return {
    title: revision ? "本轮修改未能完成" : "初版演示未能完成",
    detail: "可以调整要求后重新尝试。", state: "failed",
  };
  return { title: revision ? "正在处理这条修改" : "正在制作演示", detail: "SenseNova Present 正在继续推进。", state: "running" };
}

function dynamicAssistantSummary(turn) {
  const logs = turn?.logs || [];
  const meaningful = [...logs].reverse().find((entry) => ["final", "text", "note"].includes(entry.kind) && entry.text)?.text;
  const toolCount = logs.filter((entry) => entry.kind === "tool").length;
  const completed = turn?.status === "completed";
  const compact = meaningful ? compactPageAgentResult(meaningful, { completed }).summary : "";
  return {
    title: completed ? "本轮制作已经完成" : (turn?.status === "error" ? "本轮制作未能完成" : "已完成上一阶段处理"),
    detail: compact || (toolCount ? `本轮共推进 ${toolCount} 项制作步骤。` : "演示内容已根据你的要求完成处理。"),
    state: completed ? "completed" : (turn?.status === "error" ? "failed" : "completed"),
  };
}

function dynamicHistoricalProcessMarkup(turn) {
  const label = { tool: "制作步骤", text: "Agent 回复", note: "进展说明", final: "最终回复", error: "异常记录" };
  const seen = new Set();
  const entries = (turn?.logs || []).flatMap((entry, index) => {
    const kind = String(entry?.kind || "text");
    if (!Object.prototype.hasOwnProperty.call(label, kind)) return [];
    const text = cleanAgentText(entry?.text);
    if (!text) return [];
    const fingerprint = `${kind}\u0000${text}`;
    if (seen.has(fingerprint)) return [];
    seen.add(fingerprint);
    return [{ kind, text, index }];
  });
  const rows = entries.length ? entries.map((entry) => `<article class="dynamic-history-entry ${entry.kind}">
      <span class="dynamic-history-mark" aria-hidden="true"></span>
      <div><b>${label[entry.kind]}</b><div class="agent-markdown">${agentMarkdownHtml(entry.text)}</div></div>
    </article>`).join("") : '<div class="outline-history-unavailable">这一轮暂时没有可展示的详细记录。</div>';
  return `<div class="outline-agent-history-process dynamic-history-process">
      <div class="dynamic-history-head"><b>本轮完整制作过程</b><span>${entries.length} 条记录</span></div>
      <div class="dynamic-history-list">${rows}</div>
    </div>`;
}

function outlineHistoricalAgentMarkup(turn, kind) {
  const summary = kind === "dynamic" ? dynamicAssistantSummary(turn) : outlineAssistantSummary(turn);
  const revision = Math.max(0, Number(turn?.revision_no) || 0);
  const process = kind === "static" ? `<div class="outline-agent-history-process">
        <div class="outline-agent-progress outline-agent-history-progress" data-revision="${revision}">
          <div class="outline-history-loading"><i></i><span>正在载入这一轮的完整制作过程…</span></div>
        </div>
      </div>` : dynamicHistoricalProcessMarkup(turn);
  const open = kind === "static" ? " open" : "";
  return `<div class="outline-thread-bridge" aria-hidden="true"><span></span></div>
    <section class="outline-agent-turn outline-agent-history ${summary.state}">
      <div class="outline-agent-label"><span class="outline-agent-mark">${agentIdentityGlyph(kind)}</span><span class="outline-agent-copy"><b>SenseNova Present</b><i>上一轮回复</i></span>${outlineMessageTimeMarkup(turn?.responded_at || turn?.created_at, "agent")}</div>
      <details class="outline-agent-history-details"${open}>
        <summary class="outline-agent-history-card"><span class="outline-history-state" aria-hidden="true"></span><div><strong>${escapeHtml(summary.title)}</strong><p>${escapeHtml(summary.detail)}</p></div><i class="outline-history-chevron" aria-hidden="true"></i></summary>
        ${process}
      </details>
    </section>`;
}

function outlineCurrentAgentMarkup(kind, running, turn = {}) {
  const responseLabel = ed.status === "not_started" ? "等待开始" : (running ? "正在思考并推进" : "本次任务回复");
  return `<div class="outline-thread-bridge" aria-hidden="true"><span></span></div>
    <section class="outline-agent-turn">
      <div class="outline-agent-label"><span class="outline-agent-mark">${agentIdentityGlyph(kind)}</span><span class="outline-agent-copy"><b>SenseNova Present</b><i>${responseLabel}</i></span>${outlineMessageTimeMarkup(turn?.responded_at || turn?.created_at, "agent")}</div>
      <div class="outline-agent-progress" id="agent-progress-console"></div>
    </section>`;
}

function outlineConversationTurns(kind, data) {
  let turns = kind === "dynamic" ? (ed.dynamicChatTurns || []) : (ed.staticConversationTurns || []);
  if (!turns.length) {
    turns = [{ role: "user", content: data.query || "" }];
    const followups = kind === "dynamic" ? (ed.dynamicFollowups || []) : (ed.staticFollowups || []);
    followups.forEach((content) => {
      turns.push({ role: "assistant", status: "completed", slide_count: ed.total || ed.briefMeta?.slide_count || 0 });
      turns.push({ role: "user", content });
    });
    turns.push({ role: "assistant", current: true, status: ed.status });
  }
  return turns.map((turn, index) => index === 0 && turn.role === "user"
    ? { ...turn, content: data.query || turn.content }
    : turn);
}

function outlineConversationLaneMarkup(kind, running) {
  const data = outlineBriefData();
  const turns = outlineConversationTurns(kind, data);
  let currentRendered = false;
  let round = 0;
  const html = turns.map((turn, index) => {
    if (turn.role === "user") {
      const initial = index === 0;
      round += 1;
      return outlineTimeDividerMarkup(turn.created_at, round)
        + outlineUserTurnMarkup(turn.content, { initial, createdAt: turn.created_at });
    }
    if (turn.current || (index === turns.length - 1 && turn.role === "assistant")) {
      currentRendered = true;
      return outlineCurrentAgentMarkup(kind, running, turn);
    }
    return outlineHistoricalAgentMarkup(turn, kind);
  }).join("");
  return html + (currentRendered ? "" : outlineCurrentAgentMarkup(kind, running));
}

function outlineConversationStructureKey(kind) {
  const brief = outlineBriefData();
  return JSON.stringify({ attachments: brief.attachments, turns: outlineConversationTurns(kind, brief).map((turn) => ({
    role: turn.role, content: turn.content, deck_id: turn.deck_id, status: turn.status,
    revision_no: turn.revision_no, slide_count: turn.slide_count, current: !!turn.current,
    created_at: turn.created_at, responded_at: turn.responded_at,
  })) });
}

function syncOutlineConversationTurns() {
  const lane = $("#outline-chat-lane");
  if (!lane) return;
  const displayKind = editorPresentationKind();
  const key = outlineConversationStructureKey(displayKind);
  if (lane.dataset.turnKey !== key) {
    lane.dataset.turnKey = key;
    const running = ed.kind === "dynamic" ? ed.status === "running" : isStaticActiveStatus(ed.status);
    lane.innerHTML = outlineConversationLaneMarkup(displayKind, running);
  }
  syncTaskConfig();
  if (displayKind === "static") syncStaticHistoricalTurnFeeds();
}

async function syncStaticHistoricalTurnFeeds() {
  if (ed.kind !== "static" || !ed.id) return;
  const nodes = $$(".outline-agent-history-progress[data-revision]");
  await Promise.all(nodes.map(async (node) => {
    if (node.dataset.feedState === "loading" || node.dataset.feedState === "ready") return;
    node.dataset.feedState = "loading";
    const revision = Number(node.dataset.revision) || 0;
    try {
      const payload = await jget(`/api/decks/${encodeURIComponent(ed.id)}/turn-feed?revision_no=${revision}`);
      if (ed.kind !== "static" || !node.isConnected) return;
      node.dataset.feedState = "ready";
      renderOrchestrationProgress(node, payload.agents || {}, {
        running: false,
        specialistArtifacts: payload.specialist_artifacts || {},
      });
    } catch {
      if (!node.isConnected) return;
      node.dataset.feedState = "error";
      node.innerHTML = '<div class="outline-history-unavailable">这一轮的归档过程暂不可用，任务结果仍保留在会话中。</div>';
    }
  }));
}

function taskComposerMarkup(kind, status) {
  const copy = taskComposerCopy(kind, status);
  const draft = taskComposerDrafts.get(taskComposerKey(kind, ed.id)) || "";
  return `<div class="task-composer-wrap" data-kind="${kind}">
    <form class="task-composer" id="task-composer" autocomplete="off">
      <div class="task-composer-main">
        <textarea id="task-composer-input" rows="1" maxlength="8000" placeholder="${escapeHtml(copy.placeholder)}">${escapeHtml(draft)}</textarea>
        <button type="submit" id="task-composer-send" aria-label="发送修改要求" title="${copy.disabled ? escapeHtml(copy.helper) : "发送修改要求"}"${copy.disabled ? " disabled" : ""}>
          <svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M10 15.5V4.5M5.5 9 10 4.5 14.5 9"/></svg>
        </button>
      </div>
      <div class="task-composer-foot">
        <span class="task-composer-helper"><i></i><em>${escapeHtml(copy.helper)}</em></span>
        <span class="task-composer-shortcut">⌘/Ctrl + Enter</span>
      </div>
    </form>
  </div>`;
}

function resizeGrowingTextarea(input, { maxHeight = 236, viewportRatio = .32 } = {}) {
  if (!input) return;
  cancelAnimationFrame(input.__growFrame);
  input.__growFrame = requestAnimationFrame(() => {
    // offsetHeight is expressed in unzoomed CSS layout pixels. The Studio UI
    // uses body zoom, while getBoundingClientRect() returns the zoomed visual
    // height; comparing the latter with scrollHeight made every keystroke look
    // like a height change and continuously restarted the row animation.
    const currentHeight = input.offsetHeight;
    const minimum = Number.parseFloat(getComputedStyle(input).minHeight) || 0;
    const viewportLimit = Math.max(minimum, Math.floor(window.innerHeight * viewportRatio));
    const limit = Math.max(minimum, Math.min(maxHeight, viewportLimit));
    const inlineTransition = input.style.transition;
    input.style.transition = "none";
    input.style.height = "auto";
    const naturalHeight = input.scrollHeight;
    const targetHeight = Math.max(minimum, Math.min(naturalHeight, limit));
    const startHeight = currentHeight || minimum;
    const heightChanged = Math.abs(targetHeight - startHeight) > .5;
    input.style.height = `${heightChanged ? startHeight : targetHeight}px`;
    if (heightChanged) input.offsetHeight;
    if (inlineTransition) input.style.transition = inlineTransition;
    else input.style.removeProperty("transition");
    if (heightChanged) input.style.height = `${targetHeight}px`;
    input.style.overflowY = naturalHeight > limit + 1 ? "auto" : "hidden";
    input.classList.toggle("is-expanded", targetHeight > minimum + 8);
    if (input.id === "q") {
      clearTimeout(input.__growClampTimer);
      input.__growClampTimer = setTimeout(() => clampPrimaryComposerToViewport(input, naturalHeight, minimum), 270);
    }
  });
}

function clampPrimaryComposerToViewport(input, naturalHeight, minimum) {
  const dock = input?.closest(".composer-dock");
  const workspace = input?.closest(".creation-workspace");
  if (!dock || !workspace) return;
  const workspaceBottom = Math.min(window.innerHeight, workspace.getBoundingClientRect().bottom);
  const overflow = dock.getBoundingClientRect().bottom - workspaceBottom + 12;
  if (overflow <= 0) return;
  const visualHeight = input.getBoundingClientRect().height;
  const layoutHeight = input.offsetHeight;
  const visualScale = layoutHeight > 0 ? visualHeight / layoutHeight : 1;
  const correctedHeight = Math.max(minimum, layoutHeight - overflow / Math.max(visualScale, .01));
  input.style.height = `${correctedHeight}px`;
  input.style.overflowY = naturalHeight > correctedHeight + 1 ? "auto" : "hidden";
  input.classList.toggle("is-expanded", correctedHeight > minimum + 8);
}

function resizeTaskComposerInput(input) {
  resizeGrowingTextarea(input, { maxHeight: 196, viewportRatio: .26 });
}

function resizePrimaryComposerInput(input = $("#q")) {
  resizeGrowingTextarea(input, { maxHeight: 176, viewportRatio: .26 });
}

function resetGrowingTextarea(input) {
  if (!input) return;
  cancelAnimationFrame(input.__growFrame);
  input.style.height = "auto";
  input.style.overflowY = "hidden";
  input.classList.remove("is-expanded");
  resizeGrowingTextarea(input, input.id === "q"
    ? { maxHeight: 176, viewportRatio: .26 }
    : { maxHeight: 196, viewportRatio: .26 });
}

function syncTaskComposerState() {
  const form = $("#task-composer");
  if (!form || ed.sel !== 0) return;
  const copy = taskComposerCopy(ed.kind, ed.status);
  const input = $("#task-composer-input");
  const button = $("#task-composer-send");
  const helper = form.querySelector(".task-composer-helper em");
  input.placeholder = copy.placeholder;
  button.disabled = copy.disabled || form.classList.contains("is-sending");
  button.title = copy.disabled ? copy.helper : "发送修改要求";
  if (helper.textContent !== copy.helper) helper.textContent = copy.helper;
  form.closest(".task-composer-wrap")?.classList.toggle("is-locked", copy.disabled);
}

function bindTaskComposer() {
  const form = $("#task-composer");
  const input = $("#task-composer-input");
  if (!form || !input || form.dataset.bound === "1") return;
  form.dataset.bound = "1";
  resizeTaskComposerInput(input);
  input.addEventListener("input", () => {
    taskComposerDrafts.set(taskComposerKey(), input.value);
    resizeTaskComposerInput(input);
  });
  input.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      if (!$("#task-composer-send")?.disabled) form.requestSubmit();
    }
  });
  form.addEventListener("submit", sendTaskFollowup);
  syncTaskComposerState();
}

async function sendTaskFollowup(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const input = $("#task-composer-input");
  const message = input?.value.trim();
  if (!message) return;
  if (taskComposerCopy(ed.kind, ed.status).disabled) return;
  if (!requireAuthentication()) return;
  form.classList.add("is-sending");
  syncTaskComposerState();
  const helper = form.querySelector(".task-composer-helper em");
  helper.textContent = "正在发送修改要求…";
  try {
    const isDynamic = ed.kind === "dynamic";
    const response = await fetch(
      isDynamic ? "/api/dynamic/send" : `/api/decks/${ed.id}/continue`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(isDynamic ? { conv_id: String(ed.id), message } : { message }),
      },
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.error || "修改要求发送失败");
    taskComposerDrafts.delete(taskComposerKey());
    input.value = "";
    resetGrowingTextarea(input);
    if (isDynamic) {
      ed.status = "running";
      ed.dynamicPhase = "render";
      subscribeDynamic(String(ed.id));
      startDynamicConversationPoll(String(ed.id));
      if (!ed.timer) ed.timer = setInterval(tick, 1000);
      renderDynamicChrome();
    } else {
      const revisionId = Number(payload.deck_id);
      await loadDecks(revisionId);
      await openDeck(revisionId);
    }
  } catch (error) {
    alert("发送失败: " + error.message);
  } finally {
    form.classList.remove("is-sending");
    syncTaskComposerState();
  }
}

function agentIdentityGlyph(kind = ed.kind, className = "agent-identity-glyph") {
  if (kind === "static") {
    return `<svg class="${className}" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <rect x="3" y="3" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
      <rect x="11.5" y="3" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
      <rect x="3" y="11.5" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
      <rect x="11.5" y="11.5" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
    </svg>`;
  }
  return `<svg class="${className}" viewBox="0 0 20 20" fill="none" aria-hidden="true">
    <path d="M10 2.7c.7 3.56 3.06 5.92 6.62 6.62-3.56.7-5.92 3.06-6.62 6.62-.7-3.56-3.06-5.92-6.62-6.62C6.94 8.62 9.3 6.26 10 2.7Z" fill="currentColor"/>
  </svg>`;
}

function syncOutlineBrief() {
  const query = $("#outline-user-query");
  const meta = $("#outline-query-meta");
  if (!query || !meta) return;
  const data = outlineBriefData();
  const queryText = data.query || "正在读取你的演示需求…";
  if (query.textContent !== queryText) query.textContent = queryText;
  const metaHtml = outlineBriefMetaHtml(data);
  if (meta.dataset.renderKey !== metaHtml) {
    meta.dataset.renderKey = metaHtml;
    meta.innerHTML = metaHtml;
  }
}

function outlineConversationMarkup({ kind, running }) {
  const dynamic = kind === "dynamic";
  const mode = dynamic ? "动态演示" : "静态演示";
  return `
    <div class="ce-top workstream-head outline-head${dynamic ? " dynamic" : ""}">
      <span class="big-no" aria-hidden="true">00</span>
      <div class="ce-meta">
        <div class="outline-head-copy"><div class="ce-text">任务会话与整体规划</div><div class="ce-sub">从需求理解到演示结构，关键进展会持续更新</div></div>
        <span class="outline-mode-pill"><i></i>${mode}</span>
        ${running ? '<div class="develop-ring mini" aria-label="正在制作"></div>' : ""}
      </div>
    </div>
    <div class="term workstream outline-conversation${running ? "" : " static"}" data-kind="${kind}">
      <div class="term-body outline-chat-body" id="gen-console">
        <div class="outline-chat-lane" id="outline-chat-lane" data-turn-key="${escapeHtml(outlineConversationStructureKey(kind))}">${outlineConversationLaneMarkup(kind, running)}</div>
      </div>
      ${taskComposerMarkup(kind, ed.status)}
    </div>`;
}

function workstreamMarkup({ n, kind, running, rendered = false }) {
  const outline = n === 0;
  if (outline) return outlineConversationMarkup({ kind, running });
  const dynamic = kind === "dynamic";
  const title = dynamic
    ? `第 ${pad2(n)} 页 · ${running && !rendered ? "正在编排页面与动效" : "页面与动效制作记录"}`
    : `第 ${pad2(n)} 页 · ${running && !rendered ? "正在制作内容与画面" : "内容与画面制作记录"}`;
  const agent = dynamic
    ? "正在持续完善页面布局、动效与呈现"
    : "正在制作本页内容与视觉";
  const state = running && !rendered ? "正在制作" : (rendered ? "页面已完成" : "工作记录");
  return `
    <div class="ce-top workstream-head">
      <span class="big-no">${pad2(n)}</span>
      <div class="ce-meta">
        <div><div class="ce-text">${title}<span class="ce-task" id="ce-task"></span></div><div class="ce-sub" id="ce-agent">${agent}</div></div>
        ${running && !rendered ? '<div class="develop-ring mini" aria-label="正在制作"></div>' : ""}
      </div>
    </div>
    <div class="term workstream${running ? "" : " static"}" data-kind="${kind}">
      <div class="term-bar"><span class="workstream-mark">✦</span><span class="term-title" id="term-title">${dynamic ? "本页动态制作进展" : "本页制作进展"}</span><span class="workstream-status"><i></i>${state}</span></div>
      <div class="term-body" id="gen-console"><div class="lv-caret"><i></i><span>${running ? "AI 正在推进当前任务" : "以上为本页制作记录"}</span></div></div>
    </div>`;
}

function select(n, { byUser } = {}) {
  if (ed.kind === "dynamic") return selectDynamic(n, { byUser });
  if (byUser) setFollow(false);
  ed.sel = n;
  setRailPageScope(n);
  if (n > 0) loadPageHistory(n);
  markSel();
  const img = $("#canvas-img"), frame = $("#canvas-deck"), empty = $("#canvas-empty");
  if (n === 0) {
    // 00 · 大纲:画布 = 编排器(主 agent)输出
    const running = isStaticActiveStatus(ed.status);
    img.hidden = true; frame.hidden = true; empty.hidden = false;
    empty.innerHTML = workstreamMarkup({ n, kind: "static", running });
    bindTaskComposer();
    // The 00 conversation lane is created here, after the initial progress
    // payload has already called syncOutlineConversationTurns().  Historical
    // turn placeholders therefore did not exist during that earlier sync and
    // stayed in their loading state forever.  Load them as soon as this lane is
    // mounted (also covers returning to 00 from another slide).
    void syncStaticHistoricalTurnFeeds();
    ed.conKey = null; ed.conLines = [];
    if (!running && !ed.liveTimer) fetchLive();   // 已完成的 deck:拉一次历史输出
    else renderLive();
    syncViewToggle();
    loadPlan(0);
    updateNav();
    return;
  }
  const showConsole = ed.viewMode === "console";
  if (ed.rendered.has(n) && !showConsole) {
    if (ed.staticDeckReady && ed.staticDeckUrl) {
      // 完成态先保留稳定 PNG；字体就绪后再切到 canonical present.html。
      showStaticDeck(n);
    } else {
      // 生成过程中 present.html 仍可能被重建，继续使用稳定的逐页渲染图。
      keepStaticPng(n);
    }
  } else {
    // 制作进度模式或页面尚未渲染：展示语义化 AI 工作流，而非底层命令行。
    const running = isStaticActiveStatus(ed.status);
    const rendered = ed.rendered.has(n);
    img.hidden = true; frame.hidden = true; empty.hidden = false;
    empty.innerHTML = workstreamMarkup({ n, kind: "static", running, rendered });
    ed.conKey = null; ed.conLines = [];
    if (!running && !ed.liveTimer && !Object.keys(ed.feed || {}).length) fetchLive();
    else renderLive();
  }
  syncViewToggle();
  loadPlan(n);
  updateNav();
}

function selectDynamic(n, { byUser } = {}) {
  if (byUser) setFollow(false);
  ed.sel = n; markSel();
  setRailPageScope(n);
  if (n > 0) loadDynamicPageSpeech(n);
  const img = $("#canvas-img"), frame = $("#canvas-deck"), empty = $("#canvas-empty");
  const rendered = ed.rendered.has(n);
  const showDeck = n > 0 && rendered && ed.dynamicDeckUrl && ed.viewMode !== "console";
  if (showDeck) {
    const deckId = String(ed.id);
    const requestedStamp = String(ed.dynamicStamp || 0);
    const sameDeck = frame.dataset.deckKind === "dynamic" && frame.dataset.deckId === deckId;
    const sameArtifact = sameDeck && frame.dataset.deckStamp === requestedStamp;
    if (sameArtifact && frame.dataset.deckReady === "1" && playDynamicSlide(frame, n)) {
      frame.hidden = false;
      img.hidden = true;
      empty.hidden = true;
      $("#plan-panel").hidden = true;
      syncViewToggle(); updateNav();
      return;
    }
    frame.dataset.pendingSlide = String(n);
    frame.dataset.revealToken = String(++dynamicDeckRevealToken);
    // Keep the live HTML mounted but fully transparent while it positions the
    // requested page. Do not pass through a rendered PNG between live states.
    img.hidden = true;
    empty.hidden = true;
    frame.hidden = false;
    frame.classList.add("font-pending");
    frame.classList.remove("font-ready");
    // The iframe is still loading. Its load handler will apply the latest
    // pending page through __deckGo, so rapid thumbnail clicks stay smooth.
    if (sameArtifact && ["0", "revealing"].includes(frame.dataset.deckReady)) {
      if (typeof frame.contentWindow?.__deckGo === "function") revealDynamicDeckSlide(frame, n);
      $("#plan-panel").hidden = true;
      syncViewToggle(); updateNav();
      return;
    }
    frame.dataset.deckKind = "dynamic";
    frame.dataset.deckId = deckId;
    frame.dataset.deckStamp = requestedStamp;
    frame.dataset.deckReady = "0";
    const src = `${ed.dynamicDeckUrl}?slide=${n}&t=${requestedStamp}`;
    fitDynamicDeckAfterLoad(frame, ed.id);
    if (!frame.src.endsWith(src)) frame.src = src;
  } else {
    frame.hidden = true; empty.hidden = false;
    const running = ed.status === "running";
    empty.innerHTML = workstreamMarkup({ n, kind: "dynamic", running, rendered });
    if (n === 0) bindTaskComposer();
    renderDynamicConsole();
  }
  $("#plan-panel").hidden = true;
  syncViewToggle(); updateNav();
}

// iframe 内自行翻页时，同步主编辑器页码与左侧胶片条。动态 Dazzle Deck 与静态
// present.html 都派发 slidechange；同时读 active slide，兼容无 detail 的旧 Deck。
function dynamicDeckActiveSlide(frame) {
  try {
    const active = frame.contentDocument?.querySelector(".slide.active, [data-slide].active");
    if (!active) return 0;
    const explicit = Number(active.dataset.slide);
    if (Number.isInteger(explicit) && explicit > 0) return explicit;
    const slides = [...frame.contentDocument.querySelectorAll(".slide, [data-slide]")];
    const index = slides.indexOf(active);
    return index >= 0 ? index + 1 : 0;
  } catch {
    return 0;
  }
}

// Dynamic Skills historically kept their navigator (setSlide/goto) inside an
// IIFE, so Studio could not call it from the parent page. Reloading deck.html
// with a different ?slide= value worked, but tore down the live canvas/Three.js
// scene and exposed the iframe background between every filmstrip click. Add a
// same-origin bridge for those exports so page changes happen in the already
// painted document. Native __deckGo implementations remain authoritative.
function installDynamicDeckAdapter(frame) {
  try {
    const deckWindow = frame?.contentWindow;
    const doc = frame?.contentDocument;
    if (!deckWindow || !doc?.documentElement) return false;
    if (typeof deckWindow.__deckGo === "function") return true;
    const slides = [...doc.querySelectorAll(".slide, [data-slide]")]
      .filter((slide, index, all) => !all.some((other) => other !== slide && other.contains(slide)));
    if (!slides.length) return false;

    deckWindow.__deckGo = (requestedIndex) => {
      const target = Math.max(0, Math.min(slides.length - 1, Number(requestedIndex) || 0));
      slides.forEach((slide, index) => {
        slide.classList.toggle("active", index === target);
        slide.classList.toggle("prev", index < target);
      });

      const page = target + 1;
      const ratio = page / slides.length;
      const pageCur = doc.getElementById("pageCur");
      const pageNo = doc.getElementById("pageNo");
      const pageTotal = doc.getElementById("pageTotal");
      if (pageCur) pageCur.textContent = String(page).padStart(2, "0");
      if (pageNo) pageNo.textContent = String(page).padStart(2, "0");
      if (pageTotal) pageTotal.textContent = String(slides.length).padStart(2, "0");
      const progress = doc.getElementById("progressBar");
      if (progress) {
        // Export variants use either a full-width <i> scaled on X or a bar
        // whose width is animated. Combining both would square the progress.
        if (progress.tagName === "I") progress.style.transform = `scaleX(${ratio})`;
        else progress.style.width = `${ratio * 100}%`;
      }

      try { deckWindow.__tweenTo?.(target); } catch {}
      try { deckWindow.slideInits?.[page]?.(); } catch {}
      deckWindow.dispatchEvent(new deckWindow.CustomEvent("slidechange", { detail: { index: page } }));
    };
    deckWindow.__studioDeckAdapter = true;
    return true;
  } catch {
    return false;
  }
}

// Newer Dazzle/Cinescale decks may expose a native zero-based navigator as
// window.__deckGo; installDynamicDeckAdapter supplies the same contract for
// legacy exports. Reusing the live document is essential: changing iframe.src
// destroys the previous slide before CSS can animate the outgoing/incoming pair.
function playDynamicSlide(frame, n) {
  try {
    const deckWindow = frame.contentWindow;
    if (!installDynamicDeckAdapter(frame) || typeof deckWindow?.__deckGo !== "function") return false;
    deckWindow.__deckGo(Math.max(0, Number(n) - 1));
    stabilizeDynamicDeckPlayer(frame);
    scheduleDeckViewportFit(frame);
    setTimeout(() => {
      if (frame.hidden || frame.dataset.deckKind !== "dynamic" || Number(ed.sel) !== Number(n)) return;
      const active = dynamicDeckActiveSlide(frame);
      if (active && active !== Number(n)) deckWindow.__deckGo(Math.max(0, Number(n) - 1));
      stabilizeDynamicDeckPlayer(frame);
      scheduleDeckViewportFit(frame);
    }, 80);
    return true;
  } catch {
    return false;
  }
}

// Apply the pending page while the iframe is still transparent, verify its
// active slide, then reveal the already-correct live player directly.
// The token makes rapid filmstrip clicks cancel stale reveal callbacks.
function revealDynamicDeckSlide(frame, n) {
  const target = Number(n);
  const token = frame.dataset.revealToken;
  const deckId = frame.dataset.deckId;
  const deckStamp = frame.dataset.deckStamp;
  const deckWindow = frame.contentWindow;
  if (!target || !token || typeof deckWindow?.__deckGo !== "function") return false;
  frame.dataset.deckReady = "revealing";
  frame.classList.add("font-pending");
  frame.classList.remove("font-ready");

  const currentRequestIsValid = () => (
    ed.kind === "dynamic"
    && String(ed.id) === deckId
    && Number(ed.sel) === target
    && frame.dataset.deckKind === "dynamic"
    && frame.dataset.deckId === deckId
    && frame.dataset.deckStamp === deckStamp
    && frame.dataset.revealToken === token
    && Number(frame.dataset.pendingSlide) === target
    && frame.contentWindow === deckWindow
  );
  let attempts = 0;
  const positionThenReveal = () => {
    if (!currentRequestIsValid()) return;
    try { deckWindow.__deckGo(Math.max(0, target - 1)); } catch { return; }
    stabilizeDynamicDeckPlayer(frame);
    const active = dynamicDeckActiveSlide(frame);
    attempts += 1;
    if (active !== target && attempts < 10) {
      setTimeout(positionThenReveal, 24);
      return;
    }
    if (active !== target) {
      // Do not reveal an unverified page. A future load/selection can retry
      // without exposing the deck's initial page 1.
      frame.dataset.deckReady = "0";
      return;
    }
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!currentRequestIsValid() || dynamicDeckActiveSlide(frame) !== target) {
        if (currentRequestIsValid()) setTimeout(positionThenReveal, 24);
        return;
      }
      delete frame.dataset.pendingSlide;
      frame.dataset.deckReady = "1";
      frame.hidden = false;
      $("#canvas-empty").hidden = true;
      scheduleDeckViewportFit(frame);
      requestAnimationFrame(() => {
        if (frame.dataset.deckReady !== "1" || Number(ed.sel) !== target) return;
        frame.classList.remove("font-pending");
        frame.classList.add("font-ready");
      });
    }));
  };
  positionThenReveal();
  return true;
}

function bindDynamicDeckNavigation() {
  const frame = $("#canvas-deck");
  const deckKind = frame?.dataset.deckKind;
  const deckId = frame?.dataset.deckId;
  if (!frame || !deckKind || ed.kind !== deckKind || String(ed.id) !== deckId) return;
  try {
    const deckWindow = frame.contentWindow;
    const isBlankLoad = (() => {
      try { return deckWindow.location.href === "about:blank"; } catch { return false; }
    })();
    if (isBlankLoad) return;
    scheduleDeckViewportFit(frame);
    const sync = (byUser) => {
      if (ed.kind !== deckKind || String(ed.id) !== deckId || frame.contentWindow !== deckWindow) return;
      // The hidden player may finish loading after the user has switched back to
      // the 00 progress view. Its delayed slidechange must not steal that choice.
      if (frame.hidden || ed.sel === 0) return;
      if (deckKind === "dynamic" && (frame.dataset.pendingSlide || frame.dataset.deckReady !== "1")) return;
      const n = dynamicDeckActiveSlide(frame);
      if (!n || n > ed.total || n === ed.sel) return;
      if (byUser) setFollow(false);
      ed.sel = n;
      markSel();
      updateNav();
    };
    deckWindow.addEventListener("slidechange", () => sync(true));
    if (deckKind === "dynamic") {
      installDynamicDeckAdapter(frame);
      if (typeof deckWindow.__deckGo === "function") {
        const pending = Number(frame.dataset.pendingSlide) || ed.sel;
        if (pending > 0) revealDynamicDeckSlide(frame, pending);
      } else {
        // Legacy output without a callable navigator keeps URL-based paging as
        // a compatibility fallback.
        frame.dataset.deckReady = "fallback";
        delete frame.dataset.pendingSlide;
        frame.classList.remove("font-pending");
        frame.classList.add("font-ready");
        frame.hidden = false;
        $("#canvas-img").hidden = true;
        $("#canvas-empty").hidden = true;
      }
    } else if (deckKind === "static") {
      const isBlankLoad = (() => {
        try { return deckWindow.location.href === "about:blank"; } catch { return false; }
      })();
      if (isBlankLoad) return;
      const hasNativePlayer = !!deckWindow.cleanDeck?.go || typeof deckWindow.__deckGo === "function";
      if (hasNativePlayer) {
        if (editorPresentationKind() === "dynamic") stabilizeDynamicDeckPlayer(frame);
        else stabilizeStaticDeckPlayer(frame);
        revealStaticDeckWhenFontsReady(frame, deckWindow);
      } else {
        // Legacy/incomplete output without a player: preserve the rendered PNG fallback.
        keepStaticPng(ed.sel);
      }
    }
    setTimeout(() => scheduleDeckViewportFit(frame), 120);
    sync(false);
  } catch {
    // 非同源 Deck 无法读取内部状态；当前内置动态链路始终为同源地址。
  }
}

/* ----- AI 制作活动流：底层日志增量追加，界面只呈现可读动作 ----- */
function evKey(ev) { return ev.k === "tool" ? "t:" + ev.tool + ":" + (ev.hint || "") : "x:" + ev.s; }
function evLine(ev) { return ev.k === "tool" ? (TOOL_LABEL[ev.tool] || ev.tool) + " " + (ev.hint || "") : ev.s; }
function agentKey(n) {
  return ed.pageAgents?.[String(Number(n))] || ("slide_" + pad2(n));
}

function agentPages(key) {
  return Object.entries(ed.pageAgents || {})
    .filter(([, owner]) => owner === key)
    .map(([page]) => Number(page))
    .filter(Number.isFinite)
    .sort((a, b) => a - b);
}

function pageAgentLabel(key, n) {
  if (String(key).startsWith("slide_group_")) {
    const pages = agentPages(key);
    const related = pages.filter((page) => page !== Number(n));
    return related.length
      ? `本页与第 ${related.map((page) => Number(page)).join("、")} 页保持统一设计`
      : "正在统一制作这一组关联页面";
  }
  return "正在制作本页内容与视觉";
}

function evRow(ev) {
  const d = document.createElement("div");
  d.className = "lv-row";
  if (ev.k === "tool") {
    d.innerHTML = `<span class="lv-p lv-cmd">✦</span><span class="lv-fn">${escapeHtml(TOOL_LABEL[ev.tool] || "推进任务")}</span>` +
      `<span class="lv-hint">${escapeHtml(compactActivityHint(ev.hint || ""))}</span>`;
  } else {
    d.innerHTML = `<span class="lv-p">·</span><span class="lv-text">${escapeHtml(ev.s)}</span>`;
  }
  return d;
}

// 增量同步:只把"上次末行之后"的新事件 append 进终端,老行不动 → 不闪
function syncStream(body, evs, lines, rowFn = evRow, cap = 200, rowSel = ".lv-row") {
  if (!body) return;
  const keys = evs.map(evKey);
  let startIdx = 0;
  if (lines.length) {
    const idx = keys.lastIndexOf(lines[lines.length - 1]);
    if (idx >= 0) startIdx = idx + 1;
    else { body.querySelectorAll(rowSel).forEach((e) => e.remove()); lines.length = 0; }
  }
  if (startIdx >= keys.length) return;
  const caret = body.querySelector(".lv-caret");
  for (let i = startIdx; i < keys.length; i++) {
    body.insertBefore(rowFn(evs[i]), caret);    // caret 为 null 时等价 appendChild
    lines.push(keys[i]);
  }
  if (lines.length > cap) {
    const extra = lines.length - cap;
    lines.splice(0, extra);
    [...body.querySelectorAll(rowSel)].slice(0, extra).forEach((e) => e.remove());
  }
  body.scrollTo({ top: body.scrollHeight, behavior: "smooth" });
}

function compactActivityHint(value) {
  const clean = String(value || "").replace(/\\/g, "/").split("/").pop() || "";
  return clean.length > 34 ? `${clean.slice(0, 31)}…` : clean;
}

const TOOL_PROGRESS = {
  read: ["整理页面资料", "正在读取页面计划与参考素材"],
  read_file: ["整理页面资料", "正在读取页面计划与参考素材"],
  write: ["搭建页面内容", "正在编写本页内容与视觉结构"],
  write_file: ["搭建页面内容", "正在编写本页内容与视觉结构"],
  edit: ["调整页面细节", "正在优化页面布局与视觉细节"],
  edit_file: ["调整页面细节", "正在优化页面布局与视觉细节"],
  patch: ["调整页面细节", "正在根据检查结果优化布局与视觉细节"],
  vision_analyze: ["检查画面质量", "正在检查版式、可读性与画面完整性"],
  image_generate: ["制作视觉素材", "正在生成本页需要的视觉素材"],
  fetch_image: ["下载真实图片", "正在将检索到的真实图片保存到本地"],
  web_search: ["搜索参考资料", "正在搜索内容与视觉参考"],
  web_fetch: ["阅读参考资料", "正在整理检索到的有效信息"],
  delegate_task: ["安排页面制作", "正在安排多页内容与视觉同步推进"],
  bash: ["渲染与检查页面", "正在渲染页面预览并检查结果"],
  terminal: ["渲染与检查页面", "正在运行页面制作与检查工具"],
};

function terminalProgress(hint = "") {
  const value = String(hint).toLowerCase();
  if (/\brender\b|deck\.py/.test(value)) return ["渲染页面预览", "正在渲染最新页面，准备进行视觉检查"];
  if (/validate|lint|check|test/.test(value)) return ["检查页面结构", "正在检查页面结构与输出完整性"];
  if (/grep|sed|head|find|\bls\b/.test(value)) return ["核对页面资源", "正在核对页面结构、素材与样式定义"];
  return TOOL_PROGRESS.terminal;
}

function toolProgress(ev) {
  if (ev.tool === "terminal" || ev.tool === "bash") return terminalProgress(ev.hint);
  if (ev.tool === "dynamic_action") {
    const value = String(ev.hint || "").toLowerCase();
    const label = /检索|搜索|search|fetch/.test(value) ? "查找并整理参考资料"
      : /配图|图像|image/.test(value) ? "准备页面视觉素材"
        : /检查|复审|vision|review|validate/.test(value) ? "检查页面呈现效果"
          : /渲染|render/.test(value) ? "生成最新页面预览"
            : /撰写|修改|编写|write|edit|patch/.test(value) ? "完善页面内容与画面"
              : "推进本页制作";
    return [label, "正在执行当前制作步骤，完成后会自动继续下一项。"];
  }
  return TOOL_PROGRESS[ev.tool] || [TOOL_LABEL[ev.tool] || "推进页面制作", "正在推进当前页面的制作"];
}

function friendlyToolHint(ev) {
  const value = String(ev.hint || "").replace(/\\/g, "/");
  if (!value || /[{}\"]/g.test(value)) return "";
  const match = value.match(/(?:^|\/)([^/]+\.(?:html?|png|jpe?g|svg|md|css|json))\b/i);
  return match ? match[1] : "";
}

function cleanAgentText(value) {
  const text = String(value || "")
    .replace(/^\[\d+\]\s*💬\s*/, "")
    .replace(/^💬\s*/, "")
    .replace(/\r\n?/g, "\n")
    // Keep Markdown's structural line breaks. Collapsing all whitespace here
    // previously turned headings and lists into one unreadable paragraph.
    .replace(/[^\S\n]+/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  if (!text || /^轨迹已写\b/.test(text)) return "";
  if (/^Now I have enough context\./i.test(text)) return "资料与页面规划已梳理完成，开始搭建本页内容与版式。";
  if (/^Let me reconsider\b/i.test(text)) return "发现当前版式与页面规划不完全一致，正在重新调整。";
  if (/^Let me write the full deck\b/i.test(text)) return "演示结构已经梳理完成，正在开始整套页面编排。";
  if (/^Let me (?:build|create) the full deck\b/i.test(text)) return "整体创作方案已经明确，正在把内容转化为完整演示。";
  if (/^Let me (?:build|write|create) (?:the|this) slide/i.test(text)) return "页面资料已准备完成，开始进行内容与视觉设计。";
  if (/^Let me\b/i.test(text)) return "正在根据当前结果继续推进演示制作。";
  if (/^(?:I (?:will|need|can|should)|We need to|Now I)\b/i.test(text)) return "正在核对当前结果并安排后续步骤。";
  return text;
}

function agentStoryHtml(text) {
  return escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br>");
}

// A compact, dependency-free Markdown renderer for Agent narrative. Raw HTML
// is escaped before formatting, so model output cannot inject DOM/script. The
// supported subset intentionally matches what Agents emit most often:
// headings, paragraphs, lists, checklists, quotes and fenced code blocks.
function agentMarkdownHtml(value) {
  const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  const output = [];
  let paragraph = [];
  let listType = "";
  let listItems = [];
  let quoteLines = [];
  let codeLines = [];
  let inCode = false;
  let codeLanguage = "";
  let tableSkipUntil = -1;

  const inline = (text) => escapeHtml(String(text || ""))
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>");
  const flushParagraph = () => {
    if (!paragraph.length) return;
    output.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    output.push(`<${listType}>${listItems.map((item) => `<li${item.checked == null ? "" : ` class=\"task-item ${item.checked ? "checked" : ""}\"`}>
      ${item.checked == null ? "" : `<span class=\"task-check\" aria-hidden=\"true\">${item.checked ? "✓" : ""}</span>`}${inline(item.text)}</li>`).join("")}</${listType}>`);
    listType = "";
    listItems = [];
  };
  const flushQuote = () => {
    if (!quoteLines.length) return;
    output.push(`<blockquote>${quoteLines.map(inline).join("<br>")}</blockquote>`);
    quoteLines = [];
  };
  const flushAll = () => { flushParagraph(); flushList(); flushQuote(); };
  const tableCells = (line) => String(line || "")
    .replace(/^\s*\|/, "")
    .replace(/\|\s*$/, "")
    .split("|")
    .map((cell) => cell.trim());
  const isTableDivider = (line) => {
    const cells = tableCells(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
  };

  lines.forEach((rawLine, lineIndex) => {
    if (lineIndex <= tableSkipUntil) return;
    const line = rawLine.trim();
    const fence = line.match(/^```\s*([\w+-]*)/);
    if (fence) {
      if (inCode) {
        output.push(`<pre><code${codeLanguage ? ` data-language=\"${escapeHtml(codeLanguage)}\"` : ""}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        codeLines = [];
        codeLanguage = "";
        inCode = false;
      } else {
        flushAll();
        inCode = true;
        codeLanguage = fence[1] || "";
      }
      return;
    }
    if (inCode) {
      codeLines.push(rawLine);
      return;
    }
    if (!line) {
      flushAll();
      return;
    }
    const nextLine = lines[lineIndex + 1]?.trim() || "";
    if (line.includes("|") && nextLine.includes("|") && isTableDivider(nextLine)) {
      flushAll();
      const headers = tableCells(line);
      const rows = [];
      let cursor = lineIndex + 2;
      while (cursor < lines.length) {
        const row = lines[cursor].trim();
        if (!row || !row.includes("|") || /^```/.test(row)) break;
        const cells = tableCells(row);
        if (cells.length < 2) break;
        rows.push(cells);
        cursor += 1;
      }
      tableSkipUntil = cursor - 1;
      const columnCount = Math.max(headers.length, ...rows.map((row) => row.length));
      const normalizedHeaders = Array.from({ length: columnCount }, (_, index) => headers[index] || "");
      output.push(`<div class="agent-table-wrap"><table><thead><tr>${normalizedHeaders.map((cell) => `<th>${inline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${Array.from({ length: columnCount }, (_, index) => `<td>${inline(row[index] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      return;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushAll();
      const level = Math.min(4, heading[1].length + 1);
      output.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      return;
    }
    if (/^(?:-{3,}|\*{3,}|_{3,})$/.test(line)) {
      flushAll();
      output.push("<hr>");
      return;
    }
    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      flushParagraph(); flushList();
      quoteLines.push(quote[1]);
      return;
    }
    const unordered = line.match(/^[-*+]\s+(.+)$/);
    const ordered = line.match(/^\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph(); flushQuote();
      const nextType = ordered ? "ol" : "ul";
      if (listType && listType !== nextType) flushList();
      listType = nextType;
      const itemText = (unordered || ordered)[1];
      const task = itemText.match(/^\[([ xX])\]\s*(.*)$/);
      listItems.push(task
        ? { text: task[2], checked: task[1].toLowerCase() === "x" }
        : { text: itemText, checked: null });
      return;
    }
    flushList(); flushQuote();
    paragraph.push(line);
  });
  if (inCode) output.push(`<pre><code${codeLanguage ? ` data-language=\"${escapeHtml(codeLanguage)}\"` : ""}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
  flushAll();
  return output.join("");
}

function agentCurrentState(evs, { running, rendered, n }) {
  if (rendered) return running
    ? ["本页已有最新预览", "页面已生成预览，仍可能根据整套演示的检查结果继续优化。"]
    : ["本页制作已完成", "页面已经过渲染与检查，可以切换到页面预览查看结果。"];
  const latest = [...evs].reverse().find((ev) => ev.k === "tool" || cleanAgentText(ev.s));
  if (!latest) {
    return running
      ? ["正在理解页面任务", n ? `正在梳理第 ${n} 页的目标、素材与版式要求。` : "正在拆解演示目标与整体结构。"]
      : ["暂无制作记录", "开始生成后，这里会展示当前正在进行的工作。"];
  }
  if (latest.k === "tool") return toolProgress(latest);
  const text = cleanAgentText(latest.s);
  return [text.length > 54 ? `${text.slice(0, 53)}…` : text, "本轮判断已经完成，正在继续推进后续步骤。"];
}

function summarizeToolEvents(evs) {
  const groups = new Map();
  evs.forEach((ev, index) => {
    if (ev.k !== "tool") return;
    const [label] = toolProgress(ev);
    const previous = groups.get(label) || { label, count: 0, hint: "", last: -1 };
    previous.count += 1;
    previous.last = index;
    previous.hint = friendlyToolHint(ev) || previous.hint;
    groups.set(label, previous);
  });
  return [...groups.values()].sort((a, b) => b.last - a.last).slice(0, 7);
}

function reconcileAgentStories(list, stories) {
  const existing = new Map($$(".agent-message", list).map((card) => [card.dataset.storyKey, card]));
  if (!stories.length) {
    existing.forEach((card) => card.remove());
    if (!list.querySelector(".agent-story-empty")) {
      list.innerHTML = '<div class="agent-story-empty">正在整理上下文，关键判断会显示在这里。</div>';
    }
    return;
  }
  list.querySelector(".agent-story-empty")?.remove();
  const occurrences = new Map();
  const desired = stories.map((text, index) => {
    const occurrence = (occurrences.get(text) || 0) + 1;
    occurrences.set(text, occurrence);
    const key = JSON.stringify([text, occurrence]);
    let card = existing.get(key);
    if (!card) {
      card = document.createElement("article");
      card.dataset.storyKey = key;
      card.innerHTML = `<span class="agent-avatar">${agentIdentityGlyph(editorPresentationKind())}</span><div><b>SenseNova Present</b><p>${agentStoryHtml(text)}</p></div>`;
    }
    card.className = `agent-message${index === stories.length - 1 ? " latest" : ""}`;
    existing.delete(key);
    return card;
  });
  existing.forEach((card) => card.remove());
  desired.forEach((card, index) => {
    const current = list.children[index];
    if (current !== card) list.insertBefore(card, current || null);
  });
}

function progressHistoryItems(evs) {
  const items = evs.flatMap((ev) => {
    if (ev.k === "tool") {
      const [title, detail] = toolProgress(ev);
      return [{ type: "tool", title, text: detail, count: 1 }];
    }
    const value = cleanAgentText(ev.s);
    return value ? [{ type: "message", title: "SenseNova Present", text: value, count: 1 }] : [];
  });
  const toolIndexes = new Map();
  return items.reduce((result, item) => {
    if (item.type !== "tool") {
      result.push(item);
      return result;
    }
    const key = JSON.stringify([item.title, item.text]);
    const existingIndex = toolIndexes.get(key);
    if (existingIndex != null) result[existingIndex].count += 1;
    else {
      toolIndexes.set(key, result.length);
      result.push(item);
    }
    return result;
  }, []);
}

function pageAgentDisplayName(source, n) {
  if (String(source).startsWith("slide_group_")) return "页面组设计 Agent";
  if (/^slide_\d+/.test(String(source))) return `第 ${pad2(n)} 页设计 Agent`;
  if (String(source).startsWith("dynamic:")) return "动态创作 Agent";
  return "SenseNova Present";
}

function completedMilestoneDetail(label, count, hints) {
  const amount = Math.max(1, Number(count) || 1);
  const files = [...new Set(hints || [])].slice(0, 3);
  const fileText = files.length ? `涉及 ${files.join("、")}。` : "";
  if (label === "整理页面资料") return `已读取 ${amount} 项页面规划、视觉规范与参考资料，明确本页内容和版式约束。${fileText}`;
  if (label === "搭建页面内容") return `已完成页面内容与视觉结构的初步搭建，共写入 ${amount} 次。${fileText}`;
  if (label === "调整页面细节") return `根据页面检查结果完成 ${amount} 次布局、样式或内容调整。${fileText}`;
  if (label === "渲染页面预览") return `已生成 ${amount} 版页面预览，用于逐轮核对画面效果。`;
  if (label === "检查页面结构") return `已完成 ${amount} 次页面结构与交付完整性检查。`;
  if (label === "核对页面资源") return `已完成 ${amount} 次页面结构、素材与样式核对。${fileText}`;
  if (label === "检查画面质量") return `已完成 ${amount} 次真实视觉检查，核对排版、可读性、遮挡、溢出与整体呈现。`;
  if (label === "制作视觉素材") return `已完成 ${amount} 次页面视觉素材生成与整理。${fileText}`;
  if (label === "搜索参考资料" || label === "阅读参考资料") return `已完成 ${amount} 次资料检索与内容核验，为本页文案和视觉提供依据。`;
  return `已完成“${label}”${amount > 1 ? `等 ${amount} 次相关操作` : ""}。${fileText}`;
}

function pageAgentProgressItems(evs, source, n) {
  const owner = pageAgentDisplayName(source, n);
  const groups = new Map();
  const messages = [];
  (evs || []).forEach((ev, index) => {
    const order = Number.isFinite(Number(ev.seq)) ? Number(ev.seq) : index;
    if (ev.k === "text") {
      const text = cleanAgentText(ev.s);
      if (text && !messages.some((item) => item.text === text)) {
        messages.push({ type: "message", title: owner, text, count: 1, order });
      }
      return;
    }
    if (ev.k !== "tool") return;
    const [label] = toolProgress(ev);
    const group = groups.get(label) || { label, count: 0, hints: [], order };
    group.count += 1;
    const hint = friendlyToolHint(ev);
    if (hint && !group.hints.includes(hint)) group.hints.push(hint);
    groups.set(label, group);
  });
  const milestones = [...groups.values()].map((group) => ({
    type: "tool",
    title: `${owner} · ${group.label}`,
    text: completedMilestoneDetail(group.label, group.count, group.hints),
    count: group.count,
    order: group.order,
  }));
  const combined = [...milestones, ...messages].sort((a, b) => a.order - b.order);
  // Keep the page readable while retaining substantially more context than the
  // previous four-text cap. The final Agent result is always preserved.
  if (combined.length <= 12) return combined;
  const finalMessage = [...combined].reverse().find((item) => item.type === "message");
  const visible = combined.slice(-12);
  if (finalMessage && !visible.includes(finalMessage)) visible[visible.length - 1] = finalMessage;
  return visible.sort((a, b) => a.order - b.order);
}

function reconcileProgressHistory(list, items) {
  $$(".agent-message:not([data-history-key])", list).forEach((card) => card.remove());
  const existing = new Map($$(".agent-message[data-history-key]", list).map((card) => [card.dataset.historyKey, card]));
  const occurrences = new Map();
  const desired = items.map((item, index) => {
    const identity = JSON.stringify([item.type, item.title, item.text, item.count]);
    const occurrence = (occurrences.get(identity) || 0) + 1;
    occurrences.set(identity, occurrence);
    const key = JSON.stringify([identity, occurrence]);
    let card = existing.get(key);
    if (!card) {
      card = document.createElement("article");
      card.dataset.historyKey = key;
      card.innerHTML = `<span class="agent-avatar">${agentIdentityGlyph(editorPresentationKind())}</span><div><b>${escapeHtml(item.title)}${item.count > 1 ? `<em class="history-count">×${item.count}</em>` : ""}</b>${item.type === "message" ? `<div class="agent-markdown">${agentMarkdownHtml(item.text)}</div>` : `<p>${escapeHtml(item.text)}</p>`}</div>`;
    }
    card.className = `agent-message history-${item.type}${index === items.length - 1 ? " latest" : ""}`;
    existing.delete(key);
    return card;
  });
  existing.forEach((card) => card.remove());
  desired.forEach((card, index) => {
    const current = list.children[index];
    if (current !== card) list.insertBefore(card, current || null);
  });
}

function renderAgentProgress(body, evs, { running = false, rendered = false, n = 0, source = "" } = {}) {
  if (!body) return;
  const preserveHistory = source === "dynamic:0";
  body.classList.toggle("preserve-history", preserveHistory);
  body.classList.remove("is-orchestration");
  body.closest(".outline-agent-turn")?.classList.remove("is-orchestration");
  const allStories = evs
    .filter((ev) => ev.k === "text")
    .map((ev) => cleanAgentText(ev.s))
    .filter(Boolean)
    .filter((text, index, all) => index === 0 || text !== all[index - 1]);
  const stories = preserveHistory ? allStories : allStories.slice(-4);
  const historyItems = preserveHistory
    ? progressHistoryItems(evs)
    : pageAgentProgressItems(evs, source, n);
  const actions = summarizeToolEvents(evs);
  const actionCount = evs.filter((ev) => ev.k === "tool").length;
  const [stateTitle, stateDetail] = agentCurrentState(evs, { running, rendered, n });
  const signature = JSON.stringify([source, running, rendered, stateTitle, stories, historyItems, actions]);
  if (body.dataset.progressSignature === signature) return;
  const scroller = body.closest(".outline-chat-body") || body;
  const wasNearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  const previousTop = scroller.scrollTop;
  const shellKey = JSON.stringify([source, n]);
  if (body.dataset.progressShell !== shellKey || !body.querySelector(".agent-now")) {
    const configHtml = body.id === "agent-progress-console" && n === 0
      ? outlineTaskConfigMarkup(editorPresentationKind(), taskConfigData())
      : "";
    body.dataset.progressShell = shellKey;
    body.innerHTML = `
      <section class="agent-now">
        <span class="agent-now-icon"><i></i></span>
        <div><span class="agent-kicker">当前状态</span><strong></strong><p></p></div>
      </section>
      ${configHtml}
      <section class="agent-story">
        <div class="agent-section-head"><span>${preserveHistory ? "完整制作记录" : "本页制作记录"}</span><em>${preserveHistory ? "按发生顺序持续保留" : "责任 Agent 的判断、执行与结果"}</em></div>
        <div class="agent-story-list"></div>
      </section>
      <details class="agent-actions">
        <summary><span>执行细节</span><em></em><i></i></summary>
        <div class="agent-action-list"></div>
      </details>`;
  }
  body.dataset.progressSignature = signature;
  const now = body.querySelector(".agent-now");
  now.classList.toggle("is-running", running);
  if (now.querySelector("strong").textContent !== stateTitle) now.querySelector("strong").textContent = stateTitle;
  if (now.querySelector("p").textContent !== stateDetail) now.querySelector("p").textContent = stateDetail;

  const storyKey = JSON.stringify(historyItems);
  const storyList = body.querySelector(".agent-story-list");
  if (storyList.dataset.renderKey !== storyKey) {
    storyList.dataset.renderKey = storyKey;
    reconcileProgressHistory(storyList, historyItems);
  }

  const actionKey = JSON.stringify([running, actions]);
  const actionDetails = body.querySelector(".agent-actions");
  const actionSummary = actionDetails.querySelector("summary em");
  const actionSummaryText = actionCount ? `${actions.length} 类动作 · ${actionCount} 次执行` : "等待执行";
  if (actionSummary.textContent !== actionSummaryText) actionSummary.textContent = actionSummaryText;
  const actionList = body.querySelector(".agent-action-list");
  if (actionList.dataset.renderKey !== actionKey) {
    actionList.dataset.renderKey = actionKey;
    actionList.innerHTML = actions.length ? actions.map((action, index) => `
      <div class="agent-action${index === 0 && running ? " active" : ""}"><span>${index === 0 && running ? "•" : "✓"}</span><b>${escapeHtml(action.label)}</b>${action.hint ? `<i>${escapeHtml(action.hint)}</i>` : ""}${action.count > 1 ? `<em>×${action.count}</em>` : ""}</div>`).join("") : '<div class="agent-action-empty">还没有底层执行记录</div>';
  }
  requestAnimationFrame(() => {
    scroller.scrollTop = wasNearBottom ? scroller.scrollHeight : previousTop;
  });
}

const ORCHESTRATION_ROLE_LABELS = {
  orch: ["整体策划", "主", "梳理结构并协调制作"],
  material: ["Material Agent", "材", "解析并整理用户材料"],
  research: ["Research Agent", "研", "检索、阅读并核验外部资料"],
  image: ["Image Agent", "图", "搜索、生成并检查图片素材"],
  review: ["Review Agent", "审", "复核成稿与整体一致性"],
};

function canonicalOrchestrationAgentKey(value) {
  const raw = String(value || "").trim().replace(/_r\d+$/i, "");
  return ["orch", "orchestrator"].includes(raw.toLowerCase()) ? "orch" : raw;
}

function orchestrationAgentMeta(key) {
  key = canonicalOrchestrationAgentKey(key);
  const group = String(key).match(/^slide(?:_|-)group(?:_|-)(.+)$/);
  if (group) {
    const pages = agentPages(key);
    return {
      key, role: "slide", label: "页面组协作", icon: "组",
      note: pages.length ? `统一制作第 ${pages.map((page) => Number(page)).join("、")} 页` : "统一制作一组关联页面",
      page: pages[0] || 0, pages,
    };
  }
  const slide = String(key).match(/^slide[_-]0?(\d+)/);
  if (slide) return { key, role: "slide", label: `第 ${Number(slide[1])} 页制作`, icon: pad2(Number(slide[1])), note: "完善本页内容与画面", page: Number(slide[1]) };
  const family = String(key).split(/[_-]/)[0];
  const [label, icon, note] = ORCHESTRATION_ROLE_LABELS[family] || ["协作 Agent", "✦", "推进当前协作任务"];
  return { key, role: family, label, icon, note, page: 0 };
}

function orchestrationAgentGlyph(meta, kind = editorPresentationKind(), className = "orch-agent-glyph") {
  // Dynamic production is intentionally represented by one continuous Agent.
  // Static production has several specialist roles, so its timeline uses a
  // distinct, compact pictogram for each responsibility instead of repeating
  // the same four-square mark on every card.
  if (kind !== "static") return agentIdentityGlyph(kind, className);
  // One visual family: solid rounded modules rather than unrelated outline
  // icons.  The silhouettes differ at a glance while retaining the weight of
  // the original SenseNova four-tile mark.
  const svg = (body) => `<svg class="${className}" viewBox="0 0 20 20" fill="none" aria-hidden="true">${body}</svg>`;
  if (meta.role === "research") return svg(`
    <rect x="2.3" y="3.2" width="11.2" height="13.8" rx="3" fill="currentColor" opacity=".22"/>
    <rect x="5.2" y="1.8" width="11.5" height="12.8" rx="3" fill="currentColor" opacity=".46"/>
    <path d="M7.9 5.6h5.8M7.9 8.2h3.9" stroke="currentColor" stroke-width="1.65" stroke-linecap="round"/>
    <circle cx="14.2" cy="14" r="3.55" fill="currentColor"/>
    <circle cx="14.2" cy="14" r="1.25" fill="white" opacity=".94"/>
  `);
  if (meta.role === "material") return svg(`
    <rect x="2.7" y="3" width="14.6" height="3.5" rx="1.75" fill="currentColor" opacity=".42"/>
    <rect x="2.7" y="8.25" width="11.6" height="3.5" rx="1.75" fill="currentColor" opacity=".72"/>
    <rect x="2.7" y="13.5" width="8.2" height="3.5" rx="1.75" fill="currentColor"/>
  `);
  if (meta.role === "image") return svg(`
    <rect x="2.5" y="2.5" width="8.9" height="15" rx="2.6" fill="currentColor"/>
    <rect x="13" y="2.5" width="4.5" height="6.2" rx="1.7" fill="currentColor" opacity=".46"/>
    <rect x="13" y="10.3" width="4.5" height="7.2" rx="1.7" fill="currentColor" opacity=".74"/>
  `);
  if (meta.role === "review") return svg(`
    <rect x="2.2" y="4.8" width="11.7" height="12.7" rx="3" fill="currentColor" opacity=".23"/>
    <rect x="5" y="2.2" width="12.8" height="12.8" rx="3.3" fill="currentColor" opacity=".38"/>
    <path d="m8.2 8.5 2.25 2.25 4.15-4.25" stroke="currentColor" stroke-width="2.15" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="15.35" cy="15.3" r="2.45" fill="white"/>
    <path d="m14.2 15.3.75.75 1.55-1.65" stroke="#6258d7" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round"/>
  `);
  if (meta.role === "slide" && /^slide(?:_|-)group/i.test(String(meta.key || ""))) return svg(`
    <rect x="2.2" y="6" width="10.2" height="11.3" rx="2.3" fill="currentColor" opacity=".28"/>
    <rect x="5.1" y="3.5" width="10.2" height="11.3" rx="2.3" fill="currentColor" opacity=".58"/>
    <rect x="8" y="1.2" width="9.8" height="11" rx="2.3" fill="currentColor"/>
  `);
  if (meta.role === "slide") return svg(`
    <rect x="2.5" y="2.5" width="15" height="15" rx="3" fill="currentColor" opacity=".24"/>
    <rect x="5" y="5" width="10" height="4" rx="1.5" fill="currentColor"/>
    <rect x="5" y="10.6" width="4.3" height="4.4" rx="1.4" fill="currentColor" opacity=".72"/>
    <rect x="10.7" y="10.6" width="4.3" height="4.4" rx="1.4" fill="currentColor" opacity=".45"/>
  `);
  if (meta.role === "orch") return `<svg class="${className}" viewBox="0 0 20 20" fill="none" aria-hidden="true">
    <rect x="3" y="3" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
    <rect x="11.5" y="3" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
    <rect x="3" y="11.5" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
    <rect x="11.5" y="11.5" width="5.5" height="5.5" rx="1.35" fill="currentColor"/>
  </svg>`;
  return svg(`<path d="M10 2.7c.7 3.56 3.06 5.92 6.62 6.62-3.56.7-5.92 3.06-6.62 6.62-.7-3.56-3.06-5.92-6.62-6.62C6.94 8.62 9.3 6.26 10 2.7Z" fill="currentColor"/>`);
}

function orchestrationFlatFeed(feed) {
  let fallbackSequence = 0;
  return Object.entries(feed || {}).flatMap(([agent, events]) => (events || []).map((event) => ({
    ...event, agent: canonicalOrchestrationAgentKey(agent),
    order: Number.isFinite(Number(event.seq)) ? Number(event.seq) : fallbackSequence++,
  }))).sort((a, b) => a.order - b.order);
}

function orchestrationAssignmentText(hint) {
  const value = String(hint || "");
  if (/\bMaterial\b/i.test(value)) return "已将附件解析与材料整理分派给 Material Agent。";
  if (/\bResearch\b/i.test(value)) return "已将事实核查与资料研究分派给 Research Agent。";
  if (/\bImage\b/i.test(value)) return "已将整册配图与视觉素材准备分派给 Image Agent。";
  if (/\bReview\b/i.test(value)) return "已将成稿复核与一致性检查分派给 Review Agent。";
  if (/\bSlide\s*\d*/i.test(value)) return "页面制作任务已经安排，多页内容与视觉开始同步推进。";
  return "协作任务已经安排，相关制作环节开始推进。";
}

function agentTimingFor(key) {
  const timings = ed.agentTimings || {};
  return timings[key] || timings[canonicalOrchestrationAgentKey(key)] || null;
}

function agentTimingBadge(timing) {
  const started = conversationTime(timing?.started_at);
  const startedFull = conversationTime(timing?.started_at, { withDay: true });
  const finished = conversationTime(timing?.finished_at);
  const finishedFull = conversationTime(timing?.finished_at, { withDay: true });
  const duration = fmtDur(timing?.duration_s);
  if (!started && !finished && !duration) return "";
  const running = timing.status === "running";
  const failed = timing.status === "failed";
  const waiting = timing.status === "waiting";
  const parts = [];
  if (started) parts.push(`<span>${escapeHtml(started.text)} 发起</span>`);
  if (finished) parts.push(`<span>${escapeHtml(finished.text)} 完成</span>`);
  if (duration) parts.push(`<b>${escapeHtml(`${running ? "已进行" : "耗时"} ${duration}`)}</b>`);
  const titleParts = [];
  if (startedFull) titleParts.push(`发起：${startedFull.text}`);
  if (finishedFull) titleParts.push(`完成：${finishedFull.text}`);
  if (duration) titleParts.push(`${running ? "已进行" : "耗时"}：${duration}`);
  titleParts.push("并行 Agent 的时间可能重叠");
  return `<time class="orch-duration orch-stage-time${running ? " running" : ""}${failed ? " failed" : ""}${waiting ? " waiting" : ""}" datetime="${escapeHtml(started?.iso || finished?.iso || "")}" title="${escapeHtml(titleParts.join("；"))}">${parts.join("<span class=\"orch-time-separator\">·</span>")}</time>`;
}

function timingAgentLabel(key) {
  const raw = String(key || "");
  const meta = orchestrationAgentMeta(raw);
  if (meta.role === "orch") return "整体策划 Agent";
  if (meta.role === "image") return "Image Agent";
  if (meta.role === "research") return "Research Agent";
  if (meta.role === "material") return "Material Agent";
  if (meta.role === "review") return "Review Agent";
  if (meta.role === "slide") {
    const pages = agentPages(raw);
    const retry = raw.match(/_r(\d+)$/i);
    const scope = raw.replace(/^slide[_-]group[_-]/i, "").replace(/_r\d+$/i, "").replace(/[_-]+/g, " ").trim();
    const pageText = pages.length ? ` · 第 ${pages.join("、")} 页` : (scope ? ` · ${scope}` : "");
    return `页面组 Agent${pageText}${retry ? ` · 重试 ${retry[1]}` : ""}`;
  }
  return meta.label || "协作 Agent";
}

function orchestrationTimingMarkup() {
  const timings = Object.entries(ed.agentTimings || {})
    .filter(([, timing]) => timing && Number.isFinite(Number(timing.duration_s)))
    .sort(([, a], [, b]) => String(a.started_at || "").localeCompare(String(b.started_at || "")));
  const overall = ed.overallTiming || {};
  const overallDuration = fmtDur(overall.duration_s);
  if (!timings.length && !overallDuration) return "";
  const rows = timings.map(([key, timing]) => {
    const failed = timing.status === "failed";
    const running = timing.status === "running";
    return `<div class="orchestration-timing-agent${failed ? " failed" : ""}${running ? " running" : ""}" title="${escapeHtml(key)}">
      <span>${escapeHtml(timingAgentLabel(key))}</span>
      <b>${escapeHtml(fmtDur(timing.duration_s))}</b>
      <em>${failed ? "未完成" : (running ? "进行中" : "完成")}</em>
    </div>`;
  }).join("");
  return `<section class="orchestration-timing-card">
    <div class="orchestration-timing-head">
      <span><small>整体用时</small><strong>${escapeHtml(overallDuration || "计算中")}</strong></span>
      <em>${timings.length} 个 Agent · 墙钟时间，并行会重叠</em>
    </div>
    <div class="orchestration-timing-agents">${rows}</div>
  </section>`;
}

function chinesePageAgentSummary(value, { page = 0 } = {}) {
  const text = cleanAgentText(value);
  if (!text) return "";

  // Prefer the structured handoff summary when the Agent supplied one.  A
  // number of page Agents first write an English visual-check sentence and
  // then append the actual Chinese delivery contract; showing the first line
  // verbatim made the user-facing current-state card unexpectedly switch to
  // English even though the rest of Studio is Chinese.
  const declared = text.match(/(?:^|\n)\s*(?:[-*+]\s*)?summary\s*[:：]\s*([^\n]+)/i)?.[1]?.trim() || "";
  if (/[\u3400-\u9fff]/.test(declared)) return declared;

  const hanCount = (text.match(/[\u3400-\u9fff]/g) || []).length;
  const latinCount = (text.match(/[A-Za-z]/g) || []).length;
  if (hanCount >= Math.max(8, latinCount * 0.35)) return "";

  const statedPage = Number(text.match(/\bslide\s*0*(\d{1,3})\b/i)?.[1] || page || 0);
  const wordNumbers = {
    one: 1, two: 2, three: 3, four: 4, five: 5, six: 6,
    seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12,
  };
  const readyMatch = text.match(/\ball\s+(?:(\d+)|(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve))\s+pages?\s+(?:are\s+)?(?:ready|complete|completed|clean)\b/i);
  const readyCount = readyMatch ? Number(readyMatch[1] || wordNumbers[String(readyMatch[2] || "").toLowerCase()] || 0) : 0;
  const pagePrefix = statedPage ? `第 ${statedPage} 页` : "页面组";
  const isReady = /\b(?:ready|complete|completed|clean|verified|passes?)\b/i.test(text);
  const isAdjusting = /\b(?:fix|fixing|adjust|adjusting|revise|revising|refine|refining|update|updating)\b/i.test(text);
  const layoutDone = /\b(?:layout|composition|spacing|alignment|balance|well-balanced)\b/i.test(text);
  const visionDone = /\b(?:render|visual|vision|overflow|overlap|legible|readable)\b/i.test(text);

  if (isReady && readyCount) {
    const work = layoutDone ? "版式调整已完成" : (visionDone ? "渲染与画面检查已完成" : "制作已完成");
    return `${pagePrefix}${work}，本组 ${readyCount} 页均已就绪。`;
  }
  if (isReady) {
    const work = layoutDone ? "版式调整已完成" : (visionDone ? "渲染与画面检查已完成" : "制作与检查已完成");
    return `${pagePrefix}${work}。`;
  }
  if (isAdjusting) return `${pagePrefix}正在根据检查结果继续优化。`;
  return "页面组进展已更新，正在继续推进制作与检查。";
}

function compactPageAgentResult(value, { completed = false, page = 0 } = {}) {
  const text = cleanAgentText(value);
  if (!text) return { summary: "", detail: "" };
  const localizedSummary = chinesePageAgentSummary(text, { page });
  if (localizedSummary) {
    return {
      summary: localizedSummary,
      detail: text !== localizedSummary ? text : "",
    };
  }
  // Agent finals commonly start with one audience-facing conclusion and then
  // append an identity declaration, file inventory and validation ledger.  The
  // card should keep that first conclusion scannable; everything after it
  // belongs in "查看完整制作说明" instead of leaking into the main timeline.
  const leadLine = text.split("\n")
    .map((line) => line.trim().replace(/^#{1,6}\s+/, "").replace(/^[-*+]\s+/, ""))
    .find((line) => line && !/^(?:---+|身份(?:声明)?\s*[:：]?|产出\s*[:：]?|状态\s*[:：]?)/i.test(line)) || "";
  const leadSentenceMatch = leadLine.match(/^(.{1,160}?[。！？!?](?:[”’\"']|$))/);
  const leadSummary = (leadSentenceMatch?.[1] || leadLine).trim();
  const conciseLead = leadSummary && leadSummary.length <= 160;
  const hasHiddenDetail = text.length > leadSummary.length || text !== leadSummary;
  const technical = /(?:slides?\/slide[_-]?\d+\.html|renders?\/slide[_-]?\d+\.png|OVERFLOW|OVERLAP|CROWDED|CONTRAST|hairline|margin:\s*\d|\b(?:PASS|FAIL)\b|(?:^|\s)[.#][\w-]+\s*[{,:])/i.test(text);
  if (conciseLead && (hasHiddenDetail || /(?:ready|完成|达标|通过|良好|已交付)/i.test(leadSummary))) {
    return {
      summary: leadSummary,
      detail: hasHiddenDetail ? text : "",
    };
  }
  if (completed && technical) {
    const checksPassed = /(?:通过|\bPASS\b|无\s*(?:OVERFLOW|OVERLAP|CROWDED)|零\s*(?:溢出|遮挡|空白页|错误))/i.test(text);
    const outputs = [];
    if (/slides?\/slide[_-]?\d+\.html/i.test(text)) outputs.push("页面文件");
    if (/renders?\/slide[_-]?\d+\.png/i.test(text)) outputs.push("预览图");
    const subject = page > 0 ? `第 ${pad2(page)} 页` : "本页";
    const outputText = outputs.length ? `，已生成${outputs.join("和")}` : "";
    const checkText = checksPassed ? "并通过渲染与画面检查" : "渲染与检查记录已保存";
    return {
      summary: `${subject}内容与画面已完成${outputText}，${checkText}。`,
      detail: text,
    };
  }
  const maxLength = 160;
  return {
    summary: text.length > maxLength ? `${text.slice(0, maxLength).trim()}…` : text,
    detail: text.length > maxLength ? text : "",
  };
}

function compactOrchestrationCurrentDetail(meta, value) {
  const text = cleanAgentText(value);
  if (!text) return "当前结果已经记录，正在继续推进后续步骤。";
  // Specialist finals often contain asset manifests, validation ledgers and
  // other implementation detail. Keep the current-state card readable; the
  // complete text is still retained in the collapsible production timeline.
  if (meta.role === "image") return "图片素材与质量检查结果已整理。";
  if (meta.role === "research") return "研究资料与事实核验结果已整理。";
  if (meta.role === "material") return "附件内容与材料摘要已整理。";
  if (meta.role === "review") return "成稿检查与整体一致性结果已整理。";
  if (meta.role === "slide") {
    return compactPageAgentResult(text, { page: meta.page }).summary || "页面制作结果已整理。";
  }
  const lead = text.split("\n")
    .map((line) => line.trim().replace(/^#{1,6}\s+/, "").replace(/^[-*+]\s+/, ""))
    .find(Boolean) || text;
  return lead.length > 150 ? `${lead.slice(0, 149).trim()}…` : lead;
}

function compactReviewPublicUpdate(value) {
  const text = cleanAgentText(value);
  if (!text) return "";
  const lead = text.split("\n")
    .map((line) => line.trim()
      .replace(/^#{1,6}\s+/, "")
      .replace(/^[-*+]\s+/, "")
      .replace(/^身份(?:声明)?\s*[:：]?\s*/i, ""))
    .find((line) => line && !/^(?:---+|status\s*[:：]|产出\s*[:：])/i.test(line)) || "";
  if (!lead) return "";
  // Chinese public replies can be shown directly. Keep the live card concise;
  // the unabridged response remains available in the details disclosure.
  if (/[\u3400-\u9fff]/.test(lead)) {
    const sentence = lead.match(/^(.{1,180}?[。！？!?](?:[”’\"']|$))/)?.[1] || lead;
    return sentence.length > 180 ? `${sentence.slice(0, 179).trim()}…` : sentence;
  }
  // Do not leak an English model-default response into a Chinese process UI.
  // Preserve it in the full details while rendering a truthful localized state.
  if (/(?:pass|ready|complete|verified|approved)/i.test(lead)) return "成稿复核已经完成，检查结论已整理。";
  if (/(?:fix|revise|update|patch|issue)/i.test(lead)) return "复核发现需要调整的内容，正在修正并重新检查。";
  return "Review Agent 已更新复核进展。";
}

function specialistToolCount(events, tools) {
  const wanted = new Set(tools);
  return (events || []).filter((event) => event.k === "tool" && wanted.has(event.tool)).length;
}

function specialistStep(label, detail, state) {
  return { label, detail, state };
}

function specialistProgressEntry(meta, events, artifacts = {}, { allowLegacyPreviewFallback = true } = {}) {
  const ordered = (events || []).map((event, index) => ({
    ...event, order: Number.isFinite(Number(event.seq)) ? Number(event.seq) : index,
  })).sort((a, b) => a.order - b.order);
  const textUpdates = ordered
    .filter((event) => event.k === "text")
    .map((event) => cleanAgentText(event.s))
    .filter((text, index, values) => text && text !== values[index - 1]);
  const latestText = textUpdates.at(-1) || "";
  if (meta.role === "review") {
    const readCount = specialistToolCount(ordered, ["read", "read_file"]);
    const visionCount = specialistToolCount(ordered, ["vision_analyze"]);
    const renderCount = ordered.filter((event) => event.k === "tool" && (
      ["render_slides", "render", "screenshot"].includes(event.tool)
      || (event.tool === "terminal" && /(?:render|screenshot|playwright|chrom)/i.test(event.hint || ""))
    )).length;
    const fixCount = specialistToolCount(ordered, ["edit", "edit_file", "write", "write_file", "patch"]);
    const completed = /(?:复核|检查|交付|成稿|全部|整册).*(?:完成|通过|就绪)|status\s*[:：]\s*ready/i.test(latestText)
      || (ed.status === "completed" && ordered.length > 0);
    const reviewCount = visionCount + renderCount;
    const publicUpdates = textUpdates.map(compactReviewPublicUpdate)
      .filter((text, index, values) => text && text !== values[index - 1]);
    return {
      type: "specialist", meta, completed,
      result: publicUpdates.at(-1) || (completed ? "整册复核已完成，检查结论已整理。" : "Review Agent 正在检查成稿与页面一致性。"),
      resultDetail: latestText && latestText !== publicUpdates.at(-1) ? latestText : "",
      updates: publicUpdates,
      order: ordered[0]?.order ?? 0, key: `specialist:${meta.key}`,
      steps: [
        specialistStep("收集成稿", ordered.length ? (readCount ? `${readCount} 次读取与范围确认` : "已确认检查范围") : "等待成稿", ordered.length ? "done" : "pending"),
        specialistStep("逐页复核", reviewCount ? `${reviewCount} 次渲染与视觉检查` : "等待页面检查", completed ? "done" : (reviewCount ? "active" : "pending")),
        specialistStep("修正问题", fixCount ? `${fixCount} 次针对性修正` : (completed ? "未记录额外修正" : "发现问题后会在此更新"), completed ? "done" : (fixCount ? "active" : "pending")),
        specialistStep("汇总结论", completed ? "整册检查结论已完成" : (latestText ? "正在整理复核反馈" : "等待最终结论"), completed ? "done" : (latestText ? "active" : "pending")),
      ],
      metrics: [`${reviewCount} 次检查`, `${fixCount} 次修正`], previews: [],
    };
  }
  if (meta.role === "research") {
    const searchCount = specialistToolCount(ordered, ["web_search"]);
    const sourceCount = specialistToolCount(ordered, ["web_extract", "web_fetch"]);
    const briefStarted = ordered.some((event) => event.k === "tool" && event.tool === "write_file" && /knowledge-brief/i.test(event.hint || ""));
    const completed = !!artifacts.brief_ready || /(?:研究|核验).*(?:完成|写入)|brief.*(?:ready|complete)/i.test(latestText);
    return {
      type: "specialist", meta, completed, result: latestText,
      order: ordered[0]?.order ?? 0, key: `specialist:${meta.key}`,
      steps: [
        specialistStep("检索公开资料", searchCount ? `${searchCount} 次主题检索` : "等待开始", completed || sourceCount ? "done" : (searchCount ? "active" : "pending")),
        specialistStep("阅读并交叉核验", sourceCount ? `${sourceCount} 个来源已读取` : "等待可靠来源", completed ? "done" : (sourceCount ? "active" : "pending")),
        specialistStep("整理研究结论", completed ? "研究简报已完成" : (briefStarted ? "正在形成研究简报" : "等待汇总结论"), completed ? "done" : (briefStarted ? "active" : "pending")),
      ],
      metrics: searchCount ? [`${searchCount} 次检索`] : [], previews: [], assetCount: 0,
      queries: Array.isArray(artifacts.queries) ? artifacts.queries : [],
      sources: Array.isArray(artifacts.sources) ? artifacts.sources : [],
    };
  }
  if (meta.role === "material") {
    const files = Array.isArray(artifacts.files) ? artifacts.files : [];
    const parseCount = specialistToolCount(ordered, ["terminal", "read_file"]);
    const summaryCompleted = /(?:材料|附件).*(?:完成|已整理|已解析)|coverage.*complete/i.test(latestText);
    const completed = summaryCompleted || (files.length > 0 && files.every((file) => ["ok", "complete", "completed"].includes(String(file.status || file.coverage || "").toLowerCase())));
    return {
      type: "specialist", meta, completed, result: latestText,
      order: ordered[0]?.order ?? 0, key: `specialist:${meta.key}`,
      steps: [
        specialistStep("接收材料", files.length ? `识别 ${files.length} 份材料` : "等待材料清单", files.length ? "done" : "pending"),
        specialistStep("解析原文与页面", parseCount ? `${parseCount} 次读取/解析动作` : "等待解析", completed ? "done" : (parseCount ? "active" : "pending")),
        specialistStep("形成可引用摘要", completed ? "材料覆盖已确认" : "等待覆盖检查", completed ? "done" : "pending"),
      ],
      metrics: files.length ? [`${files.length} 份材料`] : [], previews: [], files,
    };
  }
  const searchCount = specialistToolCount(ordered, ["web_search", "web_fetch", "web_extract"]);
  const fetchCount = specialistToolCount(ordered, ["fetch_image"]);
  const generateCount = specialistToolCount(ordered, ["image_generate"]);
  const visionCount = specialistToolCount(ordered, ["vision_analyze"]);
  const imageItems = Array.isArray(artifacts.images) ? artifacts.images : [];
  const statedCount = latestText.match(/(\d+)\s*\/\s*(\d+)\s*张/) || latestText.match(/(\d+)\s*(?:个|项|张).*?(?:视觉资产|素材)/);
  const hasArtifactCount = Object.prototype.hasOwnProperty.call(artifacts, "count");
  const assetCount = Number(hasArtifactCount
    ? artifacts.count
    : (imageItems.length || statedCount?.[2] || statedCount?.[1] || generateCount || 0));
  const textCompleted = /(?:素材|视觉资产).*(?:已就绪|完成)|复检结果/i.test(latestText);
  const completed = typeof artifacts.completed === "boolean"
    ? artifacts.completed
    : ((!!artifacts.catalog_ready || textCompleted) && assetCount > 0);
  const fallbackContactSheet = allowLegacyPreviewFallback && (visionCount || completed) ? {
    path: "assets/contact-sheet.png", name: "素材总览", mtime: ordered.at(-1)?.order || 0, legacy: true,
  } : null;
  const contactSheet = artifacts.contact_sheet || fallbackContactSheet;
  const origins = imageItems.reduce((counts, item) => {
    const key = String(item.origin || "unclassified");
    counts[key] = (counts[key] || 0) + 1;
    return counts;
  }, {});
  const collectionStarted = searchCount + fetchCount + generateCount > 0;
  return {
    type: "specialist", meta, completed, result: latestText,
    collectionResult: textUpdates.length > 1 ? textUpdates[0] : "",
    order: ordered[0]?.order ?? 0, key: `specialist:${meta.key}`,
    steps: [
      specialistStep("梳理视觉需求", ordered.length ? "已读取页面规划与视觉方向" : "等待开始", ordered.length ? "done" : "pending"),
      specialistStep("搜索与生成素材", collectionStarted ? `${searchCount} 次搜索 · ${fetchCount} 次下载 · ${generateCount} 次生成` : "等待素材任务", visionCount || completed ? "done" : (collectionStarted ? "active" : "pending")),
      specialistStep("检查素材质量", visionCount ? `${visionCount} 次视觉检查` : "等待视觉检查", completed ? "done" : (visionCount ? "active" : "pending")),
      specialistStep("整理素材库", completed ? `${assetCount} 项素材已就绪` : (assetCount ? `已收集 ${assetCount} 项素材` : "等待素材产出"), completed ? "done" : (assetCount ? "active" : "pending")),
    ],
    metrics: [`${assetCount} 项素材`, `${visionCount} 次检查`],
    previews: imageItems,
    contactSheet,
    assetCount,
    visionCount,
    origins,
  };
}

function legacyImageArtifactScopes(feed, roleArtifacts = {}) {
  // Compatibility for a backend that has not reloaded the per-agent artifact
  // index yet.  The deck-wide image list is chronological, so split it by each
  // generating Agent's real image_generate count instead of repeating it on
  // every visual-material card.
  const scopes = {};
  const images = Array.isArray(roleArtifacts.images) ? roleArtifacts.images : [];
  let cursor = 0;
  Object.entries(feed || {}).forEach(([agent, events]) => {
    const meta = orchestrationAgentMeta(agent);
    if (meta.role !== "image") return;
    const count = specialistToolCount(events, ["image_generate"]);
    const scopedImages = count ? images.slice(cursor, cursor + count) : [];
    cursor += count;
    scopes[agent] = {
      catalog_ready: roleArtifacts.catalog_ready,
      count: scopedImages.length,
      contact_sheet: null,
      images: scopedImages,
    };
  });
  return scopes;
}

function orchestratorProgressStage(value) {
  const text = cleanAgentText(value);
  if (/(?:完成|已完成|ready|交付|产出|已写入|已生成|已保存|收尾|最终总结|通过)/i.test(text)) return "result";
  if (/(?:复核|review|检查|校验|审查|验证)/i.test(text)) return "review";
  if (/(?:页面|slide|编排|制作|排版|渲染|并行生成)/i.test(text)) return "production";
  if (/(?:视觉|素材|配图|图片|image)/i.test(text)) return "visual";
  if (/(?:研究|资料|检索|搜索|事实核验|research)/i.test(text)) return "research";
  return "planning";
}

function compactOrchestratorProgress(value) {
  const text = cleanAgentText(value);
  const lead = text.split("\n")
    .map((line) => line.trim().replace(/^#{1,6}\s+/, "").replace(/^[-*+]\s+/, ""))
    .find(Boolean) || text;
  return lead.length > 180 ? `${lead.slice(0, 179).trim()}…` : lead;
}

function normalizedOrchestratorProgress(value) {
  return compactOrchestratorProgress(value)
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}]+/gu, "");
}

function appendOrchestratorProgress(updates, text, order) {
  const compact = compactOrchestratorProgress(text);
  if (!compact) return;
  const normalized = normalizedOrchestratorProgress(compact);
  const previous = updates.at(-1);
  if (!previous) {
    updates.push({ text: compact, order });
    return;
  }
  const previousNormalized = normalizedOrchestratorProgress(previous.text);
  if (normalized === previousNormalized) return;
  // Models often repeat the same sentence and append one extra clause. Keep
  // the more informative version inside the same phase instead of showing two
  // near-identical progress rows.
  if (previousNormalized.length >= 10 && normalized.includes(previousNormalized)) {
    previous.text = compact;
    previous.order = order;
    return;
  }
  if (normalized.length >= 10 && previousNormalized.includes(normalized)) return;
  updates.push({ text: compact, order });
}

function orchestrationEntries(feed, specialistArtifacts = ed.specialistArtifacts || {}) {
  const entries = [];
  const occurrences = new Map();
  const lastTextByAgent = new Map();
  const flat = orchestrationFlatFeed(feed);
  const supportsAgentArtifacts = Object.prototype.hasOwnProperty.call(specialistArtifacts, "agents");
  const legacyImageScopes = supportsAgentArtifacts
    ? {}
    : legacyImageArtifactScopes(feed, specialistArtifacts.image || {});
  let orchestratorProgress = null;
  const flushOrchestratorProgress = () => {
    if (!orchestratorProgress?.updates?.length) {
      orchestratorProgress = null;
      return;
    }
    entries.push({
      type: "orchestrator-progress",
      meta: orchestratorProgress.meta,
      timing: agentTimingFor("orch"),
      stage: orchestratorProgress.stage,
      updates: orchestratorProgress.updates,
      order: orchestratorProgress.order,
      key: `orchestrator-progress:${orchestratorProgress.stage}:${orchestratorProgress.order}`,
    });
    orchestratorProgress = null;
  };
  flat.forEach((event) => {
    const agentKey = canonicalOrchestrationAgentKey(event.agent);
    const rawMeta = orchestrationAgentMeta(agentKey);
    const isOrchestrator = rawMeta.role === "orch" || agentKey === "orch";
    const meta = isOrchestrator ? orchestrationAgentMeta("orch") : rawMeta;
    if (isOrchestrator && event.k === "text") {
      const text = cleanAgentText(event.s);
      if (!text || lastTextByAgent.get(agentKey) === text) return;
      lastTextByAgent.set(agentKey, text);
      const stage = orchestratorProgressStage(text);
      if (orchestratorProgress && orchestratorProgress.stage !== stage) flushOrchestratorProgress();
      if (!orchestratorProgress) orchestratorProgress = { meta, stage, order: event.order, updates: [] };
      appendOrchestratorProgress(orchestratorProgress.updates, text, event.order);
      return;
    }
    if (isOrchestrator && event.k === "tool" && event.tool === "delegate_task") {
      flushOrchestratorProgress();
      entries.push({ type: "assignment", meta, timing: agentTimingFor("orch"), text: orchestrationAssignmentText(event.hint), order: event.order, key: `assign:${event.order}` });
      return;
    }
    if (isOrchestrator && event.k === "tool") {
      flushOrchestratorProgress();
      const [action, detail] = toolProgress(event);
      const hint = friendlyToolHint(event);
      entries.push({
        type: "orchestrator-action", meta, timing: agentTimingFor("orch"),
        action, detail: hint ? `${detail}：${hint}` : detail,
        order: event.order, key: `orchestrator-action:${event.order}`,
      });
      return;
    }
    if (isOrchestrator) return;
    // A specialist taking over marks a real phase boundary. A later
    // Orchestrator response therefore starts a new card rather than extending
    // a stale planning card from before the delegation.
    flushOrchestratorProgress();
    if (meta.role === "slide") return;
    if (["research", "image", "material", "review"].includes(meta.role)) return;
    if (event.k === "text") {
      const text = cleanAgentText(event.s);
      if (!text || lastTextByAgent.get(agentKey) === text) return;
      lastTextByAgent.set(agentKey, text);
      const occurrenceKey = `${agentKey}\u0000${text}`;
      const occurrence = (occurrences.get(occurrenceKey) || 0) + 1;
      occurrences.set(occurrenceKey, occurrence);
      entries.push({ type: "message", meta, timing: agentTimingFor(event.agent), text, order: event.order, key: JSON.stringify([occurrenceKey, occurrence]) });
    }
  });
  flushOrchestratorProgress();
  Object.entries(feed || {}).forEach(([agent, events]) => {
    const meta = orchestrationAgentMeta(agent);
    if (["research", "image", "material", "review"].includes(meta.role) && (events || []).length) {
      const agentArtifacts = specialistArtifacts.agents?.[meta.key];
      const artifacts = agentArtifacts || (meta.role === "image"
        ? (supportsAgentArtifacts ? {} : (legacyImageScopes[meta.key] || {}))
        : (specialistArtifacts[meta.role] || {}));
      const entry = specialistProgressEntry(meta, events, artifacts, {
        allowLegacyPreviewFallback: meta.role !== "image" || !supportsAgentArtifacts,
      });
      entry.timing = agentTimingFor(agent);
      if (entry.timing?.status === "complete") entry.completed = true;
      entries.push(entry);
      return;
    }
    if (meta.role !== "slide" || !(events || []).length) return;
    const ordered = (events || []).map((event, index) => ({ ...event, order: Number.isFinite(Number(event.seq)) ? Number(event.seq) : index })).sort((a, b) => a.order - b.order);
    const latestTool = [...ordered].reverse().find((event) => event.k === "tool");
    const latestText = [...ordered].reverse().map((event) => event.k === "text" ? cleanAgentText(event.s) : "").find(Boolean) || "";
    const [action] = latestTool ? toolProgress(latestTool) : ["理解本页任务"];
    const completed = (meta.pages?.length
      ? meta.pages.every((page) => ed.rendered.has(page))
      : ed.rendered.has(meta.page)) || ed.status === "completed";
    const result = compactPageAgentResult(latestText, { completed, page: meta.page });
    entries.push({
      type: "slide", meta, action, result: result.summary, resultDetail: result.detail, completed,
      timing: agentTimingFor(agent), order: ordered[0].order, key: `slide:${agent}`,
    });
  });
  return { flat, entries: entries.sort((a, b) => a.order - b.order) };
}

function orchestrationEntryContent(entry) {
  const meta = entry.meta;
  // One Agent can emit many planning/action messages. Repeating the same
  // wall-clock range under every message makes the chronology noisier instead
  // of clearer. Show it once on the durable specialist/page Agent stage card.
  const duration = ["specialist", "slide"].includes(entry.type) ? agentTimingBadge(entry.timing) : "";
  const avatar = `<span class="orch-avatar ${escapeHtml(meta.role)}" aria-hidden="true">
    ${orchestrationAgentGlyph(meta, editorPresentationKind(), "orch-agent-glyph")}
  </span>`;
  if (entry.type === "specialist") {
    const status = entry.completed ? "已完成" : "进行中";
    const steps = entry.steps.map((step) => `<div class="specialist-step ${step.state}"><i></i><span><b>${escapeHtml(step.label)}</b><small>${escapeHtml(step.detail)}</small></span></div>`).join("");
    const galleryMarkup = (items, modifier = "") => {
      const list = Array.isArray(items) ? items : [];
      const previewClass = list.length === 1 ? " single" : "";
      return list.length ? `<div class="specialist-gallery${previewClass}${modifier ? ` ${modifier}` : ""}">${list.map((item) => {
      const rel = String(item.path || "");
      const version = Number(item.mtime || 0);
      const full = `/api/decks/${encodeURIComponent(ed.id)}/file?rel=${encodeURIComponent(rel)}`;
      const thumb = item.legacy ? `${full}&v=${version}` : `/api/decks/${encodeURIComponent(ed.id)}/asset-thumbnail?rel=${encodeURIComponent(rel)}&v=${version}`;
      const label = String(item.name || rel.split("/").at(-1) || "视觉素材").replace(/\.[^.]+$/, "").replace(/^generated-[a-f0-9]+$/i, "生成素材").replace(/^contact-sheet$/i, "素材总览");
      const originLabels = { downloaded: "真实图片 · 已下载", generated: "AI 生成", material: "用户材料", derived: "派生素材", unclassified: "来源未登记" };
      const origin = String(item.origin || "unclassified");
      const sourceHint = item.source_url || item.source_path || item.generator_model || item.parent_asset || "";
      const badge = item.legacy || /contact-sheet/i.test(String(item.name || "")) ? "" : `<em class="asset-origin ${escapeHtml(origin)}" title="${escapeHtml(sourceHint)}">${escapeHtml(originLabels[origin] || originLabels.unclassified)}</em>`;
      return `<a href="${full}" target="_blank" rel="noopener" title="查看原图"><img src="${thumb}" loading="lazy" decoding="async" alt="${escapeHtml(label)}" onerror="this.closest('a').hidden=true"><span>${escapeHtml(label)}</span>${badge}</a>`;
      }).join("")}</div>` : "";
    };
    const previews = galleryMarkup(entry.previews);
    const result = entry.result ? `<div class="specialist-result agent-markdown">${agentMarkdownHtml(entry.result)}</div>` : "";
    const researchSources = meta.role === "research" && entry.sources?.length ? `<details class="specialist-phase research-source-phase specialist-research-details">
        <summary class="specialist-phase-head"><b>已浏览资料</b><span>${entry.sources.length} 个来源</span><i class="specialist-phase-chevron" aria-hidden="true"></i></summary>
        <div class="specialist-research-body">
          ${entry.queries?.length ? `<div class="specialist-query-list">${entry.queries.map((query) => `<span>${escapeHtml(query)}</span>`).join("")}</div>` : ""}
          <div class="specialist-source-list">${entry.sources.map((source) => `<a href="${escapeHtml(source.url || "#")}" target="_blank" rel="noopener noreferrer"><b>${escapeHtml(source.title || source.url || "资料来源")}</b><small>${escapeHtml(source.url || "")}</small></a>`).join("")}</div>
        </div>
      </details>` : "";
    const materialFiles = meta.role === "material" && entry.files?.length ? `<details class="specialist-phase material-file-phase specialist-material-details">
        <summary class="specialist-phase-head"><b>已处理材料</b><span>${entry.files.length} 份</span><i class="specialist-phase-chevron" aria-hidden="true"></i></summary>
        <div class="specialist-file-list">${entry.files.map((file) => `<span><i>${escapeHtml(String(file.kind || "file").toUpperCase())}</i><b>${escapeHtml(file.name || "未命名材料")}</b><em>${escapeHtml(file.coverage || file.status || "处理中")}</em></span>`).join("")}</div>
      </details>` : "";
    const reviewPhases = meta.role === "review" ? `<div class="specialist-phase review-live-phase">
        <div class="specialist-phase-head"><b>实时复核反馈</b><span>${entry.completed ? "复核已完成" : "随检查持续更新"}</span></div>
        <div class="specialist-review-live agent-markdown">${agentMarkdownHtml(entry.result || "Review Agent 正在检查成稿与页面一致性。")}</div>
        ${entry.resultDetail ? `<details class="specialist-review-details"><summary class="specialist-phase-head"><b>查看完整复核说明</b><i class="specialist-phase-chevron" aria-hidden="true"></i></summary><div class="specialist-review-body agent-markdown">${agentMarkdownHtml(entry.resultDetail)}</div></details>` : ""}
      </div>` : "";
    const imagePhases = meta.role === "image" ? `<div class="specialist-phase material-phase">
        <div class="specialist-phase-head"><b>素材采集</b><span>${entry.assetCount ? `已准备 ${entry.assetCount} 项素材` : "正在准备素材"}</span></div>
        ${entry.collectionResult ? `<div class="specialist-phase-note agent-markdown">${agentMarkdownHtml(entry.collectionResult)}</div>` : ""}
        ${previews}
      </div>
      <details class="specialist-phase review-phase specialist-review-details">
        <summary class="specialist-phase-head"><b>整体核验</b><span>${entry.visionCount ? `已完成 ${entry.visionCount} 次视觉检查` : "等待整体检查"}</span><i class="specialist-phase-chevron" aria-hidden="true"></i></summary>
        <div class="specialist-review-body">
          ${galleryMarkup(entry.contactSheet ? [entry.contactSheet] : [], "contact-sheet-gallery")}
          ${result}
        </div>
      </details>` : (meta.role === "review" ? reviewPhases : `${researchSources}${materialFiles}${previews}${result}`);
    return `${avatar}<div class="orch-entry-copy specialist-copy">
      <div class="orch-entry-head"><b>${escapeHtml(meta.label)}</b><i>${escapeHtml(meta.note)}</i>${duration}<em class="${entry.completed ? "done" : "running"}">${status}</em></div>
      <div class="specialist-steps">${steps}</div>${imagePhases}
    </div>`;
  }
  if (entry.type === "slide") {
    const status = entry.completed ? "已完成" : "正在执行";
    const detail = entry.resultDetail ? `<details class="orch-slide-detail"><summary>查看完整制作说明<i></i></summary><div class="agent-markdown">${agentMarkdownHtml(entry.resultDetail)}</div></details>` : "";
    return `${avatar}<div class="orch-entry-copy">
      <div class="orch-entry-head"><b>${escapeHtml(meta.label)}</b><i>${escapeHtml(meta.note)}</i>${duration}<em class="${entry.completed ? "done" : "running"}">${status}</em></div>
      <div class="orch-slide-flow"><span><i></i><b>执行</b><em>${escapeHtml(entry.action)}</em></span>${entry.result ? `<span class="result"><i></i><b>结果</b><em>${agentStoryHtml(entry.result)}</em></span>` : ""}</div>${detail}
    </div>`;
  }
  if (entry.type === "orchestrator-progress") {
    const updates = Array.isArray(entry.updates) ? entry.updates : [];
    const latest = updates.at(-1)?.text || "正在梳理演示结构。";
    const history = updates.slice(0, -1);
    const historyMarkup = history.length ? `<details class="orch-progress-history">
      <summary>查看已合并的 ${history.length} 条阶段进展<i aria-hidden="true"></i></summary>
      <ol>${history.map((update) => `<li>${escapeHtml(update.text)}</li>`).join("")}</ol>
    </details>` : "";
    return `${avatar}<div class="orch-entry-copy">
      <div class="orch-entry-head"><b>${escapeHtml(meta.label)}</b><i>${escapeHtml(meta.note)}</i>${duration}${updates.length > 1 ? `<em>${updates.length} 条进展</em>` : ""}</div>
      <div class="orch-progress-current"><i aria-hidden="true"></i><p>${escapeHtml(latest)}</p></div>
      ${historyMarkup}
    </div>`;
  }
  if (entry.type === "orchestrator-action") {
    return `${avatar}<div class="orch-entry-copy">
      <div class="orch-entry-head"><b>${escapeHtml(meta.label)}</b><i>${escapeHtml(meta.note)}</i>${duration}</div>
      <div class="orch-slide-flow"><span><i></i><b>${escapeHtml(entry.action || "推进任务")}</b><em>${escapeHtml(entry.detail || "继续推进整套演示。")}</em></span></div>
    </div>`;
  }
  return `${avatar}<div class="orch-entry-copy">
    <div class="orch-entry-head"><b>${escapeHtml(meta.label)}</b><i>${escapeHtml(meta.note)}</i>${duration}</div>
    ${entry.type === "assignment" ? `<p>${escapeHtml(entry.text)}</p>` : `<div class="agent-markdown">${agentMarkdownHtml(entry.text)}</div>`}
  </div>`;
}

function reconcileOrchestrationEntries(list, entries) {
  const existing = new Map($$(".orch-entry", list).map((card) => [card.dataset.entryKey, card]));
  entries.forEach((entry, index) => {
    let card = existing.get(entry.key);
    if (!card) {
      card = document.createElement("article");
      card.dataset.entryKey = entry.key;
    }
    card.className = `orch-entry ${entry.type} role-${entry.meta.role}`;
    const content = orchestrationEntryContent(entry);
    if (card.dataset.renderKey !== content) {
      const reviewWasOpen = card.querySelector(".specialist-review-details")?.open === true;
      const researchWasOpen = card.querySelector(".specialist-research-details")?.open === true;
      const materialWasOpen = card.querySelector(".specialist-material-details")?.open === true;
      card.dataset.renderKey = content;
      card.innerHTML = content;
      if (reviewWasOpen) card.querySelector(".specialist-review-details")?.setAttribute("open", "");
      if (researchWasOpen) card.querySelector(".specialist-research-details")?.setAttribute("open", "");
      if (materialWasOpen) card.querySelector(".specialist-material-details")?.setAttribute("open", "");
    }
    existing.delete(entry.key);
    const current = list.children[index];
    if (current !== card) list.insertBefore(card, current || null);
  });
  existing.forEach((card) => card.remove());
}

function renderOrchestrationProgress(body, feed, { running = false, specialistArtifacts = ed.specialistArtifacts || {} } = {}) {
  if (!body) return;
  body.classList.add("is-orchestration");
  body.closest(".outline-agent-turn")?.classList.add("is-orchestration");
  const { flat, entries } = orchestrationEntries(feed, specialistArtifacts);
  const latest = [...flat].reverse().find((event) => event.k === "tool" || cleanAgentText(event.s));
  const latestMeta = orchestrationAgentMeta(latest?.agent || "orch");
  let stateTitle = running ? "正在组织整套演示" : "整套任务记录已整理";
  let stateDetail = running ? "整体规划与各制作环节正在协同推进。" : "可以在这里回看从规划、任务安排到整套检查的完整过程。";
  if (ed.status === "not_started") {
    stateTitle = "尚未开始生成";
    stateDetail = "这条 Query 已进入正式待生成清单，开始运行后会在这里展示完整制作过程。";
  }
  if (latest?.k === "tool") {
    const [action, detail] = toolProgress(latest);
    stateTitle = `${latestMeta.label} · ${action}`;
    stateDetail = detail;
  } else if (latest) {
    const text = cleanAgentText(latest.s);
    stateTitle = `${latestMeta.label} 正在反馈`;
    stateDetail = compactOrchestrationCurrentDetail(latestMeta, text);
  }
  if (body.dataset.progressShell !== "orchestration" || !body.querySelector(".orchestration-stream")) {
    const configHtml = body.id === "agent-progress-console"
      ? outlineTaskConfigMarkup(editorPresentationKind(), taskConfigData())
      : "";
    body.dataset.progressShell = "orchestration";
    body.innerHTML = `
      <section class="agent-now orchestration-now"><div><span class="agent-kicker">当前工作</span><strong></strong><p></p></div></section>
      ${configHtml}
      <div class="orchestration-timing"></div>
      <div class="orchestration-overview"><span><b data-orch-count>${ed.status === "not_started" ? 0 : 1}</b> 整体策划</span><span class="research"><b data-research-count>0</b> Research</span><span class="image"><b data-image-count>0</b> Image</span><span class="material"><b data-material-count>0</b> Material</span><span><b data-slide-count>0</b> 页面制作</span></div>
      <section class="orchestration-stream">
        <button class="orchestration-stream-head" type="button" aria-expanded="true">
          <span>完整制作过程</span><em>关键判断、任务安排与制作结果</em><i aria-hidden="true"></i>
        </button>
        <div class="orchestration-stream-body"><div class="orchestration-entry-list"></div></div>
      </section>
      <details class="agent-actions"><summary><span>底层执行细节</span><em></em><i></i></summary><div class="agent-action-list"></div></details>`;
  }
  const stream = body.querySelector(".orchestration-stream");
  const streamToggle = stream?.querySelector(".orchestration-stream-head");
  if (streamToggle && !streamToggle.dataset.bound) {
    streamToggle.dataset.bound = "1";
    streamToggle.addEventListener("click", () => {
      const collapsed = stream.classList.toggle("collapsed");
      streamToggle.setAttribute("aria-expanded", String(!collapsed));
    });
  }
  const now = body.querySelector(".agent-now");
  now.classList.toggle("is-running", running);
  const nowIcon = now.querySelector(".agent-now-icon");
  const nowGlyph = now.querySelector("[data-now-agent-glyph]");
  const glyphKey = `${editorPresentationKind()}:${latestMeta.role}:${latestMeta.key}`;
  if (nowGlyph && nowGlyph.dataset.glyphKey !== glyphKey) {
    nowGlyph.dataset.glyphKey = glyphKey;
    nowGlyph.innerHTML = orchestrationAgentGlyph(latestMeta, editorPresentationKind(), "orch-agent-glyph");
  }
  if (nowIcon) nowIcon.className = `agent-now-icon role-${latestMeta.role}`;
  now.querySelector("strong").textContent = stateTitle;
  now.querySelector("p").textContent = stateDetail;
  const timingNode = body.querySelector(".orchestration-timing");
  const timingMarkup = orchestrationTimingMarkup();
  if (timingNode.dataset.renderKey !== timingMarkup) {
    timingNode.dataset.renderKey = timingMarkup;
    timingNode.innerHTML = timingMarkup;
    timingNode.hidden = !timingMarkup;
  }
  const keys = Object.keys(feed || {});
  const slideCount = keys.filter((key) => orchestrationAgentMeta(key).role === "slide").length;
  const roleCount = (role) => keys.filter((key) => orchestrationAgentMeta(key).role === role).length;
  body.querySelector("[data-slide-count]").textContent = slideCount;
  body.querySelector("[data-research-count]").textContent = roleCount("research");
  body.querySelector("[data-image-count]").textContent = roleCount("image");
  body.querySelector("[data-material-count]").textContent = roleCount("material");
  const list = body.querySelector(".orchestration-entry-list");
  reconcileOrchestrationEntries(list, entries);
  const actions = summarizeToolEvents(flat);
  const actionCount = flat.filter((event) => event.k === "tool").length;
  body.querySelector(".agent-actions summary em").textContent = actionCount ? `${actions.length} 类动作 · ${actionCount} 次执行` : "等待执行";
  const actionList = body.querySelector(".agent-action-list");
  const actionKey = JSON.stringify([running, actions]);
  if (actionList.dataset.renderKey !== actionKey) {
    actionList.dataset.renderKey = actionKey;
    actionList.innerHTML = actions.length ? actions.map((action, index) => `<div class="agent-action${index === 0 && running ? " active" : ""}"><span>${index === 0 && running ? "•" : "✓"}</span><b>${escapeHtml(action.label)}</b>${action.hint ? `<i>${escapeHtml(action.hint)}</i>` : ""}${action.count > 1 ? `<em>×${action.count}</em>` : ""}</div>`).join("") : '<div class="agent-action-empty">还没有底层执行记录</div>';
  }
}

function miniRow(ev) {
  const d = document.createElement("div");
  d.className = "phl-row" + (ev.k === "tool" ? " cmd" : "");
  d.textContent = ev.k === "tool"
    ? `${TOOL_LABEL[ev.tool] || "推进任务"}${compactActivityHint(ev.hint) ? " · " + compactActivityHint(ev.hint) : ""}`
    : ev.s;
  return d;
}

async function fetchLive() {
  if (!ed.id) return;
  try {
    const payload = await jget(trajectoryMode ? trajectoryDeckApi(ed.id, "/livefeed") : `/api/decks/${ed.id}/livefeed`);
    ed.feed = payload.agents || {};
    ed.pageAgents = reconcilePageAgentOwners(ed.feed, payload.page_agents || {});
    ed.specialistArtifacts = payload.specialist_artifacts || {};
    ed.agentTimings = payload.agent_timings || {};
    ed.overallTiming = payload.overall_timing || null;
  }
  catch { return; }
  renderLive();
  if (ed.kind === "static" && ed.sel > 0) loadPageHistory(ed.sel, { quiet: true });
}

function reconcilePageAgentOwners(feed, declared) {
  const owners = { ...(declared || {}) };
  const hasAgent = (key) => !!key && Array.isArray(feed?.[key]);
  Object.entries(feed || {}).forEach(([key, events]) => {
    const normalized = String(key || "");
    const grouped = /^slide[-_]group[-_]/i.test(normalized)
      || (/^slide[-_]/i.test(normalized) && !/^slide[-_]0*\d+(?:_r\d+)?$/i.test(normalized));
    if (!grouped) return;
    const pages = new Set();
    (events || []).forEach((event) => {
      const value = `${event?.hint || ""}\n${event?.s || ""}`;
      for (const match of value.matchAll(/(?:slide_|page_)0*(\d{1,3})(?=\D|$)/gi)) {
        const page = Number(match[1]);
        if (page > 0) pages.add(page);
      }
      const summary = value.match(/\bpages\s*:\s*([0-9][0-9,\s]*)/i);
      if (summary) (summary[1].match(/\d+/g) || []).forEach((page) => pages.add(Number(page)));
    });
    pages.forEach((page) => {
      // Repair stale mappings produced by older parser versions only when the
      // declared owner is absent from this feed.  A valid backend mapping wins.
      if (!hasAgent(owners[String(page)])) owners[String(page)] = key;
    });
  });
  return owners;
}

function renderLive() {
  const feed = ed.feed || {};
  // 1) 胶片条:00 帧单行;生成中占位帧 = 该页设计师的全量微型滚动日志
  $$("#filmstrip .frame .ph .ph-log").forEach((el) => {
    const n = +el.closest(".frame").dataset.n;
    ed.phLines[n] = ed.phLines[n] || [];
    syncStream(el, feed[agentKey(n)] || [], ed.phLines[n], miniRow, 30, ".phl-row");
  });
  // 2) 00=整套任务的全 Agent 编排;其他页=该页设计师,未分派时回退编排器
  const con = $("#agent-progress-console") || $("#gen-console");
  if (con) {
    if (ed.sel === 0) {
      if (ed.conKey !== "orchestration") {
        ed.conKey = "orchestration"; ed.conLines = [];
        delete con.dataset.progressSignature;
        const title = $("#term-title");
        if (title) title.textContent = "全程编排";
      }
      renderOrchestrationProgress(con, feed, { running: isStaticActiveStatus(ed.status) });
      return;
    }
    let key = agentKey(ed.sel);
    let evs = feed[key] || [];
    if (ed.conKey !== key) {
      ed.conKey = key; ed.conLines = [];
      delete con.dataset.progressSignature;
      const t = $("#term-title");
      if (t) t.textContent = "本页制作进展";
      const pageAgent = $("#ce-agent");
      if (pageAgent) pageAgent.textContent = pageAgentLabel(key, ed.sel);
    }
    const running = isStaticActiveStatus(ed.status);
    renderAgentProgress(con, evs, {
      running,
      rendered: ed.sel > 0 && ed.rendered.has(ed.sel),
      n: ed.sel || 0,
      source: key,
    });
    const task = $("#ce-task");
    if (task) {
      const lastTool = [...evs].reverse().find((e) => e.k === "tool");
      const txt = lastTool ? `· ${TOOL_LABEL[lastTool.tool] || lastTool.tool}` : "";
      if (task.textContent !== txt) task.textContent = txt;
    }
  }
}

function startLive() {
  if (ed.liveTimer) return;
  fetchLive();
  // The Harness persists model deltas continuously when streaming is enabled.
  // Poll in small display batches; canonical completed turns still replace
  // partial text atomically, so this never creates a second conversation.
  ed.liveTimer = setInterval(fetchLive, 750);
}
function stopLive() {
  if (ed.liveTimer) { clearInterval(ed.liveTimer); ed.liveTimer = null; }
}

function updateNav() {
  // 注意 ed.sel===0(过程帧)是合法值,不能用 truthy 判断
  $("#canvas-pos").textContent = ed.sel != null ? `${pad2(ed.sel)} / ${ed.total ? pad2(ed.total) : "–"}` : "– / –";
  $("#prev-btn").disabled = ed.sel == null || ed.sel <= 1;
  $("#next-btn").disabled = ed.sel == null || (ed.total && ed.sel >= ed.total);
}

function setFollow(on) {
  ed.follow = on;
  $("#follow-btn").classList.toggle("on", on);
  if (on && ed.rendered.size) select(Math.max(...ed.rendered), { byUser: false });
}

/* ----- 本页规划 + 相关素材(画布上方) ----- */
async function loadPlan(n) {
  const panel = $("#plan-panel");
  if (ed.kind === "dynamic") { panel.hidden = true; return; }
  const cached = ed.planCache[n];
  if (cached === false) {                                  // 已知没有该页 md
    if (n === 0) panel.hidden = true; else renderPlanMissing(n);
    return;
  }
  if (cached) { renderPlan(n, cached); return; }
  let info;
  try { info = await jget(`${trajectoryMode ? trajectoryDeckApi(ed.id, "/slideinfo") : `/api/decks/${ed.id}/slideinfo`}?n=${n}`); }
  catch { panel.hidden = true; return; }
  if (ed.sel !== n) return;                                // 用户已切到别页
  if (!info.exists) {
    // 生成中规划可能还没写出来 → 不缓存为永久失败,稍后再试
    if (["completed", "failed", "rejected", "interrupted"].includes(ed.status)) ed.planCache[n] = false;
    if (n === 0) panel.hidden = true;
    else renderPlanMissing(n);                             // 如实告知 + 指路全局大纲
    return;
  }
  ed.planCache[n] = info;
  renderPlan(n, info);
}

function renderPlanMissing(n) {
  const panel = $("#plan-panel");
  panel.hidden = false;
  panel.classList.remove("has-assets");
  panel.classList.toggle("collapsed", !!ed.ppCollapsed);
  $("#pp-title").textContent = `第 ${pad2(n)} 页 · 规划`;
  $("#pp-html").hidden = true;
  $("#pp-assets").hidden = true;
  $("#pp-md").innerHTML = `<p class="pp-missing">本页没有独立的规划文档(该模型把页面规划写在全局大纲里)。
    <button class="link" id="pp-goto">查看 00 · 全局大纲 →</button></p>`;
  $("#pp-toggle").textContent = ed.ppCollapsed ? "展开" : "收起";
  $("#pp-goto").onclick = () => select(0, { byUser: true });
}

function renderPlan(n, info) {
  const panel = $("#plan-panel");
  panel.hidden = false;
  $("#pp-title").textContent = n === 0 ? "全局大纲 · deck.md" : `第 ${pad2(n)} 页 · 规划`;
  const link = $("#pp-html");
  if (info.html_rel) {
    link.hidden = false;
    link.href = trajectoryMode ? trajectoryDeckApi(ed.id, `/files/${info.html_rel}`) : `/api/decks/${ed.id}/files/${info.html_rel}`;
    link.textContent = n === 0 ? "设计系统 css" : "页面源码";
  } else link.hidden = true;
  $("#pp-md").innerHTML = info.html;
  const assets = info.assets || [];
  panel.classList.toggle("has-assets", !!assets.length);
  $("#pp-assets").hidden = !assets.length;
  $("#pp-assets-grid").innerHTML = assets.map((a) =>
    `<img class="pp-asset" src="${trajectoryMode ? trajectoryDeckApi(ed.id, `/files/${a}`) : `/api/decks/${ed.id}/files/${a}`}" title="${escapeHtml(a)}">`).join("");
  $$(".pp-asset").forEach((img) =>
    (img.onclick = () => { $("#lb-img").src = img.src; $("#lightbox").hidden = false; }));
  panel.classList.toggle("collapsed", !!ed.ppCollapsed);
  $("#pp-toggle").textContent = ed.ppCollapsed ? "展开" : "收起";
}

/* ----- SSE ----- */
function subscribe(id) {
  const es = new EventSource(`/api/decks/${id}/events`);
  ed.sse = es;
  es.onmessage = (ev) => {
    if (ed.id !== id) { es.close(); return; }
    let p; try { p = JSON.parse(ev.data); } catch { return; }
    if (p && p.phase) apply(p);
    if (p && ["completed", "failed", "rejected", "interrupted"].includes(p.status)) { es.close(); if (ed.sse === es) ed.sse = null; }
  };
  es.addEventListener("end", () => { es.close(); if (ed.sse === es) ed.sse = null; });
}

/* ---------------- 初始化(defer 脚本,DOM 已就绪) ---------------- */
if (!$("#editor")) {
  /* 登录/注册页也会加载本脚本,没有工作台元素,直接不初始化 */
} else {
showComposer();
if (trajectoryMode) {
  const label = $("#new-btn .new-btn-label");
  if (label) label.textContent = "返回生成工作台";
  $("#new-btn")?.removeAttribute("aria-keyshortcuts");
  if ($("#batch-open-btn")) $("#batch-open-btn").hidden = true;
}
initRailDisclosureAnimations();
$(".creation-mode").addEventListener("click", (e) => {
  const card = e.target.closest(".creation-mode-card[data-mode]");
  if (!card) return;
  setCreationMode(card.dataset.mode);
});
$("#refresh-suggestions").addEventListener("click", renderSuggestions);
$("#user-menu-trigger").addEventListener("click", (e) => {
  e.stopPropagation();
  setUserMenu($("#user-menu").hidden);
});
$("#user-menu").addEventListener("click", (e) => {
  const item = e.target.closest("[data-user-action]");
  if (!item) return;
  const action = item.dataset.userAction;
  setUserMenu(false);
  if (action === "auth") setAuthGate(true);
  else if (action === "new") closeEditor();
  else if (action === "batch") openBatchModal();
  else if (action === "model") setModelDialog(true);
  else if (action === "services") setServiceDialog(true);
  else if (action === "static" || action === "dynamic") {
    setCreationMode(action);
    closeEditor();
    $("#q").focus();
  } else if (action === "switch") {
    switchAccount();
  } else if (action === "logout") {
    $("#logout-form").requestSubmit();
  }
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#user-menu") && !e.target.closest("#user-menu-trigger")) setUserMenu(false);
  if (e.target.closest("[data-open-auth]")) setAuthGate(true);
});
$$('[data-auth-tab]').forEach((button) => button.addEventListener("click", () => setAuthTab(button.dataset.authTab)));
$("#auth-login-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuthForm(event.currentTarget, "/api/auth/login");
});
$("#auth-register-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuthForm(event.currentTarget, "/api/auth/register");
});
$("#auth-gate-close")?.addEventListener("click", () => setAuthGate(false));
$("#auth-gate")?.addEventListener("click", (event) => {
  if (event.target === $("#auth-gate")) setAuthGate(false);
});
const savedThinkingPreference = localStorage.getItem("studio_thinking");
setThinkingEnabled(
  savedThinkingPreference === null ? true : savedThinkingPreference === "1",
  { remember: false },
);
$("#thinking-toggle").addEventListener("click", () => setThinkingEnabled(!thinkingEnabled()));
$("#style-trigger").addEventListener("click", (e) => {
  e.stopPropagation();
  setLengthMenu(false);
  setModelMenu(false);
  setSkillMenu(false);
  setAttachMenu(false);
  setFontPanel(false);
  setStyleGallery($("#style-gallery").hidden);
});
$("#length-trigger").addEventListener("click", (e) => {
  e.stopPropagation();
  setStyleGallery(false);
  setModelMenu(false);
  setSkillMenu(false);
  setAttachMenu(false);
  setFontPanel(false);
  setLengthMenu($("#length-menu").hidden);
});
$("#model-trigger").addEventListener("click", async (e) => {
  e.stopPropagation();
  setStyleGallery(false);
  setLengthMenu(false);
  setSkillMenu(false);
  setAttachMenu(false);
  setFontPanel(false);
  if (!$("#model-sel").options.length) {
    setModelMenu(false);
    await setModelDialog(true);
    return;
  }
  setModelMenu($("#model-menu").hidden);
});
$("#skill-trigger").addEventListener("click", (e) => {
  e.stopPropagation();
  setStyleGallery(false);
  setLengthMenu(false);
  setModelMenu(false);
  setAttachMenu(false);
  setFontPanel(false);
  setSkillMenu($("#skill-menu").hidden);
});
$("#model-menu").addEventListener("click", (e) => {
  const deleteButton = e.target.closest("[data-delete-model]");
  if (deleteButton) {
    e.stopPropagation();
    void deleteCustomModel(deleteButton.dataset.deleteModel, deleteButton);
    return;
  }
  if (e.target.closest("[data-configure-model]")) {
    setModelMenu(false);
    void setModelDialog(true);
    return;
  }
  const item = e.target.closest("[data-model]");
  if (!item || item.disabled) return;
  $("#model-sel").value = item.dataset.model;
  normalizeVersionSelection();
  setModelMenu(false);
});
$("#skill-menu").addEventListener("click", (e) => {
  const item = e.target.closest("[data-skill]");
  if (!item || item.disabled) return;
  if (creationMode === "dynamic") {
    dynamicSkill = item.dataset.skill;
    localStorage.setItem("studio_dynamic_skill", dynamicSkill);
    syncSkillPicker();
    setSkillMenu(false);
    return;
  }
  $("#skill-sel").value = item.dataset.skill;
  normalizeVersionSelection();
  setSkillMenu(false);
});
$("#length-menu").addEventListener("click", (e) => {
  const item = e.target.closest("[data-length]");
  if (!item) return;
  syncLengthPicker(item.dataset.length);
  localStorage.setItem("studio_length", $("#length-sel").value);
  setLengthMenu(false);
});
$("#style-gallery-close").addEventListener("click", () => setStyleGallery(false));
$("#style-gallery").addEventListener("click", (e) => {
  const card = e.target.closest(".style-card[data-style]");
  if (!card) return;
  syncStylePicker(card.dataset.style || "");
  localStorage.setItem("studio_style", $("#style-sel").value);
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#style-gallery") && !e.target.closest("#style-trigger")) setStyleGallery(false);
  if (!e.target.closest("#length-menu") && !e.target.closest("#length-trigger")) setLengthMenu(false);
  if (!e.target.closest("#model-menu") && !e.target.closest("#model-trigger")) setModelMenu(false);
  if (!e.target.closest("#skill-menu") && !e.target.closest("#skill-trigger")) setSkillMenu(false);
  if (!e.target.closest("#attach-menu") && !e.target.closest("#attach-btn")) setAttachMenu(false);
  if (!e.target.closest("#font-panel") && !e.target.closest("#font-trigger")) setFontPanel(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { setStyleGallery(false); setLengthMenu(false); setModelMenu(false); setSkillMenu(false); setAttachMenu(false); setFontPanel(false); setUserMenu(false); }
});
$("#suggestion-list").addEventListener("click", (e) => {
  const chip = e.target.closest(".suggestion-chip");
  if (!chip) return;
  const input = $("#q");
  input.value = chip.dataset.query || chip.textContent;
  resizePrimaryComposerInput(input);
  input.focus();
});
$("#send").addEventListener("click", send);
$("#q").addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") send(); });
$("#q").addEventListener("input", (e) => resizePrimaryComposerInput(e.target));
resizePrimaryComposerInput();
$("#workspace-view-switch")?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-workspace-view]");
  if (!button) return;
  setWorkspaceView(button.dataset.workspaceView);
});
$("#view-toggle").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-view]");
  if (!b || b.disabled) return;
  setPageView(b.dataset.view);
});
// 版本选择:用户选择模型和 Skill;pipeline 由后端给每个模型的默认适配版本自动同步。
// Keep retired built-ins readable for historical decks in the backend registry,
// but remove them immediately from every new-task selector even before the
// Python service is next reloaded.
const RETIRED_BUILT_IN_MODELS = new Set(["sensenova-flash-lite-v39-3"]);
[$("#model-sel"), $("#batch-model")].filter(Boolean).forEach((select) => {
  [...select.options].forEach((option) => {
    if (RETIRED_BUILT_IN_MODELS.has(option.value)) option.remove();
  });
});
if (RETIRED_BUILT_IN_MODELS.has(localStorage.getItem("studio_model"))) {
  localStorage.removeItem("studio_model");
  localStorage.removeItem("studio_model_default");
}
const currentDefaultModel = $("#model-sel").value;
const savedModel = localStorage.getItem("studio_model");
const savedModelDefault = localStorage.getItem("studio_model_default");
if (savedModel && savedModelDefault === currentDefaultModel && $(`#model-sel option[value="${savedModel}"]`)) {
  $("#model-sel").value = savedModel;
}
localStorage.setItem("studio_model_default", currentDefaultModel);
const currentDefaultSkill = $("#skill-sel").value;
const savedSkill = localStorage.getItem("studio_skill");
const savedSkillDefault = localStorage.getItem("studio_skill_default");
if (savedSkill && savedSkillDefault === currentDefaultSkill && $(`#skill-sel option[value="${savedSkill}"]`)) {
  $("#skill-sel").value = savedSkill;
}
localStorage.setItem("studio_skill_default", currentDefaultSkill);
// 篇幅控件：一级菜单与原生成参数保持同一取值契约。
const savedLength = localStorage.getItem("studio_length");
if (savedLength && $(`#length-sel option[value="${savedLength}"]`)) $("#length-sel").value = savedLength;
$("#length-sel").addEventListener("change", () => {
  localStorage.setItem("studio_length", $("#length-sel").value);
  syncLengthPicker($("#length-sel").value);
});
syncLengthPicker($("#length-sel").value);
["theme", "style", "scheme"].forEach((k) => {   // 生成偏好:记忆选择
  const el = $(`#${k}-sel`); if (!el) return;
  const saved = localStorage.getItem("studio_" + k);
  if (saved && [...el.options].some((o) => o.value === saved)) el.value = saved;
  el.addEventListener("change", () => localStorage.setItem("studio_" + k, el.value));
});
syncStylePicker($("#style-sel").value);
function modelBackend() {
  const opt = $("#model-sel").selectedOptions && $("#model-sel").selectedOptions[0];
  return opt ? opt.dataset.backend : "";
}
function selectedModelOption() {
  return $("#model-sel").selectedOptions && $("#model-sel").selectedOptions[0];
}
function selectedPipelineOption() {
  return $("#pipeline-sel").selectedOptions && $("#pipeline-sel").selectedOptions[0];
}
function selectedSkillOption() {
  return $("#skill-sel").selectedOptions && $("#skill-sel").selectedOptions[0];
}
function pipelineSupports(opt, backend) {
  return !!opt && (opt.dataset.supports || "").split(",").includes(backend);
}
function updatePipelineDisplay() {
  const pipeOpt = selectedPipelineOption();
  const display = $("#pipeline-display");
  if (display) display.textContent = pipeOpt ? (PIPELINE_LABEL[pipeOpt.value] || pipeOpt.textContent.trim()) : "自动";
}
function normalizeVersionSelection({ remember } = { remember: true }) {
  const modelOpt = selectedModelOption();
  const backend = modelBackend();
  $$("#pipeline-sel option").forEach((o) => {
    const ok = !modelOpt || pipelineSupports(o, backend);
    o.disabled = !ok;
  });
  $$("#skill-sel option").forEach((o) => {
    if (o.value === "long-horizon") {
      o.disabled = true;
      o.hidden = true;
      return;
    }
    const linked = $(`#pipeline-sel option[value="${o.dataset.pipeline || ""}"]`);
    o.disabled = o.dataset.ready !== "1" || (!!modelOpt && !pipelineSupports(linked, backend));
  });
  if (selectedSkillOption()?.disabled) {
    const fallback = $$("#skill-sel option").find((o) => !o.disabled);
    if (fallback) $("#skill-sel").value = fallback.value;
  }
  const matchedPipeline = selectedSkillOption()?.dataset.pipeline || modelOpt?.dataset.defaultPipeline || "";
  if (matchedPipeline && $(`#pipeline-sel option[value="${matchedPipeline}"]`)) {
    $("#pipeline-sel").value = matchedPipeline;
  }
  updatePipelineDisplay();
  syncAttachZone();                  // 管线能力联动:附件上传区随所选管线显隐
  syncFontControl();
  syncModelPicker();
  renderSkillMenu();
  if (remember) {
    localStorage.setItem("studio_model", $("#model-sel").value);
    localStorage.setItem("studio_skill", $("#skill-sel").value);
  }
}
$("#model-sel").addEventListener("change", () => normalizeVersionSelection());
$("#skill-sel").addEventListener("change", () => normalizeVersionSelection());
normalizeVersionSelection({ remember: false });
renderModelMenu();
$("#model-dialog-close").addEventListener("click", () => setModelDialog(false));
$("#model-form-cancel").addEventListener("click", () => setModelDialog(false));
$("#model-modal").addEventListener("click", (event) => {
  if (event.target === $("#model-modal")) setModelDialog(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#model-modal").hidden) setModelDialog(false);
  if (event.key === "Escape" && !$("#service-modal").hidden) setServiceDialog(false);
});

$("#service-dialog-close").addEventListener("click", () => setServiceDialog(false));
$("#service-form-cancel").addEventListener("click", () => setServiceDialog(false));
$("#service-modal").addEventListener("click", (event) => {
  if (event.target === $("#service-modal")) setServiceDialog(false);
});
["image", "search"].forEach((name) => {
  $(`#service-${name}-enabled`).addEventListener("change", syncServiceCards);
});
$("#service-image-provider").addEventListener("change", () => syncImageProviderFields({ applyDefaults: true }));
$("#service-generation-reset").addEventListener("click", () => {
  $("#service-max-tokens").value = 40960;
  $("#service-streaming-enabled").checked = true;
  $("#service-static-max-turns").value = 4096;
  $("#service-static-subagent-max-turns").value = 200;
  $("#service-dynamic-max-turns").value = 4096;
});
$("#service-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  const errorBox = $("#service-form-error");
  errorBox.hidden = true;
  submit.disabled = true;
  const originalText = submit.textContent;
  try {
    const response = await fetch("/api/settings/services", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image_enabled: $("#service-image-enabled").checked,
        image_provider: $("#service-image-provider").value,
        image_base_url: $("#service-image-url").value.trim(),
        image_model: $("#service-image-model").value.trim(),
        image_api_key: $("#service-image-key").value || null,
        clear_image_api_key: $("#service-image-clear").checked,
        search_enabled: $("#service-search-enabled").checked,
        search_base_url: $("#service-search-url").value.trim(),
        search_api_key: $("#service-search-key").value || null,
        clear_search_api_key: $("#service-search-clear").checked,
        max_tokens: Number($("#service-max-tokens").value),
        streaming_enabled: $("#service-streaming-enabled").checked,
        static_max_turns: Number($("#service-static-max-turns").value),
        static_subagent_max_turns: Number($("#service-static-subagent-max-turns").value),
        dynamic_max_turns: Number($("#service-dynamic-max-turns").value),
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "服务配置保存失败");
    applyServiceSettings(payload);
    submit.textContent = "已保存";
    setTimeout(() => setServiceDialog(false), 360);
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    submit.disabled = false;
    setTimeout(() => { submit.textContent = originalText; }, 500);
  }
});
$("#model-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type=submit]");
  const errorBox = $("#model-form-error");
  errorBox.hidden = true;
  submit.disabled = true;
  try {
    const response = await fetch("/api/models/custom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("#custom-model-name").value.trim(),
        model_id: $("#custom-model-id").value.trim(),
        base_url: $("#custom-model-url").value.trim(),
        api_key: $("#custom-model-key").value,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "模型添加失败");
    customModelItems.push(payload);
    upsertCustomModelOption(payload, { select: true });
    renderCustomModelList();
    form.reset();
    await setModelDialog(false);
    void guideOptionalMaterialServices();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    submit.disabled = false;
  }
});
$("#custom-model-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-model]");
  if (!button) return;
  await deleteCustomModel(button.dataset.deleteModel, button);
});
if ($("#attach-btn")) {              // 附件上传区交互(仅支持附件的管线可见)
  const savedAttachmentMode = localStorage.getItem("studio_attachment_mode");
  if (savedAttachmentMode && $(`#attachment-mode option[value="${savedAttachmentMode}"]`)) {
    $("#attachment-mode").value = savedAttachmentMode;
  }
  $("#attachment-mode").addEventListener("change", () =>
    localStorage.setItem("studio_attachment_mode", attachmentMode()));
  $("#attach-btn").addEventListener("click", (event) => {
    event.stopPropagation();
    if (creationMode !== "static") return;
    setStyleGallery(false);
    setLengthMenu(false);
    setModelMenu(false);
    setSkillMenu(false);
    setAttachMenu($("#attach-menu").hidden);
  });
  $("#attach-upload-action").addEventListener("click", () => {
    if (creationMode !== "static") return;
    setAttachMenu(false);
    $("#attach-input").click();
  });
  $("#attach-input").addEventListener("change", (e) => {
    if (creationMode !== "static") {
      e.target.value = "";
      clearAttachFiles();
      return;
    }
    for (const f of e.target.files) {
      if (attachFiles.length >= 8) { alert("最多 8 个附件"); break; }
      if (f.size > 20 * 1024 * 1024) { alert(`「${f.name}」超过 20MB,已跳过`); continue; }
      attachFiles.push(f);
    }
    e.target.value = "";             // 允许再次选同名文件
    renderAttachList();
  });
  $("#attach-list").addEventListener("click", (e) => {
    const remove = e.target.closest(".attach-x");
    if (remove) {
      const [file] = attachFiles.splice(+remove.dataset.i, 1);
      if (file) releaseAttachmentObjectUrl(file);
      renderAttachList();
      return;
    }
    const open = e.target.closest("[data-local-attachment]");
    if (!open) return;
    const file = attachFiles[+open.dataset.localAttachment];
    if (!file) return;
    const url = attachmentObjectUrl(file);
    openAttachmentViewer({ name: file.name, type: file.type, size: file.size, url, downloadUrl: url });
  });
}
if ($("#font-trigger")) {
  renderFontControls();
  $("#font-trigger").addEventListener("click", (event) => {
    event.stopPropagation();
    if (creationMode !== "static" || !pipelineHasCap("custom_fonts")) return;
    setStyleGallery(false); setLengthMenu(false); setModelMenu(false); setSkillMenu(false); setAttachMenu(false);
    setFontPanel($("#font-panel").hidden);
  });
  $("#font-upload").addEventListener("click", () => $("#font-input").click());
  $("#font-input").addEventListener("change", (event) => {
    for (const file of event.target.files || []) {
      if (fontFiles.length >= 6) { alert("自定义字体最多上传 6 个"); break; }
      if (!/\.(ttf|otf|woff2?)$/i.test(file.name)) { alert(`「${file.name}」不是支持的字体格式`); continue; }
      if (file.size > 25 * 1024 * 1024) { alert(`「${file.name}」超过 25MB，已跳过`); continue; }
      if (fontFiles.some((item) => item.name === file.name && item.size === file.size)) continue;
      fontFiles.push(file);
    }
    event.target.value = "";
    renderFontControls();
  });
  $("#font-file-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-font-remove]");
    if (!button) return;
    fontFiles.splice(Number(button.dataset.fontRemove), 1);
    renderFontControls();
  });
  $("#font-panel").addEventListener("change", (event) => {
    if (event.target.matches("[data-font-role]")) renderFontControls();
  });
}
function startNewConversation() {
  if (trajectoryMode) { window.location.href = "/"; return; }
  if (!$("#batch-modal").hidden) closeBatchModal();
  if (!$("#model-modal").hidden) setModelDialog(false);
  setUserMenu(false);
  closeEditor();
  requestAnimationFrame(() => $("#q")?.focus());
}
$("#new-btn").addEventListener("click", startNewConversation);
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    startNewConversation();
  }
});
const creationWorkspace = $("#creation-workspace");
let ambientPointerFrame = 0;
creationWorkspace?.addEventListener("pointermove", (event) => {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  cancelAnimationFrame(ambientPointerFrame);
  ambientPointerFrame = requestAnimationFrame(() => {
    const bounds = creationWorkspace.getBoundingClientRect();
    const x = ((event.clientX - bounds.left) / bounds.width - .5) * 20;
    const y = ((event.clientY - bounds.top) / bounds.height - .5) * 14;
    creationWorkspace.style.setProperty("--flow-x", `${x.toFixed(2)}px`);
    creationWorkspace.style.setProperty("--flow-y", `${y.toFixed(2)}px`);
  });
});
creationWorkspace?.addEventListener("pointerleave", () => {
  creationWorkspace.style.setProperty("--flow-x", "0px");
  creationWorkspace.style.setProperty("--flow-y", "0px");
});
$("#sidebar-toggle").addEventListener("click", () => {
  setSidebarCollapsed(!document.querySelector(".layout")?.classList.contains("sidebar-collapsed"));
});
$("#sidebar-expand").addEventListener("click", () => setSidebarCollapsed(false));
$("#sidebar-scrim")?.addEventListener("click", () => setSidebarCollapsed(true, { remember: false }));
$("#proc-toggle")?.addEventListener("click", () => {
  if (matchMedia("(max-width: 1799px)").matches) {
    setProcRailOpen(!$("#editor")?.classList.contains("proc-open"));
    return;
  }
  setProcRailCollapsed(!$("#editor")?.classList.contains("proc-rail-collapsed"));
});
$("#proc-rail-collapse")?.addEventListener("click", () => {
  if (matchMedia("(max-width: 1799px)").matches) setProcRailOpen(false);
  else setProcRailCollapsed(true);
});
$("#proc-rail-expand")?.addEventListener("click", () => setProcRailCollapsed(false));
$("#proc-scrim")?.addEventListener("click", () => setProcRailOpen(false));
$("#canvas-deck").addEventListener("load", bindDynamicDeckNavigation);
if (typeof ResizeObserver === "function") {
  const deckViewportObserver = new ResizeObserver(() => scheduleDeckViewportFit());
  deckViewportObserver.observe($("#canvas").parentElement);
  deckViewportObserver.observe($(".canvas-bottom-dock"));
}
window.addEventListener("resize", () => {
  scheduleDeckViewportFit();
  if (!$("#composer")?.hidden) resizePrimaryComposerInput();
});
function syncPresentationFullscreenScale() {
  const fullscreenElement = document.fullscreenElement || document.webkitFullscreenElement;
  const presentationFullscreen = fullscreenElement === $("#canvas");
  document.documentElement.classList.toggle("presentation-fullscreen", presentationFullscreen);
  scheduleDeckViewportFit();
  setTimeout(() => scheduleDeckViewportFit(), 180);
}
document.addEventListener("fullscreenchange", syncPresentationFullscreenScale);
document.addEventListener("webkitfullscreenchange", syncPresentationFullscreenScale);
$$('[data-theme-choice]').forEach((choice) => choice.addEventListener("click", () => {
  setTheme(choice.dataset.themeChoice);
}));
$("#settings-language").addEventListener("change", (event) => {
  setLanguage(event.target.value);
});
$("#batch-open-btn").addEventListener("click", openBatchModal);
$("#batch-close").addEventListener("click", closeBatchModal);
$("#batch-refresh").addEventListener("click", () => loadBatches());
$("#batch-form").addEventListener("submit", submitBatch);
$("#batch-modal").addEventListener("click", (e) => {
  if (e.target.id === "batch-modal") closeBatchModal();
});
$("#back-btn").addEventListener("click", () => {
  if (trajectoryMode) { window.location.href = "/"; return; }
  closeEditor(); loadDecks();
});
$("#prev-btn").addEventListener("click", () => ed.sel > 1 && select(ed.sel - 1, { byUser: true }));
$("#next-btn").addEventListener("click", () => ed.sel != null && ed.sel < ed.total && select(ed.sel + 1, { byUser: true }));
$("#follow-btn").addEventListener("click", () => setFollow(!ed.follow));
$("#page-speech-toggle").addEventListener("click", () => {
  const drawer = $("#pr-speech-section");
  const collapsed = !drawer.classList.contains("collapsed");
  localStorage.setItem("studio_speech_collapsed", collapsed ? "1" : "0");
  drawer.classList.toggle("collapsed", collapsed);
  $("#page-speech-toggle").setAttribute("aria-expanded", String(!collapsed));
  requestAnimationFrame(() => scheduleDeckViewportFit());
  setTimeout(() => scheduleDeckViewportFit(), 440);
});
$("#page-speech-source-toggle").addEventListener("click", () => {
  const drawer = $("#pr-speech-section");
  const toggle = $("#page-speech-source-toggle");
  const evidence = $("#page-speech-evidence");
  if (!evidence) return;
  if (drawer.classList.contains("collapsed")) {
    drawer.classList.remove("collapsed");
    localStorage.setItem("studio_speech_collapsed", "0");
    $("#page-speech-toggle").setAttribute("aria-expanded", "true");
  }
  const open = toggle.getAttribute("aria-expanded") !== "true";
  toggle.setAttribute("aria-expanded", String(open));
  evidence.hidden = !open;
});
document.addEventListener("keydown", (e) => {
  if ($("#editor").hidden || e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;
  if (e.key === "ArrowLeft" && ed.sel > 1) select(ed.sel - 1, { byUser: true });
  if (e.key === "ArrowRight" && ed.sel != null && ed.sel < ed.total) select(ed.sel + 1, { byUser: true });
});
$("#canvas").addEventListener("click", (e) => {
  if (e.target.id === "canvas-img") { $("#lb-img").src = e.target.src; $("#lightbox").hidden = false; }
});
$("#pr-feed").addEventListener("click", (e) => {
  if (e.target.classList.contains("pr-thumb") || e.target.closest(".pr-history-shot")) {
    const image = e.target.closest(".pr-history-shot")?.querySelector("img") || e.target;
    $("#lb-img").src = image.src; $("#lightbox").hidden = false;
  }
});
$("#pr-page-history").addEventListener("click", (e) => {
  const shot = e.target.closest(".pr-history-shot");
  if (!shot) return;
  const image = shot.querySelector("img");
  if (image) { $("#lb-img").src = image.src; $("#lightbox").hidden = false; }
});
$("#pp-toggle").addEventListener("click", () => {
  ed.ppCollapsed = !ed.ppCollapsed;
  $("#plan-panel").classList.toggle("collapsed", ed.ppCollapsed);
  $("#pp-toggle").textContent = ed.ppCollapsed ? "展开" : "收起";
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeHistoryContextMenu();
    closeHistoryRenameDialog();
    closeExportMenu();
    $("#lightbox").hidden = true;
    closeAttachmentViewer();
    closeOverviewModal();
    closeBatchModal();
    setAuthGate(false);
  }
});
$("#lightbox").addEventListener("click", () => { $("#lightbox").hidden = true; });
$("#attachment-viewer-close").addEventListener("click", closeAttachmentViewer);
$("#attachment-viewer").addEventListener("click", (event) => {
  if (event.target === $("#attachment-viewer")) closeAttachmentViewer();
});
document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-attachment-preview]");
  if (!trigger || trigger.disabled) return;
  const url = trigger.dataset.attachmentUrl || "";
  const fallbackUrl = trigger.dataset.attachmentFallbackUrl || "";
  openAttachmentViewer({
    name: trigger.dataset.attachmentName || "附件",
    type: trigger.dataset.attachmentType || "",
    size: Number(trigger.dataset.attachmentSize) || 0,
    url,
    fallbackUrl,
    previewUrl: trigger.dataset.attachmentPreviewUrl || "",
    downloadUrl: `${url}${url.includes("?") ? "&" : "?"}download=1`,
  });
});
document.addEventListener("error", (event) => {
  const image = event.target?.closest?.("img[data-attachment-image-fallback]");
  if (!image) return;
  const fallback = image.dataset.attachmentImageFallback || "";
  delete image.dataset.attachmentImageFallback;
  if (fallback && image.src !== new URL(fallback, location.origin).href) image.src = fallback;
}, true);
$("#overview-close").addEventListener("click", closeOverviewModal);
$("#overview-modal").addEventListener("click", (e) => {
  if (e.target.id === "overview-modal") closeOverviewModal();
});
$("#overview-grid").addEventListener("click", (e) => {
  const card = e.target.closest(".overview-card");
  if (!card || card.disabled) return;
  closeOverviewModal();
  ed.workspaceView = "ppt";
  ed.viewMode = "ppt";
  localStorage.setItem("studio_page_view", ed.viewMode);
  syncViewToggle();
  select(+card.dataset.n, { byUser: true });
});
document.addEventListener("click", (event) => {
  if (!event.target.closest("#history-context-menu")) closeHistoryContextMenu();
  if (!event.target.closest(".export-menu-wrap")) closeExportMenu();
  const button = event.target.closest(".outline-query-copy");
  if (button) copyOutlineQuery(button);
});
const compactWorkspaceMedia = matchMedia("(max-width: 1180px)");
const procRailDrawerMedia = matchMedia("(max-width: 1799px)");
const syncResponsiveSidebar = (media = compactWorkspaceMedia) => {
  const collapsed = media.matches || localStorage.getItem("studio_sidebar_collapsed") === "1";
  setSidebarCollapsed(collapsed, { remember: false });
};
const syncResponsiveProcRail = (media = procRailDrawerMedia) => {
  setProcRailOpen(false);
  const collapsed = !media.matches && localStorage.getItem("studio_proc_rail_collapsed") === "1";
  setProcRailCollapsed(collapsed, { remember: false });
};
syncResponsiveSidebar();
syncResponsiveProcRail();
if (compactWorkspaceMedia.addEventListener) compactWorkspaceMedia.addEventListener("change", syncResponsiveSidebar);
else compactWorkspaceMedia.addListener(syncResponsiveSidebar);
if (procRailDrawerMedia.addEventListener) procRailDrawerMedia.addEventListener("change", syncResponsiveProcRail);
else procRailDrawerMedia.addListener(syncResponsiveProcRail);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !compactWorkspaceMedia.matches) return;
  setProcRailOpen(false);
  if (!document.querySelector(".layout")?.classList.contains("sidebar-collapsed")) {
    setSidebarCollapsed(true, { remember: false });
  }
});
setTheme(document.documentElement.dataset.theme || "dark", { remember: false });
setLanguage(currentLanguage, { remember: false });
setTopbar();
const initialParams = new URLSearchParams(location.search);
if (initialParams.get("mode") === "dynamic") setCreationMode("dynamic", { remember: false });
if (["login", "register"].includes(initialParams.get("auth"))) {
  setAuthGate(true, { tab: initialParams.get("auth") });
}
loadDecks().then(openDeckFromLocationHash);
}

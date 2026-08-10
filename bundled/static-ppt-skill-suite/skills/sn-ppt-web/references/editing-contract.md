# 现有 PPT 编辑合同

本文件只在用户要求修改、续编、重排或统一现有 deck 时读取。目标是先限定影响范围，再选择 Review 快修或 Orchestrator 多 Agent 改造。

## 阅读顺序

1. §1 只读盘点；
2. §2 判定简单或复杂；
3. 只进入命中的 §3 或 §4；
4. §5 统一验收。

## 1. 只读盘点

先检查：

- 用户原始修改要求；
- `plan/design-brief.md`、`plan/deck.md` 和相关 `plan/slide_NN.md`；
- `base.css`；
- `slides/`、`renders/`、`assets/`；
- `speech.md`、`present.html`；
- 最新 overview / contact sheet。

输出一份影响清单：

```text
requested_changes:
affected_facts:
affected_narrative:
affected_pages:
affected_global_style:
new_evidence_needed:
new_assets_needed:
speech_impact:
render_scope:
protected_pages_and_assets:
```

盘点阶段只读，不修改文件。

## 2. 路径判定

### Review 快修

同时满足以下条件才使用：

- 核心论点、页序、页面职责和跨页叙事不变；
- 事实已有可靠依据，不需要新 Research/Material；
- 不需要新图片或新视觉资产；
- 不改变 Style Lock、全局字体系统或多个版式原型；
- 修改可在少量明确页面内安全完成。

典型任务：错字、局部文案替换、单页排版、图片裁切、字号/对比、局部数据修正、讲稿同步、少量页面视觉统一。

### Orchestrator 改造

命中任一条件即使用：

- 增删页、换顺序、重做章节或改变核心论点；
- 修改影响多个页面职责或跨页衔接；
- 需要新事实、新附件解析、新图片或重新生成资产；
- 改变 Style Lock、调色板、字体、页面骨架或全局 token；
- 需要把旧页面改成新的页面类型或重新分配内容；
- 无法可靠判断哪些既有成果仍有效。

页数不是唯一阈值：一页的叙事级改动也属于复杂编辑，多页的同一机械替换仍可能是简单编辑。

## 3. Review 快修合同

goal 必须包含：

```text
mode: simple_edit
Raw user request: <原文>
Target pages: <页码>
Required changes: <逐项>
Must preserve: <事实、页面职责、Style Lock、未受影响页面>
Render scope: <页码>
```

Review 按以下顺序执行：

1. 看 overview 和目标页最新 PNG；
2. 一次列完问题/修改账本；
3. 备份目标页，集中修改 HTML 和受影响计划；
4. 若改屏显字符或字体 token，运行 `deck.py prepare . --expected <总页数>` 同步讲稿并准备字体；
5. 用 `render.py --batch . --pages ...` 一次重渲；全局 token 变化说明误判为简单编辑，应停止并返回 blocked；
6. 运行 `deck.py contact . --focus ...` 并看 focus 联系表；
7. 同步讲稿并重建播放器。

Review 不扩写事实、不创建新素材、不重排全册叙事、不修改未列入影响范围的页面。

## 4. Orchestrator 改造合同

1. 把影响清单转成任务图；独立任务并行，存在前置依赖的任务分波执行。
2. 只委派实际需要的角色：
   - 事实缺口 → 唯一 Research；
   - 新附件 → 多 Material 分片；
   - 新位图 → 多 Image；
   - 页面重做 → 受影响页各一个 Slide。
3. 先更新 `grounded-knowledge.md`、Style Lock、deck/slide plan，再启动下游生产。
4. 复用未失效的 HTML、素材、讲稿和渲染图；禁止“为了省判断”从头覆盖全册。
5. 局部变化只重渲变化页；若改变 bookends/dividers 或内容组的跨页关系，重做受影响的完整页组；base.css、字体或全局骨架变化才全册 batch。
6. 最后由唯一 Review 看全册一致性、集中修复并收口讲稿。

## 5. 编辑验收

- 用户要求逐项对应到实际修改；
- 影响清单之外的文件没有无理由变化；
- 所有变化页都已重渲并看最终像素；
- 全局变化后的全册页面均新于 base.css；
- `speech.md`、字体 bundle 和 `present.html` 与最终页面一致；
- blocked 项如实保留，不用额外 Review 掩盖。

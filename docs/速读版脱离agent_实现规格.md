# 速读版脱离 Codex agent · 实现规格（混合脱离）

> 目标：把速读版从"一整段自然语言指令交给 Codex agent 自由执行"，重构成
> **确定性代码管线 + 一次结构化模型调用**。治"假快"的架构根因。
>
> 取舍（已与产品方对齐）：**检索保留轻量化**（agent/搜索引擎只返回候选列表 JSON），
> 因为小宇宙搜索 API 需登录鉴权、是逆向接口、不适合开源展示。
> **选片 / 抓简介 / 跨集比对 / 填模板 全部确定性代码化。** 拿到脱离的 80% 提速且合规可开源。

---

## 1. 现状（为什么慢）

`server.py` `production_mode=="quick"`：
```
build_prompt(job) → 一大段自然语言指令
   → subprocess.Popen(Codex agent)
   → agent 自由完成：检索小宇宙 → 选片 → 写跨集比对 → 写整篇长文 → 调 pipeline 生成 HTML
```
慢的根因：每一步都是 agent 开放式推理 + **长文写作**。`pipeline.py` 本身**没有检索能力**，
只有 `fetch`（按已知 URL 抓单集页）、转写、`build-html`。检索完全靠 agent 联网。

---

## 2. 目标架构（混合脱离）

```
[输入：问题]
  │
  ├─① 检索候选（保留轻量 agent / 搜索引擎）→ 输出 candidates.json   ← 唯一联网+不确定步骤
  │     [{title, url, episode_id, podcast, date, play_count?}]
  │
  ├─② 选片（确定性代码）→ 按规则打分排序，选 N 期 → episodes.txt + 候选单集.md
  │     规则：主题相关 + 新鲜度 + 热度 + 同节目≤2 期；可解释（每期记录入选/剔除理由）
  │
  ├─③ 抓简介（确定性代码，复用 pipeline.fetch 思路）→ 各期 show notes / 简介 / 时间轴
  │     并查 transcript 缓存：命中则该期标"听稿依据"
  │
  ├─④ 跨集比对（一次结构化模型调用）→ claim/subtopic JSON → 观点原子.md + 跨集比对.md
  │     这是唯一真正需要"智能"的一步。输入各期简介+缓存，输出结构化比对。
  │
  └─⑤ 生成首屏（确定性代码）→ pipeline.build-html 填观点地图 + 矩阵 + 折叠
        速读版不写长文。长文是精读版后台才写的。
```

对比现状：①仍联网但只返回 JSON（不写文章）；②③⑤纯代码；④从"写整篇"瘦成"一次结构化调用"。

---

## 3. 链接模式优先（最快验证，无需检索）

链接模式（用户贴链接）**不需要步骤①检索**，是验证整套确定性管线的最佳起点：
```
用户贴链接 → ②跳过(已有url) → ③抓简介+查缓存 → ④比对 → ⑤生成
```
建议：**先把链接模式跑通确定性管线**，再回头接问题模式的轻量检索①。

---

## 4. 跨集比对的模型调用（步骤④）

**用什么调**：复用现有 Codex/模型配置，但**改成"一次性结构化调用"而非跑完整 agent**：
- 不再 `codex exec` 跑开放式任务；而是给一个**固定 prompt + 固定输入（各期简介/缓存）+ 要求输出 JSON**。
- 可以仍走 Codex（`codex exec` 带一个"只输出 JSON、不准做别的"的强约束 prompt），
  或直接调模型 API。先用 Codex 最省事（已配置），跑通后若想更快可换直连 API。

**输出**：claim/subtopic JSON（见《跨集比对_产物与实现设计》四段流水线的数据结构）。
落地为 `观点原子.md`（claim）和 `跨集比对.md`（subtopic 矩阵），与现有 build_viewpoint_map / build_html 兼容。

**可信度铁律**（沿用第 6 节）：缓存命中的期=听稿依据，仅简介的=简介依据；
比对里不把简介推断写成听稿确认。

---

## 5. 新增 / 改动模块

### server.py
- 新增 `run_quick_pipeline(job)`：确定性编排①→⑤，替换 quick 分支的 `codex exec`。
  - 步骤①检索：抽成 `retrieve_candidates(request) -> list[dict]`，内部仍可调轻量 codex/搜索，
    但**强约束只返回 JSON**。失败可降级（让用户贴链接）。
  - 步骤②选片：`select_episodes(candidates, count, recency) -> chosen`（纯函数，可单测）。
  - 步骤④比对：`run_comparison(project) -> None`（一次模型调用，写 md）。
- `run_job`：quick 走 `run_quick_pipeline`；deep / deep_one 维持现在的 agent 路径（暂不动）。

### pipeline.py
- 已有 `fetch` / `build-html` 可直接复用。
- 步骤③可能要新增一个"只抓简介、不下载音频"的子命令或函数（若 fetch 现在会下载音频，需拆分）。

### 不动
- deep / deep_one（精读、补全文）继续走 agent——它们本来就慢、本就该后台，不在本次范围。
- 金句图 / 书内搜索 / 书架 等前端能力不变。

---

## 6. 风险与回退

1. **检索质量**：确定性选片依赖①返回的候选质量。①仍是 agent/搜索，可能不稳→保留"链接模式"
   和"检索失败提示贴链接"作为回退。
2. **比对一次调用 vs agent**：一次结构化调用可能不如 agent 反复推理细致→先接受"速读版比对略粗"，
   精读版再用 agent 精修。符合"先快后全"。
3. **改坏现有可用流程**：quick 是当前主力。**务必保留开关**：
   加一个 `QUICK_ENGINE = "pipeline" | "agent"` 配置，出问题一键切回旧 agent 路径。
4. **沙箱测不了 Codex**：本机才有 Codex。确定性部分（选片/解析/填模板）可单测；
   含模型调用的步骤需在你本机验证。

---

## 7. 分阶段落地（建议顺序）

**阶段 1 · 确定性管线骨架（链接模式，不碰检索）**
- `select_episodes` 纯函数 + 单测
- 步骤③抓简介+查缓存（复用 pipeline）
- 步骤⑤直接 build-html（先不做比对，用简介拼最简矩阵）
- 加 `QUICK_ENGINE` 开关，默认仍 agent，新管线灰度

**阶段 2 · 接入结构化跨集比对（步骤④）**
- `run_comparison` 一次模型调用输出 claim/subtopic JSON → md
- 接上 build_viewpoint_map / 矩阵

**阶段 3 · 问题模式轻量检索（步骤①）**
- `retrieve_candidates` 强约束 JSON 输出
- 选片可解释（入选/剔除理由）

**阶段 4 · 切换默认 + 观察**
- `QUICK_ENGINE="pipeline"` 设为默认，旧 agent 路径保留兜底

每阶段都能独立验证、可回退。阶段 1 跑通就能证明"确定性速读版"成立。

---

## 8. 一句话给 codex
> 把 quick 从 `codex exec` 一段自然语言指令，重构成 server.py 里 `run_quick_pipeline` 的确定性编排：
> 检索①保留轻量 JSON 输出，选片②/抓简介③/填模板⑤纯代码，跨集比对④改成一次结构化调用。
> 加 `QUICK_ENGINE` 开关随时切回旧路径。先做链接模式 + 阶段 1，跑通再往下。

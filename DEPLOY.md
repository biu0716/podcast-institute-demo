# 播客研究所线上部署说明

这是“播客研究所”的线上部署准备版。

## 当前适合上线的范围：面试展示版

- 可以打开首页；
- 可以打开网页书架；
- 可以浏览已经生成的 HTML 专题；
- 可以公开演示“粘贴小宇宙链接 → 生成速读专题 → 保存到网页书架”的闭环；
- 默认不开放问题检索、文稿导入、精读 ASR 和密钥配置，避免消耗额度或暴露内部能力；
- 如需私密演示，可以用 Basic Auth 给整个站点加访问密码。

需要注意：完整本地版依赖 Codex 桌面应用和本机 skill 目录来生成深度专题。普通云服务器没有 `/Applications/Codex.app`。所以面试展示版默认使用内置的 deterministic pipeline，只开放链接模式速读版。

这一版适合面试展示：

1. 面试官能打开公开链接；
2. 能看到产品首页、书架和阅读页；
3. 能用 3–8 个小宇宙链接生成一份速读专题；
4. 不会消耗你的云端 ASR 额度；
5. 不需要在线配置密钥。

后续如果要做真正多人平台，需要把“生成任务”拆成独立 worker，并增加用户隔离、队列、额度控制、持久化存储和登录系统。

## 环境变量

必填或常用：

```bash
PODCAST_ASSISTANT_HOST=0.0.0.0
PORT=8765
PODCAST_ASSISTANT_ROOT=/app/data
PODCAST_LIBRARY_DIR=/app/data/网页书架
PODCAST_JOBS_DIR=/app/jobs
PODCAST_DEMO_MODE=1
PODCAST_QUICK_ENGINE=pipeline
PODCAST_COMPARISON_ENGINE=deterministic
PODCAST_DISABLE_UV=1
```

访问保护，强烈建议公开部署时设置：

```bash
PODCAST_AUTH_USER=podcast
PODCAST_AUTH_PASSWORD=换成一个强密码
```

ASR 密钥，完整私有版按需设置；公开 Demo 不建议设置：

```bash
GROQ_API_KEY=你的_Groq_Key
TENCENT_SECRET_ID=你的腾讯云SecretId
TENCENT_SECRET_KEY=你的腾讯云SecretKey
```

如果你在云服务器上安装了 Codex CLI / worker，可以额外设置：

```bash
CODEX_BIN=/path/to/codex
CODEX_SKILL_DIR=/path/to/skills
```

## Docker 本地试跑

```bash
cd /path/to/专题研究助手
docker build -t podcast-institute .
docker run --rm -p 8765:8765 \
  -e PODCAST_AUTH_PASSWORD=demo-password \
  podcast-institute
```

打开：

```text
http://127.0.0.1:8765/
```

## Procfile 平台

支持读取平台注入的 `PORT`。如果平台使用 Procfile，启动命令是：

```bash
PODCAST_ASSISTANT_HOST=0.0.0.0 python server.py
```

## Render 一键部署思路

项目已经包含 `render.yaml`。最省事的方式：

1. 把 `专题研究助手/` 单独推到一个 GitHub 仓库；
2. 打开 Render，New → Blueprint；
3. 选择这个仓库；
4. Render 会读取 `render.yaml`；
5. 部署完成后获得一个公开 URL。

默认是公开 Demo 版：

- 只开放链接模式；
- 只生成速读版；
- 不开放 ASR 设置；
- 不开放精读升级；
- 不需要 Codex CLI。

注意：Render 免费 Web Service 的本地文件更适合演示，不适合长期保存用户数据。重启或重新部署后，`data/` 里的任务和生成页可能丢失。面试展示可以接受；真正多人使用时，建议把专题 HTML 存到对象存储，任务记录存到数据库。

如果想加访问密码，在 Render 的 Environment 里添加：

```bash
PODCAST_AUTH_USER=podcast
PODCAST_AUTH_PASSWORD=你的演示密码
```

## 重要安全提醒

如果公开给别人使用，不要直接暴露无密码的生成入口。否则别人可能消耗你的 Groq / 腾讯云 / 模型额度。

建议上线节奏：

1. 第一阶段：带密码的个人线上版；
2. 第二阶段：只开放书架分享页；
3. 第三阶段：多人生成，增加账号、队列、限额、日志和支付/额度控制。

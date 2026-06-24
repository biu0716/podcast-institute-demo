# 播客研究所 · Podcast Institute Demo

把多期播客整理成可阅读、可检索、可分享的专题研究网页。

这是面试展示用的公开 Demo 版本，保留稳定的链接生成路径：

- 粘贴小宇宙单集链接；
- 生成速读版专题；
- 自动保存到网页书架；
- 输出单文件 HTML 专题页。

为控制成本和滥用风险，公开 Demo 默认关闭：

- 云端 ASR / 本地 ASR；
- 精读升级；
- 密钥配置；
- 剪贴板监听；
- 问题模式和文稿模式。

## 本地运行

```bash
pip install -r requirements.txt
PODCAST_DEMO_MODE=1 \
PODCAST_QUICK_ENGINE=pipeline \
PODCAST_COMPARISON_ENGINE=deterministic \
python server.py
```

打开：

```text
http://127.0.0.1:8765/
```

## Render 部署

仓库已包含 `render.yaml`，可在 Render 中选择 Blueprint 部署。

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/biu0716/podcast-institute-demo)

推荐环境变量：

```text
PODCAST_DEMO_MODE=1
PODCAST_QUICK_ENGINE=pipeline
PODCAST_COMPARISON_ENGINE=deterministic
PODCAST_DISABLE_UV=1
PODCAST_ASSISTANT_HOST=0.0.0.0
```

## 作品集项目页

项目介绍页已发布到个人网站：

https://biu0716.github.io/portfolio/podcast-institute.html

线上 Demo：

https://podcast-institute-demo.onrender.com

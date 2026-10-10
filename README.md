# OpenClaw Xiaohongshu integrations

A source-only snapshot of integration utilities used with OpenClaw and Xiaohongshu workflows. It is not the complete upstream OpenClaw platform or the Xiaohongshu skills package.

## Included work

- `health_supervisor.py`: container/service health checks and escalation logic.
- `error_supervisor.py`: event-driven error supervision.
- `text_card_generator.py`: multi-page text card rendering.
- `image_generator.py`: DashScope image-generation adapter.

Technologies: Python, Docker-based operations, HTTP health checks, Pillow and DashScope. Install the dependencies with `pip install -r requirements.txt`. The supervisors require a separately configured deployment, private runtime configuration and operator-reviewed escalation settings. They can restart services or invoke a repair agent: do not run them against a production deployment without reviewing those actions.

## Attribution and scope

This is integration/extension work, not a claim of sole authorship of upstream social tooling. Related upstream project: [autoclaw-cc/xiaohongshu-skills](https://github.com/autoclaw-cc/xiaohongshu-skills). Its MIT notice is retained in `third-party/xiaohongshu-skills.LICENSE`; upstream software is not bundled wholesale.

Login profiles, QR codes, tokens, Telegram configuration, account data, scheduled jobs and deployment logs are excluded. Automated publishing or interaction requires operator authorization and compliance with platform rules. Only static source checks were performed for this snapshot; live-account operations were not tested.

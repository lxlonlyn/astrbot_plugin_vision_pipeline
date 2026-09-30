# Git repository setup

This plugin directory is repository-ready.

Recommended repository name: `astrbot_plugin_vision_pipeline`.

```bash
git init
git add .
git commit -m "Vision Pipeline v0.7.0"
git branch -M main
git remote add origin <YOUR_REPOSITORY_URL>
git push -u origin main
```

For later revisions, keep `pipeline.yaml` as the architecture contract and update `CHANGELOG.md` for behavioral changes. Do not commit runtime API keys or AstrBot config files.

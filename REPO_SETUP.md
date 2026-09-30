# Git repository

Canonical repository:

`https://github.com/lxlonlyn/astrbot_plugin_vision_pipeline`

The plugin is kept repo-ready. `pipeline.yaml` is the compact architecture contract and `CHANGELOG.md` records behavior changes.

Recommended local workflow:

```bash
git clone https://github.com/lxlonlyn/astrbot_plugin_vision_pipeline.git
cd astrbot_plugin_vision_pipeline
# edit + validate
git add .
git commit -m "Describe the change"
git push
```

Do not commit AstrBot runtime config, API keys, caches, downloaded web reference images, or `data/plugin_data` state.

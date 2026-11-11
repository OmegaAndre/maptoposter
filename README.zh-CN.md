# 城市地图海报生成器

[English](README.md)

这个 [MapToPoster](https://github.com/originalankur/maptoposter) Fork 支持直接用本地 OpenStreetMap 文件生成海报，也提供 Tkinter 桌面界面。样例海报见[原项目](https://github.com/originalankur/maptoposter#examples)。

## 安装

安装 [uv](https://docs.astral.sh/uv/) 后，在仓库目录运行：

```bash
uv sync --locked
uv run python -m tkinter
```

如果最后一条命令弹出测试窗口，当前 Python 就可以运行桌面版。如果自定义镜像导致锁文件校验失败，可改用 `uv sync --locked --default-index https://pypi.org/simple`。

## 使用桌面版

```bash
uv run python poster_gui.py
```

1. 下载所需地区的 OSM 数据，例如 [Geofabrik 浙江数据](https://download.geofabrik.de/asia/china/zhejiang.html)。把 `.osm` 或 `.pbf` 文件放进项目的 `osm/` 目录，或在界面点击「浏览」选取。
2. 在城市栏输入部分名称并选择匹配项，也可以手动填写。经纬度留空时使用本地城市坐标，手动指定中心时要同时填两项。国家名用于海报标签。
3. 选择主题和系统字体。中文可选系统中已安装的中文字体，例如 `Microsoft YaHei`。地图范围以千米输入，画面短边约覆盖输入值的一半。
4. 宽高可用像素或英寸设置。PNG 按 300 DPI 换算，每边支持 300 至 12,000 像素。可以分别移动城市中心和标题，按需设置是否显示经纬度文字和 OpenStreetMap 署名。
5. 点击「生成预览」，满意后点「保存 PNG」。预览长边最多 900 像素，导出会按最近一次预览的设置渲染完整尺寸。

## 使用命令行

离线生成海报：

```bash
uv run python create_map_poster.py --osm-file osm/zhejiang-latest.osm.pbf --city Hangzhou --country China --distance 30000
```

命令行的 `--distance` 以米为单位。可以同时传入 `--latitude` 和 `--longitude` 来指定中心。使用本地文件时，`--font-family` 应填写已安装的系统字体。

联网生成海报：

```bash
uv run python create_map_poster.py --city Paris --country France
```

运行 `uv run python create_map_poster.py --help` 查看完整参数，或参阅[英文 README](README.md)。

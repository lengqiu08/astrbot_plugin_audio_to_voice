# 深海电台 · 小深音乐台（重置版）

一个群里的点歌台：搜歌 → 出歌单 → 回编号 → 出语音条或原轨。
顺手带本地曲库、收歌入库、音频↔语音条双向转换。

- 插件目录：`data/plugins/astrbot_plugin_audio_to_voice/`
- 内部注册名：`astrbot_plugin_audio_to_voice`（沿用旧名，因为 AstrBot 的 `plugin_set` 白名单按内部名匹配）
- 面板显示名：深海电台
- 版本：1.1.0

---

## 一、指令

**不需要 @ 机器人，也不需要唤醒前缀**，直接发就行（插件自己看文本开头）。

| 指令 | 作用 | 例子 |
| --- | --- | --- |
| `点歌 <关键词>` / `来一首 <关键词>` | 搜索并列出歌单 | `点歌 倔强`、`点歌 五月天 倔强` |
| `点歌 BV号` | 直接认这条 B站视频，跳过搜索直接发 | `点歌 BV1a8411o71x` |
| `点歌 网易云链接/歌曲id` | 直接认这首网易云歌 | `点歌 https://music.163.com/song?id=386175` |
| 回数字 | 选第 N 首，默认发语音条 | `1` |
| `N 文件` | 选第 N 首，直接发原轨 | `3 文件` |
| `上` / `下` | 翻页 | `下` |
| `取消` | 退出当前点歌 | `取消` |
| `歌单` / `曲库` | 列出本地曲库 | `歌单` |
| `收歌` / `入库` | **回复**一条语音/音频/文件，把它存进曲库 | 回复音频 → `收歌` |
| `转语音` | **回复**一条音频/文件，转成语音条 | 回复 mp3 → `转语音` |
| `转音频` | **回复**一条语音/音频，转成 mp3 文件 | 回复语音条 → `转音频` |
| `电台自检` | 打印链路状态（曲库/源/B站连通性/依赖） | `电台自检` |
| `点歌榜` | 这台电台上被点最多的歌 TOP10 | `点歌榜` |
| `我的点歌` | 自己点过几首 | `我的点歌` |
| `登录状态` / `B站状态` | 查 B站 cookie 是否有效、是否大会员、网易云 cookie 有没有 | `登录状态` |
| `扫码登录` / `B站登录` | 发二维码，扫完自动存 cookie（90 秒） | `扫码登录` |

说明：

- 默认一次取 27 首、每页 9 首，正好三页；B站一页 20 条，插件会按需多翻一页。
- 歌单会话 **10 分钟**有效，超时提示过期；同一会话里不同人互不干扰。
- 点歌成功后有 **60 秒冷却**（可配），防止刷屏。
- 在线点的歌默认**自动存进曲库**，下次直接本地发。

---

## 二、歌单长什么样

发 `点歌 关键词` 会先来一张**九宫格封面图**（一页 9 首，左上角编号），再跟文字歌单：

```
《倔强》搜到 27 首，第 1/3 页：
1. 歌名 - 歌手（4:21）　[曲库]
2. 歌名 - 歌手（4:25）　[B站]
...
回编号选歌｜「编号 文件」发原轨｜「下」翻页｜「取消」退出
```

- 封面来源：B站视频封面 / 网易云专辑图 / 本地同名图片，再不行抽内嵌封面，最后给一张带编号的占位卡。
- 默认 `cover_style=inline`：一整页文字与封面拼成**一条消息**，1 歌名 / 图 / 2 歌名 / 图……
- 想只看长图就切 `poster`：封面+编号+歌名+歌手+时长全画进一张图。
- 插件自带文泉驿中文字体（`assets/fonts/wqy-zenhei.ttc`），所以手机/服务器没有中文字体也能把歌名画进图里。

- `cover_style=inline` 时的样子：

```
《倔强》搜到 27 首，第 1/3 页：
1. 【小连杀】兔子音翻唱《倔强》 - 米琪小连杀（4:21）　[曲库]
   └ [封面图]
2. ...
回编号选歌｜「编号 文件」发原轨｜「下」翻页｜「取消」退出
```
- 三源**并发**查，同名同歌手会去重（本地优先）。
- 本地曲库文件名按「歌名 - 歌手」自动拆出歌手；**支持拼音和首字母**（`点歌 juejiang`、`点歌 jj`）。
- `点歌` 后面直接给 BV 号、网易云链接或纯数字 id，就不搜了，认了直接发。

## 三、搜索源

在配置的 `sources` 里勾选，可多选：

| 源 | 说明 | 出声需要什么 |
| --- | --- | --- |
| `local` | 本地曲库（`library_dirs`） | 无 |
| `bilibili` | B站**音乐区**（带 `tids=3`，不会糊你一脸视频） | 无，免登录发 AAC；填 `bilibili_cookie` 可拿大会员 FLAC |
| `netease` | 网易云 | 必须填 `netease_cookie`（`MUSIC_U=...`），否则只能搜不能响 |

> 周杰伦一类版权下架的，网易云只有翻唱；B站能找到原曲视频。

---

## 四、控制台（WebUI）

入口：WebUI → 插件 → 深海电台 → **控制台**（页面 `pages/settings`）。

四个页签：

| 页签 | 内容 |
| --- | --- |
| 状态 | B站登录/账号/会员、曲库首数与体积、缓存体积、语音上限、当前会话与冷却数 |
| 曲库 | 曲库文件列表（名称/时长/体积/修改时间），支持搜索与分页，可勾选"量时长" |
| 维护 | 一键清理：封面/拼图缓存 + `/tmp` 里的临时音频；看日志尾巴 |
| 设置 | 改常用配置（曲库目录、翻页条数、冷却、封面开关等），保存即写回配置文件 |

后端接口（面板代理，无需自己拼）：

```
GET  /api/v1/plugins/extensions/astrbot_plugin_audio_to_voice/console/state
GET  /api/v1/plugins/extensions/astrbot_plugin_audio_to_voice/console/library?q=&page=&size=&probe=
POST /api/v1/plugins/extensions/astrbot_plugin_audio_to_voice/console/cleanup
GET  /api/v1/plugins/extensions/astrbot_plugin_audio_to_voice/console/log?lines=80
POST /api/v1/plugins/extensions/astrbot_plugin_audio_to_voice/console/save
```

## 五、配置项（WebUI → 插件 → 深海电台）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | true | 总开关 |
| `sources` | `["local","bilibili"]` | 搜索源 |
| `max_results` | 27 | 一次最多列几首（1-50，默认 27 = 三页 × 9） |
| `show_covers` | true | 歌单配封面总开关 |
| `cover_style` | inline | `inline`=每首歌后跟自己的封面、整页拼成一条消息（默认）；`poster`=整页一张图文海报；`grid`=只拼封面；`off` 不配图 |
| `show_quality` | false | 歌单标音质（无损/320k），代价是每页多探 2 秒 |
| `auto_clean_hours` | 6 | 自动清理间隔（小时）：临时音频、封面缓存、过期歌单；0 关闭 |
| `cache_max_files` | 200 | 封面缓存上限，超了按时间删最旧 |
| `page_size` | 9 | 每页几首 |
| `library_dirs` | `/root/AstrBot/data/songs` | 曲库目录，英文分号分隔；第一个也是收歌落盘处 |
| `mode` | auto | `auto` 短的语音条、长的文件；`voice` 尽量语音条；`file` 一律原轨 |
| `voice_max_seconds` | 900 | 协议端单条语音上限；**0 = 不切，整首一条** |
| `max_segments` | 6 | 切段超过这个数就改发文件 |
| `codec` | wav | `wav` 交给适配器编 silk（推荐）；`silk` 插件自己编（需 pilk） |
| `platform_mode` | auto | `auto`/`official`/`napcat`，平台判定 |
| `reply_hint` | true | 发送前加一句《歌名》，关掉更干净 |
| `save_downloaded` | true | 点过的在线歌自动入库 |
| `cooldown` | 30 | 点歌冷却秒数，0 关闭 |
| `session_ttl` | 600 | 歌单有效期 |
| `audio_bitrate` | 192k | 收歌 / 转音频的 mp3 码率 |
| `collect_name` | 空 | 收歌默认名，留空用 `收歌_月日_时分` |
| `bilibili_cookie` | 空 | 只填 `SESSDATA=...` 即可，用来拿 FLAC；也可以发「扫码登录」自动填 |
| `bili_music_only` | true | B站只搜音乐区 |
| `title_chars` | 26 | B站标题截断长度，0 不截 |
| `netease_cookie` | 空 | 网易云出声必填，整条 Cookie 至少带 `MUSIC_U=` |

配置文件落盘：`data/config/astrbot_plugin_audio_to_voice_config.json`

---

## 六、日志与排错

### 日志在哪

- 主日志：`/root/AstrBot/data/logs/astrbot.log`
- WebUI 里也有「日志」页面，可直接搜 `DeepseaRadio`

### 关心的关键词

| 关键词 | 含义 |
| --- | --- |
| `Loading plugin astrbot_plugin_audio_to_voice` | 插件被加载（后面括号里的描述是"小深音乐台重启版"才是新代码） |
| `[DeepseaRadio] 命中「点歌」：点歌 倔强` | 指令被插件接住了（说明链路是通的） |
| `[DeepseaRadio] 点歌 执行失败：...` | 指令进到插件但报错，后面跟 traceback |
| `[DeepseaRadio] B 站 412，换 buvid 重试` | 风控，插件自动换指纹重试，通常无害 |
| `[DeepseaRadio] 切出 xx.xs 超硬限，重切成 xx.xs` | 官方接口 300 秒限制触发的自动重切 |
| `[DeepseaRadio] 顺手入库失败` | 曲库目录不可写，不影响本次发送 |

### 三步排错

1. **发了没反应** → 看日志有没有 `命中`。
   - 没有：插件没被激活。检查 WebUI 插件列表里「深海电台」是否启用、`data/cmd_config.json` 的 `plugin_set` 是否含 `astrbot_plugin_audio_to_voice`。
   - 有：看下一行的 `执行失败` 或异常。
2. **发了提示"没搜到"** → 发 `电台自检`，看 B站那行通不通；检查 `sources` 是否只留了 `local`。
3. **封面图没出来** → 配置里 `show_covers` 是否关着；本地曲库缺封面属正常（放个同名 jpg 就好）。
4. **选号后报"没拿到音源"** → 网易云缺 cookie，或 B站音轨被限制；换一首，或把 `sources` 调成 `["local","bilibili"]`。

### 常用命令

```bash
# 只看这个插件的日志
grep "DeepseaRadio" /root/AstrBot/data/logs/astrbot.log | tail -50

# 确认插件加载的是新代码（描述应为"小深音乐台重启版"）
grep "Plugin astrbot_plugin_audio_to_voice" /root/AstrBot/data/logs/astrbot.log | tail -3

# 确认白名单
python3 -c "import json;print(json.load(open('/root/AstrBot/data/cmd_config.json',encoding='utf-8-sig'))['plugin_set'])"
```

---

## 七、已知限制

- **QQ 官方接口语音硬限 300 秒**：多一毫秒都发不出去，插件自动按 299.5 秒切段连发；超过 `max_segments` 段就改发文件。
- **协议端（NapCat）** 宽松得多，`voice_max_seconds=0` 时整首一条。
- B站 DASH 音轨实际是 `m4a`，入库时按容器改名，不会乱标成 mp3。
- 本地曲库路径在 proot/Android 环境要挂载后才能读。
- 控制台只做常用项，其余配置还是走插件配置页。

---

## 八、回滚

旧版插件备份在 `data/plugins_disabled/astrbot_plugin_audio_to_voice_legacy/`。
要回滚：把当前目录挪出去，把 backup 挪回 `data/plugins/astrbot_plugin_audio_to_voice/`，然后在 WebUI 重载插件。

---

## 九、实验性功能

### B站分享卡片（`bili_card`，默认关）

`B站卡片 BV1xxxxxxxxx` 会发一张 QQ 结构化卡片（`com.tencent.structmsg`）。

注意：实测多数客户端会渲染成**空白消息**——原因是 QQ 的卡片格式由客户端决定，B站不在内置白名单里。插件的 json 段已按 OneBot 规范输出（`data.data` 为字符串），发是能发出去，看不看得见取决于客户端版本。

- 想试：配置开 `bili_card`，或直接发 `B站卡片 <BV号>`
- 不想折腾：保持关闭，B站歌照样有封面、歌名和链接

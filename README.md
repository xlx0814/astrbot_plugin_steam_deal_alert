# Steam 特惠提醒插件

> AstrBot 专用的 Steam 优惠查询 + 降价提醒插件。

## ✨ 功能亮点

- 🔎 查询当前 Steam 特惠（最多 30 条）
- 🔔 按游戏单独订阅折扣提醒
- 📬 达到阈值后自动推送到当前会话（Telegram / 微信）

---

## 📌 指令速查

### 查看特惠

```bash
/steam特惠
```

### 订阅游戏

```bash
/steam订阅 游戏名(有英文名的话优先使用) [折扣阈值]
```

示例：

```bash
/steam订阅 Resident Evil Requiem 20
/steam订阅 幻兽帕鲁 25
```

> 不填阈值时默认 `1`（即有折扣就提醒）

### 查看我的订阅

```bash
/steam我的
```

### 取消订阅

```bash
/steam取消 游戏名或 appid
```

示例：

```bash
/steam取消 Resident Evil Requiem
/steam取消 1623730
```

---

## 💬 消息事件触发（不加斜杠）

你也可以直接发自然文本：

```text
steam特惠
订阅steam Resident Evil Requiem 20
取消steam Resident Evil Requiem
我的steam
```

---

## ⚙️ 配置项

配置文件：

`data/config/astrbot_plugin_steam_deal_alert_config.json`

- `cc`：价格地区（默认 `cn`）
- `lang`：语言（默认 `schinese`）
- `poll_seconds`：轮询间隔秒数（默认 `300`）
- `top_deals_limit`：特惠条数上限（默认 `30`）

---

## 🧠 提醒规则

- 后台定时轮询（默认每 300 秒）
- 折扣达到阈值会提醒
- 同一折扣不会重复狂刷
- 折扣降到阈值以下后，再次升上来会再次提醒
- 订阅/更新阈值后会立刻检查一次（若已满足阈值可即时提醒）

---

## 🗂️ 数据说明

- 数据来源：Steam 官方接口/页面结果
- 插件不会上传订阅数据到第三方
- 本地订阅数据位置：

`data/plugin_data/astrbot_plugin_steam_deal_alert/subscriptions.json`

---

## ❓常见问题

### Q1：我订阅了但没提醒？

先用下面指令确认订阅是否已落库：

```bash
/steam我的
```

### Q2：为什么有时名字显示异常？

插件会优先使用 Steam 返回名；若异常，会回退到你订阅时记录的游戏名。

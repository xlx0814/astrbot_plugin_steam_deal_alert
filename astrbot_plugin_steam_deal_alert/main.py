import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register


@register("astrbot_plugin_steam_deal_alert", "naizhouwang", "Steam 特惠查询与订阅提醒", "0.1.0")
class SteamDealAlertPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.cc = str(config.get("cc", "cn"))
        self.lang = str(config.get("lang", "schinese"))
        self.poll_seconds = int(config.get("poll_seconds", 300))
        self.top_deals_limit = int(config.get("top_deals_limit", 30))

        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_steam_deal_alert"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "subscriptions.json"

        self._db = self._load_db()
        self._task: asyncio.Task | None = None
        self._http: aiohttp.ClientSession | None = None
        self._last_deals_sent_at: dict[str, float] = {}

        self._start_poll_task()

    def _start_poll_task(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._poll_loop())

    async def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._http

    def _load_db(self) -> dict[str, Any]:
        if self.db_path.exists():
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    users = data.get("users")
                    data["users"] = users if isinstance(users, dict) else {}
                    return data
            except Exception as e:
                logger.error(f"[SteamDeal] 读取订阅库失败: {e}")
        return {"users": {}}

    def _save_db(self):
        try:
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(self._db, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[SteamDeal] 写入订阅库失败: {e}")

    def _event_platform_session(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        platform = getattr(event, "adapter_name", "unknown")
        msg_obj = getattr(event, "message_obj", None)
        session_id = ""
        if msg_obj is not None:
            session_id = getattr(msg_obj, "session_id", "") or getattr(msg_obj, "group_id", "") or getattr(msg_obj, "sender_id", "")
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return str(platform), str(session_id), umo

    async def _http_get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        session = await self._get_http()
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with session.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        return data if isinstance(data, dict) else None
                    if resp.status in {429, 500, 502, 503, 504}:
                        await asyncio.sleep(0.6 * (2 ** attempt))
                        continue
                    return None
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.6 * (2 ** attempt))
        if last_err:
            logger.error(f"[SteamDeal] HTTP请求失败: {url} {last_err}")
        return None

    async def _steam_store_search(self, keyword: str) -> list[dict]:
        url = "https://store.steampowered.com/api/storesearch"
        params = {"term": keyword, "l": self.lang, "cc": self.cc}
        data = await self._http_get_json(url, params)
        if not data:
            return []

        items = data.get("items", []) if isinstance(data, dict) else []
        out = []
        for it in items[:10]:
            if not isinstance(it, dict):
                continue
            out.append({
                "id": int(it.get("id", 0) or 0),
                "name": str(it.get("name", "")),
            })
        return [x for x in out if x["id"] > 0 and x["name"]]

    @staticmethod
    def _norm_text(s: str) -> str:
        return re.sub(r"\s+", "", (s or "").strip().lower())

    async def _app_price(self, app_id: int) -> dict[str, Any] | None:
        url = "https://store.steampowered.com/api/appdetails"
        params = {"appids": str(app_id), "cc": self.cc, "l": self.lang, "filters": "price_overview,name"}
        data = await self._http_get_json(url, params)
        if not data:
            return None

        node = data.get(str(app_id), {}) if isinstance(data, dict) else {}
        if not node or not node.get("success"):
            return None
        app = node.get("data", {}) or {}
        po = app.get("price_overview") or {}

        discount = int(po.get("discount_percent", 0) or 0)
        final_price = po.get("final_formatted") or "未知"
        initial_price = po.get("initial_formatted") or "未知"
        name = str(app.get("name") or f"App {app_id}")

        return {
            "app_id": app_id,
            "name": name,
            "discount": discount,
            "final_price": final_price,
            "initial_price": initial_price,
        }

    async def _featured_deals(self) -> list[dict[str, Any]]:
        # 使用官方 featuredcategories，合并多个分组以扩展到 30 条，避免 HTML 正则解析
        url = "https://store.steampowered.com/api/featuredcategories"
        params = {"cc": self.cc, "l": self.lang}
        data = await self._http_get_json(url, params)
        if not data:
            return []

        groups = ["specials", "top_sellers", "new_releases"]
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()

        for g in groups:
            items = ((data.get(g) or {}).get("items") or []) if isinstance(data, dict) else []
            for it in items:
                if not isinstance(it, dict):
                    continue
                app_id = int(it.get("id", 0) or 0)
                if app_id <= 0 or app_id in seen:
                    continue

                discount = int(it.get("discount_percent", 0) or 0)
                if discount <= 0:
                    continue

                rows.append({
                    "app_id": app_id,
                    "name": str(it.get("name") or f"App {app_id}"),
                    "discount": discount,
                    "final_price": str(it.get("final_formatted") or it.get("final_price_formatted") or "未知"),
                    "original_price": str(it.get("original_price_formatted") or it.get("initial_formatted") or "未知"),
                })
                seen.add(app_id)

        rows.sort(key=lambda x: x["discount"], reverse=True)
        return rows[: self.top_deals_limit]

    @staticmethod
    def _cents_to_str(v: int) -> str:
        try:
            return f"¥{v/100:.2f}"
        except Exception:
            return str(v)

    def _ensure_user_slot(self, event: AstrMessageEvent) -> tuple[str, dict[str, Any]]:
        user_id = str(event.get_sender_id())
        users = self._db.setdefault("users", {})
        if user_id not in users:
            platform, session_id, umo = self._event_platform_session(event)
            users[user_id] = {
                "platform": platform,
                "session_id": session_id,
                "umo": umo,
                "watch": [],
            }
        else:
            platform, session_id, umo = self._event_platform_session(event)
            users[user_id]["platform"] = platform
            users[user_id]["session_id"] = session_id
            users[user_id]["umo"] = umo
            users[user_id].setdefault("watch", [])
        return user_id, users[user_id]

    @filter.command("steam特惠")
    async def cmd_steam_deals(self, event: AstrMessageEvent):
        # 防重复发送：同一会话 3 秒内只发一次
        key = str(getattr(event, "unified_msg_origin", "") or event.get_sender_id())
        now_ts = time.time()
        last = self._last_deals_sent_at.get(key, 0)
        if now_ts - last < 3:
            return
        self._last_deals_sent_at[key] = now_ts

        try:
            rows = await self._featured_deals()
            if not rows:
                yield event.plain_result("没拉到特惠数据，晚点再试。")
                return

            lines = ["┏━ 🎮 Steam 特惠 ━"]
            for i, x in enumerate(rows, 1):
                lines.append(
                    f"┃{i:02d}｜{x['name'] or ('App '+str(x['app_id']))}｜-{x['discount']}%｜现价 {x['final_price']}｜原价 {x['original_price']}"
                )
                lines.append("┃")
            lines.append("┗━ 订阅: /steam订阅 游戏名 [折扣阈值]")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"[SteamDeal] 查询特惠失败: {e}")
            yield event.plain_result("查特惠失败了，稍后再试。")

    @filter.command("steam订阅")
    async def cmd_subscribe(self, event: AstrMessageEvent, args: str = ""):
        raw = (args or "").strip()
        if not raw:
            yield event.plain_result("用法：/steam订阅 游戏名 [折扣阈值]\n例：/steam订阅 黑神话 悟空 20")
            return

        m = re.match(r"^(.+?)(?:\s+(\d{1,2}))?$", raw)
        if not m:
            yield event.plain_result("格式不对。用法：/steam订阅 游戏名 [折扣阈值]")
            return

        keyword = m.group(1).strip()
        threshold = int(m.group(2)) if m.group(2) else 1
        threshold = max(1, min(int(threshold), 95))

        try:
            # 支持直接按 appid 订阅（最准确）
            appid_kw = keyword.lower().replace("appid:", "").strip()
            app: dict[str, Any] | None = None
            if appid_kw.isdigit():
                info = await self._app_price(int(appid_kw))
                if info:
                    app = {"id": int(info["app_id"]), "name": str(info["name"])}
                else:
                    yield event.plain_result(f"appid {appid_kw} 无效或当前地区不可用。")
                    return
            else:
                result = await self._steam_store_search(keyword)
                if not result:
                    yield event.plain_result(f"没搜到《{keyword}》，换个关键词试试。")
                    return

                k = self._norm_text(keyword)
                exact = [x for x in result if self._norm_text(str(x.get("name", ""))) == k]
                if len(exact) == 1:
                    app = exact[0]
                else:
                    contains = [x for x in result if k and k in self._norm_text(str(x.get("name", "")))]
                    if len(contains) == 1:
                        app = contains[0]
                    elif len(exact) > 1 or len(contains) > 1 or len(result) > 1:
                        cands = exact if len(exact) > 1 else (contains if len(contains) > 1 else result)
                        lines = ["匹配到多个游戏，请用 appid 精确订阅："]
                        for i, c in enumerate(cands[:5], 1):
                            lines.append(f"{i}. {c['name']}（appid:{c['id']}）")
                        lines.append("示例：/steam订阅 appid:381210 20")
                        yield event.plain_result("\n".join(lines))
                        return
                    else:
                        app = result[0]

            if not app:
                yield event.plain_result("订阅失败：无法确定唯一游戏，请改用 appid。")
                return

            _, slot = self._ensure_user_slot(event)
            watch = slot["watch"]

            for item in watch:
                if int(item.get("app_id", 0)) == app["id"]:
                    item["threshold"] = threshold
                    self._save_db()
                    yield event.plain_result(f"已更新订阅：{app['name']}（appid:{app['id']}），阈值 -{threshold}%")
                    try:
                        await self._check_user_subscription_now(slot, item)
                    except Exception as e:
                        logger.error(f"[SteamDeal] 即时提醒失败 app={app['id']}: {e}")
                    return

            watch.append({
                "app_id": app["id"],
                "name": app["name"],
                "threshold": threshold,
                "last_notified_discount": 0,
            })
            self._save_db()
            yield event.plain_result(f"订阅成功：{app['name']}（appid:{app['id']}），达到 -{threshold}% 及以上就提醒你。")
            try:
                await self._check_user_subscription_now(slot, watch[-1])
            except Exception as e:
                logger.error(f"[SteamDeal] 即时提醒失败 app={app['id']}: {e}")
        except Exception as e:
            logger.error(f"[SteamDeal] 订阅失败: {e}")
            yield event.plain_result("订阅失败，稍后再试。")

    @filter.command("steam取消")
    async def cmd_unsubscribe(self, event: AstrMessageEvent, arg: str = ""):
        arg = (arg or "").strip()
        if not arg:
            yield event.plain_result("用法：/steam取消 游戏名或appid")
            return

        _, slot = self._ensure_user_slot(event)
        before = len(slot["watch"])

        appid = int(arg) if arg.isdigit() else None
        slot["watch"] = [
            x for x in slot["watch"]
            if not (
                (appid is not None and int(x.get("app_id", 0)) == appid)
                or (appid is None and arg.lower() in str(x.get("name", "")).lower())
            )
        ]
        after = len(slot["watch"])
        self._save_db()

        if after < before:
            yield event.plain_result("已取消订阅。")
        else:
            yield event.plain_result("没找到对应订阅项。")

    @filter.command("steam我的")
    async def cmd_my_subscriptions(self, event: AstrMessageEvent):
        _, slot = self._ensure_user_slot(event)
        watch = slot.get("watch", [])
        if not watch:
            yield event.plain_result("你还没订阅任何游戏。\n先用：/steam订阅 游戏名 [折扣阈值]")
            return

        lines = ["📌 你的 Steam 订阅："]
        for i, item in enumerate(watch, 1):
            lines.append(
                f"{i}. {item.get('name')} | appid:{item.get('app_id')} | 阈值:-{item.get('threshold',1)}%"
            )
        yield event.plain_result("\n".join(lines))

    def _resolve_umo(self, slot: dict[str, Any]) -> str:
        umo = str(slot.get("umo", "") or "")
        if umo:
            return umo
        platform = str(slot.get("platform", "Unknown") or "Unknown").capitalize()
        session_id = str(slot.get("session_id", "") or "")
        if not session_id:
            return ""
        return f"{platform}/{session_id}"

    @staticmethod
    def _display_name(info: dict[str, Any], item: dict[str, Any]) -> str:
        name = str(info.get("name") or "").strip()
        if (not name or name.startswith("App ")) and item.get("name"):
            name = str(item.get("name"))
        return name or f"App {int(item.get('app_id', 0) or 0)}"

    def _build_discount_message(self, info: dict[str, Any], item: dict[str, Any], threshold: int, discount: int) -> str:
        display_name = self._display_name(info, item)
        return (
            "┏━ 🎯 Steam 降价提醒 ━\n"
            f"┃{display_name}｜-{discount}%（阈值 -{threshold}%）｜现价 {info['final_price']}｜原价 {info['initial_price']}\n"
            "┗━"
        )

    async def _check_user_subscription_now(self, slot: dict[str, Any], item: dict[str, Any]):
        app_id = int(item.get("app_id", 0) or 0)
        if app_id <= 0:
            return

        info = await self._app_price(app_id)
        if not info:
            return

        discount = int(info.get("discount", 0) or 0)
        threshold = int(item.get("threshold", 1) or 1)
        if discount < threshold:
            return

        umo = self._resolve_umo(slot)
        if not umo:
            return

        msg = self._build_discount_message(info, item, threshold, discount)
        await self.context.send_message(umo, MessageChain([Plain(msg)]))
        item["last_notified_discount"] = discount
        self._save_db()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message_shortcuts(self, event: AstrMessageEvent):
        txt = (event.message_str or "").strip()
        if not txt or txt.startswith("/"):
            return

        if txt in {"steam特惠", "steam优惠", "特惠steam"}:
            async for r in self.cmd_steam_deals(event):
                yield r
            event.stop_event()
            return

        # 关键词消息事件：订阅 steam 黑神话 30
        m = re.match(r"^(?:订阅steam|steam订阅)\s+(.+)$", txt, flags=re.IGNORECASE)
        if m:
            async for r in self.cmd_subscribe(event, m.group(1).strip()):
                yield r
            event.stop_event()
            return

        m2 = re.match(r"^(?:取消steam|steam取消)\s+(.+)$", txt, flags=re.IGNORECASE)
        if m2:
            async for r in self.cmd_unsubscribe(event, m2.group(1).strip()):
                yield r
            event.stop_event()
            return

        if txt in {"我的steam", "steam我的", "steam订阅列表"}:
            async for r in self.cmd_my_subscriptions(event):
                yield r
            event.stop_event()

    async def _poll_loop(self):
        await asyncio.sleep(5)
        while True:
            try:
                await self._check_all_subscriptions_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SteamDeal] 轮询检查失败: {e}")
            await asyncio.sleep(max(60, self.poll_seconds))

    async def _check_all_subscriptions_once(self):
        users = self._db.get("users", {})
        if not isinstance(users, dict) or not users:
            return

        # 去重 appid，避免 N+1 重复请求
        all_app_ids: set[int] = set()
        for slot in users.values():
            watch = slot.get("watch", []) if isinstance(slot, dict) else []
            for item in watch:
                app_id = int(item.get("app_id", 0) or 0)
                if app_id > 0:
                    all_app_ids.add(app_id)

        if not all_app_ids:
            return

        sem = asyncio.Semaphore(5)
        app_info_map: dict[int, dict[str, Any] | None] = {}

        async def fetch_one(app_id: int):
            async with sem:
                try:
                    app_info_map[app_id] = await self._app_price(app_id)
                except Exception as e:
                    logger.error(f"[SteamDeal] 获取价格失败 app={app_id}: {e}")
                    app_info_map[app_id] = None
                await asyncio.sleep(0.05)

        await asyncio.gather(*(fetch_one(aid) for aid in all_app_ids))

        for user_id, slot in users.items():
            watch = slot.get("watch", []) if isinstance(slot, dict) else []
            if not watch:
                continue

            umo = self._resolve_umo(slot)
            if not umo:
                continue

            for item in watch:
                app_id = int(item.get("app_id", 0) or 0)
                if app_id <= 0:
                    continue

                info = app_info_map.get(app_id)
                if not info:
                    continue

                discount = int(info.get("discount", 0) or 0)
                threshold = int(item.get("threshold", 1) or 1)
                last_notified = int(item.get("last_notified_discount", 0) or 0)

                if discount >= threshold and discount > last_notified:
                    msg = self._build_discount_message(info, item, threshold, discount)
                    try:
                        await self.context.send_message(umo, MessageChain([Plain(msg)]))
                        item["last_notified_discount"] = discount
                    except Exception as e:
                        logger.error(f"[SteamDeal] 发送提醒失败 user={user_id} app={app_id}: {e}")

                if discount < threshold:
                    item["last_notified_discount"] = 0

        self._save_db()

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._http and not self._http.closed:
            await self._http.close()

        logger.info("[SteamDeal] 插件已停用")

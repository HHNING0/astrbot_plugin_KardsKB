import asyncio
import json
import random
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Node, Nodes

CARDS_DIR = Path(__file__).parent / "cards"
CARD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}

RARITY_ORDER = ["Tie", "Tong", "Yin", "Jin"]
RARITY_WEIGHTS = [0.90, 0.08, 0.019, 0.001]
RARITY_LABEL = {"Tie": "铁", "Tong": "铜", "Yin": "银", "Jin": "金"}
PACK_SIZE = 5

WEB_API_BASE = "https://1939.giaory.xyz/KARDS_Database"
WEB_FACTIONS = ["Britain", "Germany", "Soviet", "USA", "Japan", "France", "Italy", "Finland", "Poland", "Neutral"]
WEB_POOLS = ["active_pool", "reserve_pool", "derived_pool"]

WEB_RARITY_MAP = {
    "Standard": "Tie",
    "Limited": "Tong",
    "Special": "Yin",
    "Elite": "Jin",
}

@dataclass
class CardEntry:
    name: str
    rarity: str
    pool: str
    card_id: str


@register("kardskb", "KardsKB", "Kards 卡牌游戏知识百科插件", "1.0.0")
class KardsKBPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cards: dict[str, list[CardEntry]] = {}

    async def initialize(self):
        logger.info("KardsKB 插件初始化...")
        if await self._fetch_web_cards():
            logger.info("正在同步卡牌图片到本地...")
            await self._download_all_images()
            logger.info("初始化完成（在线数据）")
        else:
            logger.warning("无法获取在线数据，使用本地文件")
            self._load_local_cards()

    async def _fetch_web_cards(self) -> bool:
        self.cards = {r: [] for r in RARITY_ORDER}
        total = 0
        headers = {"User-Agent": "Mozilla/5.0"}
        url_meta = [(f, p, f"{WEB_API_BASE}/{f}/{p}.json") for f in WEB_FACTIONS for p in WEB_POOLS]

        def fetch(url):
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=10) as executor:
            tasks = [(pool, loop.run_in_executor(executor, fetch, url))
                     for _, pool, url in url_meta]
            for pool_name, fut in tasks:
                try:
                    card_list = await fut
                except Exception:
                    continue
                pool_type = pool_name.replace("_pool", "")
                for c in card_list:
                    rarity = WEB_RARITY_MAP.get(c.get("rarity"))
                    if not rarity:
                        continue
                    card_id = c.get("card_id", "")
                    if not card_id:
                        continue
                    self.cards[rarity].append(CardEntry(
                        name=c.get("name_zh") or c.get("name_en") or "未知",
                        rarity=rarity,
                        pool=pool_type,
                        card_id=card_id,
                    ))
                    total += 1

        if total == 0:
            return False
        for rarity in RARITY_ORDER:
            logger.info(f"  {RARITY_LABEL[rarity]}: {len(self.cards[rarity])} 张")
        logger.info(f"共获取 {total} 张卡牌元数据")
        return True

    async def _download_all_images(self):
        headers = {"User-Agent": "Mozilla/5.0"}
        tasks_data = []
        for rarity in RARITY_ORDER:
            folder = CARDS_DIR / rarity
            folder.mkdir(parents=True, exist_ok=True)
            for card in self.cards.get(rarity, []):
                dest = folder / f"{card.card_id}.jpg"
                if dest.exists():
                    continue
                raw_url = f"https://www.kards.com/images/card/v48/zh-Hans/{card.card_id}.avif"
                proxy_url = f"https://1939.giaory.xyz/img-proxy?url={urllib.parse.quote(raw_url)}"
                tasks_data.append((card, proxy_url, dest))

        if not tasks_data:
            logger.info("所有卡牌图片已是最新")
            return

        logger.info(f"需要下载 {len(tasks_data)} 张图片...")

        def download_one(card, url, dest):
            tmp = dest.with_suffix(".tmp")
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    tmp.write_bytes(resp.read())
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(tmp), "-q:v", "85", str(dest)],
                    capture_output=True, timeout=30,
                )
                return card, True
            except Exception as e:
                return card, False
            finally:
                tmp.unlink(missing_ok=True)

        loop = asyncio.get_event_loop()
        done = 0
        with ThreadPoolExecutor(max_workers=5) as executor:
            futs = [loop.run_in_executor(executor, download_one, card, url, dest)
                    for card, url, dest in tasks_data]
            for fut in asyncio.as_completed(futs):
                try:
                    card, ok = await fut
                    done += 1
                    if ok:
                        logger.info(f"  [{done}/{len(tasks_data)}] {card.name}")
                    else:
                        logger.warning(f"  [{done}/{len(tasks_data)}] 失败: {card.name}")
                except Exception:
                    done += 1
        logger.info("图片同步完成")

    def _load_local_cards(self):
        self.cards = {}
        total = 0
        for rarity in RARITY_ORDER:
            folder = CARDS_DIR / rarity
            if not folder.is_dir():
                continue
            files = sorted(f for f in folder.iterdir() if f.suffix.lower() in CARD_EXTENSIONS)
            entries = [CardEntry(name=f.stem, rarity=rarity, pool="active", card_id=f.stem) for f in files]
            if entries:
                self.cards[rarity] = entries
                total += len(entries)
            logger.info(f"  {RARITY_LABEL.get(rarity, rarity)}: {len(entries)} 张")
        logger.info(f"共加载 {total} 张卡牌图片")

    def _card_image_path(self, card: CardEntry) -> Path:
        return CARDS_DIR / card.rarity / f"{card.card_id}.jpg"

    def _open_pack(self, pool_filter: str | None = None) -> list[CardEntry]:
        all_cards: dict[str, list[CardEntry]] = {}
        for rarity in RARITY_ORDER:
            pool = [c for c in self.cards.get(rarity, []) if pool_filter is None or c.pool == pool_filter]
            if pool:
                all_cards[rarity] = pool
        if not all_cards:
            return []
        available = {r: list(cards) for r, cards in all_cards.items()}
        pack = []
        for i in range(PACK_SIZE):
            rarity = "Tong" if i == 0 else random.choices(RARITY_ORDER, weights=RARITY_WEIGHTS, k=1)[0]
            pool = available.get(rarity)
            if not pool:
                fallback = [c for lst in available.values() for c in lst]
                if not fallback:
                    break
                card = random.choice(fallback)
                for r in available:
                    if card in available[r]:
                        available[r].remove(card)
                        break
            else:
                card = random.choice(pool)
                available[rarity].remove(card)
            pack.append(card)
        return pack

    def _open_officer_pack(self) -> list[CardEntry]:
        all_cards: dict[str, list[CardEntry]] = {}
        for rarity in RARITY_ORDER:
            pool = [c for c in self.cards.get(rarity, []) if c.pool == "active"]
            if pool:
                all_cards[rarity] = pool
        if not all_cards:
            return []

        available = {r: list(cards) for r, cards in all_cards.items()}
        pack = []

        def pick(rarity):
            p = available.get(rarity)
            if p:
                card = random.choice(p)
                available[rarity].remove(card)
                return card
            fallback = [c for lst in available.values() for c in lst]
            if not fallback:
                return None
            card = random.choice(fallback)
            for r in available:
                if card in available[r]:
                    available[r].remove(card)
                    break
            return card

        def remaining_any():
            return [c for lst in available.values() for c in lst]

        for _ in range(6):
            rarity = random.choices(RARITY_ORDER, weights=RARITY_WEIGHTS, k=1)[0]
            card = pick(rarity)
            if card:
                pack.append(card)

        yj_pool = list(available.get("Yin", []) + available.get("Jin", []))
        if yj_pool:
            card = random.choice(yj_pool)
            for r in available:
                if card in available[r]:
                    available[r].remove(card)
                    break
        else:
            rest = remaining_any()
            card = random.choice(rest) if rest else None
        if card:
            pack.append(card)

        random.shuffle(pack)
        return pack

    async def _send_pack(self, event: AstrMessageEvent, pool_filter: str | None):
        if not self.cards or all(len(v) == 0 for v in self.cards.values()):
            yield event.plain_result("未找到卡牌数据。")
            return
        pack = self._open_pack(pool_filter)
        if not pack:
            yield event.plain_result("没有可用的卡牌。")
            return
        nodes = []
        for card in pack:
            img_path = self._card_image_path(card)
            if not img_path.exists():
                continue
            nodes.append(Node(
                name=card.name,
                uin=event.get_self_id(),
                content=[Comp.Image.fromFileSystem(str(img_path))],
            ))
        if nodes:
            yield event.chain_result([Nodes(nodes=nodes)])
        else:
            yield event.plain_result("卡牌图片缺失，请重新同步。")

    @filter.command("开包")
    async def open_pack(self, event: AstrMessageEvent):
        yield event.plain_result("该命令已废弃，请使用 /普通卡包 或 /预备卡包。")

    @filter.command("普通卡包")
    async def normal_pack(self, event: AstrMessageEvent):
        async for r in self._send_pack(event, "active"):
            yield r

    @filter.command("预备卡包")
    async def reserve_pack(self, event: AstrMessageEvent):
        async for r in self._send_pack(event, "reserve"):
            yield r

    @filter.command("军官卡包")
    async def officer_pack(self, event: AstrMessageEvent):
        if not self.cards or all(len(v) == 0 for v in self.cards.values()):
            yield event.plain_result("未找到卡牌数据。")
            return
        pack = self._open_officer_pack()
        if not pack:
            yield event.plain_result("没有可用的卡牌。")
            return
        nodes = []
        for card in pack:
            img_path = self._card_image_path(card)
            if not img_path.exists():
                continue
            nodes.append(Node(
                name=card.name,
                uin=event.get_self_id(),
                content=[Comp.Image.fromFileSystem(str(img_path))],
            ))
        if nodes:
            yield event.chain_result([Nodes(nodes=nodes)])
        else:
            yield event.plain_result("卡牌图片缺失，请重新同步。")

    @filter.command("卡牌数")
    async def card_count(self, event: AstrMessageEvent):
        if not self.cards:
            yield event.plain_result("未找到卡牌数据。")
            return
        lines = ["📊 Kards 卡牌统计", ""]
        total = 0
        pools_seen = set()
        for cards in self.cards.values():
            for card in cards:
                pools_seen.add(card.pool)
        pool_order = [("active", "现役"), ("reserve", "预备"), ("derived", "衍生")]
        for pool_type, pool_label in pool_order:
            if pool_type not in pools_seen:
                continue
            pool_total = 0
            for rarity in RARITY_ORDER:
                count = sum(1 for c in self.cards.get(rarity, []) if c.pool == pool_type)
                if count:
                    pool_total += count
            if pool_total:
                lines.append(f"  {pool_label}: {pool_total} 张")
                total += pool_total
            for rarity in RARITY_ORDER:
                count = sum(1 for c in self.cards.get(rarity, []) if c.pool == pool_type)
                if count:
                    lines.append(f"    {RARITY_LABEL[rarity]}: {count} 张")
        lines.extend(["", f"总计: {total} 张"])
        yield event.plain_result("\n".join(lines))

    @filter.command_group("kards")
    def kards(self):
        pass

    @kards.command("reload")
    async def reload_cards(self, event: AstrMessageEvent):
        logger.info("手动重新加载卡牌数据...")
        if await self._fetch_web_cards():
            await self._download_all_images()
            total = sum(len(v) for v in self.cards.values())
            yield event.plain_result(f"已重新加载 {total} 张卡牌（在线数据已同步到本地）。")
        else:
            self._load_local_cards()
            total = sum(len(v) for v in self.cards.values())
            yield event.plain_result(f"已重新加载 {total} 张卡牌（本地）。")

    async def terminate(self):
        logger.info("KardsKB 插件已卸载")

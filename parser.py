"""
Парсер для Poizon (dewu.com), Taobao, 1688, 95 (95fen.com / 9five)
Возвращает: {"name": str, "price": float|None, "image": str, "source": str}
price=None означает "не удалось — спроси вручную"
"""

import re
import json
import logging
import asyncio
from urllib.parse import urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Единый async клиент с ротацией User-Agent
HEADERS_PC = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

HEADERS_MOBILE = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def detect_source(url: str) -> str:
    """Определяем сайт по URL."""
    u = url.lower()
    if "dewu.com" in u or "poizon.com" in u or "du.com" in u:
        return "poizon"
    if "1688.com" in u:
        return "1688"
    if "taobao.com" in u or "tmall.com" in u or "item.taobao" in u:
        return "taobao"
    if "95fen.com" in u or "95.com" in u or "9five" in u or "九五" in u:
        return "95"
    return "unknown"


def _extract_price_from_text(html: str) -> float | None:
    """Универсальный поиск цены в HTML/JSON."""
    # JSON-паттерны (встречаются во всех сайтах)
    patterns = [
        r'"price"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        r'"originalPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        r'"skuPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        r'"currentPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        r'"promoPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        r'"salePrice"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        r'[¥￥]\s*(\d+(?:\.\d+)?)',
    ]
    candidates = []
    for p in patterns:
        for m in re.finditer(p, html):
            try:
                v = float(m.group(1))
                if 1 < v < 100000:   # адекватный диапазон цен
                    candidates.append(v)
            except ValueError:
                pass

    if not candidates:
        return None

    # Берём медиану — отсекаем мусорные значения
    candidates.sort()
    # Отдаём предпочтение значениям из середины диапазона
    return candidates[len(candidates) // 2]


# ── POIZON / dewu.com ─────────────────────────────────────────────────────────
async def parse_poizon(url: str, client: httpx.AsyncClient) -> dict:
    result = {"name": "", "price": None, "image": "", "source": "Poizon"}
    try:
        # Poizon отдаёт больше данных мобильному UA
        r = await client.get(url, headers=HEADERS_MOBILE, timeout=15, follow_redirects=True)
        html = r.text
        soup = BeautifulSoup(html, "html.parser")

        # Название — og:title или <title>
        og_title = soup.find("meta", property="og:title")
        result["name"] = og_title["content"].strip() if og_title else (soup.title.text.strip() if soup.title else "")

        # Картинка
        og_img = soup.find("meta", property="og:image")
        if og_img:
            result["image"] = og_img.get("content", "")

        # Цена из мета og:price или JSON в <script>
        og_price = soup.find("meta", property="og:price:amount") or soup.find("meta", attrs={"name": "og:price:amount"})
        if og_price:
            try:
                result["price"] = float(og_price["content"])
                return result
            except (ValueError, KeyError):
                pass

        # Ищем в <script> блоках
        for script in soup.find_all("script"):
            txt = script.string or ""
            if "price" in txt.lower() and len(txt) > 50:
                p = _extract_price_from_text(txt)
                if p:
                    result["price"] = p
                    break

        if not result["price"]:
            result["price"] = _extract_price_from_text(html)

    except Exception as e:
        log.warning(f"Poizon parse error: {e}")
    return result


# ── TAOBAO / Tmall ────────────────────────────────────────────────────────────
async def parse_taobao(url: str, client: httpx.AsyncClient) -> dict:
    result = {"name": "", "price": None, "image": "", "source": "Taobao"}
    try:
        # Taobao требует referer
        headers = {**HEADERS_PC, "Referer": "https://www.taobao.com/"}
        r = await client.get(url, headers=headers, timeout=15, follow_redirects=True)
        html = r.text
        soup = BeautifulSoup(html, "html.parser")

        # Название
        og = soup.find("meta", property="og:title")
        result["name"] = og["content"].strip() if og else (soup.title.text.strip() if soup.title else "")

        # Картинка
        og_img = soup.find("meta", property="og:image")
        if og_img:
            result["image"] = og_img.get("content", "")

        # Taobao часто прячет цену в window.__STORE__ или g_config
        for pattern in [
            r'defaultItemPrice["\s:]+(\d+(?:\.\d+)?)',
            r'"price"\s*:\s*"(\d+(?:\.\d+)?)"',
            r'g_config\.price\s*=\s*["\'](\d+(?:\.\d+)?)["\']',
            r'"reservePrice"\s*:\s*"(\d+(?:\.\d+)?)"',
        ]:
            m = re.search(pattern, html)
            if m:
                try:
                    v = float(m.group(1))
                    if 1 < v < 100000:
                        result["price"] = v
                        break
                except ValueError:
                    pass

        if not result["price"]:
            result["price"] = _extract_price_from_text(html)

    except Exception as e:
        log.warning(f"Taobao parse error: {e}")
    return result


# ── 1688 ──────────────────────────────────────────────────────────────────────
async def parse_1688(url: str, client: httpx.AsyncClient) -> dict:
    result = {"name": "", "price": None, "image": "", "source": "1688"}
    try:
        headers = {**HEADERS_PC, "Referer": "https://www.1688.com/"}
        r = await client.get(url, headers=headers, timeout=15, follow_redirects=True)
        html = r.text
        soup = BeautifulSoup(html, "html.parser")

        og = soup.find("meta", property="og:title")
        result["name"] = og["content"].strip() if og else (soup.title.text.strip() if soup.title else "")

        og_img = soup.find("meta", property="og:image")
        if og_img:
            result["image"] = og_img.get("content", "")

        # 1688 кладёт данные в window.__INIT_DATA__
        m = re.search(r'window\.__INIT_DATA__\s*=\s*(\{.+?\});', html, re.S)
        if m:
            try:
                data = json.loads(m.group(1))
                # Обходим вложенный JSON в поисках price
                raw = json.dumps(data)
                p = _extract_price_from_text(raw)
                if p:
                    result["price"] = p
            except json.JSONDecodeError:
                pass

        if not result["price"]:
            for pattern in [
                r'"priceInfo".*?"price"\s*:\s*"?(\d+(?:\.\d+)?)"?',
                r'<strong[^>]*class="[^"]*price[^"]*"[^>]*>(\d+(?:\.\d+)?)',
            ]:
                m = re.search(pattern, html, re.S)
                if m:
                    try:
                        v = float(m.group(1))
                        if 1 < v < 100000:
                            result["price"] = v
                            break
                    except ValueError:
                        pass

        if not result["price"]:
            result["price"] = _extract_price_from_text(html)

    except Exception as e:
        log.warning(f"1688 parse error: {e}")
    return result


# ── 95 (95fen.com) ────────────────────────────────────────────────────────────
async def parse_95(url: str, client: httpx.AsyncClient) -> dict:
    result = {"name": "", "price": None, "image": "", "source": "95"}
    try:
        r = await client.get(url, headers=HEADERS_MOBILE, timeout=15, follow_redirects=True)
        html = r.text
        soup = BeautifulSoup(html, "html.parser")

        og = soup.find("meta", property="og:title")
        result["name"] = og["content"].strip() if og else (soup.title.text.strip() if soup.title else "")

        og_img = soup.find("meta", property="og:image")
        if og_img:
            result["image"] = og_img.get("content", "")

        result["price"] = _extract_price_from_text(html)

    except Exception as e:
        log.warning(f"95 parse error: {e}")
    return result


# ── Главная функция ────────────────────────────────────────────────────────────
async def parse_product(url: str) -> dict:
    """
    Возвращает dict:
      name   — название товара (может быть пустым)
      price  — цена в юанях (float) или None если не удалось
      image  — URL картинки (может быть пустым)
      source — название сайта
    """
    source = detect_source(url)

    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=5),
    ) as client:
        if source == "poizon":
            return await parse_poizon(url, client)
        elif source == "taobao":
            return await parse_taobao(url, client)
        elif source == "1688":
            return await parse_1688(url, client)
        elif source == "95":
            return await parse_95(url, client)
        else:
            # Неизвестный сайт — пробуем универсально
            try:
                r = await client.get(url, headers=HEADERS_MOBILE, timeout=15)
                html = r.text
                soup = BeautifulSoup(html, "html.parser")
                og = soup.find("meta", property="og:title")
                name = og["content"].strip() if og else (soup.title.text.strip() if soup.title else "")
                og_img = soup.find("meta", property="og:image")
                image = og_img.get("content", "") if og_img else ""
                price = _extract_price_from_text(html)
                return {"name": name, "price": price, "image": image, "source": "Другой сайт"}
            except Exception as e:
                log.warning(f"Unknown site parse error: {e}")
                return {"name": "", "price": None, "image": "", "source": "Неизвестный сайт"}

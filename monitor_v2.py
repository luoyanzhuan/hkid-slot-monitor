"""
GitHub Actions 监控脚本 v2
用 Playwright headless 浏览器打开 step3 页面，从 DOM 读取真实号源数据
绕过 Imperva TLS 指纹检测
"""
import asyncio
import json
import os
import re
import requests
from playwright.async_api import async_playwright

BARK_KEY = os.environ.get("BARK_KEY", "")
COOKIE_STR = os.environ.get("COOKIE_STR", "")
# 上次推送的状态（避免重复推送）
STATE_FILE = "/tmp/last_push.json"

TARGET_MONTH = 9  # 监控 9 月份号源


def send_bark(title, body):
    """发送 Bark 推送"""
    if not BARK_KEY:
        print("BARK_KEY 未设置，跳过推送")
        return
    try:
        url = f"https://api.day.app/{BARK_KEY}/{title}/{body}"
        requests.get(url, timeout=10)
        print(f"Bark 推送成功: {title}")
    except Exception as e:
        print(f"Bark 推送失败: {e}")


def load_last_push():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}


def save_last_push(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except:
        pass


def parse_cookies(cookie_str):
    """把 cookie 字符串解析为 Playwright cookies 格式"""
    cookies = []
    for item in cookie_str.split("; "):
        if "=" in item:
            name, value = item.split("=", 1)
            cookies.append({
                "name": name,
                "value": value,
                "domain": ".es2.immd.gov.hk",
                "path": "/",
            })
    return cookies


async def main():
    if not COOKIE_STR:
        print("COOKIE_STR 未设置")
        return

    cookies = parse_cookies(COOKIE_STR)
    print(f"加载了 {len(cookies)} 个 cookie")

    async with async_playwright() as p:
        # 启动 headless Chromium
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ]
        )

        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0',
            viewport={'width': 1280, 'height': 800},
            locale='zh-CN',
        )

        # 注入 stealth 脚本
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
            window.chrome = {runtime: {}};
        """)

        # 设置 cookie
        await context.add_cookies(cookies)

        page = await context.new_page()

        # 打开 step3 页面
        step3_url = "https://system.es2.immd.gov.hk/smartics2-client/ropbooking/zh-CN/eservices/ropChangeCancelAppointment/step3"
        print(f"打开 {step3_url}")

        try:
            await page.goto(step3_url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            print(f"页面加载: {e}")

        await asyncio.sleep(3)
        current_url = page.url
        print(f"当前页面: {current_url}")

        # 如果被重定向到 term 页面（隐私政策同意），需要勾选并点击开始
        if "/term" in current_url or "privacy" in current_url.lower() or "我已阅读" in await page.inner_text("body"):
            print("检测到隐私政策页面，自动勾选并继续...")
            try:
                # 勾选复选框
                checkbox = await page.query_selector('input[type="checkbox"]')
                if checkbox:
                    await checkbox.check()
                    await asyncio.sleep(1)
                # 点击"开始"按钮
                start_btn = await page.query_selector('button:has-text("开始"), input[value="开始"], a:has-text("开始")')
                if start_btn:
                    await start_btn.click()
                    await asyncio.sleep(5)
                    print(f"点击开始后页面: {page.url}")
            except Exception as e:
                print(f"处理隐私页面失败: {e}")

        # 再次检查是否在 step3
        current_url = page.url
        print(f"当前页面: {current_url}")

        # 如果还不是 step3，再次尝试导航
        if "step3" not in current_url:
            print(f"不在 step3，尝试直接导航...")
            try:
                await page.goto(step3_url, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(5)
                current_url = page.url
                print(f"导航后页面: {current_url}")
            except Exception as e:
                print(f"导航失败: {e}")

        await asyncio.sleep(3)

        # 截图
        await page.screenshot(path="/tmp/step3.png", full_page=False)
        print("截图已保存")

        # 检查是否被重定向到登录页
        if "step1" in current_url or "login" in current_url.lower():
            print("⚠️ 被重定向到登录页，cookie 可能已过期")
            send_bark(
                "HKID监控-凭证过期",
                "Cookie已过期，请重新登录并更新GitHub Secrets"
            )
            await browser.close()
            return

        # 检查是否被 Imperva 拦截
        page_text = await page.inner_text("body")
        if "Pardon Our Interruption" in page_text or "interruption" in page_text.lower():
            print("⚠️ 被 Imperva 拦截")
            send_bark(
                "HKID监控-被拦截",
                "Imperva拦截了GitHub Actions请求"
            )
            await browser.close()
            return

        # 从页面文本中提取日期
        # 查找所有 "X月X日" 或 "XXXX年X月X日" 格式的日期
        dates = re.findall(r'(\d{1,2})月(\d{1,2})日', page_text)
        print(f"页面上找到 {len(dates)} 个日期")

        # 查找 9 月份的日期
        september_dates = []
        for month, day in dates:
            m = int(month)
            d = int(day)
            if m == TARGET_MONTH:
                september_dates.append(f"{m}月{d}日")

        if september_dates:
            # 有 9 月份号源！
            last_push = load_last_push()
            new_dates = set(september_dates) - set(last_push.get("dates", []))

            if new_dates or True:  # 简化：每次都推送
                msg = "、".join(september_dates[:10])
                print(f"🎉 发现 {TARGET_MONTH} 月份号源: {msg}")
                send_bark(
                    f"HKID监控-{TARGET_MONTH}月有号源",
                    f"发现号源: {msg}，请立即登录抢号！"
                )
                save_last_push({"dates": september_dates})
            else:
                print("没有新的号源，不推送")
        else:
            print(f"页面上没有 {TARGET_MONTH} 月份号源")
            # 打印所有找到的日期用于调试
            all_dates = [f"{m}月{d}日" for m, d in dates]
            print(f"所有日期: {all_dates}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

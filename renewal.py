#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
XServer VPS 自动续期脚本（优化版）
- 修复：只调用一次 YesCaptcha，避免浪费额度
- 修复：注入 token 后模拟点击 Turnstile 复选框
- 优化：增加调试信息，保存错误页面截图
"""

import asyncio
import re
import datetime
from datetime import timezone, timedelta
import os
import importlib.util
import json
import logging
from typing import Optional, Dict

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# 尝试兼容两种 playwright-stealth 版本
_stealth_spec = importlib.util.find_spec("playwright_stealth")
if _stealth_spec:
    from playwright_stealth import stealth_async
    STEALTH_VERSION = 'old'
else:
    STEALTH_VERSION = 'new'
    stealth_async = None

_aiohttp_available = importlib.util.find_spec("aiohttp") is not None


# ======================== 配置 ==========================

class Config:
    LOGIN_EMAIL = os.getenv("XSERVER_EMAIL")
    LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD")
    VPS_ID = os.getenv("XSERVER_VPS_ID", "40124478")

    USE_HEADLESS = os.getenv("USE_HEADLESS", "true").lower() == "true"
    WAIT_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "30000"))

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    PROXY_SERVER = os.getenv("PROXY_SERVER")

    CAPTCHA_API_URL = os.getenv(
        "CAPTCHA_API_URL",
        "https://captcha-120546510085.asia-northeast1.run.app"
    )

    YESCAPTCHA_API_KEY = os.getenv("YESCAPTCHA_API_KEY")

    DETAIL_URL = f"https://secure.xserver.ne.jp/xapanel/xvps/server/detail?id={VPS_ID}"
    EXTEND_URL = f"https://secure.xserver.ne.jp/xapanel/xvps/server/freevps/extend/index?id_vps={VPS_ID}"


# ======================== 日志 ==========================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('renewal.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ======================== 通知器 ==========================

class Notifier:
    @staticmethod
    async def send_telegram(message: str):
        if not all([Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID]):
            return
        if not _aiohttp_available:
            logger.error("❌ 未安装 aiohttp，无法发送 Telegram 通知")
            return

        import aiohttp

        try:
            url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {
                "chat_id": Config.TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data) as resp:
                    if resp.status == 200:
                        logger.info("✅ Telegram 通知发送成功")
                    else:
                        logger.error(f"❌ Telegram 返回非 200 状态码: {resp.status}")
        except Exception as e:
            logger.error(f"❌ Telegram 发送失败: {e}")

    @staticmethod
    async def notify(subject: str, message: str):
        await Notifier.send_telegram(message)


# ======================== 验证码识别 ==========================

class CaptchaSolver:
    """外部 API OCR 验证码识别器"""

    def __init__(self):
        self.api_url = Config.CAPTCHA_API_URL

    def _validate_code(self, code: str) -> bool:
        """验证识别出的验证码是否合理"""
        if not code:
            return False

        if len(code) < 4 or len(code) > 6:
            logger.warning(f"⚠️ 验证码长度异常: {len(code)} 位")
            return False

        if len(set(code)) == 1:
            logger.warning(f"⚠️ 验证码可疑(所有数字相同): {code}")
            return False

        if not code.isdigit():
            logger.warning(f"⚠️ 验证码包含非数字字符: {code}")
            return False

        return True

    async def solve(self, img_data_url: str) -> Optional[str]:
        """使用外部 API 识别验证码"""
        if not _aiohttp_available:
            logger.error("❌ 未安装 aiohttp，无法调用验证码识别接口")
            return None

        import aiohttp

        try:
            logger.info(f"📤 发送验证码到 API: {self.api_url}")

            max_retries = 3
            retry_count = 0

            while retry_count < max_retries:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            self.api_url,
                            data=img_data_url,
                            headers={'Content-Type': 'text/plain'},
                            timeout=aiohttp.ClientTimeout(total=20)
                        ) as resp:
                            if not resp.ok:
                                raise Exception(f"API 请求失败: {resp.status}")

                            code_response = await resp.text()
                            code = code_response.strip()

                            logger.info(f"📥 API 返回验证码: {code}")

                            if code and len(code) >= 4:
                                numbers = re.findall(r'\d+', code)
                                if numbers:
                                    code = numbers[0][:6]

                                    if self._validate_code(code):
                                        logger.info(f"🎯 API 识别成功: {code}")
                                        return code

                            raise Exception('API 返回无效验证码')

                except Exception as err:
                    retry_count += 1
                    if retry_count >= max_retries:
                        logger.error(f"❌ API 识别失败(已重试 {max_retries} 次): {err}")
                        return None
                    logger.info(f"🔄 验证码识别失败,正在进行第 {retry_count} 次重试...")
                    await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"❌ API 识别错误: {e}")

        return None


class TurnstileSolver:
    """使用 https://yescaptcha.com 代破解 Cloudflare Turnstile"""

    CREATE_TASK_URL = "https://api.yescaptcha.com/createTask"
    RESULT_URL = "https://api.yescaptcha.com/getTaskResult"

    def __init__(self):
        self.api_key = Config.YESCAPTCHA_API_KEY

    async def solve(self, site_key: str, page_url: str, max_wait: int = 120) -> Optional[str]:
        if not self.api_key:
            logger.warning("⚠️ 未配置 YESCAPTCHA_API_KEY，跳过代破解 Turnstile")
            return None
        if not _aiohttp_available:
            logger.error("❌ 未安装 aiohttp，无法调用 YesCaptcha 接口")
            return None

        import aiohttp

        try:
            payload = {
                "clientKey": self.api_key,
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                },
                "softID": 36,
            }

            logger.info("📤 发送 Turnstile 代破解任务至 YesCaptcha...")

            async with aiohttp.ClientSession() as session:
                async with session.post(self.CREATE_TASK_URL, json=payload, timeout=30) as resp:
                    data = await resp.json()
                    if data.get("errorId") != 0:
                        raise Exception(data.get("errorDescription", "创建任务失败"))

                    task_id = data.get("taskId")
                    logger.info(f"🆔 YesCaptcha 任务已创建: {task_id}")

                # 轮询获取结果
                start_time = datetime.datetime.utcnow()
                while (datetime.datetime.utcnow() - start_time).total_seconds() < max_wait:
                    await asyncio.sleep(5)
                    async with session.post(
                        self.RESULT_URL,
                        json={"clientKey": self.api_key, "taskId": task_id},
                        timeout=20,
                    ) as resp:
                        result = await resp.json()
                        if result.get("errorId") != 0:
                            raise Exception(result.get("errorDescription", "查询任务失败"))

                        if result.get("status") == "ready":
                            solution = result.get("solution", {})
                            token = solution.get("token")
                            if token:
                                logger.info("✅ YesCaptcha 返回 Turnstile token")
                                return token
                        else:
                            logger.info("⏳ 等待 YesCaptcha 返回结果...")

                logger.error("❌ YesCaptcha 轮询超时，未获取到 token")
                return None

        except Exception as e:
            logger.error(f"❌ YesCaptcha 处理 Turnstile 失败: {e}")
            return None


# ======================== 核心类 ==========================

class XServerVPSRenewal:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None

        self.renewal_status: str = "Unknown"
        self.old_expiry_time: Optional[str] = None
        self.new_expiry_time: Optional[str] = None
        self.error_message: Optional[str] = None

        self.captcha_solver = CaptchaSolver()
        self.turnstile_solver = TurnstileSolver()

    # ---------- 缓存 ----------
    def load_cache(self) -> Optional[Dict]:
        if os.path.exists("cache.json"):
            try:
                with open("cache.json", "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载缓存失败: {e}")
        return None

    def save_cache(self):
        cache = {
            "last_expiry": self.old_expiry_time,
            "status": self.renewal_status,
            "last_check": datetime.datetime.now(timezone.utc).isoformat(),
            "vps_id": Config.VPS_ID
        }
        try:
            with open("cache.json", "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存缓存失败: {e}")

    # ---------- 生成 README ----------
    def generate_readme(self):
        """生成 README.md 文件"""
        try:
            status_emoji = {
                "Success": "✅",
                "Failed": "❌",
                "Unexpired": "ℹ️",
                "Unknown": "❓"
            }
            
            emoji = status_emoji.get(self.renewal_status, "❓")
            
            readme_content = f"""# XServer VPS 自动续期状态

## 📊 最新状态

**状态**: {emoji} {self.renewal_status}  
**检查时间**: {datetime.datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S JST')}  
**VPS ID**: {Config.VPS_ID}

## 📅 到期信息

- **当前到期时间**: {self.old_expiry_time or '未知'}
- **新到期时间**: {self.new_expiry_time or '未更新'}

## ⚠️ 错误信息

{self.error_message or '无'}

---

*此文件由自动化脚本生成*
"""
            
            with open("README.md", "w", encoding="utf-8") as f:
                f.write(readme_content)
            
            logger.info("✅ README.md 已生成")
            
        except Exception as e:
            logger.error(f"❌ 生成 README 失败: {e}")

    # ---------- 截图 ----------
    async def shot(self, name: str):
        """安全截图,不影响主流程"""
        if not self.page:
            return
        try:
            await self.page.screenshot(path=f"{name}.png", full_page=True)
        except Exception:
            pass

    # ---------- 浏览器 ----------
    async def setup_browser(self) -> bool:
        try:
            self._pw = await async_playwright().start()
            launch_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-infobars",
                "--start-maximized",
            ]

            proxy_url = None
            if Config.PROXY_SERVER:
                proxy_url = Config.PROXY_SERVER
                logger.info(f"🌐 使用代理: {Config.PROXY_SERVER}")

            if Config.USE_HEADLESS:
                logger.info("⚠️ 为了通过 Turnstile，强制使用非无头模式(headless=False)")
            else:
                logger.info("ℹ️ 已配置非无头模式(headless=False)")

            if proxy_url:
                launch_args.append(f"--proxy-server={proxy_url}")

            launch_kwargs = {
                "headless": False,
                "args": launch_args
            }

            self.browser = await self._pw.chromium.launch(**launch_kwargs)

            context_options = {
                "viewport": {"width": 1920, "height": 1080},
                "locale": "ja-JP",
                "timezone_id": "Asia/Tokyo",
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }

            self.context = await self.browser.new_context(**context_options)

            await self.context.add_init_script("""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','ja-JP','en-US']});
Object.defineProperty(navigator, 'permissions', {
    get: () => ({
        query: ({name}) => Promise.resolve({state: 'granted'})
    })
});
""")

            self.page = await self.context.new_page()
            self.page.set_default_timeout(Config.WAIT_TIMEOUT)

            if STEALTH_VERSION == 'old' and stealth_async is not None:
                await stealth_async(self.page)
            else:
                logger.info("ℹ️ 使用新版 playwright_stealth 或未安装,跳过 stealth 处理")

            logger.info("✅ 浏览器初始化成功")
            return True
        except Exception as e:
            logger.error(f"❌ 浏览器初始化失败: {e}")
            self.error_message = str(e)
            return False

    # ---------- 登录 ----------
    async def login(self) -> bool:
        try:
            logger.info("🌐 开始登录")
            await self.page.goto(
                "https://secure.xserver.ne.jp/xapanel/login/xvps/",
                timeout=30000
            )
            await asyncio.sleep(2)
            await self.shot("01_login")

            await self.page.fill("input[name='memberid']", Config.LOGIN_EMAIL)
            await self.page.fill("input[name='user_password']", Config.LOGIN_PASSWORD)
            await self.shot("02_before_submit")

            logger.info("📤 提交登录表单...")
            await self.page.click("input[type='submit']")
            await asyncio.sleep(5)
            await self.shot("03_after_submit")

            if "xvps/index" in self.page.url or "login" not in self.page.url.lower():
                logger.info("🎉 登录成功")
                return True

            logger.error("❌ 登录失败")
            self.error_message = "登录失败"
            return False
        except Exception as e:
            logger.error(f"❌ 登录错误: {e}")
            self.error_message = f"登录错误: {e}"
            return False

    # ---------- 获取到期时间 ----------
    async def get_expiry(self) -> bool:
        try:
            await self.page.goto(Config.DETAIL_URL, timeout=30000)
            await asyncio.sleep(3)
            await self.shot("04_detail")

            expiry_date = await self.page.evaluate("""
                () => {
                    const rows = document.querySelectorAll('tr');
                    for (const row of rows) {
                        const text = row.innerText || row.textContent;
                        if (text.includes('利用期限') && !text.includes('利用開始')) {
                            const match = text.match(/(\d{4})年(\d{1,2})月(\d{1,2})日/);
                            if (match) return {year: match[1], month: match[2], day: match[3]};
                        }
                    }
                    return null;
                }
            """)

            if expiry_date:
                self.old_expiry_time = (
                    f"{expiry_date['year']}-"
                    f"{expiry_date['month'].zfill(2)}-"
                    f"{expiry_date['day'].zfill(2)}"
                )
                logger.info(f"📅 利用期限: {self.old_expiry_time}")
                return True

            logger.warning("⚠️ 未能解析利用期限")
            return False
        except Exception as e:
            logger.error(f"❌ 获取到期时间失败: {e}")
            return False

    # ---------- 点击"更新する" ----------
    async def click_update(self) -> bool:
        try:
            try:
                await self.page.click("a:has-text('更新する')", timeout=3000)
                await asyncio.sleep(2)
                logger.info("✅ 点击更新按钮(链接)")
                return True
            except Exception:
                pass

            try:
                await self.page.click("button:has-text('更新する')", timeout=3000)
                await asyncio.sleep(2)
                logger.info("✅ 点击更新按钮(按钮)")
                return True
            except Exception:
                pass

            logger.info("ℹ️ 未找到更新按钮")
            return False
        except Exception as e:
            logger.info(f"ℹ️ 点击更新按钮失败: {e}")
            return False

    # ---------- 打开续期页面 ----------
    async def open_extend(self) -> bool:
        try:
            await asyncio.sleep(2)
            await self.shot("05_before_extend")

            try:
                logger.info("🔍 方法1: 查找续期按钮(按钮)...")
                await self.page.click(
                    "button:has-text('引き続き無料VPSの利用を継続する')",
                    timeout=3000
                )
                await asyncio.sleep(5)
                await self.shot("06_extend_page")
                logger.info("✅ 打开续期页面(按钮点击成功)")
                return True
            except Exception as e1:
                logger.info(f"ℹ️ 方法1失败(按钮): {e1}")

            try:
                logger.info("🔍 方法1b: 尝试链接形式...")
                await self.page.click(
                    "a:has-text('引き続き無料VPSの利用を継続する')",
                    timeout=3000
                )
                await asyncio.sleep(5)
                await self.shot("06_extend_page")
                logger.info("✅ 打开续期页面(链接点击成功)")
                return True
            except Exception as e1b:
                logger.info(f"ℹ️ 方法1b失败(链接): {e1b}")

            try:
                logger.info("🔍 方法2: 直接访问续期URL...")
                await self.page.goto(Config.EXTEND_URL, timeout=Config.WAIT_TIMEOUT)
                await asyncio.sleep(3)
                await self.shot("05_extend_url")

                content = await self.page.content()

                if "引き続き無料VPSの利用を継続する" in content:
                    try:
                        await self.page.click(
                            "button:has-text('引き続き無料VPSの利用を継続する')",
                            timeout=5000
                        )
                        await asyncio.sleep(5)
                        await self.shot("06_extend_page")
                        logger.info("✅ 打开续期页面(方法2-按钮)")
                        return True
                    except Exception:
                        await self.page.click(
                            "a:has-text('引き続き無料VPSの利用を継続する')",
                            timeout=5000
                        )
                        await asyncio.sleep(5)
                        await self.shot("06_extend_page")
                        logger.info("✅ 打开续期页面(方法2-链接)")
                        return True

                if "延長期限" in content or "期限まで" in content:
                    logger.info("ℹ️ 未到续期时间窗口")
                    self.renewal_status = "Unexpired"
                    return False

            except Exception as e2:
                logger.info(f"ℹ️ 方法2失败: {e2}")

            logger.warning("⚠️ 所有打开续期页面的方法都失败")
            return False

        except Exception as e:
            logger.warning(f"⚠️ 打开续期页面异常: {e}")
            return False

    # ---------- Turnstile 处理（改进版）---------- 
    async def inject_and_trigger_turnstile(self, token: str) -> bool:
        """注入 token 并触发 Turnstile 复选框的视觉反馈"""
        try:
            logger.info("🔧 步骤1: 注入 Turnstile token...")
            
            # 注入 token
            inject_result = await self.page.evaluate("""
                (tokenValue) => {
                    const input = document.querySelector('[name="cf-turnstile-response"]');
                    if (input) {
                        input.value = tokenValue;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                    return false;
                }
            """, token)
            
            if not inject_result:
                logger.error("❌ 未找到 cf-turnstile-response 输入框")
                return False
            
            logger.info("✅ Token 已注入到隐藏字段")
            await asyncio.sleep(2)
            
            # 步骤2: 尝试触发 Turnstile 复选框的点击
            logger.info("🖱️ 步骤2: 触发 Turnstile 复选框点击...")
            
            # 方法1: 通过 iframe 坐标点击
            click_result = await self.page.evaluate("""
                () => {
                    const turnstileDiv = document.querySelector('.cf-turnstile');
                    if (!turnstileDiv) return {success: false, reason: 'no container'};
                    
                    const iframe = turnstileDiv.querySelector('iframe');
                    if (!iframe) return {success: false, reason: 'no iframe'};
                    
                    const rect = iframe.getBoundingClientRect();
                    return {
                        success: true,
                        x: rect.x + 30,
                        y: rect.y + rect.height / 2,
                        width: rect.width,
                        height: rect.height
                    };
                }
            """)
            
            if click_result.get('success'):
                logger.info(f"📍 找到 Turnstile iframe 位置: ({click_result['x']:.0f}, {click_result['y']:.0f})")
                
                # 模拟真实的鼠标移动和点击
                await self.page.mouse.move(100, 100)
                await asyncio.sleep(0.3)
                await self.page.mouse.move(click_result['x'], click_result['y'], steps=20)
                await asyncio.sleep(0.5)
                await self.page.mouse.down()
                await asyncio.sleep(0.2)
                await self.page.mouse.up()
                
                logger.info("✅ 已模拟点击 Turnstile 复选框")
                await asyncio.sleep(3)
            else:
                logger.warning(f"⚠️ 无法定位 iframe: {click_result.get('reason')}")
            
            # 步骤3: 验证 Turnstile 状态
            logger.info("🔍 步骤3: 验证 Turnstile 状态...")
            await asyncio.sleep(2)
            
            status = await self.page.evaluate("""
                () => {
                    const input = document.querySelector('[name="cf-turnstile-response"]');
                    const container = document.querySelector('.cf-turnstile');
                    
                    return {
                        hasToken: input && input.value && input.value.length > 0,
                        tokenLength: input && input.value ? input.value.length : 0,
                        containerClasses: container ? container.className : '',
                        isChecked: container && (
                            container.querySelector('[aria-checked="true"]') !== null ||
                            container.classList.contains('success') ||
                            container.classList.contains('verified')
                        )
                    };
                }
            """)
            
            logger.info(f"📊 Turnstile 状态: Token长度={status['tokenLength']}, 复选框勾选={status['isChecked']}")
            
            if not status['isChecked']:
                logger.warning("⚠️ 复选框未显示为已勾选状态，尝试通过 CDP 操作 iframe 内部...")
                
                # 方法2: 使用 CDP 直接操作 iframe 内部元素
                try:
                    cdp = await self.page.context.new_cdp_session(self.page)
                    await cdp.send('Runtime.enable')
                    
                    # 获取所有 frames
                    frames_data = await cdp.send('Page.getFrameTree')
                    
                    def collect_frame_ids(frame_tree):
                        ids = [frame_tree['frame']['id']]
                        if 'childFrames' in frame_tree:
                            for child in frame_tree['childFrames']:
                                ids.extend(collect_frame_ids(child))
                        return ids
                    
                    frame_ids = collect_frame_ids(frames_data['frameTree'])
                    logger.info(f"📋 找到 {len(frame_ids)} 个 frames，尝试在每个 frame 中点击...")
                    
                    for i, frame_id in enumerate(frame_ids):
                        try:
                            result = await cdp.send('Runtime.evaluate', {
                                'expression': '''
                                    (() => {
                                        const checkbox = document.querySelector('input[type="checkbox"]');
                                        if (checkbox) {
                                            checkbox.checked = true;
                                            checkbox.dispatchEvent(new Event('change', {bubbles: true}));
                                            checkbox.click();
                                            return 'checkbox_clicked';
                                        }
                                        
                                        const clickableLabel = document.querySelector('label');
                                        if (clickableLabel) {
                                            clickableLabel.click();
                                            return 'label_clicked';

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
XServer VPS 自动续期脚本（修复版）
- 修复：添加缺失的 generate_readme() 方法
- 优化：Turnstile token 注入时机和方式
- 改进：增加表单提交前的等待时间，确保 token 生效
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

    # ---------- Turnstile 处理（优化版）---------- 
    async def inject_turnstile_token(self, token: str) -> bool:
        """改进的 Turnstile token 注入方法"""
        try:
            logger.info("🔧 开始注入 Turnstile token...")
            
            # 方法1: 直接设置 input 值
            success1 = await self.page.evaluate("""
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
            
            if success1:
                logger.info("✅ 方法1: 已注入 input[name='cf-turnstile-response']")
            
            # 方法2: 尝试通过 window.turnstile API
            success2 = await self.page.evaluate("""
                (tokenValue) => {
                    if (window.turnstile && window.turnstile.reset) {
                        try {
                            const widgets = document.querySelectorAll('.cf-turnstile');
                            widgets.forEach((widget, idx) => {
                                try {
                                    window.turnstile.reset(idx);
                                } catch(e) {}
                            });
                        } catch(e) {}
                    }
                    return false;
                }
            """, token)
            
            # 方法3: 设置隐藏的 response 字段
            success3 = await self.page.evaluate("""
                (tokenValue) => {
                    const responses = document.querySelectorAll('input[name*="turnstile"], input[id*="turnstile"]');
                    let found = false;
                    responses.forEach(input => {
                        input.value = tokenValue;
                        found = true;
                    });
                    return found;
                }
            """, token)
            
            if success3:
                logger.info("✅ 方法3: 已注入其他 turnstile 相关字段")
            
            # 验证注入结果
            await asyncio.sleep(2)
            verification = await self.page.evaluate("""
                () => {
                    const input = document.querySelector('[name="cf-turnstile-response"]');
                    return {
                        hasInput: !!input,
                        hasValue: input && input.value && input.value.length > 0,
                        valueLength: input && input.value ? input.value.length : 0
                    };
                }
            """)
            
            logger.info(f"🔍 Token 注入验证: {verification}")
            
            return success1 or success3
            
        except Exception as e:
            logger.error(f"❌ Token 注入失败: {e}")
            return False

    # ---------- 提交续期表单（优化版）----------
    async def submit_extend(self) -> bool:
        """提交续期表单 - 优化 Turnstile 处理"""

        async def _read_captcha_image() -> Optional[str]:
            return await self.page.evaluate("""
                () => {
                    const img =
                      document.querySelector('img[src^="data:image"]') ||
                      document.querySelector('img[src^="data:"]') ||
                      document.querySelector('img[alt="画像認証"]') ||
                      document.querySelector('img');
                    if (!img || !img.src) {
                        return null;
                    }
                    return img.src;
                }
            """)

        async def _fill_captcha(code: str) -> bool:
            return await self.page.evaluate("""
                (code) => {
                    const input =
                      document.querySelector('[placeholder*="上の画像"]') ||
                      document.querySelector('input[type="text"]');
                    if (!input) {
                        return false;
                    }
                    input.value = code;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
            """, code)

        try:
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                logger.info(f"📄 开始提交续期表单 (尝试 {attempt}/{max_attempts})")
                await asyncio.sleep(3)

                if attempt > 1:
                    logger.info("🔄 正在刷新续期页面以获取新验证码和 Turnstile...")
                    await self.page.reload()
                    await asyncio.sleep(5)

                # 步骤 1: 获取 Turnstile sitekey
                turnstile_info = await self.page.evaluate("""
                    () => {
                        const el = document.querySelector('.cf-turnstile');
                        if (!el) return null;
                        return {
                            hasTurnstile: true,
                            sitekey: el.getAttribute('data-sitekey'),
                        };
                    }
                """)

                if not turnstile_info or not turnstile_info.get('sitekey'):
                    logger.warning("⚠️ 未检测到 Turnstile，跳过验证")
                else:
                    site_key = turnstile_info['sitekey']
                    page_url = self.page.url
                    
                    logger.info(f"🔑 检测到 Turnstile sitekey: {site_key}")
                    
                    # 获取 token
                    token = await self.turnstile_solver.solve(site_key, page_url, max_wait=90)
                    
                    if token:
                        # 注入 token
                        inject_success = await self.inject_turnstile_token(token)
                        
                        if inject_success:
                            logger.info("✅ Turnstile token 注入成功")
                            # 等待更长时间确保 token 生效
                            await asyncio.sleep(5)
                        else:
                            logger.warning("⚠️ Token 注入失败，但继续尝试")
                    else:
                        logger.warning("⚠️ 未获取到 Turnstile token，但继续尝试")

                # 步骤 2: 获取并识别验证码图片
                logger.info("🔍 步骤2: 查找验证码图片...")
                img_data_url = await _read_captcha_image()

                if not img_data_url:
                    logger.info("ℹ️ 无验证码,可能未到续期时间")
                    self.renewal_status = "Unexpired"
                    return False

                logger.info("📸 已找到验证码图片,正在发送到 API 进行识别...")
                await self.shot(f"08_captcha_found_attempt_{attempt}")

                code = await self.captcha_solver.solve(img_data_url)
                if not code:
                    logger.error("❌ 验证码识别失败")
                    self.renewal_status = "Failed"
                    self.error_message = "验证码识别失败"
                    if attempt < max_attempts:
                        logger.info("🔁 将在下一次尝试中重新识别验证码")
                        continue
                    return False

                # 步骤 3: 填写验证码
                logger.info(f"⌨️ 步骤3: 填写验证码: {code}")
                input_filled = await _fill_captcha(code)

                if not input_filled:
                    raise Exception("未找到验证码输入框")

                await asyncio.sleep(3)
                await self.shot(f"09_captcha_filled_attempt_{attempt}")

                # 步骤 4: 最终确认 Turnstile token
                logger.info("🔍 步骤4: 最终确认 Turnstile token...")
                final_check = await self.page.evaluate("""
                    () => {
                        const tokenField = document.querySelector('[name="cf-turnstile-response"]');
                        return {
                            hasToken: tokenField && tokenField.value && tokenField.value.length > 0,
                            tokenLength: tokenField && tokenField.value ? tokenField.value.length : 0,
                            tokenPreview: tokenField && tokenField.value 
                                ? tokenField.value.substring(0, 50) + '...'
                                : 'empty'
                        };
                    }
                """)

                if final_check['hasToken']:
                    logger.info(
                        f"✅ Turnstile 令牌确认 (长度: {final_check['tokenLength']})"
                    )
                    logger.info(f"📝 Token 预览: {final_check['tokenPreview']}")
                else:
                    logger.warning("⚠️ Turnstile 令牌缺失，提交可能失败")

                # 等待更长时间确保所有验证完成
                await asyncio.sleep(5)

                # 步骤 5: 提交表单
                logger.info("🖱️ 步骤5: 提交表单...")
                await self.shot(f"10_before_submit_attempt_{attempt}")

                submitted = await self.page.evaluate("""
                    () => {
                        if (typeof window.submit_button !== 'undefined' &&
                            window.submit_button &&
                            typeof window.submit_button.click === 'function') {
                            window.submit_button.click();
                            return true;
                        }
                        const submitBtn =
                          document.querySelector('input[type="submit"], button[type="submit"]');
                        if (submitBtn) {
                            submitBtn.click();
                            return true;
                        }
                        return false;
                    }
                """)

                if not submitted:
                    logger.error("❌ 无法提交表单")
                    raise Exception("无法提交表单")

                logger.info("✅ 表单已提交，等待响应...")
                await asyncio.sleep(8)
                await self.shot(f"11_after_submit_attempt_{attempt}")

                html = await self.page.content()

                # 检查错误提示
                error_keywords = [
                    "入力された認証コードが正しくありません",
                    "認証コードが正しくありません",
                    "Turnstileの検証に失敗しました",
                    "エラー",
                    "間違"
                ]
                
                has_error = any(err in html for err in error_keywords)
                
                if has_error:
                    logger.error(f"❌ 提交失败 (尝试 {attempt}/{max_attempts})")
                    await self.shot(f"11_error_attempt_{attempt}")
                    
                    if attempt < max_attempts:
                        logger.info("🔁 检测到错误，准备重新刷新并重试...")
                        await asyncio.sleep(3)
                        continue
                    
                    self.renewal_status = "Failed"
                    self.error_message = "验证码或 Turnstile 验证失败"
                    return False

                # 检查成功提示
                success_keywords = [
                    "完了",
                    "継続",
                    "完成",
                    "更新しました",
                    "延長されました"
                ]
                
                has_success = any(success in html for success in success_keywords)
                
                if has_success:
                    logger.info("🎉 续期成功！")
                    self.renewal_status = "Success"
                    await self.get_expiry()
                    self.new_expiry_time = self.old_expiry_time
                    return True

                logger.warning(f"⚠️ 续期提交结果未知 (尝试 {attempt}/{max_attempts})")
                
                if attempt < max_attempts:
                    logger.info("🔁 结果未知，尝试重新提交...")
                    await asyncio.sleep(3)
                    continue

                self.renewal_status = "Unknown"
                return False

        except Exception as e:
            logger.error(f"❌ 续期错误: {e}")
            self.renewal_status = "Failed"
            self.error_message = str(e)
            return False

    async def run(self):
        try:
            logger.info("=" * 60)
            logger.info("🚀 XServer VPS 自动续期开始")
            logger.info("=" * 60)

            # 1. 启动浏览器
            if not await self.setup_browser():
                self.renewal_status = "Failed"
                self.generate_readme()
                await Notifier.notify("❌ 续期失败", f"浏览器初始化失败: {self.error_message}")
                return

            # 2. 登录
            if not await self.login():
                self.renewal_status = "Failed"
                self.generate_readme()
                await Notifier.notify("❌ 续期失败", f"登录失败: {self.error_message}")
                return

            # 3. 获取当前到期时间
            await self.get_expiry()

            # 3.5 自动判断是否已经续期
            try:
                if self.old_expiry_time:
                    today_jst = datetime.datetime.now(timezone(timedelta(hours=9))).date()
                    expiry_date = datetime.datetime.strptime(
                        self.old_expiry_time, "%Y-%m-%d"
                    ).date()
                    can_extend_date = expiry_date - datetime.timedelta(days=1)

                    logger.info(f"📅 今日日期(JST): {today_jst}")
                    logger.info(f"📅 到期日期: {expiry_date}")
                    logger.info(f"📅 可续期开始日: {can_extend_date}")

                    if today_jst < can_extend_date:
                        logger.info("ℹ️ 当前 VPS 尚未到可续期时间，无需续期。")
                        self.renewal_status = "Unexpired"
                        self.error_message = None
                        self.save_cache()
                        self.generate_readme()
                        await Notifier.notify(
                            "ℹ️ 尚未到续期日",
                            f"当前利用期限: {self.old_expiry_time}\n"
                            f"可续期开始日: {can_extend_date}"
                        )
                        return
                    else:
                        logger.info("✅ 已达到可续期日期，继续执行续期流程...")
                else:
                    logger.warning("⚠️ 未获取到 old_expiry_time，跳过自动判断逻辑")
            except Exception as e:
                logger.error(f"❌ 自动判断是否需要续期失败: {e}")

            # 4. 进入详情页
            await self.page.goto(Config.DETAIL_URL, timeout=Config.WAIT_TIMEOUT)
            await asyncio.sleep(2)
            await self.click_update()
            await asyncio.sleep(3)

            # 5. 打开续期页面
            opened = await self.open_extend()
            if not opened and self.renewal_status == "Unexpired":
                self.generate_readme()
                await Notifier.notify("ℹ️ 尚未到期", f"当前到期时间: {self.old_expiry_time}")
                return
            elif not opened:
                self.renewal_status = "Failed"
                self.error_message = "无法打开续期页面"
                self.generate_readme()
                await Notifier.notify("❌ 续期失败", "无法打开续期页面")
                return

            # 6. 提交续期
            await self.submit_extend()

            # 7. 保存缓存 & README & 通知
            self.save_cache()
            self.generate_readme()

            if self.renewal_status == "Success":
                await Notifier.notify("✅ 续期成功", f"续期成功，新到期时间: {self.new_expiry_time}")
            elif self.renewal_status == "Unexpired":
                await Notifier.notify("ℹ️ 尚未到期", f"当前到期时间: {self.old_expiry_time}")
            else:
                await Notifier.notify("❌ 续期失败", f"错误信息: {self.error_message or '未知错误'}")

        finally:
            logger.info("=" * 60)
            logger.info(f"✅ 流程完成 - 状态: {self.renewal_status}")
            logger.info("=" * 60)
            try:
                if self.page:
                    await self.page.close()
                if self.context:
                    await self.context.close()
                if self.browser:
                    await self.browser.close()
                if self._pw:
                    await self._pw.stop()
                logger.info("🧹 浏览器已关闭")
            except Exception as e:
                logger.warning(f"关闭浏览器时出错: {e}")


async def main():
    runner = XServerVPSRenewal()
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())

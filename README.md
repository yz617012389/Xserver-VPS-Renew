# XServer VPS 自动续期状态

**运行时间**: `2025-12-12 15:21:09 (UTC+8)`<br>
**VPS ID**: `40130246`<br>

---

## ❌ 续期失败

- 🕛 **到期**: `未知`
- ⚠️ **错误**: BrowserType.launch: Target page, context or browser has been closed
Browser logs:

╔════════════════════════════════════════════════════════════════════════════════════════════════╗
║ Looks like you launched a headed browser without having a XServer running.                     ║
║ Set either 'headless: true' or use 'xvfb-run <your-playwright-app>' before running Playwright. ║
║                                                                                                ║
║ <3 Playwright Team                                                                             ║
╚════════════════════════════════════════════════════════════════════════════════════════════════╝
Call log:
  - <launching> /home/runner/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome --disable-field-trial-config --disable-background-networking --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-back-forward-cache --disable-breakpad --disable-client-side-phishing-detection --disable-component-extensions-with-background-pages --disable-component-update --no-default-browser-check --disable-default-apps --disable-dev-shm-usage --disable-extensions --disable-features=AcceptCHFrame,AvoidUnnecessaryBeforeUnloadCheckSync,DestroyProfileOnBrowserClose,DialMediaRouteProvider,GlobalMediaControls,HttpsUpgrades,LensOverlay,MediaRouter,PaintHolding,ThirdPartyStoragePartitioning,Translate,AutoDeElevate,RenderDocument,OptimizationHints --enable-features=CDPScreenshotNewSurface --allow-pre-commit-input --disable-hang-monitor --disable-ipc-flooding-protection --disable-popup-blocking --disable-prompt-on-repost --disable-renderer-backgrounding --force-color-profile=srgb --metrics-recording-only --no-first-run --password-store=basic --use-mock-keychain --no-service-autorun --export-tagged-pdf --disable-search-engine-choice-screen --unsafely-disable-devtools-self-xss-warnings --edge-skip-compat-layer-relaunch --enable-automation --disable-infobars --disable-search-engine-choice-screen --disable-sync --no-sandbox --no-sandbox --disable-dev-shm-usage --disable-blink-features=AutomationControlled --disable-web-security --disable-features=IsolateOrigins,site-per-process --disable-infobars --start-maximized --user-data-dir=/tmp/playwright_chromiumdev_profile-SzmFY8 --remote-debugging-pipe --no-startup-window
  - <launched> pid=3432
  - [pid=3432][err] [3432:3432:1212/072109.940228:ERROR:ui/ozone/platform/x11/ozone_platform_x11.cc:259] Missing X server or $DISPLAY
  - [pid=3432][err] [3432:3432:1212/072109.940263:ERROR:ui/aura/env.cc:257] The platform failed to initialize.  Exiting.
  - [pid=3432] <gracefully close start>
  - [pid=3432] <kill>
  - [pid=3432] <will force kill>
  - [pid=3432] <process did exit: exitCode=1, signal=null>
  - [pid=3432] starting temporary directories cleanup
  - [pid=3432] finished temporary directories cleanup
  - [pid=3432] <gracefully close end>


---

*最后更新: 2025-12-12 15:21:09*

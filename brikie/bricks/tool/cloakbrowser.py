#!/usr/bin/env python3
"""CloakBrowser Tool Brick — Stealth browser automation via Playwright + CloakBinary.

Tools:
    - browser_navigate: Navigate to a URL.
    - browser_extract: Extract text content from the DOM.
    - browser_click: Click an element by CSS selector.
    - browser_evaluate: Execute JavaScript on the current page.

Requires:
    - Playwright (`pip install playwright`)
    - CloakBrowser binary (`npm install -g cloakbrowser` or pip/cloakbrowser binary)

Environment Variables:
    AGENT_BROWSER_EXECUTABLE_PATH: Path to the CloakBrowser Chromium binary.
                                    Falls back to ~/.cloakbrowser/.

References:
    - CloakBrowser: https://github.com/CloakHQ/CloakBrowser
    - CloakBrowser npm: https://www.npmjs.com/package/cloakbrowser
    - Design spec: design.md (Phase 2, Step 2.1-2.4)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List

from brikie.bricks.tool.base import ToolBrick

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CloakBrowser binary paths
# ---------------------------------------------------------------------------

# Known CloakBrowser version directories in the cache
_CLOAKBROWSER_CACHE_DIR = Path.home() / ".cloakbrowser"
_CLOAKBROWSER_VERSIONS = [
    "146.0.7680.177.4",
    "146.0.7680.177.3",
    "146.0.7680.177.5",
    "146.0.7680.177.2",
    "146.0.7680.177.1",
]

# Stealth args for CloakBrowser Chromium
STEALTH_ARGS = [
    "--disable-blink-features=AutomationTriggered",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
    "--window-size=1920,1080",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--lang=en-US",
    "--language=en-US",
    "--disable-animations",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-gpu",
    "--force-color-profile=srgb",
    "--disable-ipc-flooding-protection",
    "--disable-2d-canvas-clip-antialiasing",
]


def find_cloakbrowser_binary() -> str | None:
    """Find the CloakBrowser binary path.

    Checks AGENT_BROWSER_EXECUTABLE_PATH env var first, then falls back
    to the npm package's cache directory.

    Returns:
        The path to the CloakBrowser binary, or None if not found.
    """
    import os

    env_path = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH")
    if env_path:
        return env_path

    for version in _CLOAKBROWSER_VERSIONS:
        binary = _CLOAKBROWSER_CACHE_DIR / f"chromium-{version}" / "chrome"
        if binary.exists():
            logger.info("Found CloakBrowser binary: %s", binary)
            return str(binary)

    # Also check for any chromium- directory with chrome binary
    if _CLOAKBROWSER_CACHE_DIR.exists():
        for item in _CLOAKBROWSER_CACHE_DIR.glob("chromium-*/chrome"):
            logger.info("Found CloakBrowser binary: %s", item)
            return str(item)

    return None


# ---------------------------------------------------------------------------
# CloakBrowser Brick
# ---------------------------------------------------------------------------

class CloakBrowserBrick(ToolBrick):
    BRICK_NUMBER = "BRK-420"
    """Browser automation tool using Playwright + CloakBrowser binary.

    Exposes browser_navigate, browser_extract, browser_click, and
    browser_evaluate as tool calls.
    """

    tools: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "browser_navigate",
                "description": "Navigate the browser to a URL and wait for it to load.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to navigate to (e.g., 'https://example.com').",
                        },
                        "wait_until": {
                            "type": "string",
                            "description": "When to consider navigation complete: 'load', 'domcontentloaded', 'networkidle'.",
                            "default": "domcontentloaded",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in milliseconds (default: 15000).",
                            "default": 15000,
                        },
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_extract",
                "description": "Extract text content from the current page. Use 'selector' to target a specific element.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "CSS selector to target a specific element (e.g., '#main', '.title'). Defaults to the entire body.",
                        },
                        "mode": {
                            "type": "string",
                            "description": "Extraction mode: 'text' (default), 'html', or 'json'.",
                            "enum": ["text", "html", "json"],
                            "default": "text",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_click",
                "description": "Click an element on the page by its CSS selector.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for the element to click (e.g., '#submit-btn', '.link a').",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in milliseconds (default: 5000).",
                            "default": 5000,
                        },
                    },
                    "required": ["selector"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_evaluate",
                "description": "Execute arbitrary JavaScript on the current page and return the result.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "JavaScript expression to evaluate (e.g., 'document.title', 'window.location.href').",
                        },
                    },
                    "required": ["expression"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_back",
                "description": "Navigate back in the browser history.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in milliseconds (default: 5000).",
                            "default": 5000,
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_forward",
                "description": "Navigate forward in the browser history.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in milliseconds (default: 5000).",
                            "default": 5000,
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_screenshot",
                "description": "Capture a screenshot of the current page.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Output file path (default: '.screenshot.png').",
                            "default": ".screenshot.png",
                        },
                        "full_page": {
                            "type": "boolean",
                            "description": "Capture the full scrollable page.",
                            "default": True,
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_state",
                "description": "Get the current page metadata (URL, title, dimensions).",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_type",
                "description": "Type text into an input element.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for the input element.",
                        },
                        "text": {
                            "type": "string",
                            "description": "Text to type.",
                        },
                    },
                    "required": ["selector", "text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_scroll",
                "description": "Scroll the page or a specific element.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "CSS selector to scroll (default: 'body').",
                            "default": "body",
                        },
                        "direction": {
                            "type": "string",
                            "description": "Direction: 'up' or 'down'.",
                            "default": "down",
                        },
                        "amount": {
                            "type": "integer",
                            "description": "Pixels to scroll (default: 300).",
                            "default": 300,
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_wait",
                "description": "Wait for a condition or fixed duration.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "duration": {
                            "type": "integer",
                            "description": "Duration in milliseconds to wait.",
                            "default": 2000,
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector to wait for (optional).",
                        },
                    },
                },
            },
        },
    ]

    def __init__(self) -> None:
        super().__init__()
        self._name = "cloakbrowser"
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None

    async def init(self) -> None:
        """Lightweight init — the browser launches lazily on first use.

        Launching Chromium at warm-up would slow every boot and break the
        Baseplate when no browser binary is present; the agent may never
        call a browser tool in a session.
        """
        binary_path = find_cloakbrowser_binary()
        if binary_path:
            logger.info("CloakBrowser binary found: %s", binary_path)
        else:
            logger.info("No CloakBrowser binary cached — will fall back to Chromium on first use.")
        await super().init()

    async def _launch_browser(self) -> None:
        """Start Playwright and launch the stealth browser."""
        binary_path = find_cloakbrowser_binary()

        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()

            launch_kwargs: Dict[str, Any] = {
                "headless": True,
                "args": STEALTH_ARGS,
            }

            if binary_path:
                launch_kwargs["executable_path"] = binary_path
                logger.info("Launching with CloakBrowser: %s", binary_path)
            else:
                logger.warning("No CloakBrowser binary found, using default Chromium")

            # Launch browser context with stealth settings
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            context = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                bypass_csp=True,
                java_script_enabled=True,
            )

            self._page = await context.new_page()

            # Inject stealth scripts to hide webdriver
            await self._page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {} };
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [
                        {name: 'Chromium PDF Viewer'},
                        {name: 'Portable Document Format'},
                        {name: 'Chrome PDF Viewer'},
                        {name: 'PDF Viewer'},
                    ],
                });
            """)

            logger.info("CloakBrowser launched successfully")

        except Exception as exc:
            logger.error("CloakBrowser launch failed: %s", exc, exc_info=True)
            raise

    async def _ensure_browser(self) -> None:
        """Launch the browser on first tool use."""
        if self._page is None:
            await self._launch_browser()

    async def shutdown(self) -> None:
        """Gracefully close the browser."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception as exc:
                logger.warning("Browser close error: %s", exc)
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.warning("Playwright stop error: %s", exc)
        self._page = None
        self._browser = None

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        """Execute a browser tool by name.

        Tools:
            - browser_navigate: Navigate to URL
            - browser_extract: Extract DOM text
            - browser_click: Click element by CSS selector
            - browser_evaluate: Execute JS
            - browser_back: Navigate back
            - browser_forward: Navigate forward
            - browser_screenshot: Capture screenshot
            - browser_state: Get page metadata
            - browser_type: Type text into element
            - browser_scroll: Scroll page
            - browser_wait: Wait for condition
        """
        try:
            await self._ensure_browser()
        except Exception as exc:
            return {
                "success": False,
                "error": f"Browser unavailable: {exc}",
            }

        if name == "browser_navigate":
            return await self._navigate(args)
        elif name == "browser_extract":
            return await self._extract(args)
        elif name == "browser_click":
            return await self._click(args)
        elif name == "browser_evaluate":
            return await self._evaluate(args)
        elif name == "browser_back":
            return await self._back(args)
        elif name == "browser_forward":
            return await self._forward(args)
        elif name == "browser_screenshot":
            return await self._screenshot(args)
        elif name == "browser_state":
            return await self._get_page_state(args)
        elif name == "browser_type":
            return await self._type(args)
        elif name == "browser_scroll":
            return await self._scroll(args)
        elif name == "browser_wait":
            return await self._wait(args)
        else:
            raise KeyError(f"Unknown browser tool: {name}")

    async def _navigate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Navigate to a URL."""
        url = args.get("url")
        wait_until = args.get("wait_until", "domcontentloaded")
        timeout = args.get("timeout", 15000)

        try:
            await self._page.goto(url, wait_until=wait_until, timeout=timeout)
            title = await self._page.title()
            return {"success": True, "url": self._page.url, "title": title}
        except Exception as exc:
            logger.warning("Navigate failed: %s", exc)
            return {"success": False, "error": str(exc), "url": url}

    async def _extract(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Extract text content from the DOM."""
        selector = args.get("selector")
        mode = args.get("mode", "text")

        try:
            if mode == "json":
                value = await self._page.evaluate("""
                    document.querySelector('pre, code, .json')?.textContent ||
                    JSON.stringify(Array.from(document.querySelectorAll('table')).map(
                        t => Array.from(t.querySelectorAll('tr')).map(
                            r => Array.from(r.querySelectorAll('td, th')).map(c => c.textContent.trim())
                        )
                    )
                )""")
            elif selector:
                el = await self._page.query_selector(selector)
                if mode == "html":
                    value = await el.inner_html() if el else ""
                else:
                    value = await el.inner_text() if el else ""
            elif mode == "html":
                value = await self._page.content()
            else:
                value = await self._page.evaluate("""
                    (() => {
                        const el = document.body;
                        const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                        const texts = [];
                        let node;
                        while (walker.nextNode()) {
                            node = walker.currentNode;
                            if (node.parentElement &&
                                node.parentElement.nodeName !== 'SCRIPT' &&
                                node.parentElement.nodeName !== 'STYLE' &&
                                node.parentElement.nodeName !== 'NOSCRIPT') {
                                texts.push(node.textContent.trim());
                            }
                        }
                        return texts.filter(t => t).join('\\n');
                    })();
                """)
            return {"success": True, "content": value, "url": self._page.url}
        except Exception as exc:
            logger.warning("Extract failed: %s", exc)
            return {"success": False, "error": str(exc), "url": self._page.url}

    async def _click(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Click an element by CSS selector."""
        selector = args.get("selector", "body")
        timeout = args.get("timeout", 5000)

        try:
            el = await self._page.query_selector(selector)
            if not el:
                return {"success": False, "error": f"No element found: {selector}"}

            await el.scroll_into_view_if_needed()
            await el.click(timeout=timeout)
            title = await self._page.title()
            return {"success": True, "url": self._page.url, "title": title}
        except Exception as exc:
            logger.warning("Click failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def _evaluate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute JavaScript on the current page."""
        expression = args.get("expression", "1")

        try:
            result = await self._page.evaluate(expression)
            return {"success": True, "result": result, "url": self._page.url}
        except Exception as exc:
            logger.warning("Evaluate failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def _back(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Navigate back."""
        timeout = args.get("timeout", 5000)
        try:
            await self._page.go_back(timeout=timeout)
            return {"success": True, "url": self._page.url, "title": await self._page.title()}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def _forward(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Navigate forward."""
        timeout = args.get("timeout", 5000)
        try:
            await self._page.go_forward(timeout=timeout)
            return {"success": True, "url": self._page.url, "title": await self._page.title()}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def _screenshot(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Capture a screenshot of the current page."""
        path = args.get("path", ".screenshot.png")
        full_page = args.get("full_page", True)

        try:
            from pathlib import Path
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            await self._page.screenshot(path=path, full_page=full_page)
            return {"success": True, "path": path, "url": self._page.url}
        except Exception as exc:
            logger.warning("Screenshot failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def _get_page_state(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return current page metadata."""
        try:
            info = {
                "url": self._page.url,
                "title": await self._page.title(),
                "dimensions": self._page.viewport_size,
            }
            return {"success": True, "info": info}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def _type(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Type text into an input element."""
        selector = args.get("selector")
        text = args.get("text")

        try:
            el = await self._page.query_selector(selector)
            if not el:
                return {"success": False, "error": f"No element found: {selector}"}

            await el.fill(text)
            return {"success": True, "url": self._page.url, "title": await self._page.title()}
        except Exception as exc:
            logger.warning("Type failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def _scroll(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Scroll the page or a specific element."""
        selector = args.get("selector", "body")
        direction = args.get("direction", "down")
        amount = args.get("amount", 300)

        try:
            if direction == "down":
                await self._page.evaluate(f"document.querySelector('{selector}').scrollBy(0, {amount})")
            else:
                await self._page.evaluate(f"document.querySelector('{selector}').scrollBy(0, -{amount})")
            return {"success": True, "url": self._page.url, "title": await self._page.title()}
        except Exception as exc:
            logger.warning("Scroll failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def _wait(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Wait for a condition or fixed duration."""
        duration = args.get("duration", 2000)
        selector = args.get("selector")

        try:
            if selector:
                await self._page.wait_for_selector(selector, timeout=duration)
            else:
                await asyncio.sleep(duration / 1000)
            return {"success": True, "url": self._page.url, "title": await self._page.title()}
        except Exception as exc:
            logger.warning("Wait failed: %s", exc)
            return {"success": False, "error": str(exc)}

#!/usr/bin/env python3
"""
Plumbing/HVAC Website Submission Agent - submit partnership messages through
prospect websites directly.

Instead of cold email, navigate to the prospect's own website, find the contact
page, fill the form, solve any CAPTCHA via CapSolver, and submit. The prospect
sees it as an inbound inquiry — not spam.

Usage:
    # Single website
    python -X utf8 website-submission-agent/execution/plumbing_website_submission_agent.py \
        --url "https://www.comfortsquadair.com/" \
        --target-company "Comfort Squad Heating & Cooling" \
        --location "Sugar Land, TX" \
        --niche "HVAC contractor" \
        --dry-run

    # Dry run (detect form but don't submit)
    python -X utf8 website-submission-agent/execution/plumbing_website_submission_agent.py \
        --batch website-submission-agent/data/sample-plumbing-companies.csv --dry-run

    # Batch from CSV or JSON
    python -X utf8 website-submission-agent/execution/plumbing_website_submission_agent.py \
        --batch website-submission-agent/data/sample-plumbing-companies.csv --limit 5
"""

import asyncio
import argparse
import base64
import csv
import json
import os
import re
import sys
import concurrent.futures
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
load_dotenv(PROJECT_DIR / ".env", override=False)
LOG_FILE = PROJECT_DIR / ".tmp" / "plumbing_website_submission_log.json"
SCREENSHOT_DIR = PROJECT_DIR / ".tmp" / "plumbing_website_submission_screenshots"
REVIEW_SCREENSHOT_DIR = PROJECT_DIR / "review-screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
REVIEW_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
(PROJECT_DIR / ".tmp").mkdir(parents=True, exist_ok=True)

# Contact page URL patterns to try
CONTACT_PATHS = [
    "/contact", "/contact-us", "/contact-us/", "/contact/",
    "/get-in-touch", "/reach-us", "/inquiry", "/request-service",
    "/schedule-service", "/book-online", "/quote", "/free-estimate",
    "/estimate", "/request-a-quote", "/service-request",
    "/about/contact", "/about-us/contact",
    "/#contact", "/contactus",
]

DEFAULT_SENDER_NAME = os.environ.get("WEBSITE_SUBMISSION_SENDER_NAME", "")
DEFAULT_SENDER_EMAIL = os.environ.get("WEBSITE_SUBMISSION_SENDER_EMAIL", "")
DEFAULT_SENDER_PHONE = os.environ.get("WEBSITE_SUBMISSION_SENDER_PHONE", "")


def load_log():
    if LOG_FILE.exists():
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_log(log):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def normalize_website(url):
    if not url:
        return ""
    url = url.strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def lead_value(lead, *keys, default=""):
    for key in keys:
        value = lead.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def infer_niche(lead):
    text = " ".join([
        lead_value(lead, "niche", "categoryName"),
        lead_value(lead, "categories"),
        lead_value(lead, "notes"),
    ]).lower()
    if "plumb" in text or "drain" in text or "water heater" in text:
        return "plumbing"
    if "hvac" in text or "air conditioning" in text or "heating" in text:
        return "HVAC"
    return lead_value(lead, "niche", "categoryName", "categories", default="home services")


def generate_partnership_message(lead):
    company = lead_value(lead, "company_name", "company", "name", default="your team")
    city = lead_value(lead, "city")
    state = lead_value(lead, "state")
    location = lead_value(lead, "location", default=", ".join(x for x in [city, state] if x))
    niche = infer_niche(lead)
    notes = lead_value(lead, "notes", default="")

    local_phrase = f" in {location}" if location else ""
    service_phrase = "plumbing/HVAC" if "home services" in niche.lower() else niche
    angle = "water damage referrals"
    if "hvac" in niche.lower() and "plumb" not in niche.lower():
        angle = "water-damage referrals from AC leaks, drain line backups, and related service calls"

    note_sentence = f" I noticed {notes.rstrip('.')}." if notes else ""

    return (
        f"Hi {company} team, I came across {company} while looking at local {service_phrase} companies"
        f"{local_phrase}.{note_sentence} We help mitigation and restoration partners build referral relationships "
        f"with local service companies so {angle} do not get missed when they show up during normal service calls. "
        "A lot of the larger companies already have partnerships like this in place because it can increase revenue "
        "per service call without adding more ad spend. If it is relevant, would you be open to a quick conversation "
        "this week to see whether a simple mitigation partnership could make sense locally?"
    )


def normalize_lead(raw):
    company = lead_value(raw, "company_name", "company", "name")
    city = lead_value(raw, "city")
    state = lead_value(raw, "state")
    location = lead_value(raw, "location", default=", ".join(x for x in [city, state] if x))
    lead = dict(raw)
    lead["company_name"] = company
    lead["website"] = normalize_website(lead_value(raw, "website", "url"))
    lead["location"] = location
    lead["niche"] = lead_value(raw, "niche", "categoryName", "categories")
    lead["message"] = lead_value(raw, "message", default=generate_partnership_message(lead))
    return lead


def load_leads(path):
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return [normalize_lead(row) for row in csv.DictReader(f)]
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [normalize_lead(row) for row in data]


def validate_sender_config(name, email):
    missing = []
    if not name.strip():
        missing.append("--name or WEBSITE_SUBMISSION_SENDER_NAME")
    if not email.strip():
        missing.append("--email or WEBSITE_SUBMISSION_SENDER_EMAIL")
    if missing:
        raise SystemExit(
            "Missing required sender config: "
            + ", ".join(missing)
            + ". Set values in website-submission-agent/.env or pass CLI flags."
        )


async def set_field_value(page, selector, value):
    """Use the browser-native value setter first; fallback to Playwright fill."""
    return await page.evaluate("""(args) => {
        const [selector, val] = args;
        const el = document.querySelector(selector);
        if (!el) return false;
        el.scrollIntoView({ behavior: 'instant', block: 'center' });
        el.focus();
        const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
        if (setter) setter.call(el, val);
        else el.value = val;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        return el.value === val;
    }""", [selector, value])


async def find_contact_page(page, base_url):
    """Find the contact page by reading the site's actual navigation."""
    # First check if current page already has a contact form
    has_form = await page.evaluate("""() => {
        const forms = document.querySelectorAll('form');
        const inputs = document.querySelectorAll('input[type="text"], input[type="email"], textarea');
        return forms.length > 0 && inputs.length > 1;
    }""")
    if has_form:
        print(f"    [NAV] Homepage already has a contact form")
        return page.url

    # Read ALL navigation links from the page — nav, header, footer, menus
    all_nav_links = await page.evaluate(r"""() => {
        const results = [];
        const seen = new Set();

        // Priority 1: nav/header/menu links (main navigation)
        const navAreas = document.querySelectorAll('nav, header, [role="navigation"], .menu, .nav, .navbar, #menu, #nav, #navigation');
        for (const area of navAreas) {
            for (const a of area.querySelectorAll('a[href]')) {
                const href = a.href;
                if (seen.has(href) || !href || href === '#' || href.startsWith('javascript:') || href.startsWith('tel:')) continue;
                seen.add(href);
                const text = (a.textContent || '').trim().replace(/\s+/g, ' ').substring(0, 60);
                if (text.length > 0) {
                    results.push({ href, text, source: 'nav', priority: 1 });
                }
            }
        }

        // Priority 2: footer links
        const footerAreas = document.querySelectorAll('footer, .footer, #footer, [role="contentinfo"]');
        for (const area of footerAreas) {
            for (const a of area.querySelectorAll('a[href]')) {
                const href = a.href;
                if (seen.has(href) || !href || href === '#' || href.startsWith('javascript:') || href.startsWith('tel:')) continue;
                seen.add(href);
                const text = (a.textContent || '').trim().replace(/\s+/g, ' ').substring(0, 60);
                if (text.length > 0) {
                    results.push({ href, text, source: 'footer', priority: 2 });
                }
            }
        }

        // Priority 3: all remaining page links
        for (const a of document.querySelectorAll('a[href]')) {
            const href = a.href;
            if (seen.has(href) || !href || href === '#' || href.startsWith('javascript:') || href.startsWith('tel:')) continue;
            seen.add(href);
            const text = (a.textContent || '').trim().replace(/\s+/g, ' ').substring(0, 60);
            if (text.length > 0) {
                results.push({ href, text, source: 'body', priority: 3 });
            }
        }

        return results;
    }""")

    print(f"    [NAV] Found {len(all_nav_links)} links on page")

    # Score each link for contact-page likelihood
    contact_keywords = ['contact', 'kontakt', 'get in touch', 'reach us', 'reach out',
                        'send message', 'write to us', 'inquiry', 'enquiry',
                        'schedule', 'consultation', 'book a call', 'free consultation']
    # These are NOT contact pages
    exclude_keywords = ['contact lens', 'login', 'sign in', 'cart', 'shop', 'blog',
                        'facebook', 'twitter', 'instagram', 'linkedin', 'youtube',
                        'privacy', 'terms', 'sitemap']

    scored = []
    for link in all_nav_links:
        text = link["text"].lower()
        href = link["href"].lower()

        # Skip mailto, external social, excluded
        if "mailto:" in href:
            continue
        if any(ex in text for ex in exclude_keywords):
            continue

        score = 0
        # Exact text match "Contact" or "Contact Us" in nav = highest score
        if re.match(r'^contact(\s+us)?$', text.strip()):
            score = 100
        elif any(kw in text for kw in contact_keywords):
            score = 80
        elif any(kw in href for kw in ['/contact', '/get-in-touch', '/reach-us', '/inquiry']):
            score = 60
        # "About" pages sometimes have contact forms
        elif 'about' in text and 'contact' in href:
            score = 40

        # Boost for nav/header location
        if link["source"] == "nav":
            score += 10
        elif link["source"] == "footer":
            score += 5

        if score > 0:
            scored.append((score, link))

    # Sort by score descending
    scored.sort(key=lambda x: -x[0])

    if scored:
        # Show top candidates
        for s, l in scored[:3]:
            print(f"    [NAV] Candidate (score {s}): \"{l['text']}\" → {l['href'][:70]} [{l['source']}]")

        best = scored[0][1]
        print(f"    [NAV] Selected: \"{best['text']}\" → {best['href'][:80]}")
        return best["href"]

    # Last resort: show what nav links we DID find so we can debug
    nav_only = [l for l in all_nav_links if l["source"] == "nav"]
    if nav_only:
        print(f"    [NAV] No contact link found. Nav links seen:")
        for l in nav_only[:10]:
            print(f"           \"{l['text']}\" → {l['href'][:60]}")

    print(f"    [NAV] No contact link found; trying common contact/quote paths")
    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    for path in CONTACT_PATHS:
        candidate = urljoin(base, path)
        try:
            await page.goto(candidate, wait_until="domcontentloaded", timeout=12000)
            await page.wait_for_timeout(1200)
            has_form = await page.evaluate("""() => {
                const forms = document.querySelectorAll('form');
                const inputs = document.querySelectorAll('input[type="text"], input[type="email"], input[type="tel"], textarea');
                return forms.length > 0 && inputs.length > 1;
            }""")
            if has_form:
                print(f"    [NAV] Common path has form: {candidate}")
                return candidate
        except Exception:
            continue

    print(f"    [NAV] No contact page found")
    return None


async def detect_captcha_type(page):
    """Detect what type of CAPTCHA is on the page."""
    return await page.evaluate(r"""() => {
        const bodyHtml = document.body.innerHTML;
        const body = bodyHtml.toLowerCase();
        const result = { type: 'none', site_key: null };

        function selectorFor(el) {
            if (!el) return null;
            if (el.id) return '#' + CSS.escape(el.id);
            if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
            const all = Array.from(document.querySelectorAll(el.tagName.toLowerCase()));
            const idx = all.indexOf(el);
            return `${el.tagName.toLowerCase()}:nth-of-type(${idx + 1})`;
        }

        function decodeRocketLazyScripts() {
            const decoded = [];
            for (const s of document.querySelectorAll('script[data-rocketlazyloadscript]')) {
                const val = s.getAttribute('data-rocketlazyloadscript') || '';
                const m = val.match(/^data:text\/javascript;base64,(.+)$/);
                if (m) {
                    try { decoded.push(atob(m[1])); } catch(e) {}
                }
            }
            return decoded;
        }

        // Custom image/text CAPTCHAs like "Enter the code from the image here".
        if (body.includes('enter the code from the image') ||
            body.includes('security code') ||
            body.includes('verification code') ||
            body.includes('captcha code')) {
            const images = Array.from(document.querySelectorAll('img')).filter(img => {
                const r = img.getBoundingClientRect();
                const src = (img.src || '').toLowerCase();
                const alt = (img.alt || '').toLowerCase();
                return r.width > 35 && r.height > 12 &&
                    (src.includes('captcha') || src.includes('security') || src.includes('code') ||
                     alt.includes('captcha') || alt.includes('security') || alt.includes('code') ||
                     body.includes('enter the code from the image'));
            });
            const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea')).filter(el => {
                const r = el.getBoundingClientRect();
                const ctx = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                return r.width > 30 && r.height > 10 &&
                    (ctx.includes('captcha') || ctx.includes('security') || ctx.includes('code') || !el.value);
            });
            if (images.length && inputs.length) {
                const img = images[images.length - 1];
                const imgRect = img.getBoundingClientRect();
                let bestInput = inputs[inputs.length - 1];
                let bestDist = Infinity;
                for (const input of inputs) {
                    const r = input.getBoundingClientRect();
                    const dist = Math.abs(r.top - imgRect.top) + Math.max(0, imgRect.left - r.left);
                    if (r.top >= imgRect.top - 80 && dist < bestDist) {
                        bestDist = dist;
                        bestInput = input;
                    }
                }
                result.type = 'custom_image';
                result.image_selector = selectorFor(img);
                result.input_selector = selectorFor(bestInput);
                return result;
            }
        }

        // reCAPTCHA v2 (visible checkbox OR invisible)
        const recapEl = document.querySelector('[data-sitekey]');
        if (recapEl) {
            const isInvisible = recapEl.getAttribute('data-size') === 'invisible' ||
                                recapEl.getAttribute('data-badge') !== null ||
                                recapEl.classList.contains('g-recaptcha-invisible');
            result.type = isInvisible ? 'recaptcha_v2_invisible' : 'recaptcha_v2';
            result.site_key = recapEl.getAttribute('data-sitekey');
            return result;
        }
        const recapIframe = document.querySelector('iframe[src*="recaptcha"]');
        if (recapIframe) {
            // Check if invisible by looking for badge or invisible param
            const isInvisible = recapIframe.src.includes('size=invisible') ||
                                document.querySelector('.grecaptcha-badge') !== null;
            result.type = isInvisible ? 'recaptcha_v2_invisible' : 'recaptcha_v2';
            const match = recapIframe.src.match(/[?&]k=([^&]+)/);
            if (match) result.site_key = match[1];
            return result;
        }
        // Check for invisible badge (no iframe visible yet, loads on submit)
        if (document.querySelector('.grecaptcha-badge')) {
            result.type = 'recaptcha_v2_invisible';
            // Find site key from script tags
            const scripts = document.querySelectorAll('script[src*="recaptcha"]');
            for (const s of scripts) {
                const m = s.src.match(/render=([^&]+)/);
                if (m && m[1] !== 'explicit') { result.site_key = m[1]; break; }
            }
            // Also check inline scripts for sitekey
            if (!result.site_key) {
                for (const s of document.querySelectorAll('script:not([src])')) {
                    const m = (s.textContent || '').match(/sitekey['":\s]+['"]([^'"]+)['"]/i);
                    if (m) { result.site_key = m[1]; break; }
                }
            }
            return result;
        }

        // reCAPTCHA v3 (invisible, loaded via render=SITEKEY)
        const v3Scripts = document.querySelectorAll('script[src*="recaptcha"][src*="render="]');
        if (v3Scripts.length > 0) {
            result.type = 'recaptcha_v3';
            for (const s of v3Scripts) {
                const m = s.src.match(/render=([^&]+)/);
                if (m && m[1] !== 'explicit') { result.site_key = m[1]; break; }
            }
            if (result.site_key) return result;
        }

        // WordPress plugins may lazy-load invisible reCAPTCHA config as base64.
        const lazyScripts = decodeRocketLazyScripts();
        for (const txt of lazyScripts) {
            if (txt.includes('grecaptcha.render') || txt.includes('grecaptcha.execute') || txt.includes('sitekey')) {
                result.type = txt.includes("'size': 'invisible'") || txt.includes('"size": "invisible"')
                    ? 'recaptcha_v2_invisible'
                    : 'recaptcha_v2';
                const m = txt.match(/['"]sitekey['"]\s*:\s*['"]([^'"]+)['"]/i);
                if (m) result.site_key = m[1];
                return result;
            }
        }

        // hCaptcha
        if (document.querySelector('.h-captcha') || body.includes('hcaptcha')) {
            result.type = 'hcaptcha';
            const hEl = document.querySelector('.h-captcha[data-sitekey]');
            if (hEl) result.site_key = hEl.getAttribute('data-sitekey');
            return result;
        }

        // Cloudflare Turnstile
        if (document.querySelector('.cf-turnstile') || body.includes('turnstile')) {
            result.type = 'turnstile';
            const tEl = document.querySelector('.cf-turnstile[data-sitekey]');
            if (tEl) result.site_key = tEl.getAttribute('data-sitekey');
            return result;
        }

        // Generic reCAPTCHA detection
        if (body.includes('recaptcha') || body.includes('g-recaptcha')) {
            result.type = 'recaptcha_v2';
            return result;
        }

        return result;
    }""")


async def solve_captcha(page, captcha_info):
    """Solve any detected CAPTCHA using CapSolver."""
    import capsolver

    api_key = os.environ.get("CAPSOLVER_API_KEY") or os.environ.get("Capsolver_API_KEY")
    if not api_key:
        print("    [CAPSOLVER] No CAPSOLVER_API_KEY in .env")
        return False

    capsolver.api_key = api_key
    captcha_type = captcha_info.get("type", "none")
    site_key = captcha_info.get("site_key")
    page_url = page.url

    if captcha_type == "none":
        return True  # No CAPTCHA to solve

    if captcha_type == "custom_image":
        image_selector = captcha_info.get("image_selector")
        input_selector = captcha_info.get("input_selector")
        if not image_selector or not input_selector:
            print("    [CAPSOLVER] Custom image CAPTCHA detected but image/input selector missing")
            return False
        print(f"    [CAPSOLVER] Custom image CAPTCHA detected; solving image...")
        try:
            image_bytes = await page.locator(image_selector).last.screenshot(timeout=10000)
            image_b64 = base64.b64encode(image_bytes).decode("ascii")

            def _solve_image():
                return capsolver.solve({
                    "type": "ImageToTextTask",
                    "body": image_b64,
                })

            loop = asyncio.get_event_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                solution = await loop.run_in_executor(pool, _solve_image)

            nested_solution = solution.get("solution") if isinstance(solution.get("solution"), dict) else {}
            text = (
                solution.get("text") or
                solution.get("answer") or
                solution.get("captcha") or
                nested_solution.get("text") or
                ""
            )
            text = (text or "").strip()
            if not text:
                print(f"    [CAPSOLVER] No text in image CAPTCHA response: {list(solution.keys())}")
                return False
            print(f"    [CAPSOLVER] Image CAPTCHA solved as '{text}'")
            success = await set_field_value(page, input_selector, text)
            if not success:
                await page.fill(input_selector, text, timeout=5000)
            await page.wait_for_timeout(800)
            return True
        except Exception as e:
            print(f"    [CAPSOLVER] Image CAPTCHA error: {str(e)[:200]}")
            return False

    if not site_key:
        print(f"    [CAPSOLVER] {captcha_type} detected but no site key found")
        return False

    print(f"    [CAPSOLVER] {captcha_type} detected | site key: {site_key[:20]}...")
    print(f"    [CAPSOLVER] Sending to CapSolver API...")

    try:
        # Map captcha type to CapSolver task type
        task_config = {"websiteURL": page_url, "websiteKey": site_key}

        if captcha_type == "recaptcha_v2":
            task_config["type"] = "ReCaptchaV2TaskProxyLess"
        elif captcha_type == "recaptcha_v2_invisible":
            task_config["type"] = "ReCaptchaV2TaskProxyLess"
            task_config["isInvisible"] = True
        elif captcha_type == "recaptcha_v3":
            task_config["type"] = "ReCaptchaV3TaskProxyLess"
            task_config["pageAction"] = "submit"
            task_config["minScore"] = 0.7
        elif captcha_type == "hcaptcha":
            task_config["type"] = "HCaptchaTaskProxyLess"
        elif captcha_type == "turnstile":
            task_config["type"] = "AntiTurnstileTaskProxyLess"
        else:
            print(f"    [CAPSOLVER] Unsupported type: {captcha_type}")
            return False

        # Run blocking solve in thread to keep Playwright alive
        def _solve():
            return capsolver.solve(task_config)

        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            solution = await loop.run_in_executor(pool, _solve)

        # Get the token from solution
        token = (solution.get("gRecaptchaResponse") or
                 solution.get("token") or
                 solution.get("captcha_response") or "")

        if not token:
            print(f"    [CAPSOLVER] No token in response: {list(solution.keys())}")
            return False

        print(f"    [CAPSOLVER] Got token ({len(token)} chars) — injecting...")

        # Inject token based on type
        if captcha_type.startswith("recaptcha"):
            injected = await page.evaluate("""(token) => {
                let count = 0;
                document.querySelectorAll('textarea[name="g-recaptcha-response"], #g-recaptcha-response').forEach(ta => {
                    ta.value = token; count++;
                });
                document.querySelectorAll('textarea[id*="recaptcha-response"]').forEach(ta => {
                    ta.value = token; count++;
                });
                try {
                    if (typeof ___grecaptcha_cfg !== 'undefined') {
                        const clients = ___grecaptcha_cfg.clients;
                        for (const cid in clients) {
                            const walk = (obj, d) => {
                                if (d > 5 || !obj) return;
                                for (const k in obj) {
                                    if (typeof obj[k] === 'function' && k === 'callback') obj[k](token);
                                    if (typeof obj[k] === 'object') walk(obj[k], d+1);
                                }
                            };
                            walk(clients[cid], 0);
                        }
                    }
                } catch(e) {}
                return count;
            }""", token)
        elif captcha_type == "hcaptcha":
            injected = await page.evaluate("""(token) => {
                let count = 0;
                document.querySelectorAll('textarea[name="h-captcha-response"], [name="g-recaptcha-response"]').forEach(ta => {
                    ta.value = token; count++;
                });
                return count;
            }""", token)
        elif captcha_type == "turnstile":
            injected = await page.evaluate("""(token) => {
                let count = 0;
                document.querySelectorAll('[name="cf-turnstile-response"], input[name="turnstileToken"]').forEach(el => {
                    el.value = token; count++;
                });
                return count;
            }""", token)
        else:
            injected = 0

        print(f"    [CAPSOLVER] Token injected into {injected} field(s)")
        await page.wait_for_timeout(1000)
        return True

    except Exception as e:
        print(f"    [CAPSOLVER] Error: {str(e)[:200]}")
        return False


async def detect_form_fields(page):
    """Detect contact form fields on the page.

    IMPORTANT: When multiple forms exist (e.g. contact form + newsletter subscribe),
    we must identify the CONTACT form (the one with the most fields / has a textarea)
    and scope all field detection to that specific <form> element. This prevents
    newsletter email fields from overwriting the contact form email field.
    """
    return await page.evaluate(r"""() => {
        const result = {
            has_form: false,
            has_mailto: false,
            mailto_email: null,
            fields: {},
        };

        // Mailto check
        const mailtoLinks = document.querySelectorAll('a[href^="mailto:"]');
        if (mailtoLinks.length > 0) {
            result.has_mailto = true;
            result.mailto_email = mailtoLinks[0].href.replace('mailto:', '').split('?')[0];
        }

        const allForms = document.querySelectorAll('form');
        if (allForms.length === 0) {
            const inputs = document.querySelectorAll('input[type="text"], input[type="email"], textarea');
            if (inputs.length < 2) return result;
        }

        // CRITICAL: Find the CONTACT form, not the newsletter/subscribe form.
        // The contact form is the one with the most input fields and/or a textarea.
        // Newsletter forms typically have just 1 email field.
        let contactForm = null;
        let bestScore = 0;

        for (const form of allForms) {
            const inputs = form.querySelectorAll('input:not([type="submit"]):not([type="button"]):not([type="hidden"]):not([type="checkbox"]):not([type="radio"])');
            const textareas = form.querySelectorAll('textarea');
            const visibleInputs = Array.from(inputs).filter(el => el.offsetHeight > 0 || el.offsetWidth > 0);

            // Score: number of visible inputs + 5 bonus for having a textarea (strong contact form signal)
            let score = visibleInputs.length + (textareas.length > 0 ? 5 : 0);

            // Penalty for forms inside footer (likely newsletter)
            if (form.closest('footer, .footer, #footer, [role="contentinfo"]')) {
                score -= 10;
            }

            if (score > bestScore) {
                bestScore = score;
                contactForm = form;
            }
        }

        // If no form found with good score, fall back to first form with 2+ inputs
        if (!contactForm && allForms.length > 0) {
            contactForm = allForms[0];
        }

        if (!contactForm) return result;

        result.has_form = true;
        const formIndex = Array.from(allForms).indexOf(contactForm);
        result.form_index = formIndex;
        result.total_forms = allForms.length;

        // Helper: check if a field is required
        function isRequired(el) {
            if (el.required || el.getAttribute('aria-required') === 'true') return true;
            const labelEl = el.closest('label') || document.querySelector(`label[for="${el.id}"]`);
            if (labelEl && (labelEl.textContent || '').includes('*')) return true;
            if ((el.placeholder || '').includes('*')) return true;
            const parent = el.parentElement;
            if (parent) {
                const asterisk = parent.querySelector('.required, .asterisk, abbr[title="required"]');
                if (asterisk) return true;
                const prevSib = el.previousElementSibling;
                if (prevSib && (prevSib.textContent || '').includes('*')) return true;
            }
            return false;
        }

        // Scan inputs and selects ONLY within the identified contact form
        for (const el of contactForm.querySelectorAll('input, select')) {
            const inputType = (el.type || 'text').toLowerCase();
            if (['submit', 'button', 'hidden', 'checkbox', 'radio', 'file', 'image', 'reset'].includes(inputType)) continue;
            if (el.offsetHeight === 0 && el.offsetWidth === 0) continue;

            const n = (el.name || '').toLowerCase();
            const id = (el.id || '').toLowerCase();
            const ph = (el.placeholder || '').toLowerCase();
            const labelEl = el.closest('label') || document.querySelector(`label[for="${el.id}"]`);
            const label = (labelEl?.textContent || '').toLowerCase();
            const prevLabel = el.previousElementSibling?.textContent?.toLowerCase() || '';
            const parentText = el.parentElement?.textContent?.toLowerCase().trim() || '';
            const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
            const ctx = n + ' ' + id + ' ' + ph + ' ' + label + ' ' + prevLabel + ' ' + parentText + ' ' + ariaLabel;
            const looksInternal = ctx.includes('company email for contacts') || ctx.includes('for contacts') || ctx.includes('routing');
            const sel = el.id ? '#' + CSS.escape(el.id) : (el.name ? `[name="${CSS.escape(el.name)}"]` : null);
            if (!sel) continue;
            const req = isRequired(el);
            const isSelectEl = el.tagName === 'SELECT';
            let options = null;
            if (isSelectEl) {
                options = Array.from(el.options).map(o => ({ value: o.value, text: o.textContent.trim() })).filter(o => o.value);
            }

            if ((ctx.includes('first') && (ctx.includes('name') || label.trim() === 'first' || parentText === 'first')) ||
                ctx.includes('fname') || ctx.includes('first_name') || ctx.includes('vorname')) {
                result.fields.first_name = { selector: sel, required: req };
            } else if ((ctx.includes('last') && (ctx.includes('name') || label.trim() === 'last' || parentText === 'last')) ||
                ctx.includes('lname') || ctx.includes('last_name') || ctx.includes('nachname')) {
                result.fields.last_name = { selector: sel, required: req };
            } else if (ctx.includes('name') && !ctx.includes('email') && !ctx.includes('company') && !ctx.includes('user') && !result.fields.name && !result.fields.first_name) {
                result.fields.name = { selector: sel, required: req };
            }
            if (!looksInternal && (ctx.includes('email') || inputType === 'email') &&
                (!result.fields.email || req || !result.fields.email.required)) {
                result.fields.email = { selector: sel, required: req };
            }
            if (ctx.includes('phone') || ctx.includes('tel') || inputType === 'tel') {
                result.fields.phone = { selector: sel, required: req };
            }
            if (ctx.includes('subject') || ctx.includes('subj') || ctx.includes('betreff')) {
                result.fields.subject = { selector: sel, required: req };
            }
            if (!looksInternal && inputType !== 'email' &&
                (ctx.includes('company') || ctx.includes('organization') || ctx.includes('firma'))) {
                result.fields.company = { selector: sel, required: req };
            }
            if (ctx.includes('address') || ctx.includes('street') || ctx.includes('service address')) {
                result.fields.address = { selector: sel, required: req };
            }
            if (ctx.includes('city') || ctx.includes('town')) {
                result.fields.city = { selector: sel, required: req };
            }
            if (ctx.includes('state') || ctx.includes('province')) {
                result.fields.state = { selector: sel, required: req };
            }
            if (ctx.includes('zip') || ctx.includes('postal')) {
                result.fields.postal_code = { selector: sel, required: req };
            }
            if (isSelectEl && !result.fields[ctx.split(' ')[0]]) {
                const cleanLabel = (label || prevLabel || ph || n).replace('*', '').trim();
                if (cleanLabel && !['name','email','phone','subject','company'].some(k => ctx.includes(k))) {
                    result.fields['_select_' + n] = { selector: sel, required: req, type: 'select', label: cleanLabel, options };
                }
            }
        }

        // Find message textarea ONLY within the contact form
        for (const ta of contactForm.querySelectorAll('textarea')) {
            const taId = (ta.id || '').toLowerCase();
            const taName = (ta.name || '').toLowerCase();
            if (taId.includes('recaptcha') || taName.includes('recaptcha')) continue;
            if (taId.includes('captcha') || taName.includes('captcha')) continue;
            if (ta.offsetHeight === 0 && ta.offsetWidth === 0) continue;
            const sel = ta.id ? '#' + CSS.escape(ta.id) : (ta.name ? `textarea[name="${CSS.escape(ta.name)}"]` : 'textarea:not([name*="recaptcha"])');
            result.fields.message = { selector: sel, required: isRequired(ta) };
            break;
        }

        // Find submit button ONLY within the contact form
        const btns = contactForm.querySelectorAll('button[type="submit"], input[type="submit"], button:not([type])');
        for (const btn of btns) {
            const text = (btn.textContent || btn.value || '').toLowerCase();
            if (text.match(/send|submit|contact|get in touch|request|anfrage|absenden/)) {
                const sel = btn.id ? '#' + CSS.escape(btn.id) : null;
                result.fields.submit = { selector: sel || 'button[type="submit"], input[type="submit"]', text: (btn.textContent || btn.value || '').trim() };
                break;
            }
        }
        if (!result.fields.submit && btns.length > 0) {
            const btn = btns[0];
            const sel = btn.id ? '#' + CSS.escape(btn.id) : null;
            result.fields.submit = { selector: sel || 'button[type="submit"], input[type="submit"]', text: (btn.textContent || btn.value || '').trim() };
        }

        return result;
    }""")


async def fill_and_submit(page, name, email, message, subject="", phone="", company="",
                          address="", city="", state="", postal_code="",
                          dry_run=False, review_before_submit=False, review_path=None):
    """Fill form fields and submit. Intelligently handles required fields."""
    form_info = await detect_form_fields(page)
    captcha_info = await detect_captcha_type(page)

    total_forms = form_info.get('total_forms', '?')
    form_idx = form_info.get('form_index', '?')
    print(f"    [FORM] Has form: {form_info.get('has_form')} | Using form {form_idx}/{total_forms} | CAPTCHA: {captcha_info.get('type')}")

    if not form_info.get("has_form"):
        if form_info.get("has_mailto"):
            return {"status": "mailto_only", "email": form_info.get("mailto_email"),
                    "note": "No form — only mailto link found"}
        return {"status": "no_form", "note": "No contact form found"}

    fields = form_info.get("fields", {})

    # Show all detected fields with required status
    for fname, fdata in fields.items():
        if fname == "submit":
            continue
        req = "REQUIRED" if fdata.get("required") else "optional"
        extra = ""
        if fdata.get("type") == "select":
            opts = fdata.get("options", [])
            extra = f" [{len(opts)} options: {', '.join(o['text'][:20] for o in opts[:4])}...]"
        print(f"    [FORM]   {fname}: {req}{extra}")

    filled = []
    missing_required = []

    # Build ordered field values (matches visual tab order on most forms)
    fill_order = []
    if fields.get("first_name") and fields.get("last_name"):
        parts = name.split(" ", 1)
        fill_order.append(("first_name", parts[0]))
        fill_order.append(("last_name", parts[1] if len(parts) > 1 else ""))
    elif fields.get("name"):
        fill_order.append(("name", name))
    if fields.get("email"):
        fill_order.append(("email", email))
    if fields.get("phone"):
        fill_order.append(("phone", phone))
    if fields.get("subject"):
        fill_order.append(("subject", subject or "Business Inquiry"))
    if fields.get("company"):
        fill_order.append(("company", company))
    if fields.get("address"):
        fill_order.append(("address", address))
    if fields.get("city"):
        fill_order.append(("city", city))
    if fields.get("state"):
        fill_order.append(("state", state))
    if fields.get("postal_code"):
        fill_order.append(("postal_code", postal_code))
    if fields.get("message"):
        fill_order.append(("message", message))

    if not fill_order:
        return {
            "status": "no_fillable_fields",
            "fields_filled": [],
            "captcha_type": captcha_info.get("type", "none"),
            "missing_required": [],
            "note": "Detected a form container, but no fillable outreach fields were mapped. Submission skipped."
        }

    # Check for missing required fields
    fill_names = {n for n, _ in fill_order}
    for fname, fdata in fields.items():
        if fname == "submit":
            continue
        if fdata.get("required") and fname not in fill_names and not fname.startswith("_select_"):
            missing_required.append(fname)

    # React/Wix/custom forms: use the native browser value setter first so fields
    # do not clear each other during framework re-renders.
    for fname, value in fill_order:
        if not value:
            if fields.get(fname, {}).get("required"):
                missing_required.append(fname)
            continue

        sel = fields[fname]["selector"]
        try:
            success = await set_field_value(page, sel, value)
            if not success:
                await page.fill(sel, value, timeout=5000)
            filled.append(fname)
            print(f"    [FILL] {fname}: set ({len(value)} chars)")
            await page.wait_for_timeout(1200)
        except Exception as e:
            print(f"    [WARN] Could not fill {fname}: {str(e)[:100]}")

    # Handle select dropdowns (unknown fields like "Case Type", "How did you hear")
    for fname, fdata in fields.items():
        if fdata.get("type") != "select":
            continue
        options = fdata.get("options", [])
        label = fdata.get("label", fname)
        if not options:
            continue

        # Smart selection: pick the most generic/relevant option
        chosen = None
        for opt in options:
            text = opt["text"].lower()
            # Prefer generic options
            if any(kw in text for kw in ["other", "general", "inquiry", "business", "consultation", "referral", "website"]):
                chosen = opt
                break
        if not chosen:
            # Pick the first non-empty option (skip "Select..." / "Choose..." placeholders)
            for opt in options:
                if not opt["text"].lower().startswith(("select", "choose", "please", "--", "—")):
                    chosen = opt
                    break
        if not chosen and options:
            chosen = options[0]

        if chosen:
            try:
                await page.select_option(fdata["selector"], chosen["value"])
                filled.append(f"{label}={chosen['text']}")
                print(f"    [FORM] Selected '{chosen['text']}' for dropdown '{label}'")
                await page.wait_for_timeout(300)
            except Exception as e:
                print(f"    [WARN] Could not select {label}: {str(e)[:100]}")
                if fdata.get("required"):
                    missing_required.append(label)

    result = {
        "status": "filled",
        "fields_filled": filled,
        "captcha_type": captcha_info.get("type", "none"),
        "missing_required": missing_required,
    }

    # Check if we're missing any required fields
    if missing_required:
        print(f"    [FORM] MISSING REQUIRED FIELDS: {', '.join(missing_required)}")
        result["status"] = "missing_required"
        result["note"] = f"Cannot submit — missing required fields: {', '.join(missing_required)}"
        return result

    if dry_run:
        result["status"] = "dry_run"
        return result

    if review_before_submit:
        if review_path:
            await page.screenshot(path=str(review_path), full_page=False)
            result["review_screenshot"] = str(review_path)
        result["status"] = "review_pending"
        result["note"] = "Form filled and screenshot saved; not submitted because review-before-submit is enabled"
        return result

    # Solve CAPTCHA if present
    if captcha_info.get("type") != "none":
        if captcha_info.get("type") == "recaptcha_v3" and not captcha_info.get("site_key"):
            print("    [CAPTCHA] reCAPTCHA v3 detected without exposed site key; trying native site submit")
        else:
            print(f"    [CAPTCHA] Solving {captcha_info['type']}...")
            solved = await solve_captcha(page, captcha_info)
            if not solved:
                result["status"] = "captcha_failed"
                return result
            print(f"    [CAPTCHA] Solved!")

    # Submit
    if fields.get("submit"):
        try:
            sel = fields["submit"]["selector"]
            # Use .last to pick the visible one if multiple match
            btn = page.locator(sel).last
            await btn.scroll_into_view_if_needed(timeout=5000)
            await btn.click(timeout=10000)
            await page.wait_for_timeout(5000)
        except Exception as e:
            # Fallback: press Enter on the form
            try:
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(5000)
            except Exception:
                result["status"] = "submit_error"
                result["note"] = str(e)[:200]
                return result
    else:
        try:
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)
        except Exception:
            result["status"] = "no_submit_button"
            return result

    # Verify submission — look for confirmation message or validation errors
    verification = await page.evaluate(r"""() => {
        const body = document.body.innerText.toLowerCase();
        const html = document.body.innerHTML.toLowerCase();

        // Check for success confirmation messages
        const invalid = Array.from(document.querySelectorAll('input, textarea, select')).find(el => {
            try { return el.willValidate && !el.checkValidity(); } catch(e) { return false; }
        });
        if (invalid) {
            const labelEl = invalid.closest('label') || document.querySelector(`label[for="${invalid.id}"]`);
            const label = (labelEl?.textContent || invalid.placeholder || invalid.name || invalid.id || invalid.validationMessage || 'invalid required field').trim();
            return { confirmed: false, type: 'browser_validation', match: label.substring(0, 120) };
        }

        const captchaSignals = ['enter the code from the image', 'security code', 'verification code', 'captcha code'];
        for (const pat of captchaSignals) {
            if (body.includes(pat)) {
                return { confirmed: false, type: 'custom_captcha_or_verification', match: pat };
            }
        }

        if (html.includes('captcha') && !body.includes('thank you')) {
            return { confirmed: false, type: 'custom_captcha_or_verification', match: 'captcha present after submit' };
        }

        const successPatterns = [
            'thank you', 'thanks for', 'message sent', 'submission received',
            'successfully submitted', 'we will get back', 'we\'ll get back',
            'form submitted', 'request received', 'inquiry received',
            'been received', 'reach out soon', 'contact you soon',
            'message has been', 'form has been', 'successfully sent',
            'we have received your inquiry', 'received your inquiry',
            'success!',
            'danke', 'nachricht wurde', 'erfolgreich'
        ];
        for (const pat of successPatterns) {
            if (body.includes(pat)) {
                return { confirmed: true, type: 'success_text', match: pat };
            }
        }

        // Check for Webflow-specific success state (w-form-done visible)
        const wfDone = document.querySelector('.w-form-done');
        if (wfDone) {
            const style = window.getComputedStyle(wfDone);
            if (style.display !== 'none' && style.opacity !== '0') {
                return { confirmed: true, type: 'webflow_done', match: wfDone.textContent.trim().substring(0, 80) };
            }
        }

        // Check for validation errors still visible
        const errorPatterns = [
            'this field needs to be filled', 'please fill', 'required field',
            'invalid', 'please enter a valid', 'is required', 'fill in',
            'oops', 'something went wrong', 'error', 'failed'
        ];
        for (const pat of errorPatterns) {
            if (body.includes(pat)) {
                return { confirmed: false, type: 'validation_error', match: pat };
            }
        }

        // Check for Webflow error state (w-form-fail visible)
        const wfFail = document.querySelector('.w-form-fail');
        if (wfFail) {
            const style = window.getComputedStyle(wfFail);
            if (style.display !== 'none' && style.opacity !== '0') {
                return { confirmed: false, type: 'webflow_fail', match: wfFail.textContent.trim().substring(0, 80) };
            }
        }

        // No clear signal either way
        return { confirmed: null, type: 'unknown', match: 'no confirmation or error detected' };
    }""")

    print(f"    [VERIFY] {verification.get('type')}: \"{verification.get('match', '')}\"")

    if verification.get("confirmed") is True:
        result["status"] = "confirmed"
        result["confirmation"] = verification
    elif verification.get("confirmed") is False:
        result["status"] = "failed_validation"
        result["confirmation"] = verification
        result["note"] = f"Form validation failed: {verification.get('match', 'unknown error')}"
    else:
        result["status"] = "submitted_unconfirmed"
        result["confirmation"] = verification
        result["note"] = "Submitted but no confirmation message detected — verify manually"

    return result


async def process_website(page, website_url, name, email, message, subject="",
                          phone="", company_name="", address="", city="", state="",
                          postal_code="", dry_run=False, review_before_submit=False):
    """Navigate to a website, find contact page, fill and submit form."""
    slug = re.sub(r'[^a-z0-9]', '_', company_name.lower())[:30] or "site"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"\n  {'='*55}")
    print(f"  Company: {company_name or 'N/A'}")
    print(f"  Website: {website_url[:80]}")

    # Navigate to homepage
    try:
        await page.goto(website_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
    except Exception as e:
        print(f"  [ERROR] Failed to load: {str(e)[:150]}")
        return {"status": "load_error", "url": website_url, "error": str(e)[:200]}

    # Find contact page
    try:
        contact_url = await find_contact_page(page, website_url)
    except Exception as e:
        print(f"  [ERROR] Contact page detection failed: {str(e)[:150]}")
        return {"status": "detection_error", "url": website_url, "error": str(e)[:200]}
    if not contact_url:
        return {"status": "no_contact_page", "url": website_url,
                "note": "Could not find a contact page with a form"}

    # Navigate to contact page if different from current
    if contact_url != page.url:
        try:
            await page.goto(contact_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            print(f"  [ERROR] Failed to load contact page: {str(e)[:150]}")
            return {"status": "load_error", "url": contact_url, "error": str(e)[:200]}

    # Scroll to form
    await page.evaluate("""() => {
        const form = document.querySelector('form') || document.querySelector('textarea');
        if (form) form.scrollIntoView({ behavior: 'smooth', block: 'center' });
        else window.scrollTo(0, document.body.scrollHeight * 0.4);
    }""")
    await page.wait_for_timeout(1500)

    # Screenshot before
    before_path = SCREENSHOT_DIR / f"{slug}_{timestamp}_before.png"
    await page.screenshot(path=str(before_path), full_page=False)
    review_path = REVIEW_SCREENSHOT_DIR / f"{slug}_{timestamp}_filled_before_submit.png"

    # Fill and submit
    result = await fill_and_submit(
        page, name, email, message, subject, phone,
        company=company_name,
        address=address,
        city=city,
        state=state,
        postal_code=postal_code,
        dry_run=dry_run,
        review_before_submit=review_before_submit,
        review_path=review_path,
    )

    # Screenshot after
    after_path = SCREENSHOT_DIR / f"{slug}_{timestamp}_after.png"
    try:
        await page.screenshot(path=str(after_path), full_page=False)
    except Exception:
        pass

    result["url"] = website_url
    result["contact_url"] = contact_url
    result["company_name"] = company_name
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    result["screenshot_before"] = str(before_path)
    result["screenshot_after"] = str(after_path)

    icon = {"confirmed": "OK", "submitted": "OK", "submitted_enter": "OK",
            "submitted_unconfirmed": "??", "dry_run": "DRY",
            "review_pending": "REV",
            "mailto_only": "MAIL", "no_form": "SKIP", "captcha_failed": "CAP",
            "load_error": "ERR", "submit_error": "ERR", "no_contact_page": "SKIP",
            "failed_validation": "FAIL", "missing_required": "MISS"
            }.get(result["status"], "???")

    print(f"  [{icon}] {result['status']} | Fields: {', '.join(result.get('fields_filled', []))}")
    if result.get("note"):
        print(f"       {result['note']}")

    return result


async def main():
    parser = argparse.ArgumentParser(description="Plumbing/HVAC website form submission via Playwright + CapSolver")
    parser.add_argument("--url", help="Single website URL")
    parser.add_argument("--name", default=DEFAULT_SENDER_NAME, help="Sender name")
    parser.add_argument("--email", default=DEFAULT_SENDER_EMAIL, help="Sender email")
    parser.add_argument("--subject", default="", help="Subject (if form has one)")
    parser.add_argument("--phone", default=DEFAULT_SENDER_PHONE, help="Sender phone number (if form has one)")
    parser.add_argument("--sender-address", default="", help="Sender/service address if form asks for address")
    parser.add_argument("--sender-city", default="", help="Sender/service city if form asks for city")
    parser.add_argument("--sender-state", default="", help="Sender/service state if form asks for state")
    parser.add_argument("--sender-postal-code", default="", help="Sender/service ZIP/postal code if form asks for it")
    parser.add_argument("--message", default="", help="Message body")
    parser.add_argument("--company", default="", help="Company name for the target")
    parser.add_argument("--target-company", default="", help="Alias for --company")
    parser.add_argument("--location", default="", help="Target location, e.g. Sugar Land, TX")
    parser.add_argument("--niche", default="", help="Target niche, e.g. Plumber or HVAC contractor")
    parser.add_argument("--notes", default="", help="Optional personalization notes about the target")
    parser.add_argument("--batch", help="Path to CSV or JSON file with leads")
    parser.add_argument("--limit", type=int, default=5, help="Max submissions per run")
    parser.add_argument("--dry-run", action="store_true", help="Fill forms but don't submit")
    parser.add_argument("--review-before-submit", action="store_true",
                        help="Fill forms, save a preview screenshot, and stop before CAPTCHA/submit")
    parser.add_argument("--delay", type=int, default=5, help="Seconds between submissions")
    parser.add_argument("--browser-channel", default="chrome",
                        help="Playwright browser channel. Use 'chrome' for Google Chrome or 'chromium' for bundled Chromium.")
    args = parser.parse_args()

    if not args.url and not args.batch:
        parser.error("Either --url or --batch is required")
    validate_sender_config(args.name, args.email)

    from playwright.async_api import async_playwright

    leads = []
    if args.batch:
        leads = load_leads(args.batch)
        leads = leads[:args.limit]
        print(f"\n  Loaded {len(leads)} leads from {args.batch}")
    elif args.url:
        lead = normalize_lead({
            "website": args.url,
            "company_name": args.target_company or args.company,
            "location": args.location,
            "niche": args.niche,
            "notes": args.notes,
            "message": args.message,
        })
        leads = [lead]

    async with async_playwright() as p:
        user_data_dir = str(PROJECT_DIR / ".tmp" / "browser_profile_website")
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)

        launch_kwargs = {
            "headless": False,
            "viewport": {"width": 1280, "height": 900},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if args.browser_channel and args.browser_channel.lower() != "chromium":
            launch_kwargs["channel"] = args.browser_channel

        context = await p.chromium.launch_persistent_context(user_data_dir, **launch_kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        log = load_log()
        results = []

        print(f"\n{'='*60}")
        print(f"  PLUMBING/HVAC WEBSITE SUBMISSION AGENT")
        print(f"  Leads: {len(leads)} | Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
        if args.review_before_submit:
            print(f"  Review mode: filled screenshots only, no submissions")
        print(f"  Sender: {args.name} <{args.email}>")
        print(f"{'='*60}")

        for i, lead in enumerate(leads):
            url = normalize_website(lead.get("website") or lead.get("url", ""))
            company = lead.get("company_name", "")
            msg = lead.get("message", args.message)

            if not msg:
                msg = generate_partnership_message(lead)

            if not url:
                print(f"\n  [{i+1}/{len(leads)}] SKIP {company} — no URL")
                continue

            result = await process_website(
                page, url, args.name, args.email, msg,
                subject=args.subject, phone=args.phone,
                company_name=company,
                address=args.sender_address,
                city=args.sender_city,
                state=args.sender_state,
                postal_code=args.sender_postal_code,
                dry_run=args.dry_run,
                review_before_submit=args.review_before_submit,
            )
            results.append(result)
            log.append(result)
            save_log(log)

            if i < len(leads) - 1:
                print(f"    Waiting {args.delay}s...")
                await asyncio.sleep(args.delay)

        await context.close()

    # Summary
    submitted = sum(1 for r in results if r["status"] in ("confirmed", "submitted", "submitted_enter", "submitted_unconfirmed"))
    dry = sum(1 for r in results if r["status"] == "dry_run")
    review = sum(1 for r in results if r["status"] == "review_pending")
    skipped = sum(1 for r in results if r["status"] in ("no_form", "mailto_only", "no_contact_page"))
    errors = sum(1 for r in results if "error" in r["status"])
    captcha_fail = sum(1 for r in results if r["status"] == "captcha_failed")

    print(f"\n{'='*60}")
    print(f"  WEBSITE OUTREACH COMPLETE")
    print(f"{'='*60}")
    print(f"  Submitted:       {submitted}")
    if dry: print(f"  Dry run:         {dry}")
    if review: print(f"  Review pending:  {review}")
    print(f"  Skipped:         {skipped}")
    print(f"  Errors:          {errors}")
    if captcha_fail: print(f"  Captcha failed:  {captcha_fail}")
    print(f"  Log:             {LOG_FILE.name}")
    print(f"  Screenshots:     {SCREENSHOT_DIR}")
    if review: print(f"  Review images:   {REVIEW_SCREENSHOT_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import datetime
import json
import logging
import re
import sys
from typing import Dict, List, Set, Any, Optional

from playwright.async_api import async_playwright, Page

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("scraper.log"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Constants
BASE_URL = "https://www.axismaxlife.com"
MAX_CONCURRENT_PAGES = 3
TIMEOUT = 60000
EXCLUDED_TERMS = ["calculator", "claim", "settlement", "faqs", "compare"]
RETRY_ATTEMPTS = 3
RETRY_DELAY = 5


class TermPlanScraper:
    """Scrapes term insurance plans from Axis Max Life Insurance website."""

    def __init__(self, headless: bool = True, limit: Optional[int] = None):
        """
        Args:
            headless: Run browser in headless mode
            limit: Max number of plans to scrape
        """
        self.headless = headless
        self.limit = limit
        self.browser = None
        self.context = None
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

    async def start(self):
        """Start browser and create context."""
        playwright = await async_playwright().start()
        try:
            import platform
            system = platform.system()
            brave_path = None
            if system == "Windows":
                import os
                possible_paths = [
                    "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
                    "C:\\Program Files (x86)\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
                ]
                for path in possible_paths:
                    if os.path.exists(path):
                        brave_path = path
                        break
            elif system == "Darwin":
                brave_path = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
            elif system == "Linux":
                brave_path = "/usr/bin/brave-browser"
            if brave_path and os.path.exists(brave_path):
                logger.info(f"Using Brave browser at: {brave_path}")
                self.browser = await playwright.chromium.launch(
                    headless=self.headless, executable_path=brave_path
                )
            else:
                logger.info("Brave browser not found, using default Chromium")
                self.browser = await playwright.chromium.launch(headless=self.headless)
        except Exception as e:
            logger.warning(f"Error launching Brave, falling back to Chromium: {e}")
            self.browser = await playwright.chromium.launch(headless=self.headless)
        self.context = await self.browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        )
        return playwright

    async def close(self):
        """Close browser."""
        if self.browser:
            await self.browser.close()

    async def collect_plan_urls(self) -> Set[str]:
        """Collect term plan URLs from the main page."""
        urls = set()
        try:
            page = await self.context.new_page()
            logger.info(f"Navigating to {BASE_URL}/term-insurance-plans")
            for attempt in range(RETRY_ATTEMPTS):
                try:
                    await page.goto(f"{BASE_URL}/term-insurance-plans", timeout=TIMEOUT)
                    await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
                    break
                except Exception as e:
                    if attempt < RETRY_ATTEMPTS - 1:
                        logger.warning(f"Failed to load page (attempt {attempt+1}/{RETRY_ATTEMPTS}): {e}")
                        await asyncio.sleep(RETRY_DELAY)
                    else:
                        logger.error(f"Failed to load page after {RETRY_ATTEMPTS} attempts: {e}")
                        raise
            links = await page.locator("a[href*='-plan']").all()
            for link in links:
                try:
                    href = await link.get_attribute("href")
                    if (
                        href
                        and href.startswith("/term-insurance-plans/")
                        and not any(bad in href for bad in EXCLUDED_TERMS)
                    ):
                        clean_url = BASE_URL + href.split("?")[0]
                        urls.add(clean_url)
                        logger.debug(f"Found plan URL: {clean_url}")
                except Exception as e:
                    logger.warning(f"Error processing link: {e}")
            logger.info(f"Found {len(urls)} term plan URLs")
        except Exception as e:
            logger.error(f"Error collecting plan URLs: {e}")
        finally:
            await page.close()
        return urls

    async def scrape_plan(self, url: str) -> Dict[str, Any]:
        """Scrape a single term plan."""
        async with self.semaphore:
            plan_data = {
                "source_url": url,
                "source_scrape_date": str(datetime.date.today()),
                "insurer": "Axis Max Life Insurance",
                "plan_type": "Term Insurance",
            }
            plan = None
            try:
                plan = await self.context.new_page()
                logger.info(f"Navigating to plan: {url}")
                for attempt in range(RETRY_ATTEMPTS):
                    try:
                        await plan.goto(url, timeout=TIMEOUT)
                        await plan.wait_for_load_state("networkidle", timeout=TIMEOUT)
                        break
                    except Exception as e:
                        if attempt < RETRY_ATTEMPTS - 1:
                            logger.warning(f"Failed to load plan (attempt {attempt+1}/{RETRY_ATTEMPTS}): {e}")
                            await asyncio.sleep(RETRY_DELAY)
                        else:
                            logger.error(f"Failed to load plan after {RETRY_ATTEMPTS} attempts: {e}")
                            return plan_data
                plan_data["plan_name"] = self.clean_plan_name_from_url(url)
                # Try block for eligibility criteria accordion (min/max entry age)
                try:
                    eligibility_heading = plan.locator('h3:text("What are The Eligibility Criteria for Axis Max Life Smart Secure Plus Plan?")')
                    if await eligibility_heading.count() > 0:
                        accordion_parent = eligibility_heading.locator('xpath=ancestor::div[contains(@class, "accordionwfull")]')
                        eligibility_content = accordion_parent.locator('.accordion-content')
                        if await eligibility_content.count() > 0:
                            eligibility_text = await eligibility_content.inner_text()
                            import re
                            min_age_match = re.search(r"minimum entry age[^\d]*(\d{1,2})", eligibility_text, re.IGNORECASE)
                            max_age_match = re.search(r"maximum age[^\d]*(\d{1,2})", eligibility_text, re.IGNORECASE)
                            if min_age_match:
                                plan_data["min_entry_age"] = min_age_match.group(1)
                            if max_age_match:
                                plan_data["max_entry_age"] = max_age_match.group(1)
                except Exception as e:
                    logger.warning(f"Could not extract min/max entry age from eligibility accordion: {e}")
                info_texts = await plan.locator("li, p, td, span").all_inner_texts()
                joined_text = " ".join(info_texts)
                form_results = await self.fill_and_submit_form(plan)
                text_content = "\n".join(info_texts)
                plan_data.update(
                    {
                        "monthly_premium": form_results["monthly_premium"],
                        "coverage_duration": self.extract_coverage_duration(
                            text_content
                        )
                        or f"Up to {form_results['cover_till_age']} years",
                        "payout_type": (
                            "Lump sum / Monthly income"
                            if "lump" in joined_text.lower()
                            else "N/A"
                        ),
                        "medical_required": "medical" in joined_text.lower(),
                        "smoker_premium_diff": "smoker" in joined_text.lower(),
                        "add_ons": self.clean_addons(
                            self.parse_addons_from_text(text_content)
                        ),
                        "gender": form_results.get("gender", "N/A"),
                        "smoker": form_results.get("smoker", "N/A"),
                        "age": form_results.get("age", "N/A"),
                        "life_cover_amount": form_results.get("life_cover_amount", "N/A"),
                    }
                )
                logger.info(f"Successfully scraped plan: {plan_data['plan_name']}")
                return self.parse_maxlife_plan(plan_data)
            except Exception as e:
                logger.error(f"Error scraping plan {url}: {e}")
                return plan_data
            finally:
                if plan:
                    await plan.close()

    async def fill_and_submit_form(self, page: Page) -> Dict[str, str]:
        """Fill and extract premium info from the calculator form."""
        results = {
            "monthly_premium": "N/A",
            "cover_till_age": "N/A",
            "gender": "N/A",
            "smoker": "N/A",
            "age": "N/A",
            "life_cover_amount": "N/A",
        }
        form_values = {
            "gender": "Female",
            "smoker": "No",
            "age": "35yrs",
            "sum_assured": "₹ 1 cr"
        }
        try:
            for attempt in range(RETRY_ATTEMPTS):
                try:
                    calculator_selectors = [
                        "text=Term Plan Calculator",
                        "text=Calculate Premium",
                        "text=Premium Calculator"
                    ]
                    calculator_element = None
                    for selector in calculator_selectors:
                        elements = await page.locator(selector).all()
                        if elements:
                            calculator_element = elements[0]
                            logger.info(f"Found calculator using selector: {selector}")
                            break
                    if not calculator_element:
                        logger.warning("⚠️ Form 2 (calculator) not found, skipping dynamic extraction.")
                        return results
                    await calculator_element.scroll_into_view_if_needed()
                    await page.wait_for_timeout(1000)
                    viewport_height = await page.evaluate("window.innerHeight")
                    element_box = await calculator_element.bounding_box()
                    if element_box:
                        current_scroll = await page.evaluate("window.scrollY")
                        target_scroll = current_scroll + element_box['y'] - (viewport_height / 2) + (element_box['height'] / 2)
                        await page.evaluate(f"window.scrollTo({{top: {target_scroll}, behavior: 'smooth'}})")
                        await page.wait_for_timeout(1500)
                    is_visible = await calculator_element.is_visible()
                    if not is_visible:
                        raise Exception("Calculator found but not visible after scroll")
                    break
                except Exception as e:
                    if attempt < RETRY_ATTEMPTS - 1:
                        logger.warning(f"Failed to locate calculator (attempt {attempt+1}/{RETRY_ATTEMPTS}): {e}")
                        await asyncio.sleep(RETRY_DELAY)
                    else:
                        logger.error(f"Failed to locate calculator after {RETRY_ATTEMPTS} attempts: {e}")
                        return results
            logger.info("▶ Using Form Type 2 (Quick calculator)")
            async def click_with_retry(selector: str, max_attempts: int = 3) -> bool:
                for attempt in range(max_attempts):
                    try:
                        element = await page.wait_for_selector(selector, timeout=5000, state="visible")
                        if element:
                            await element.scroll_into_view_if_needed()
                            await page.wait_for_timeout(500)
                            is_visible = await element.is_visible()
                            if not is_visible:
                                raise Exception(f"Element {selector} not visible after scroll")
                            await element.click()
                            return True
                    except Exception as e:
                        if attempt < max_attempts - 1:
                            logger.warning(f"Click failed for {selector} (attempt {attempt+1}/{max_attempts}): {e}")
                            await asyncio.sleep(1)
                        else:
                            logger.error(f"Failed to click {selector} after {max_attempts} attempts: {e}")
                return False
            # Interact with form fields using form_values
            gender_selector = 'label[for="232"]' if form_values["gender"].lower() == "male" else 'label[for="233"]'
            if not await click_with_retry(gender_selector):
                logger.error("Form submission failed at gender selection")
                return results
            smoker_selector = 'label[for="239"]' if form_values["smoker"].lower() == "no" else 'label[for="240"]'
            if not await click_with_retry(smoker_selector):
                logger.error("Form submission failed at smoker selection")
                return results
            sum_assured_map = {
                "₹ 75l": "16299",
                "₹ 80l": "16300",
                "₹ 1 cr": "16301",
                "₹ 1.5 cr": "16302",
                "₹ 2 cr": "16303"
            }
            sum_assured_id = sum_assured_map.get(form_values["sum_assured"].lower(), "16301")
            sum_selector = f'label[for="{sum_assured_id}"]'
            if not await click_with_retry(sum_selector):
                logger.error("Form submission failed at sum assured selection")
                return results
            try:
                await page.wait_for_timeout(2000)
                premium_selector = 'div.text-amount'
                if await page.locator(premium_selector).count() > 0:
                    premium_text = await page.locator(premium_selector).inner_text()
                    match = re.search(r"₹\s?[\d,]+", premium_text)
                    if match:
                        results["monthly_premium"] = match.group(0).replace(" ", "")
                        logger.info(f"Found premium: {results['monthly_premium']}")
                else:
                    logger.warning("Premium value not found after form selection")
                results["gender"] = form_values["gender"]
                results["smoker"] = form_values["smoker"]
                results["age"] = form_values["age"]
                results["life_cover_amount"] = form_values["sum_assured"]
            except Exception as e:
                logger.error(f"Error extracting premium value or form selections: {e}")
        except Exception as e:
            logger.error(f"⚠️ Dynamic form (Form 2) submission failed: {e}")
        return results

    @staticmethod
    def extract_coverage_duration(text: str) -> str:
        """Extract coverage duration from text."""
        coverage_patterns = [
            r"(?:coverage|cover) (?:duration|period)(?:\s*is|\s*:)?\s*(?:up to|until|till)?\s*(\d+)\s*(?:years|yrs)",
            r"(?:coverage|cover) (?:for|of)\s*(?:up to|until|till)?\s*(\d+)\s*(?:years|yrs)",
        ]
        text_lower = text.lower()
        for pattern in coverage_patterns:
            match = re.search(pattern, text_lower)
            if match:
                return f"Up to {match.group(1)} years"
        return "N/A"

    @staticmethod
    def parse_addons_from_text(text: str) -> List[str]:
        """Extract add-on features from text."""
        addon_list = []
        addon_keywords = [
            "accidental death benefit",
            "critical illness",
            "disability benefit",
            "premium waiver",
            "terminal illness",
            "income benefit",
            "return of premium",
            "increasing cover",
            "decreasing cover",
            "child support",
            "spouse cover",
            "funeral expenses",
        ]
        text_lower = text.lower()
        for keyword in addon_keywords:
            if keyword in text_lower and keyword not in addon_list:
                addon_list.append(keyword)
        return addon_list

    @staticmethod
    def clean_addons(addons: List[str]) -> List[str]:
        """Clean and format the extracted add-ons."""
        if not addons:
            return []
        unique_addons = list(set(addons))
        cleaned_addons = [addon.title() for addon in unique_addons]
        cleaned_addons.sort()
        return cleaned_addons

    @staticmethod
    def parse_maxlife_plan(raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """Clean and format the extracted plan data."""
        plan_data = raw_data.copy()
        if "source_url" in plan_data:
            url_parts = plan_data["source_url"].split("/")
            if len(url_parts) > 0:
                plan_shortname = (
                    url_parts[-1].replace("-plan", "").replace("-", " ").title()
                )
                if "plan_name" not in plan_data or "Axis" not in plan_data["plan_name"]:
                    plan_data["plan_name"] = f"Axis Max Life {plan_shortname} Plan"
        required_keys = [
            "plan_name",
            "insurer",
            "plan_type",
            "coverage_duration",
            "monthly_premium",
            "payout_type",
            "medical_required",
            "smoker_premium_diff",
            "add_ons",
            "source_url",
            "source_scrape_date",
            "gender",
            "smoker",
            "age",
            "life_cover_amount",
        ]
        for key in required_keys:
            if key not in plan_data:
                plan_data[key] = "N/A"
        return plan_data

    @staticmethod
    def clean_plan_name_from_url(url: str) -> str:
        """
        Extract and clean the plan name from the URL.
        """
        import re

        # Get the last part of the URL
        plan_slug = url.rstrip("/").split("/")[-1]
        # Remove '-plan' suffix if present
        plan_slug = re.sub(r"-plan$", "", plan_slug, flags=re.IGNORECASE)
        # Replace hyphens with spaces and capitalize
        plan_name = plan_slug.replace("-", " ").title()
        # Remove common marketing words
        plan_name = re.sub(
            r"\b(buy|best|online|in india|202\d|axis max life insurance)\b",
            "",
            plan_name,
            flags=re.IGNORECASE,
        )
        # Remove extra spaces
        plan_name = re.sub(r"\s+", " ", plan_name).strip()
        # Add standard prefix/suffix
        plan_name = f"Axis Max Life {plan_name} Plan"
        return plan_name


async def main():
    """Main function to run the scraper."""
    try:
        logger.info("Starting term plan scraper")
        scraper = TermPlanScraper(headless=False, limit=None)
        playwright = await scraper.start()
        plan_urls = await scraper.collect_plan_urls()
        if scraper.limit:
               plan_urls = list(plan_urls)[: scraper.limit]
        tasks = [scraper.scrape_plan(url) for url in plan_urls]
        parsed_plans = await asyncio.gather(*tasks)
        with open("final_parsed_term_plans.json", "w", encoding="utf-8") as f:
            json.dump(parsed_plans, f, indent=2, ensure_ascii=False)
        logger.info("✅ Saved to 'final_parsed_term_plans.json'")
    except Exception as e:
        logger.error(f"Error in main function: {e}")
    finally:
        if scraper:
            await scraper.close()
        if "playwright" in locals():
            await playwright.stop()


if __name__ == "__main__":
    asyncio.run(main())

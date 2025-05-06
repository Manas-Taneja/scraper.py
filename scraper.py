import asyncio
import datetime
import json
import logging
import re
import sys
from typing import Dict, List, Set, Tuple, Any, Optional, Union

import aiohttp
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("scraper.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Constants
BASE_URL = "https://www.axismaxlife.com"
MAX_CONCURRENT_PAGES = 3  # Limit concurrent page processing
TIMEOUT = 60000  # Increased timeout in milliseconds
EXCLUDED_TERMS = ["calculator", "claim", "settlement", "faqs", "compare"]
RETRY_ATTEMPTS = 3
RETRY_DELAY = 5  # seconds

class TermPlanScraper:
    """A class to scrape term insurance plans from Axis Max Life Insurance website."""
    
    def __init__(self, headless: bool = True, limit: Optional[int] = None):
        """
        Initialize the scraper.
        
        Args:
            headless: Whether to run the browser in headless mode
            limit: Maximum number of plans to scrape (None for all)
        """
        self.headless = headless
        self.limit = limit
        self.browser = None
        self.context = None
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)
    
    async def start(self):
        """Start the browser and create a context."""
        playwright = await async_playwright().start()
        
        # Try to launch Brave if available, fall back to Chromium
        try:
            # Path to Brave on different operating systems
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
            elif system == "Darwin":  # macOS
                brave_path = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
            elif system == "Linux":
                brave_path = "/usr/bin/brave-browser"
            
            if brave_path and os.path.exists(brave_path):
                logger.info(f"Using Brave browser at: {brave_path}")
                self.browser = await playwright.chromium.launch(
                    headless=self.headless,
                    executable_path=brave_path
                )
            else:
                logger.info("Brave browser not found, using default Chromium")
                self.browser = await playwright.chromium.launch(headless=self.headless)
        except Exception as e:
            logger.warning(f"Error launching Brave, falling back to Chromium: {e}")
            self.browser = await playwright.chromium.launch(headless=self.headless)
        
        self.context = await self.browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        )
        return playwright

    

    async def close(self):
        """Close the browser and context."""
        if self.browser:
            await self.browser.close()
    
    async def collect_plan_urls(self) -> Set[str]:
        """
        Collect term plan URLs from the main page.
        
        Returns:
            A set of URLs for term plans
        """
        urls = set()
        try:
            page = await self.context.new_page()
            logger.info(f"Navigating to {BASE_URL}/term-insurance-plans")
            
            # Try multiple times to load the page
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
            
            # Find all plan links
            links = await page.locator("a[href*='-plan']").all()
            for link in links:
                try:
                    href = await link.get_attribute("href")
                    if href and href.startswith("/term-insurance-plans/") and not any(bad in href for bad in EXCLUDED_TERMS):
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
        """
        Scrape a single term plan.
        
        Args:
            url: The URL of the plan to scrape
            
        Returns:
            Dictionary containing plan information
        """
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
                
                # Try multiple times to load the page
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
                            return plan_data  # Return partial data
                
                # Extract page title for plan name
                plan_data["plan_name"] = await plan.title()
                
                # Get all text elements
                info_texts = await plan.locator("li, p, td, span").all_inner_texts()
                joined_text = " ".join(info_texts)
                # Try dynamic form fill and get additional data
                form_results = await self.fill_and_submit_form(plan)

                text_content = "\n".join(info_texts)
                
                # Extract information
                sum_min, sum_max = self.extract_sum_assured_range(info_texts)
                plan_data.update({
                    "min_sum_assured": sum_min,
                    "max_sum_assured": sum_max,
                    "monthly_premium": form_results["monthly_premium"],
                    "coverage_duration": self.extract_coverage_duration(text_content) or f"Up to {form_results['cover_till_age']} years",
                    "payment_term": form_results["payment_term"],
                    "payout_type": "Lump sum / Monthly income" if "lump" in joined_text.lower() else "N/A",
                    "medical_required": "medical" in joined_text.lower(),
                    "smoker_premium_diff": "smoker" in joined_text.lower(),
                    "add_ons": self.clean_addons(self.parse_addons_from_text(text_content)),
                })
                
                logger.info(f"Successfully scraped plan: {plan_data['plan_name']}")
                return self.parse_maxlife_plan(plan_data)
                
            except Exception as e:
                logger.error(f"Error scraping plan {url}: {e}")
                return plan_data
            finally:
                if plan:
                    await plan.close()
    async def fill_and_submit_form(self, page: Page) -> Dict[str, str]:
        """Fill and extract premium info from 'Term Plan Calculator' form (Form 2 only)."""
        results = {
            "monthly_premium": "N/A",
            "cover_till_age": "N/A",
            "payment_term": "N/A"
        }

        try:
            # Scroll to bring form into view
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(1500)

            # Check for the presence of calculator
            if await page.locator("text=Term Plan Calculator").count() == 0:
                logger.warning("⚠️ Form 2 (calculator) not found, skipping dynamic extraction.")
                return results

            logger.info("▶ Using Form Type 2 (Quick calculator)")

            # Interact with form
            await page.get_by_role("button", name="Male").click()
            await page.get_by_role("button", name="No").click()

            await page.get_by_text("35yrs", exact=True).click()
            await page.get_by_text("₹ 1 cr", exact=True).click()

            await page.get_by_role("button", name="Check Premium").click()
            await page.wait_for_timeout(3000)

            # Extract premium value if visible
            if await page.locator("text=Premium starting at").count():
                premium_text = await page.locator("text=Premium starting at").locator("..").inner_text()
                match = re.search(r"₹\s?[\d,]+", premium_text)
                if match:
                    results["monthly_premium"] = match.group(0).replace(" ", "")

        except Exception as e:
            logger.warning(f"⚠️ Dynamic form (Form 2) submission failed: {e}")

        return results



    @staticmethod
    def extract_sum_assured_range(texts: List[str]) -> Tuple[str, str]:
        """
        Extract minimum and maximum sum assured from text.
        
        Args:
            texts: List of text elements from the page
            
        Returns:
            Tuple of (min_sum_assured, max_sum_assured)
        """
        min_sum = "N/A"
        max_sum = "N/A"
        
        # Join all texts to search through them
        combined_text = " ".join(texts).lower()
        
        # Look for common patterns in insurance sites
        sum_patterns = [
            # Pattern for "Sum Assured ranges from ₹XX lakh to ₹XX crore"
            r"sum assured (?:ranges?|from) (?:inr|rs\.?|₹)\s*(\d+(?:\.\d+)?)\s*(lakh|crore)(?:[s\s]*to|-)\s*(?:inr|rs\.?|₹)\s*(\d+(?:\.\d+)?)\s*(lakh|crore)",
            
            # Pattern for "minimum sum assured of ₹XX lakh/crore" and "maximum sum assured of ₹XX lakh/crore"
            r"(?:minimum|min\.?)?\s*sum assured\s*(?:of|is|:)?\s*(?:inr|rs\.?|₹)\s*(\d+(?:\.\d+)?)\s*(lakh|crore)",
            r"(?:maximum|max\.?)?\s*sum assured\s*(?:of|is|:)?\s*(?:inr|rs\.?|₹)\s*(\d+(?:\.\d+)?)\s*(lakh|crore)",
            
            # General pattern for amounts
            r"(?:inr|rs\.?|₹)\s*(\d+(?:\.\d+)?)\s*(lakh|crore)\s*(?:sum assured|coverage)",
        ]
        
        for pattern in sum_patterns:
            matches = re.finditer(pattern, combined_text)
            for match in matches:
                groups = match.groups()
                
                if len(groups) == 4:  # Range pattern with min and max
                    min_value = float(groups[0])
                    min_unit = groups[1]
                    max_value = float(groups[2])
                    max_unit = groups[3]
                    
                    min_sum = f"{min_value} {min_unit.capitalize()}"
                    max_sum = f"{max_value} {max_unit.capitalize()}"
                    break
                
                elif len(groups) == 2:  # Single value pattern
                    value = float(groups[0])
                    unit = groups[1].capitalize()
                    
                    if "minimum" in match.string[max(0, match.start()-20):match.start()] or "min" in match.string[max(0, match.start()-10):match.start()]:
                        min_sum = f"{value} {unit}"
                    elif "maximum" in match.string[max(0, match.start()-20):match.start()] or "max" in match.string[max(0, match.start()-10):match.start()]:
                        max_sum = f"{value} {unit}"
                    else:
                        # If no clear indicator, use context to guess
                        context = match.string[max(0, match.start()-50):min(len(match.string), match.end()+50)]
                        if "minimum" in context or "starting" in context or "at least" in context:
                            min_sum = f"{value} {unit}"
                        elif "maximum" in context or "up to" in context or "highest" in context:
                            max_sum = f"{value} {unit}"
        
        return min_sum, max_sum
    
    @staticmethod
    def extract_premium_details(text: str) -> str:
        """
        Extract monthly premium information from text.
        
        Args:
            text: Text content from the page
            
        Returns:
            Monthly premium as string
        """
        # Monthly premium pattern
        monthly_patterns = [
            r"(?:monthly|mon\.) premium\s*(?:starting|starts|from|at)?\s*(?:inr|rs\.?|₹)\s*(\d+(?:,\d+)*(?:\.\d+)?)",
            r"(?:inr|rs\.?|₹)\s*(\d+(?:,\d+)*(?:\.\d+)?)\s*(?:per|\/)\s*month",
        ]
        
        text_lower = text.lower()
        
        # Extract monthly premium
        for pattern in monthly_patterns:
            match = re.search(pattern, text_lower)
            if match:
                return f"₹{match.group(1)}"
        
        return "N/A"
    
    # @staticmethod
    # def extract_maturity_age(text: str) -> str:
    #     """
    #     Extract maximum maturity age from text.
        
    #     Args:
    #         text: Text content from the page
            
    #     Returns:
    #         Maximum maturity age as string
    #     """
    #     maturity_patterns = [
    #         r"(?:maturity|cover till) age(?:\s*is|\s*:)?\s*(\d+)\s*(?:years|yrs)",
    #         r"coverage (?:till|until) (?:age )?(\d+)\s*(?:years|yrs)?",
    #         r"policy (?:till|until) age (\d+)",
    #     ]
        
    #     text_lower = text.lower()
        
    #     for pattern in maturity_patterns:
    #         match = re.search(pattern, text_lower)
    #         if match:
    #             return match.group(1)
        
    #     return "N/A"
    
    @staticmethod
    def extract_coverage_duration(text: str) -> str:
        """
        Extract coverage duration information from text.
        
        Args:
            text: Text content from the page
            
        Returns:
            Coverage duration as string
        """
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
    def extract_policy_term(text: str) -> str:
        """
        Extract policy term range from text.
        
        Args:
            text: Text content from the page
            
        Returns:
            Policy term range as string
        """
        term_patterns = [
            r"policy term(?:\s*is|\s*:)?\s*(\d+)\s*(?:to|-)\s*(\d+)\s*(?:years|yrs)",
            r"term(?:\s*is|\s*:)?\s*(\d+)\s*(?:to|-)\s*(\d+)\s*(?:years|yrs)",
            r"policy term(?:\s*is|\s*:)?\s*(\d+)\s*(?:years|yrs)",
        ]
        
        text_lower = text.lower()
        
        for pattern in term_patterns:
            match = re.search(pattern, text_lower)
            if match:
                groups = match.groups()
                if len(groups) == 2:  # Range with min and max
                    return f"{groups[0]} – {groups[1]} years"
                elif len(groups) == 1:  # Single value
                    return f"{groups[0]} years"
        
        return "N/A"
    
    @staticmethod
    def extract_payment_term(text: str) -> str:
        """
        Extract payment term information from text.
        
        Args:
            text: Text content from the page
            
        Returns:
            Payment term as string
        """
        payment_patterns = [
            r"(?:payment|premium payment|premium) term(?:\s*is|\s*:)?\s*(\d+)\s*(?:years|yrs)",
            r"pay (?:for|premium for)\s*(\d+)\s*(?:years|yrs)",
            r"pay (?:as|till|until) you (?:live|survive|age (\d+))",
        ]
        
        text_lower = text.lower()
        
        for pattern in payment_patterns:
            match = re.search(pattern, text_lower)
            if match:
                groups = match.groups()
                if groups[0]:  # Fixed number of years
                    return f"{groups[0]} years"
                else:  # Till a certain age or whole life
                    return "Whole life" if "live" in match.group(0) else "Till age 60"
        
        return "N/A"
    
    @staticmethod
    def parse_addons_from_text(text: str) -> List[str]:
        """
        Extract add-on features from text.
        
        Args:
            text: Text content from the page
            
        Returns:
            List of addon features
        """
        addon_list = []
        
        # Common term plan add-ons
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
        
        # Look for sections that might contain add-ons
        addon_sections = re.findall(r"(?:add[- ]ons?|additional benefits|riders|extra coverage)[\s\S]*?(?:\n\n|\.\s|$)", text_lower)
        
        # Process each potential addon section
        for section in addon_sections:
            for keyword in addon_keywords:
                if keyword in section:
                    addon_list.append(keyword)
        
        # Look for specific addon mentions outside of sections
        for keyword in addon_keywords:
            if keyword in text_lower and keyword not in addon_list:
                addon_list.append(keyword)
        
        return addon_list
    
    @staticmethod
    def clean_addons(addons: List[str]) -> List[str]:
        """
        Clean and format the extracted add-ons.
        
        Args:
            addons: List of raw addon features
            
        Returns:
            List of cleaned addon features
        """
        if not addons:
            return []
        
        # Remove duplicates
        unique_addons = list(set(addons))
        
        # Title case and sort
        cleaned_addons = [addon.title() for addon in unique_addons]
        cleaned_addons.sort()
        
        return cleaned_addons
    
    @staticmethod
    def parse_maxlife_plan(raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Clean and format the extracted plan data.
        
        Args:
            raw_data: Dictionary containing raw scraped data
            
        Returns:
            Dictionary containing cleaned and formatted plan data
        """
        # Create a copy to avoid modifying the original
        plan_data = raw_data.copy()
        
        # Extract plan shortname from URL
        if "source_url" in plan_data:
            url_parts = plan_data["source_url"].split("/")
            if len(url_parts) > 0:
                plan_shortname = url_parts[-1].replace("-plan", "").replace("-", " ").title()
                if "plan_name" not in plan_data or "Axis" not in plan_data["plan_name"]:
                    plan_data["plan_name"] = f"Axis Max Life {plan_shortname} Plan"
        
        # Ensure all required keys are present
        required_keys = [
            "plan_name", "insurer", "plan_type", "coverage_duration", 
            "min_sum_assured", "max_sum_assured", "monthly_premium",
            "payout_type", "medical_required", "smoker_premium_diff", 
            "add_ons", "source_url", "source_scrape_date"
        ]
        
        for key in required_keys:
            if key not in plan_data:
                plan_data[key] = "N/A"
        
        return plan_data

async def main():
    """Main function to run the scraper."""
    try:
        logger.info("Starting term plan scraper")
        scraper = TermPlanScraper(headless=False, limit=1)  # Set to False for debugging
        
        # Start browser and get playwright instance
        playwright = await scraper.start()
        
        # Collect plan URLs
        plan_urls = await scraper.collect_plan_urls()
        
        if scraper.limit:
            plan_urls = list(plan_urls)[:scraper.limit]
        
        # Scrape each plan
        tasks = [scraper.scrape_plan(url) for url in plan_urls]
        parsed_plans = await asyncio.gather(*tasks)
        
        # Save results
        with open("final_parsed_term_plans.json", "w", encoding="utf-8") as f:
            json.dump(parsed_plans, f, indent=2, ensure_ascii=False)
        
        logger.info("✅ Saved to 'final_parsed_term_plans.json'")
        
    except Exception as e:
        logger.error(f"Error in main function: {e}")
    finally:
        if scraper:
            await scraper.close()
        if 'playwright' in locals():
            await playwright.stop()

if __name__ == "__main__":
    asyncio.run(main())
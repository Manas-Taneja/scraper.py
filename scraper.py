import asyncio
import datetime
import json
import logging
import re
import sys
from playwright.async_api import async_playwright, Page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("scraper.log"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

BASE_URL = "https://www.axismaxlife.com"
MAX_CONCURRENT_PAGES = 3
TIMEOUT = 60000
EXCLUDED_TERMS = ["calculator", "claim", "settlement", "faqs", "compare"]
RETRY_ATTEMPTS = 3
RETRY_DELAY = 5

class TermPlanScraper:
    def __init__(self, headless: bool = True, limit = None):
        self.headless = headless
        self.limit = limit
        self.browser = None
        self.context = None
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

    async def start(self):
        playwright = await async_playwright().start()
        try:
            self.browser = await playwright.chromium.launch(headless=self.headless)
        except Exception as e:
            logger.error(f"Error launching browser: {e}")
            raise
        self.context = await self.browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        )
        return playwright

    async def close(self):
        if self.browser:
            await self.browser.close()

    async def collect_plan_urls(self):
        urls = set()
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
                    return urls
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
        await page.close()
        return urls

    async def handle_any_form(self, page, plan_data=None):
        logger.info("Trying to handle Form 2")
        form2_success = await self.handle_form2(page, plan_data=plan_data)
        if form2_success:
            logger.info("Form 2 handled (no fallback to Form 1 or 3)")
            return True
        logger.info("Form 2 not found or not handled, trying Form 1")
        form1_success = await self.handle_form1(page, plan_data=plan_data)
        if form1_success:
            logger.info("Form 1 handled (no fallback to Form 3)")
            return True
        logger.info("Form 1 not found or not handled, trying Form 3")
        form3_success = await self.handle_form3(page, plan_data=plan_data)
        if form3_success:
            logger.info("Form 3 handled")
            return True
        logger.info("No known form found or handled on this page")
        return False

    async def scrape_plan(self, url):
        async with self.semaphore:
            plan_data = {
                "source_url": url,
                "source_scrape_date": str(datetime.date.today()),
                "insurer": "Axis Max Life Insurance",
                "plan_name": self.clean_plan_name_from_url(url),
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

                # Try to handle any form (2, then 1, then 3)
                form_success = await self.handle_any_form(plan, plan_data=plan_data)
                logger.info(f"Form handling {'succeeded' if form_success else 'failed'}")

                info_texts = await plan.locator("li, p, td, span").all_inner_texts()
                joined_text = " ".join(info_texts)
                text_content = "\n".join(info_texts)
                plan_data.update(
                    {
                        "monthly_premium": plan_data.get("monthly_premium", "N/A"),
                        "medical_required": "medical" in joined_text.lower(),
                        "smoker_premium_diff": "smoker" in joined_text.lower(),
                        # Removed payout_type, add_ons, coverage_duration
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

    @staticmethod
    def parse_maxlife_plan(raw_data):
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
            "source_url",
            "source_scrape_date",
            "insurer",
            "plan_name",
            "plan_type",
            "monthly_premium",
            "medical_required",
            "smoker_premium_diff",
            "add_on_riders",
            "quote_details",
        ]
        for key in required_keys:
            if key not in plan_data:
                plan_data[key] = "N/A"
        return plan_data

    @staticmethod
    def extract_coverage_duration(text):
        # This function is now unused but kept for reference
        return "N/A"

    @staticmethod
    def parse_addons_from_text(text):
        # This function is now unused but kept for reference
        return []

    @staticmethod
    def clean_addons(addons):
        # This function is now unused but kept for reference
        return []

    @staticmethod
    def clean_plan_name_from_url(url):
        import re
        plan_slug = url.rstrip("/").split("/")[-1]
        plan_slug = re.sub(r"-plan$", "", plan_slug, flags=re.IGNORECASE)
        plan_name = plan_slug.replace("-", " ").title()
        plan_name = re.sub(
            r"\b(buy|best|online|in india|202\d|axis max life insurance)\b",
            "",
            plan_name,
            flags=re.IGNORECASE,
        )
        plan_name = re.sub(r"\s+", " ", plan_name).strip()
        plan_name = f"Axis Max Life {plan_name} Plan"
        return plan_name

    async def handle_form1(self, page, plan_data=None):
        try:
            logger.info("Trying to handle Form 1")
            await page.wait_for_load_state('networkidle')
            await page.wait_for_timeout(2000)

            gender_label = await page.query_selector('label[for="233"]')  # Female
            if gender_label:
                await gender_label.click()
                logger.info("Clicked gender 'Female' label")
            else:
                logger.error("Could not find gender 'Female' label")
                return False

            tobacco_label = await page.query_selector('label[for="239"]')
            if tobacco_label:
                await tobacco_label.click()
                logger.info("Clicked tobacco 'No' label")
            else:
                logger.error("Could not find tobacco 'No' label")
                return False

            sumassured_label = await page.query_selector('label[for="16301"]')
            if sumassured_label:
                await sumassured_label.click()
                logger.info("Clicked sum assured '₹ 1 cr' label")
            else:
                logger.error("Could not find sum assured '₹ 1 cr' label")
                return False

            submit_button = await page.query_selector('button.gtm-leadform2')
            if submit_button:
                await submit_button.click()
                logger.info("Clicked 'Check Premium' button")
            else:
                logger.error("Could not find 'Check Premium' button")
                return False

            await page.wait_for_timeout(2000)
            # Handle modal after form submission
            if await self.handle_modal_form(page, plan_data=plan_data):
                logger.info("Modal handled successfully after Form 1")
                return True
            else:
                logger.error("Modal not handled after Form 1")
                return False
        except Exception as e:
            logger.error(f"Error in handle_form1: {e}")
            import traceback
            logger.error(f"Full stack trace: {traceback.format_exc()}")
            return False

    async def handle_form2(self, page, plan_data=None):
        try:
            current_url = page.url
            logger.info(f"Current URL: {current_url}")
            await page.wait_for_load_state('networkidle')
            await page.wait_for_timeout(2000)
            form_container = await page.query_selector('.form-container, form.w-full, section.form')
            if not form_container:
                logger.error("Could not find form container")
                return False
            try:
                name_input = await page.query_selector('#fullName')
                if name_input:
                    await name_input.fill("Aditya Jha")
                    logger.info("Filled name input")
                else:
                    logger.error("Could not find name input")
                    return False
                dob_input = await page.query_selector('input[name="dob"]')
                if dob_input:
                    await dob_input.fill("01/01/1990")
                    logger.info("Filled DOB input")
                    # Calculate age from DOB
                    from datetime import datetime
                    dob = datetime.strptime("01/01/1990", "%d/%m/%Y")
                    today = datetime.now()
                    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
                else:
                    logger.error("Could not find DOB input")
                    return False
                nri_label = await page.query_selector('label[for="64762"]')
                if nri_label:
                    await nri_label.click()
                    logger.info("Clicked NRI 'No' label")
                else:
                    logger.error("Could not find NRI 'No' label")
                    nri_labels = await page.query_selector_all('label[for]')
                    for lbl in nri_labels:
                        html = await lbl.inner_html()
                        logger.info(f"NRI label HTML: {html}")
                    return False
                phone_input = await page.query_selector('input[name="phoneNumber"]')
                if phone_input:
                    await phone_input.fill("9876543210")
                    logger.info("Filled phone input")
                else:
                    logger.error("Could not find phone input")
                    return False
                income_label = await page.query_selector('label[for="64764"]')
                if income_label:
                    await income_label.click()
                    logger.info("Clicked income '5-7 Lacs' label")
                else:
                    logger.error("Could not find income '5-7 Lacs' label")
                    income_labels = await page.query_selector_all('label[for]')
                    for lbl in income_labels:
                        html = await lbl.inner_html()
                        logger.info(f"Income label HTML: {html}")
                    return False
                submit_button = await page.query_selector('button.gtm-leadform')
                if submit_button:
                    await submit_button.click()
                    logger.info("Clicked submit button")
                else:
                    logger.error("Could not find submit button")
                    return False
                await page.wait_for_timeout(2000)
                # Handle modal after form submission
                if await self.handle_modal_form(page, plan_data=plan_data):
                    logger.info("Modal handled successfully after Form 2")
                    return True
                else:
                    logger.error("Modal not handled after Form 2")
                    return False
            except Exception as e:
                logger.error(f"Error interacting with form: {e}")
                return False
        except Exception as e:
            logger.error(f"Error in handle_form2: {e}")
            import traceback
            logger.error(f"Full stack trace: {traceback.format_exc()}")
            return False

    async def handle_form3(self, page, plan_data=None):
        try:
            logger.info("Trying to handle Form 3")
            await page.wait_for_load_state('networkidle')
            await page.wait_for_timeout(2000)
            name_input = await page.query_selector('#fullName')
            if name_input:
                await name_input.fill("Aditya Jha")
                logger.info("Filled name input")
            else:
                logger.error("Could not find name input")
                return False
            dob_input = await page.query_selector('input[name="dob"]')
            if dob_input:
                await dob_input.fill("01/01/1990")
                logger.info("Filled DOB input")
            else:
                logger.error("Could not find DOB input")
                return False
            nri_label = await page.query_selector('label[for="42508"]')
            if not nri_label:
                nri_label = await page.query_selector('label[for="64762"]')
            if nri_label:
                await nri_label.click()
                logger.info("Clicked NRI 'No' label")
            else:
                logger.error("Could not find NRI 'No' label")
                return False
            phone_input = await page.query_selector('input[name="phoneNumber"]')
            if not phone_input:
                phone_input = await page.query_selector('input#3')
            if phone_input:
                await phone_input.fill("9876543210")
                logger.info("Filled phone input")
            else:
                logger.error("Could not find phone input")
                return False
            income_label = await page.query_selector('label[for="16298"]')
            if not income_label:
                income_label = await page.query_selector('label[for="64764"]')
            if income_label:
                await income_label.click()
                logger.info("Clicked income '5-7 Lacs' label")
            else:
                logger.error("Could not find income '5-7 Lacs' label")
                return False
            submit_button = await page.query_selector('button.gtm-leadform')
            if not submit_button:
                submit_button = await page.query_selector('button.gtm-leadform2')
            if submit_button:
                await submit_button.click()
                logger.info("Clicked submit button")
            else:
                logger.error("Could not find submit button")
                return False
            await page.wait_for_timeout(2000)
            # Handle modal after form submission
            if await self.handle_modal_form(page, plan_data=plan_data):
                logger.info("Modal handled successfully after Form 3")
                return True
            else:
                logger.error("Modal not handled after Form 3")
                return False
        except Exception as e:
            logger.error(f"Error in handle_form3: {e}")
            import traceback
            logger.error(f"Full stack trace: {traceback.format_exc()}")
            return False

    async def handle_final_form_and_extract_premium(self, page, plan_data=None):
        try:
            logger.info("Trying to handle final form and extract monthly premium")
            await page.wait_for_load_state('networkidle')
            await page.wait_for_timeout(2000)

            # Wait for final form with multiple possible selectors
            try:
                logger.info("Waiting for final form to load...")
                selectors = [
                    '.jsx-1782489574',
                    'form[class*="form"]',
                    'div[class*="form"]',
                    'section[class*="form"]'
                ]
                form_visible = False
                for selector in selectors:
                    try:
                        await page.wait_for_selector(selector, timeout=2000, state='visible')
                        form_visible = True
                        logger.info(f"Found final form with selector: {selector}")
                        break
                    except Exception:
                        continue
                if not form_visible:
                    logger.error("Could not find final form with any selector")
                    return False, None
                await page.wait_for_timeout(2000)  # Wait for animations
            except Exception as e:
                logger.error(f"Error waiting for final form: {e}")
                return False, None

            # Life Cover is already selected as "2 Crore" by default
            logger.info("Life Cover already selected as 2 Crore")

            # Select Cover Till Age: 75 years (for free of cost option)
            try:
                cover_till_label = await page.query_selector('label[for="75"]')
                if cover_till_label:
                    await cover_till_label.click()
                    logger.info("Selected Cover Till Age: 75 years")
                    await page.wait_for_timeout(1000)  # Wait for selection to register
                else:
                    logger.warning("Could not find Cover Till Age: 75 years option")
            except Exception as e:
                logger.error(f"Error selecting Cover Till Age: 75 years: {e}")

            # Premium Payment Term is already selected as "Pay Till Age 60" by default
            logger.info("Premium Payment Term already selected as Pay Till Age 60")

            # Extract monthly premium from the selected Cover Till Age option
            premium_text = None
            try:
                # Try multiple selectors for premium
                premium_selectors = [
                    'label[for="75"] .premium',
                    '.premium',
                    '[class*="premium"]',
                    'span:has-text("₹")'
                ]
                for selector in premium_selectors:
                    try:
                        premium_element = await page.wait_for_selector(selector, timeout=2000, state='visible')
                        if premium_element:
                            premium_text = await premium_element.inner_text()
                            logger.info(f"Extracted monthly premium: {premium_text}")
                            if plan_data is not None:
                                plan_data['monthly_premium'] = premium_text
                            break
                    except Exception:
                        continue
                if not premium_text:
                    logger.error("Could not find monthly premium element with any selector")
            except Exception as e:
                logger.error(f"Error extracting monthly premium: {e}")

            # Click the Proceed button with retry logic
            try:
                proceed_selectors = [
                    'button#viewPlans',
                    'button:has-text("Proceed")',
                    'button[type="submit"]',
                    'button.btn-primary'
                ]
                
                proceed_btn = None
                for selector in proceed_selectors:
                    try:
                        proceed_btn = await page.wait_for_selector(selector, timeout=2000, state='visible')
                        if proceed_btn:
                            logger.info(f"Found proceed button with selector: {selector}")
                            break
                    except Exception:
                        continue

                if not proceed_btn:
                    logger.error("Could not find proceed button with any selector")
                    return False, premium_text

                # Try clicking with retries
                max_retries = 3
                retry_delay = 1000
                
                for attempt in range(max_retries):
                    try:
                        logger.info(f"Proceed button click attempt {attempt + 1}/{max_retries}")
                        
                        await proceed_btn.scroll_into_view_if_needed()
                        await proceed_btn.hover()
                        await page.wait_for_timeout(500)
                        
                        # Try normal click first
                        try:
                            await proceed_btn.click(timeout=5000)
                            logger.info("Proceed button click succeeded")
                            await page.wait_for_timeout(2000)
                            # After clicking, handle diabetic popup (click Proceed only, no radio)
                            await self.handle_diabetic_popup(page, plan_data=plan_data)
                            return True, premium_text
                        except Exception as e1:
                            logger.error(f"Normal click failed: {e1}")
                            
                            # Try dispatch_event
                            try:
                                await proceed_btn.dispatch_event('click')
                                logger.info("dispatch_event click succeeded")
                                await page.wait_for_timeout(2000)
                                await self.handle_diabetic_popup(page, plan_data=plan_data)
                                return True, premium_text
                            except Exception as e2:
                                logger.error(f"dispatch_event click failed: {e2}")
                                
                                # Try JS evaluation
                                try:
                                    await page.evaluate('(btn) => { btn.click(); btn.dispatchEvent(new Event("click")); }', proceed_btn)
                                    logger.info("JS evaluation click succeeded")
                                    await page.wait_for_timeout(2000)
                                    await self.handle_diabetic_popup(page, plan_data=plan_data)
                                    return True, premium_text
                                except Exception as e3:
                                    logger.error(f"JS evaluation click failed: {e3}")
                                    
                                    if attempt < max_retries - 1:
                                        await page.wait_for_timeout(retry_delay)
                                        retry_delay *= 2
                                        continue
                                    else:
                                        logger.error("All proceed button click attempts failed")
                                        return False, premium_text
                    except Exception as e:
                        logger.error(f"Error during proceed button click attempt {attempt + 1}: {e}")
                        if attempt < max_retries - 1:
                            await page.wait_for_timeout(retry_delay)
                            retry_delay *= 2
                            continue
                        else:
                            return False, premium_text
            except Exception as e:
                logger.error(f"Error handling proceed button: {e}")
                return False, premium_text

        except Exception as e:
            logger.error(f"Error in handle_final_form_and_extract_premium: {e}")
            import traceback
            logger.error(f"Full stack trace: {traceback.format_exc()}")
            return False, None

    async def handle_modal_form(self, page, plan_data=None):
        try:
            logger.info("Trying to handle modal form (new page)")
            await page.wait_for_load_state('networkidle')
            await page.wait_for_timeout(2000)

            # Gender: select 'Female' (id='gender_F')
            gender_label = await page.query_selector('label[for="gender_F"]')
            if gender_label:
                await gender_label.click()
                logger.info("Clicked gender 'Female' label in modal")
            else:
                logger.error("Could not find gender 'Female' label in modal")
                return False

            # Tobacco: select 'No' (id='tobacco_No')
            tobacco_label = await page.query_selector('label[for="tobacco_No"]')
            if tobacco_label:
                await tobacco_label.click()
                logger.info("Clicked tobacco 'No' label in modal")
            else:
                logger.error("Could not find tobacco 'No' label in modal")
                return False

            # Occupation: select 'Salaried' (id='occupation_salaried')
            occupation_label = await page.query_selector('label[for="occupation_salaried"]')
            if occupation_label:
                await occupation_label.click()
                logger.info("Clicked occupation 'Salaried' label in modal")
            else:
                logger.error("Could not find occupation 'Salaried' label in modal")
                return False

            # Education: select 'Graduate & Above' (id='education_graduateAndAbove')
            education_label = await page.query_selector('label[for="education_graduateAndAbove"]')
            if education_label:
                await education_label.click()
                logger.info("Clicked education 'Graduate & Above' label in modal")
            else:
                logger.error("Could not find education 'Graduate & Above' label in modal")
                return False

            # Wait for modal to be fully loaded and visible
            try:
                logger.info("Waiting for modal to be fully loaded...")
                await page.wait_for_selector('.modal-content, .modal-dialog', state='visible', timeout=5000)
                # Wait for any loading spinners to disappear
                await page.wait_for_selector('.loading, .spinner, .loader', state='hidden', timeout=5000)
                await page.wait_for_timeout(1000)  # Additional wait for animations
            except Exception as e:
                logger.error(f"Modal not visible: {e}")
                return False

            # Try three click methods for the 'Check Coverage' button
            try:
                # Try multiple selectors for the button
                selectors = [
                    'button#viewPlans',
                    'button:has-text("Check Coverage")',
                    'button.gtm-leadform',
                    'button[type="submit"]',
                    'button.btn-primary'
                ]
                
                btn = None
                for selector in selectors:
                    try:
                        logger.info(f"Trying to find button with selector: {selector}")
                        btn = await page.wait_for_selector(selector, timeout=2000, state='visible')
                        if btn:
                            logger.info(f"Found button with selector: {selector}")
                            # Wait for button to be enabled
                            await page.wait_for_function(
                                '(btn) => !btn.disabled && !btn.classList.contains("disabled") && btn.offsetParent !== null',
                                btn,
                                timeout=5000
                            )
                            break
                    except Exception as e:
                        logger.debug(f"Selector {selector} not found: {e}")
                        continue

                if not btn:
                    logger.error("Could not find button with any selector")
                    return False
                
                # Try clicking with retries
                max_retries = 3
                retry_delay = 1000  # Start with 1 second delay
                
                for attempt in range(max_retries):
                    try:
                        logger.info(f"Click attempt {attempt + 1}/{max_retries}")
                        
                        # Ensure button is in view and hoverable
                        await btn.scroll_into_view_if_needed()
                        await btn.hover()
                        await page.wait_for_timeout(500)  # Wait for hover effect
                        
                        # Check if button is covered by any overlays
                        is_clickable = await page.evaluate('''(btn) => {
                            const rect = btn.getBoundingClientRect();
                            const element = document.elementFromPoint(rect.left + rect.width/2, rect.top + rect.height/2);
                            return element === btn || btn.contains(element);
                        }''', btn)
                        
                        if not is_clickable:
                            logger.error("Button is covered by an overlay")
                            if attempt < max_retries - 1:
                                await page.wait_for_timeout(retry_delay)
                                retry_delay *= 2
                                continue
                            return False
                        
                        # Try normal click first
                        try:
                            logger.info("Attempting normal click")
                            await btn.click(timeout=5000)
                            logger.info("Normal click succeeded")
                            await page.wait_for_timeout(1000)
                            break  # Exit retry loop after success
                        except Exception as e1:
                            logger.error(f"Normal click failed: {e1}")
                            
                            # Try dispatch_event
                            try:
                                logger.info("Attempting dispatch_event click")
                                await btn.dispatch_event('click')
                                logger.info("dispatch_event click succeeded")
                                await page.wait_for_timeout(1000)
                                break  # Exit retry loop after success
                            except Exception as e2:
                                logger.error(f"dispatch_event click failed: {e2}")
                                
                                # Try JS evaluation as last resort
                                try:
                                    logger.info("Attempting JS evaluation click")
                                    await page.evaluate('(btn) => { btn.click(); btn.dispatchEvent(new Event("click")); }', btn)
                                    logger.info("JS evaluation click succeeded")
                                    await page.wait_for_timeout(1000)
                                    break  # Exit retry loop after success
                                except Exception as e3:
                                    logger.error(f"JS evaluation click failed: {e3}")
                                    
                                    if attempt < max_retries - 1:
                                        logger.info(f"Waiting {retry_delay}ms before next attempt")
                                        await page.wait_for_timeout(retry_delay)
                                        retry_delay *= 2  # Exponential backoff
                                        continue
                                    else:
                                        logger.error("All click attempts failed")
                                        return False
                    except Exception as e:
                        logger.error(f"Error during click attempt {attempt + 1}: {e}")
                        if attempt < max_retries - 1:
                            await page.wait_for_timeout(retry_delay)
                            retry_delay *= 2
                            continue
                        else:
                            return False
            except Exception as e:
                logger.error(f"Could not find or click 'Check Coverage' button in modal: {e}")
                return False

            # Wait for the final form to load after modal submission
            try:
                logger.info("Waiting for final form to load...")
                await page.wait_for_selector('.jsx-1782489574', timeout=5000, state='visible')
                await page.wait_for_timeout(2000)  # Additional wait for animations
            except Exception as e:
                logger.error(f"Final form not visible: {e}")
                return False

            # After modal, handle final form and extract premium
            final_success, premium = await self.handle_final_form_and_extract_premium(page, plan_data=plan_data)
            if final_success:
                logger.info("Final form handled and premium extracted after modal")
            else:
                logger.error("Final form not handled after modal")
            return final_success
        except Exception as e:
            logger.error(f"Error in handle_modal_form: {e}")
            import traceback
            logger.error(f"Full stack trace: {traceback.format_exc()}")
            return False

    async def handle_diabetic_popup(self, page, plan_data=None):
        try:
            logger.info("Checking for diabetic popup to click Proceed only (no radio)")
            # Wait for the popup to appear (short timeout)
            try:
                await page.wait_for_selector('div.rider-popup-content', timeout=3000, state='visible')
                logger.info("Diabetic popup appeared")
            except Exception:
                logger.info("No diabetic popup detected")
                return True  # Not an error if popup doesn't appear
            # Click the Proceed button inside the popup
            try:
                proceed_btn = await page.wait_for_selector('div.rider-popup-content button#viewPlans', timeout=3000, state='visible')
                await proceed_btn.click()
                logger.info("Clicked Proceed button in diabetic popup (no radio)")
                await page.wait_for_timeout(1000)
                # After proceeding, extract Add-On Riders
                await self.extract_add_on_riders(page, plan_data=plan_data)
                return True
            except Exception as e:
                logger.error(f"Could not click Proceed in diabetic popup: {e}")
                return False
        except Exception as e:
            logger.error(f"Error in handle_diabetic_popup: {e}")
            return False

    async def extract_add_on_riders(self, page, plan_data=None):
        try:
            logger.info("Waiting for Add-On Riders container to appear...")
            await page.wait_for_selector('div.rider-container', timeout=8000, state='visible')
            logger.info("Add-On Riders container appeared")
            riders = []
            rider_cards = await page.query_selector_all('div.rider-container .rider-card')
            for idx, card in enumerate(rider_cards):
                name = coverage = premium = None
                try:
                    name_el = await card.query_selector('span.title')
                    if name_el:
                        name = await name_el.inner_text()
                except Exception:
                    name = None
                try:
                    coverage_el = await card.query_selector('.coverage-amount')
                    if coverage_el:
                        coverage = await coverage_el.inner_text()
                except Exception:
                    coverage = None
                try:
                    premium_el = await card.query_selector('.rider-premium')
                    if premium_el:
                        premium = await premium_el.inner_text()
                except Exception:
                    premium = None
                # If all are None, log the card's HTML for debugging
                if not name and not coverage and not premium:
                    try:
                        card_html = await card.inner_html()
                        logger.warning(f"Could not extract fields for rider card {idx}, card HTML: {card_html}")
                        # Fallback: extract all text
                        all_text = await card.inner_text()
                        logger.info(f"Fallback all text for rider card {idx}: {all_text}")
                    except Exception:
                        pass
                riders.append({
                    'name': name,
                    'coverage': coverage,
                    'premium': premium
                })
            logger.info(f"Extracted {len(riders)} add-on riders")
            if plan_data is not None:
                plan_data['add_on_riders'] = riders

            # Click the skip button after extracting riders
            try:
                logger.info("Looking for skip button...")
                skip_button = await page.wait_for_selector('button#viewPlans', timeout=5000, state='visible')
                if skip_button:
                    logger.info("Found skip button, clicking...")
                    await skip_button.click()
                    logger.info("Successfully clicked skip button")
                    await page.wait_for_timeout(2000)  # Wait for any animations/transitions
                    
                    # Handle the final details form after clicking skip
                    form_success = await self.handle_final_details_form(page, plan_data)
                    if form_success:
                        logger.info("Successfully handled final details form")
                    else:
                        logger.error("Failed to handle final details form")
                else:
                    logger.warning("Skip button not found")
            except Exception as e:
                logger.error(f"Error clicking skip button: {e}")

            return riders
        except Exception as e:
            logger.error(f"Error extracting add-on riders: {e}")
            return []

    async def extract_final_quote_details(self, page, plan_data=None):
        try:
            logger.info("Waiting for final quote details to appear...")
            await page.wait_for_selector('div.jsx-1807434918.px-2.card', timeout=5000, state='visible')
            logger.info("Final quote details appeared")

            # Extract all the details
            quote_details = {}

            # Extract Equote Number
            equote_el = await page.query_selector('div.jsx-933454567.middle.sec.undefined.text-xs.data-value span')
            if equote_el:
                quote_details['equote_number'] = await equote_el.inner_text()
                logger.info(f"Extracted Equote Number: {quote_details['equote_number']}")

            # Extract Policy Name
            policy_name_el = await page.query_selector('div.jsx-933454567.middle.sec.undefined.text-sm.data-value span')
            if policy_name_el:
                quote_details['policy_name'] = await policy_name_el.inner_text()
                logger.info(f"Extracted Policy Name: {quote_details['policy_name']}")

            # Extract Life Cover
            life_cover_el = await page.query_selector('div.jsx-933454567.middle.sec.undefined.text-sm.data-value span:has-text("₹")')
            if life_cover_el:
                quote_details['life_cover'] = await life_cover_el.inner_text()
                logger.info(f"Extracted Life Cover: {quote_details['life_cover']}")

            # Extract Cover till age
            cover_age_el = await page.query_selector('div.jsx-933454567.middle.sec.undefined.text-sm.data-value span:has-text("yrs")')
            if cover_age_el:
                quote_details['cover_till_age'] = await cover_age_el.inner_text()
                logger.info(f"Extracted Cover till age: {quote_details['cover_till_age']}")

            # Extract Base Premium
            base_premium_el = await page.query_selector('div.jsx-933454567.middle.sec.undefined.text-sm.data-value span.font-weight-bold')
            if base_premium_el:
                quote_details['base_premium'] = await base_premium_el.inner_text()
                logger.info(f"Extracted Base Premium: {quote_details['base_premium']}")

            # Extract Add-ons
            addons = []
            addon_elements = await page.query_selector_all('div.jsx-933454567.accordion-title span')
            for addon_el in addon_elements:
                addon_name = await addon_el.inner_text()
                if addon_name and "Monthly Add-ons" not in addon_name:
                    addons.append(addon_name)
            quote_details['add_ons'] = addons
            logger.info(f"Extracted Add-ons: {addons}")

            # Extract Base + Add-ons (Excl. of GST)
            base_addons_el = await page.query_selector('div.jsx-933454567.baseAddOns span.font-weight-semi-bold')
            if base_addons_el:
                quote_details['base_plus_addons'] = await base_addons_el.inner_text()
                logger.info(f"Extracted Base + Add-ons: {quote_details['base_plus_addons']}")

            # Extract GST Amount
            gst_el = await page.query_selector('div.jsx-933454567:has-text("GST Amount") + div span.font-weight-semi-bold')
            if gst_el:
                quote_details['gst_amount'] = await gst_el.inner_text()
                logger.info(f"Extracted GST Amount: {quote_details['gst_amount']}")

            # Extract Total Amount
            total_amount_el = await page.query_selector('div.jsx-1297005779.flex.justify-between.py-3 p.text-sm.font-bold.discount-summary-label:last-child')
            if total_amount_el:
                quote_details['total_amount'] = await total_amount_el.inner_text()
                logger.info(f"Extracted Total Amount: {quote_details['total_amount']}")

            # Extract Premium from 2nd year
            second_year_el = await page.query_selector('div.jsx-1297005779.flex.justify-between.py-3.border-t p.text-sm.font-bold.discount-summary-label:last-child')
            if second_year_el:
                quote_details['premium_from_second_year'] = await second_year_el.inner_text()
                logger.info(f"Extracted Premium from 2nd year: {quote_details['premium_from_second_year']}")

            if plan_data is not None:
                plan_data['quote_details'] = quote_details

            return quote_details
        except Exception as e:
            logger.error(f"Error extracting final quote details: {e}")
            return None

    async def handle_final_details_form(self, page, plan_data=None):
        try:
            logger.info("Waiting for final details form to appear...")
            await page.wait_for_selector('input#firstName', timeout=5000, state='visible')
            logger.info("Final details form appeared")

            # Fill First Name
            await page.fill('input#firstName', 'Aditya')
            logger.info("Filled First Name")

            # Fill Middle Name (Optional)
            await page.fill('input#middleName', 'Kumar')
            logger.info("Filled Middle Name")

            # Fill Last Name
            await page.fill('input#lastName', 'Jha')
            logger.info("Filled Last Name")

            # Fill Email
            await page.fill('input#email', 'aditya.jha@gmail.com')
            logger.info("Filled Email")

            # Fill Annual Income
            await page.fill('input#eligibilityAnnualIncome', '700000')
            logger.info("Filled Annual Income")

            # Fill Pincode
            await page.fill('input#pincode', '110001')
            logger.info("Filled Pincode")

            # Wait for any animations/validations
            await page.wait_for_timeout(1000)

            # Click the checkbox for benefit illustration agreement
            try:
                checkbox = await page.query_selector('input[type="checkbox"]')
                if checkbox:
                    await checkbox.click()
                    logger.info("Clicked benefit illustration agreement checkbox")
            except Exception as e:
                logger.warning(f"Could not find or click checkbox: {e}")

            # Try to find and click the submit button
            try:
                submit_button = await page.query_selector('button[type="submit"]')
                if submit_button:
                    await submit_button.click()
                    logger.info("Clicked submit button on final form")
                    await page.wait_for_timeout(2000)  # Wait for submission
                else:
                    logger.warning("Submit button not found")
            except Exception as e:
                logger.error(f"Error clicking submit button: {e}")

            # Click the Proceed button
            try:
                logger.info("Looking for Proceed button...")
                proceed_button = await page.wait_for_selector('button#viewPlans.unified-button-primary', timeout=5000, state='visible')
                if proceed_button:
                    logger.info("Found Proceed button, clicking...")
                    await proceed_button.click()
                    logger.info("Successfully clicked Proceed button")
                    await page.wait_for_timeout(2000)  # Wait for any transitions

                    # Extract final quote details after clicking proceed
                    quote_details = await self.extract_final_quote_details(page, plan_data)
                    if quote_details:
                        logger.info("Successfully extracted final quote details")
                    else:
                        logger.error("Failed to extract final quote details")
                else:
                    logger.warning("Proceed button not found")
            except Exception as e:
                logger.error(f"Error clicking Proceed button: {e}")

            return True
        except Exception as e:
            logger.error(f"Error handling final details form: {e}")
            return False

async def main():
    scraper = None
    try:
        logger.info("Starting term plan scraper")
        scraper = TermPlanScraper(headless=False, limit=1)
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

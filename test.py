from patchright.async_api import async_playwright
from flask import Flask, request, jsonify
import asyncio
import logging
import re
import hypercorn.asyncio
import hypercorn.config

# Set up logging to show more info for debugging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

def convert_fractional_to_decimal(fraction):
    """Convert fractional odds (e.g., '5/2') to decimal odds."""
    try:
        if '/' in fraction:
            numerator, denominator = map(float, fraction.split('/'))
            decimal = (numerator / denominator) + 1
            return round(decimal, 2)
        return float(fraction)  # Handle decimal odds directly
    except (ValueError, ZeroDivisionError):
        return None

async def scrape_bet365(fixture, bet_type):
    async with async_playwright() as p:
        browser = None
        try:
            # Launch browser with stealth configuration
            browser = await p.chromium.launch_persistent_context(
                user_data_dir="./browser_data",
                channel="chrome",
                headless=False,
                no_viewport=True,
            )

            # Create a new page
            page = await browser.new_page()

            # Navigate to Bet365
            url = "https://www.bet365.com"
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Wait for page content
            try:
                await page.wait_for_selector("body", timeout=10000)
            except Exception as e:
                logger.error(f"Error waiting for content: {e}")

            # Extract the first team from the fixture
            search_query = fixture.split(" - ")[0]  # e.g., "FC Anyang" from "FC Anyang - FC Seoul"

            # Locate and interact with the search bar
            try:
                search_bar_selector = "div.wc-SearchBar_Inner"
                await page.wait_for_selector(search_bar_selector, timeout=10000)
                search_bar = await page.query_selector(search_bar_selector)
                await search_bar.click()

                input_selector = "input[type='text'], input[type='search']"
                try:
                    input_field = await page.query_selector(input_selector)
                    if input_field:
                        await input_field.click()
                        await input_field.type(search_query, delay=100)
                    else:
                        await search_bar.type(search_query, delay=100)
                except Exception as e:
                    logger.error(f"Error interacting with input field: {e}")
                    await search_bar.type(search_query, delay=100)

                await page.keyboard.press("Enter")

            except Exception as e:
                logger.error(f"Error interacting with search bar: {e}")
                await page.screenshot(path="search_error_screenshot.png")
                return {"error": "Search bar interaction failed"}

            # Wait for search results and find the target match
            try:
                await page.wait_for_selector("span.ssm-SiteSearchLabelOnlyParticipant_Name", timeout=15000)
                name_selector = "span.ssm-SiteSearchLabelOnlyParticipant_Name"
                name_elements = await page.query_selector_all(name_selector)

                found = False
                for name_element in name_elements:
                    try:
                        name_text = await name_element.inner_text()
                        if search_query in name_text and " v " in name_text:
                            parent = await name_element.query_selector("xpath=../..")
                            if parent:
                                clickable = await parent.query_selector("a, button")
                                if clickable:
                                    await clickable.click()
                                    found = True
                                    break
                                else:
                                    try:
                                        await name_element.click()
                                        found = True
                                        break
                                    except Exception as e:
                                        await parent.click()
                                        found = True
                                        break
                    except Exception as e:
                        logger.error(f"Error processing name element: {e}")

                if not found:
                    await page.screenshot(path="no_match_screenshot.png")
                    return {"error": "Match not found in search results"}

                await page.wait_for_load_state("networkidle", timeout=10000)

                # Determine which tab to click based on bet_type
                is_corner_bet = "Corner" in bet_type or "Corners" in bet_type
                tab_to_click = "Corners/Cards" if is_corner_bet else "Goals"
                
                try:
                    # Create a more generic tab selector that works for both Goals and Corners/Cards
                    tab_selector = f"div.ipe-GridHeaderTabLink:has-text('{tab_to_click}')"
                    await page.wait_for_selector(tab_selector, timeout=15000)
                    tab = await page.query_selector(tab_selector)
                    
                    if tab:
                        logger.info(f"Found and clicking on {tab_to_click} tab")
                        await tab.click()
                        await page.wait_for_timeout(5000)
                    else:
                        # Fallback to JavaScript if the selector doesn't work
                        logger.info(f"Using JavaScript to click on {tab_to_click} tab")
                        await page.evaluate(f"""
                            Array.from(document.querySelectorAll('div.ipe-GridHeaderTabLink')).find(
                                el => el.textContent.includes('{tab_to_click}')
                            )?.click();
                        """)
                        await page.wait_for_timeout(5000)

                except Exception as e:
                    logger.error(f"Error clicking on {tab_to_click} tab: {e}")
                    await page.screenshot(path=f"{tab_to_click.lower().replace('/', '_')}_tab_error_screenshot.png")
                    return {"error": f"{tab_to_click} tab navigation failed"}

            except Exception as e:
                logger.error(f"Error processing search results or match page: {e}")
                await page.screenshot(path="results_error_screenshot.png")
                return {"error": "Match page loading failed"}

            try:
                # Wait for market grid to load
                await page.wait_for_selector("div.ipe-EventViewDetail_MarketGrid", timeout=10000)
                
                # Parse the bet_type string
                bet_parts = bet_type.split(" | ")
                market = bet_parts[0]
                condition = bet_parts[1] if len(bet_parts) > 1 else ""
                
                # Map the market names to what actually appears on the Bet365 site
                market_mapping = {
                    "First Half": "1st Half Goals",
                    "Match Total": "Match Goals",
                    "First Half Asian Corners": "1st Half Asian Corners",
                    "Asian Corners": "Asian Corners",
                    "Match Corners": "Match Corners",
                    "First Half Corners": "1st Half Corners"
                }
                
                # Handle special case for Asian Corners
                if "Asian Corner" in bet_type or "Asian Corners" in bet_type:
                    if "First Half" in bet_type or "1st Half" in bet_type:
                        market_name = "1st Half Asian Corners"
                    else:
                        market_name = "Asian Corners"
                elif "Corner" in bet_type or "Corners" in bet_type:
                    if "First Half" in bet_type or "1st Half" in bet_type:
                        market_name = "1st Half Corners"
                    else:
                        market_name = "Match Corners"
                else:
                    market_name = market_mapping.get(market, market)
                
                # For Match Goals/Corners, we may need to check multiple markets
                markets_to_check = []
                if market_name == "Match Goals":
                    markets_to_check = ["Match Goals", "Alternative Match Goals"]
                elif market_name == "Match Corners":
                    markets_to_check = ["Match Corners", "Alternative Match Corners"]
                else:
                    markets_to_check = [market_name]
                
                # Extract threshold and whether it's over/under
                threshold_match = re.match(r"Over (\d+\.?\d*)|Under (\d+\.?\d*)", condition)
                threshold = float(threshold_match.group(1) or threshold_match.group(2)) if threshold_match else None
                is_over = condition.startswith("Over")
                
                logger.info(f"Looking for markets: {markets_to_check}, threshold: {threshold}, is_over: {is_over}")
                
                # Get all market pods on the page
                market_pods = await page.query_selector_all("div.gl-MarketGroupPod")
                
                logger.info(f"Found {len(market_pods)} market pods")
                
                # Get all market titles for debugging
                market_titles = await page.evaluate("""
                    () => {
                        const titles = [];
                        document.querySelectorAll('.sip-MarketGroupButton_Text').forEach(el => {
                            titles.push(el.innerText);
                        });
                        return titles;
                    }
                """)
                logger.info(f"Available markets: {market_titles}")
                
                # Look through each market in our priority list to find the best match
                # We'll collect data from each market that we find
                market_data = {}
                
                for current_market in markets_to_check:
                    # First try using the pod objects
                    current_target_market = None
                    
                    for pod in market_pods:
                        pod_title = await pod.evaluate("el => el.querySelector('.sip-MarketGroupButton_Text')?.innerText")
                        if pod_title and pod_title == current_market:
                            current_target_market = pod
                            logger.info(f"Found target market: {current_market}")
                            break
                    
                    # If not found with objects, try JavaScript evaluation
                    if not current_target_market:
                        current_target_handle = await page.evaluate_handle(f"""
                            () => {{
                                const marketTitle = "{current_market}";
                                const titleEls = Array.from(document.querySelectorAll('.sip-MarketGroupButton_Text'));
                                const marketEl = titleEls.find(el => el.innerText === marketTitle);
                                return marketEl ? marketEl.closest('.gl-MarketGroupPod') : null;
                            }}
                        """)
                        
                        if current_target_handle and not await current_target_handle.evaluate("el => el === null"):
                            current_target_market = current_target_handle
                            logger.info(f"Found target market with JavaScript: {current_market}")
                    
                    # If we found this market, process its odds data
                    if current_target_market:
                        # Process this market and store its data
                        market_data[current_market] = {
                            "market_pod": current_target_market,
                            "parsed_data": {},  # Will be filled with threshold data
                            "thresholds": []    # Will be filled with available thresholds
                        }
                
                # If no markets found after checking all options
                if not market_data:
                    # Take a screenshot for debugging
                    await page.screenshot(path=f"market_search_screenshot.png")
                    
                    # Return an error with available markets for debugging
                    return {
                        "error": f"No markets found. Available markets: {', '.join(market_titles)}",
                        "fixture": fixture,
                        "bet_type": bet_type
                    }

                # Process each market we found
                for market_name, market_info in market_data.items():
                    target_market = market_info["market_pod"]
                    
                    # Extract the market content and structure
                    market_content = await target_market.evaluate("el => el.innerText")
                    logger.info(f"Market content from {market_name}: {market_content}")
                    
                    # Also get the HTML structure to help with parsing
                    market_html = await target_market.evaluate("el => el.outerHTML")
                    with open(f"{market_name.replace(' ', '_')}_structure.html", "w", encoding="utf-8") as f:
                        f.write(market_html)

                    # Parse and format the content
                    lines = [line.strip() for line in market_content.split('\n') if line.strip()]
                    logger.info(f"Raw lines: {lines}")
                    
                    # Initialize variables for parsing
                    parsed_data = {}
                    thresholds = []
                    over_odds = []
                    under_odds = []
                    current_section = None
                    
                    # First pass: extract thresholds, identify over/under sections
                    for i, line in enumerate(lines):
                        # Skip the market name and BB lines
                        if line == market_name or line == "BB" or line == " ":
                            continue
                        
                        # Check if this line is "Over" or "Under" to mark the section
                        if line == "Over":
                            current_section = "Over"
                            continue
                        elif line == "Under":
                            current_section = "Under"
                            continue
                        
                        # If we're not in a section yet, this might be a threshold value
                        if current_section is None:
                            try:
                                val = float(line)
                                thresholds.append(val)
                            except ValueError:
                                pass
                        # If we're in the "Over" section, collect odds
                        elif current_section == "Over":
                            over_odds.append(line)
                        # If we're in the "Under" section, collect odds
                        elif current_section == "Under":
                            under_odds.append(line)
                    
                    logger.info(f"Thresholds: {thresholds}")
                    logger.info(f"Over odds: {over_odds}")
                    logger.info(f"Under odds: {under_odds}")
                    
                    # Try multiple parsing approaches to handle different market layouts
                    # Approach 1: Direct mapping if counts align
                    if len(thresholds) == len(over_odds) == len(under_odds):
                        for i in range(len(thresholds)):
                            parsed_data[thresholds[i]] = {"Over": over_odds[i], "Under": under_odds[i]}
                    
                    # Approach 2: Handle different lengths but maintain order
                    elif len(thresholds) == len(over_odds) and len(under_odds) > 0:
                        for i in range(len(thresholds)):
                            parsed_data[thresholds[i]] = {
                                "Over": over_odds[i] if i < len(over_odds) else None,
                                "Under": under_odds[i] if i < len(under_odds) else None
                            }
                    
                    # Approach 3: Look for pattern matches in the content
                    else:
                        # Try to match threshold with over/under odds directly from content
                        pattern = r"(\d+\.?\d*)\s+([0-9/.]+)\s+([0-9/.]+)"
                        matches = re.findall(pattern, market_content)
                        
                        if matches:
                            logger.info(f"Found pattern matches: {matches}")
                            for match in matches:
                                try:
                                    thresh = float(match[0])
                                    parsed_data[thresh] = {"Over": match[1], "Under": match[2]}
                                except (ValueError, IndexError):
                                    pass
                        
                        # If still no data, try another pattern approach
                        if not parsed_data:
                            over_sections = re.findall(r"Over\s+([\d.]+)\s+([0-9/.]+)", market_content)
                            under_sections = re.findall(r"Under\s+([\d.]+)\s+([0-9/.]+)", market_content)
                            
                            logger.info(f"Over sections: {over_sections}")
                            logger.info(f"Under sections: {under_sections}")
                            
                            # Combine the data from both patterns
                            for section in over_sections:
                                try:
                                    thresh = float(section[0])
                                    if thresh not in parsed_data:
                                        parsed_data[thresh] = {"Over": section[1], "Under": None}
                                    else:
                                        parsed_data[thresh]["Over"] = section[1]
                                except (ValueError, IndexError):
                                    pass
                                    
                            for section in under_sections:
                                try:
                                    thresh = float(section[0])
                                    if thresh not in parsed_data:
                                        parsed_data[thresh] = {"Over": None, "Under": section[1]}
                                    else:
                                        parsed_data[thresh]["Under"] = section[1]
                                except (ValueError, IndexError):
                                    pass
                    
                    # Store the parsed data and thresholds in our market info
                    market_info["parsed_data"] = parsed_data
                    market_info["thresholds"] = list(parsed_data.keys())
                    logger.info(f"Parsed data from {market_name}: {parsed_data}")
                
                # Look for the exact threshold across all markets
                exact_match_market = None
                for market_name, market_info in market_data.items():
                    if threshold in market_info["parsed_data"]:
                        exact_match_market = market_name
                        break
                
                # If found an exact match
                if exact_match_market:
                    parsed_data = market_data[exact_match_market]["parsed_data"]
                    over_odds = parsed_data[threshold].get("Over", "N/A")
                    under_odds = parsed_data[threshold].get("Under", "N/A")
                    
                    over_decimal = convert_fractional_to_decimal(over_odds) if over_odds and over_odds != "N/A" else None
                    under_decimal = convert_fractional_to_decimal(under_odds) if under_odds and under_odds != "N/A" else None
                    
                    over_decimal_str = f"({over_decimal})" if over_decimal else "(N/A)"
                    under_decimal_str = f"({under_decimal})" if under_decimal else "(N/A)"
                    
                    return {
                        "fixture": fixture,
                        "bet_type": bet_type,
                        "market_used": exact_match_market,
                        "odds": f"{threshold} {over_odds} {over_decimal_str} | {under_odds} {under_decimal_str}"
                    }
                # If no exact match, find the closest threshold
                else:
                    best_market = None
                    closest_threshold = None
                    smallest_diff = float('inf')
                    
                    # Find the market with the closest threshold
                    for market_name, market_info in market_data.items():
                        if not market_info["thresholds"]:
                            continue
                            
                        current_closest = min(market_info["thresholds"], key=lambda x: abs(x - threshold))
                        current_diff = abs(current_closest - threshold)
                        
                        if current_diff < smallest_diff:
                            smallest_diff = current_diff
                            closest_threshold = current_closest
                            best_market = market_name
                    
                    if best_market:
                        parsed_data = market_data[best_market]["parsed_data"]
                        over_odds = parsed_data[closest_threshold].get("Over", "N/A")
                        under_odds = parsed_data[closest_threshold].get("Under", "N/A")
                        
                        over_decimal = convert_fractional_to_decimal(over_odds) if over_odds and over_odds != "N/A" else None
                        under_decimal = convert_fractional_to_decimal(under_odds) if under_odds and under_odds != "N/A" else None
                        
                        over_decimal_str = f"({over_decimal})" if over_decimal else "(N/A)"
                        under_decimal_str = f"({under_decimal})" if under_decimal else "(N/A)"
                        
                        return {
                            "fixture": fixture,
                            "bet_type": bet_type,
                            "market_used": best_market,
                            "note": f"Exact threshold {threshold} not found. Using closest: {closest_threshold}",
                            "odds": f"{closest_threshold} {over_odds} {over_decimal_str} | {under_odds} {under_decimal_str}"
                        }
                    else:
                        # No usable data found in any market
                        return {
                            "error": f"No odds found for {bet_type} with threshold {threshold} in any available market",
                            "fixture": fixture,
                            "bet_type": bet_type
                        }

            except Exception as e:
                logger.error(f"Error scraping markets: {e}")
                await page.screenshot(path="markets_error_screenshot.png")
                html = await page.content()
                with open("markets_error.html", "w", encoding="utf-8") as f:
                    f.write(html)
                return {"error": f"Market scraping failed: {str(e)}"}

        except Exception as e:
            logger.error(f"An error occurred: {e}")
            return {"error": f"General scraping error: {str(e)}"}
        finally:
            if browser:
                await browser.close()

@app.route('/get-odds', methods=['POST'])
async def get_odds():
    data = request.get_json()
    if not data or 'fixture' not in data or 'bet_type' not in data:
        return jsonify({"error": "Missing fixture or bet_type"}), 400

    fixture = data['fixture']
    bet_type = data['bet_type']
    result = await scrape_bet365(fixture, bet_type)
    return jsonify(result)

if __name__ == "__main__":
    import asyncio
    config = hypercorn.config.Config()
    config.bind = ["127.0.0.1:8000"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
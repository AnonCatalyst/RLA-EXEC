import argparse
import asyncio
import re
import os
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from tqdm import tqdm  # For progress bars
from aiohttp import ClientSession, ClientError, ClientResponseError, hdrs
import logging
from colorama import init, Fore, Style
from fake_useragent import UserAgent
from src.diver.wdc import get_ip_info   
import json


init(autoreset=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
file_handler = logging.FileHandler('error.log')  # File handler for error log
file_handler.setLevel(logging.ERROR)  # Set to log only errors
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

async def fetch_html(url, session, retries=3):
    """Fetch HTML content from a URL asynchronously with retries and random user agents."""
    try:
        user_agent = UserAgent().random
        headers = {
            hdrs.USER_AGENT: user_agent  # Random user agent generated by fake_useragent
        }
        async with session.get(url, headers=headers, timeout=10) as response:
            response.raise_for_status()
            return await response.text()
    except asyncio.TimeoutError:
        logger.error(f"Timeout fetching {url}")
    except ClientResponseError as e:
        logger.error(f"ClientResponseError fetching {url}: {e.status}")
    except ClientError as e:
        logger.error(f"ClientError fetching {url}: {e}")
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")

    if retries > 0:
        await asyncio.sleep(2 ** (3 - retries))  # Exponential backoff: 2^(3-retries) seconds
        return await fetch_html(url, session, retries - 1)
    else:
        return None

def extract_emails(html):
    """Extract email addresses from HTML content.

    Args:
        html (str): The HTML content of the page.

    Returns:
        set: A set of unique email addresses found in the HTML.
    """
    emails = set(re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', html))
    return emails

def get_links(html, base_url):
    """Extract internal and external links from HTML."""
    try:
        soup = BeautifulSoup(html, 'html.parser')
        internal_links = set()
        external_links = set()
        parsed_base_url = urlparse(base_url)
        base_domain = parsed_base_url.netloc

        for link in soup.find_all('a', href=True):
            href = link['href']
            absolute_url = urljoin(base_url, href)
            parsed_url = urlparse(absolute_url)
            
            if parsed_url.scheme in ('http', 'https'):
                if parsed_url.netloc == base_domain:
                    internal_links.add(absolute_url)
                else:
                    external_links.add((absolute_url, base_url))

        return internal_links, external_links
    except Exception as e:
        logger.error(f"Error parsing links from {base_url}: {e}")
        return set(), set()

async def crawl_website(url, session, visited_urls, all_external_links, output_dir):
    try:
        if url in visited_urls:
            return None
        
        visited_urls.add(url)
        
        html = await fetch_html(url, session)
        if not html:
            return None
        
        soup = BeautifulSoup(html, 'html.parser')
        title = soup.title.string.strip() if soup.title else "No title"
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        description = meta_desc.get('content').strip() if meta_desc else "No description"
        
        meta_data = {}
        for tag in tqdm(soup.find_all('meta'), desc=Fore.CYAN + "Extracting Meta Tags", unit=Fore.CYAN + " tag"):
            tag_attrs = tag.attrs
            if 'name' in tag_attrs:
                meta_data[tag_attrs['name']] = tag_attrs.get('content', '')
            elif 'property' in tag_attrs:
                meta_data[tag_attrs['property']] = tag_attrs.get('content', '')
            elif 'http-equiv' in tag_attrs:
                meta_data[tag_attrs['http-equiv']] = tag_attrs.get('content', '')
            elif 'charset' in tag_attrs:
                meta_data['charset'] = tag_attrs.get('charset', '')

        internal_links, external_links = get_links(html, url)
        
        # Separate sets for accumulating new links
        new_internal_links = set()
        new_external_links = set()

        # Extract emails from internal links
        all_emails = set()
        for link in tqdm(internal_links, desc=Fore.YELLOW + "Extracting Emails", unit=Fore.YELLOW + " link"):
            try:
                internal_html = await fetch_html(link, session)
                if internal_html:
                    emails = extract_emails(internal_html)
                    all_emails.update(emails)

                    # Extract internal and external links from internal pages
                    internal_soup = BeautifulSoup(internal_html, 'html.parser')
                    internal_base_url = urlparse(link)
                    for internal_link in internal_soup.find_all('a', href=True):
                        internal_absolute_url = urljoin(link, internal_link['href'])
                        internal_parsed_url = urlparse(internal_absolute_url)
                        if internal_parsed_url.scheme in ('http', 'https') and internal_parsed_url.netloc == internal_base_url.netloc:
                            new_internal_links.add(internal_absolute_url)
                        else:
                            new_external_links.add((internal_absolute_url, link))
            except Exception as e:
                logger.error(f"Error extracting from internal link {link}: {e}")
        
        # Update main sets with new links
        internal_links.update(new_internal_links)
        all_external_links.update(new_external_links)

        # Combine internal and external links for output
        all_links = internal_links.union(external_links)

        # Save internal and external links to files if more than 20 links
        if len(internal_links) > 20:
            # Save internal links
            internal_links_file = os.path.join(output_dir, f"{urlparse(url).netloc}_internal_links.txt")
            with open(internal_links_file, 'w', encoding='utf-8') as internal_file:
                for link in internal_links:
                    internal_file.write(f"{link}\n")
            internal_links = list(internal_links)[:20]  # Limit display to first 20 links

        if len(all_external_links) > 20:
            # Save external links
            external_links_file = os.path.join(output_dir, f"{urlparse(url).netloc}_external_links.txt")
            with open(external_links_file, 'w', encoding='utf-8') as external_file:
                for ext_link, origin_url in all_external_links:
                    external_file.write(f"{ext_link} (from {origin_url})\n")
            all_external_links = list(all_external_links)[:20]  # Limit display to first 20 links

        # Save meta data to a file
        meta_output_file = os.path.join(output_dir, f"{urlparse(url).netloc}_meta.txt")
        with open(meta_output_file, 'w', encoding='utf-8') as meta_file:
            meta_file.write(f"{Fore.CYAN}Title: {title}\n")
            meta_file.write(f"{Fore.CYAN}URL: {url}\n")
            #meta_file.write(f"{Fore.CYAN}Description: {description}\n\n")
            meta_file.write(f"{Style.BRIGHT}{Fore.CYAN}☆ Meta Data:\n")
            for key, value in meta_data.items():
                meta_file.write(f"{Fore.CYAN}  • {key}: {value}\n")

        # Fetch IP information
        ip_info_str = await get_ip_info(urlparse(url).netloc)  # Await get_ip_info coroutine
        try:
            ip_info = json.loads(ip_info_str)  # Parse JSON string to dictionary
            crawl_results = {
                'url': url,
                'title': title,
                'description': description,
                'internal_links': internal_links,
                'external_links': all_external_links,
                'emails': all_emails,
                'meta_data': meta_data,
                'ip_info': ip_info  # Include IP information in crawl results
            }
            return crawl_results
        except json.JSONDecodeError:
            logger.error(f"Error decoding IP information: {ip_info_str}")
            return None

    except Exception as e:
        logger.error(f"Error crawling {url}: {e}")
        return None

async def main():
    try:
        visited_urls = set()
        all_external_links = set()
        
        # Use argparse for command-line arguments
        parser = argparse.ArgumentParser(description="Website Crawler")
        parser.add_argument("url", help="Target URL to crawl")
        parser.add_argument("--output", help="Directory to save results", default="results")

        args = parser.parse_args()
        target_url = args.url
        output_dir = args.output
        
        # Ensure the output directory exists
        os.makedirs(output_dir, exist_ok=True)
        
        print(Style.BRIGHT + Fore.YELLOW + "★ Website Crawler")
        print(Style.BRIGHT + Fore.CYAN + "~~~ Initiating Crawling Process ~~~\n")
        
        async with ClientSession() as session:
            crawl_results = await crawl_website(target_url, session, visited_urls, all_external_links, output_dir)
            if crawl_results:
                print(Fore.YELLOW + f"\n{'='*40}")
                print(f"~~~ Crawling Result for {crawl_results['url']} ~~~")
                print(f"{'='*40}\n")
                print(f"{Fore.CYAN}Title: {crawl_results['title']}")
                print(f"{Fore.CYAN}URL: {crawl_results['url']}")
                print(f"{Fore.CYAN}Description: {crawl_results['description']}")
                print(f"{Style.BRIGHT}{Fore.CYAN}☆ Meta Data:")
                for key, value in crawl_results['meta_data'].items():
                    print(f"{Fore.CYAN}  • {key}: {value}")
                
                if crawl_results['internal_links']:
                    print(f"\n{Style.BRIGHT}{Fore.GREEN}Internal Links:")
                    for link in crawl_results['internal_links']:
                        print(f"{Fore.GREEN}  • {link}")
                
                if crawl_results['external_links']:
                    print(f"\n{Style.BRIGHT}{Fore.RED}External Links:")
                    for ext_link, origin_url in crawl_results['external_links']:
                        print(f"{Fore.RED}  • {ext_link} (from {origin_url})")
                
                if crawl_results['emails']:
                    print(f"\n{Style.BRIGHT}{Fore.YELLOW}Emails Found:")
                    for email in crawl_results['emails']:
                        print(f"{Fore.YELLOW}  • {email}")
                
                # Display IP information if available
                if 'ip_info' in crawl_results:
                    print(f"\n{Style.BRIGHT}{Fore.BLUE}IP Information:")
                    for key, value in crawl_results['ip_info'].items():
                        print(f"{Fore.BLUE}  • {key}: {value}")
                
                print(f"\n{'='*40}\n")
            else:
                print(Fore.RED + "No data crawled or error occurred.")
    except Exception as e:
        logger.error(f"Main error: {e}")

if __name__ == "__main__":
    asyncio.run(main())

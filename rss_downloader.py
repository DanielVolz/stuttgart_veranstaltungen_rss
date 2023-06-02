import datetime
import json
import locale
import logging
import os
import shutil
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET

from email.utils import formatdate

import ics
import jinja2
import pytz
import requests

from bs4 import BeautifulSoup


# Constants
DEFAULT_IMAGE_URL = "https://www.stuttgart.de/openGraph-200x200.png"


def count_events(xml_file):
    """
    Count the number of events in an XML file.

    Parameters:
        xml_file (str): The path to the XML file containing event data.

    Returns:
        int: The number of events found in the XML file.

    Raises:
        Exception: If an error occurs during parsing or finding events.
    """

    try:
        tree = ET.parse(xml_file)
        root = tree.getroot()
        event_count = len(root.findall(".//item"))
        return event_count
    except ET.ParseError as parse_error:
        # Handle parse errors
        print(f"Error parsing XML: {parse_error}")
    except IOError as io_error:
        # Handle IO errors
        print(f"IO Error occurred: {io_error}")
    except Exception as e:
        # Handle any other unexpected exception
        print(f"An error occurred: {e}")


def get_running_containers(container_name):
    """
    Check if a Docker container is running.

    Parameters:
        container_name (str): The name of the Docker container.

    Returns:
        bool: True if the specified container is running, False otherwise.
    """

    command = f"docker ps --filter name={container_name} --format '{{{{.Names}}}}'"
    output = subprocess.check_output(command, shell=True, text=True)
    running_containers = output.splitlines()
    return container_name in running_containers


def execute_shell_command(command):
    """
    Execute a shell command.

    Parameters:
        command (str): The shell command to execute.

    Returns:
        None

    Raises:
        CalledProcessError: If the shell command returns a non-zero exit status.
    """

    process = subprocess.Popen(command, shell=True)
    process.communicate()

    if process.returncode == 0:
        logger.info(f"Command '{command}' executed successfully.")
    else:
        logger.error(
            f"Command '{command}' encountered an error with return code"
            f" {process.returncode}."
        )


def update_nextcloud_news(nextcloud_user_id, tld_rss_feed, nextcloud_container_name):
    """
    Update Nextcloud News feeds for a specific user.

    Parameters:
        nextcloud_user_id (str): The ID of the Nextcloud user.
        tld_rss_feed (str): The top-level domain of the RSS feed.
        nextcloud_container_name (str): The name of the Nextcloud Docker container.

    Returns:
        None

    Raises:
        Exception: If an error occurs during the update process or if the container is not running.
    """

    output = subprocess.check_output(
        [
            "sudo",
            "docker",
            "exec",
            "--user",
            "www-data",
            nextcloud_container_name,
            "php",
            "occ",
            "news:feed:list",
            nextcloud_user_id,
        ]
    )
    parsed_data = json.loads(output)
    nextcloud_news_feed_ids = [
        entry["id"] for entry in parsed_data if entry["url"].startswith(tld_rss_feed)
    ]

    if get_running_containers(nextcloud_container_name):
        logger.info("Updating Nextcloud News feeds.")
        for nextcloud_news_feed_id in nextcloud_news_feed_ids:
            commands = [
                (
                    f"sudo docker exec --user www-data {nextcloud_container_name} php"
                    f" occ news:feed:read {nextcloud_user_id} {nextcloud_news_feed_id}"
                ),
                (
                    f"sudo docker exec --user www-data {nextcloud_container_name} php"
                    " occ news:updater:update-feed"
                    f" {nextcloud_user_id} {nextcloud_news_feed_id}"
                ),
            ]
            for command in commands:
                execute_shell_command(command)
    else:
        logger.error(f"Container '{nextcloud_container_name}' is not running.")


def move_rss_log_files(destination_folder):
    """
    Move RSS and log files to a destination folder.

    Parameters:
        destination_folder (str): The path to the destination folder.

    Returns:
        None
    """

    script_directory = os.path.dirname(os.path.abspath(__file__))
    source_folder = script_directory

    file_list = os.listdir(source_folder)
    files_moved = False  # Variable to track if any files were moved

    for file_name in file_list:
        if file_name.endswith((".rss", ".log")):
            source_path = os.path.join(source_folder, file_name)
            destination_path = os.path.join(destination_folder, file_name)
            shutil.move(source_path, destination_path)
            logger.info("Moved file: %s", file_name)
            files_moved = True  # Set the flag to indicate files were moved

    if not files_moved:
        logger.warning("No files to move.")


def create_rss_element(rss_title):
    """
    Create an RSS element with a title.

    Parameters:
        rss_title (str): The title of the RSS element.

    Returns:
        Tuple: A tuple containing the root RSS element and the channel element.
    """

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    title = ET.SubElement(channel, "title")
    copyright_stadt_stuttgart = ET.SubElement(channel, "copyright")
    copyright_stadt_stuttgart.text = "Copyright 2023, Landeshauptstadt Stuttgart"
    link = ET.SubElement(channel, "link")
    link.text = "https://www.stuttgart.de/service/veranstaltungen.php"
    title.text = rss_title

    return rss, channel


def create_date_list():
    """
    Create a list of dates for the next 7 days.

    Returns:
        List: A list of date objects representing the next 7 days.
    """

    today = datetime.date.today()
    date_list = [today + datetime.timedelta(days=i) for i in range(7)]
    return date_list


def fetch_event_entries(url):
    """
    Fetch event entries from a given URL.

    Parameters:
        url (str): The URL of the webpage to fetch event entries from.

    Returns:
        List: A list of BeautifulSoup article objects representing event entries.

    Raises:
        requests.exceptions.RequestException: If an error occurs while making the HTTP request.
    """

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching event entries: {e}")
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    event_entries = soup.find_all(
        "article",
        class_=(
            "SP-Teaser SP-Grid__full SP-Teaser--event SP-Teaser--hasLinks"
            " SP-Teaser--textual"
        ),
    )
    return event_entries


def process_event_entry(event_entry, url, channel):
    """
    Process an event entry by extracting event information, building event HTML,
    and adding the event to a channel.

    Parameters:
        - event_entry (BeautifulSoup.Tag): The HTML tag representing the event entry.
        - url (str): The base URL of the event.
        - channel (ElementTree.Element): The XML element representing the channel.

    Returns:
        None
    """

    ical_link, event = extract_event_info(event_entry, url)
    event_html = build_event_html(event, ical_link)
    add_event_to_channel(event, event_html, channel)


def extract_event_info(event_entry, url):
    """
    Extract event information from an event entry.

    Parameters:
        - event_entry (BeautifulSoup.Tag): The HTML tag representing the event entry.
        - url (str): The base URL of the event.

    Returns:
        Tuple[str, ics.Event]: A tuple containing the extracted iCal link and the event object.
    """

    ical_link_element = event_entry.select_one(
        ".SP-Teaser__links .SP-Link.SP-Iconized--left"
    )
    ical_link = urllib.parse.urljoin(url, ical_link_element["href"])
    ical_link = urllib.parse.unquote(ical_link)
    ical_response = requests.get(ical_link, timeout=10)
    ical_data = ical_response.text
    cal = ics.Calendar(ical_data)
    event = list(cal.events)[0]
    return ical_link, event


def build_event_html(event, ical_link):
    """
    Build HTML content for an event.

    Parameters:
        - event (ics.Event): The event object.
        - ical_link (str): The iCal link of the event.

    Returns:
        str: The generated HTML content for the event.
    """

    event_data = extract_event_data(event)
    image_url = fetch_event_image_url(event_data["url"])
    google_maps_link = generate_google_maps_link(event_data["location"])
    entrance_fee = parse_entrance_fee(event_data["url"])
    extended_description = parse_extended_description(event_data["url"])
    exhibition_hours_html = parse_exhibition_hours(event_data["url"])

    event_html = render_event_html(
        event_data,
        image_url,
        google_maps_link,
        entrance_fee,
        extended_description,
        exhibition_hours_html,
        ical_link,
    )
    return event_html


def extract_event_data(event):
    """
    Extract event data from an event object.

    Parameters:
        - event (ics.Event): The event object.

    Returns:
        dict: A dictionary containing the extracted event data.
    """

    germany_tz = pytz.timezone("Europe/Berlin")
    event_start = event.begin.astimezone(germany_tz)
    event_end = event.end.astimezone(germany_tz)

    return {
        "title": event.name,
        "start_time": event_start.strftime("%H:%M Uhr"),
        "end_time": event_end.strftime("%H:%M Uhr"),
        "date": event_start.strftime("%a, %d %b %Y"),
        "location": event.location,
        "description": event.description,
        "url": event.url,
        "categories": event.categories,
        "tags_str": ", ".join(event.categories),
    }


def fetch_event_image_url(event_url):
    """
    Fetch the image URL for an event.

    Parameters:
        - event_url (str): The URL of the event.

    Returns:
        str: The fetched image URL or the default image URL if fetching fails.
    """

    if not event_url:
        return DEFAULT_IMAGE_URL

    try:
        webpage_response = requests.get(event_url, timeout=10)
        webpage_response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching event image URL: {e}")
        return DEFAULT_IMAGE_URL

    webpage_soup = BeautifulSoup(webpage_response.content, "html.parser")
    picture_tag = webpage_soup.select_one("picture")

    if picture_tag:
        img_tag = picture_tag.find("img")
        if img_tag:
            image_url = urllib.parse.urljoin(event_url, img_tag.get("src"))
            php_file_name = os.path.basename(event_url)
            php_file_name = os.path.splitext(php_file_name)[0]
            if php_file_name in image_url:
                return image_url

    return DEFAULT_IMAGE_URL


def generate_google_maps_link(location):
    """
    Generate a Google Maps link for a location.

    Parameters:
        - location (str): The location string.

    Returns:
        str: The generated Google Maps link.
    """

    if not location:
        return None
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(location)}"


def parse_entrance_fee(event_url):
    """
    Parse the entrance fee for an event.

    Parameters:
        - event_url (str): The URL of the event.

    Returns:
        str or None: The parsed entrance fee or None if parsing fails.
    """

    if not event_url:
        return None

    try:
        webpage_response = requests.get(event_url, timeout=10)
        webpage_response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error parsing entrance fee: {e}")
        return None

    webpage_soup = BeautifulSoup(webpage_response.content, "html.parser")
    entrance_fee_element = webpage_soup.select_one(
        ".SP-CallToAction__text .SP-Paragraph p"
    )

    if entrance_fee_element and entrance_fee_element.contents:
        return entrance_fee_element.contents[0].strip()

    return None


def parse_extended_description(event_url):
    """
    Parse the extended description for an event.

    Parameters:
        - event_url (str): The URL of the event.

    Returns:
        str or None: The parsed extended description or None if parsing fails.
    """

    if not event_url:
        return None

    webpage_response = requests.get(event_url, timeout=10)
    webpage_soup = BeautifulSoup(webpage_response.content, "html.parser")
    extended_description_elements = webpage_soup.select(
        ".SP-ArticleContent .SP-Text:not(.SP-Text--notice) .SP-Paragraph"
        " p:not(.SP-CallToAction__text .SP-Paragraph p)"
    )

    for element in extended_description_elements:
        br_tags = element.find_all("br")
        for br_tag in br_tags:
            br_tag.replace_with("\n")

    return "\n".join(
        [element.text.strip() for element in extended_description_elements]
    )


def parse_exhibition_hours(event_url):
    """
    Parse the exhibition hours for an event.

    Parameters:
        - event_url (str): The URL of the event.

    Returns:
        str or None: The parsed exhibition hours or None if parsing fails.
    """

    if not event_url:
        return None

    webpage_response = requests.get(event_url, timeout=10)
    webpage_soup = BeautifulSoup(webpage_response.content, "html.parser")
    exhibition_hours = webpage_soup.find(
        "section",
        class_=(
            "SP-Text SP-Text--boxed SP-Grid__full--background"
            " SP-Grid__full--backgroundHighlighted"
        ),
    )

    if exhibition_hours:
        exhibition_hours_div = exhibition_hours.find("div")
        if exhibition_hours_div:
            return "".join(map(str, exhibition_hours_div.contents))

    return None


def render_event_html(
    event_data,
    image_url,
    google_maps_link,
    entrance_fee,
    extended_description,
    exhibition_hours_html,
    ical_link,
):
    """
    Render the HTML content for an event.

    Parameters:
        - event_data (dict): The event data dictionary.
        - image_url (str): The URL of the event image.
        - google_maps_link (str): The Google Maps link for the event location.
        - entrance_fee (str or None): The entrance fee for the event.
        - extended_description (str or None): The extended description of the event.
        - exhibition_hours_html (str or None): The HTML content for exhibition hours.
        - ical_link (str): The iCal link for the event.

    Returns:
        str: The rendered HTML content for the event.
    """
    template_loader = jinja2.FileSystemLoader(searchpath="./templates")
    template_env = jinja2.Environment(loader=template_loader)
    template = template_env.get_template("event_template.html")

    rendered_html = template.render(
        event_data=event_data,
        image_url=image_url,
        google_maps_link=google_maps_link,
        entrance_fee=entrance_fee,
        extended_description=extended_description,
        exhibition_hours_html=exhibition_hours_html,
        ical_link=ical_link,
    )

    return rendered_html


def add_event_to_channel(event, event_html, channel):
    """
    Add an event to the XML channel.

    Parameters:
        - event (ics.Event): The event object.
        - event_html (str): The HTML content for the event.
        - channel (ElementTree.Element): The XML element representing the channel.

    Returns:
        None
    """

    event_title = event.name
    event_start = event.begin
    pub_date = formatdate(event_start.datetime.timestamp(), usegmt=True)

    event_exists = False
    for existing_item in channel.findall("item"):
        existing_title_element = existing_item.find("title")
        if existing_title_element is None:
            continue

        existing_title = existing_title_element.text
        existing_pub_date = existing_item.find("pubDate").text
        if existing_title == event_title and existing_pub_date == pub_date:
            event_exists = True
            logger.info(
                f"Skipping adding event: Duplicate event: {event_title}, date:"
                f" {pub_date}"
            )
            break

    if event_exists:
        return

    # Add the event to the XML channel
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = event_title
    ET.SubElement(item, "description").text = event_html
    ET.SubElement(item, "link").text = event.url
    ET.SubElement(item, "pubDate").text = pub_date
    logger.info(f"Added to xml file: {event_title}, date: {pub_date}")


def write_rss_to_file(rss, rss_name):
    """
    Write the RSS feed to a file.

    Parameters:
        - rss (ElementTree.Element): The XML element representing the RSS feed.
        - rss_name (str): The name of the RSS feed file.

    Returns:
        None
    """

    script_directory = os.path.dirname(os.path.abspath(__file__))
    rss_path = os.path.join(script_directory, rss_name)

    xml_data = ET.tostring(rss, encoding="utf-8")  # Changed encoding to "unicode"
    
    try:
        with open(rss_path, "w", encoding="utf-8") as f:
            f.write(xml_data.decode("utf-8"))  # Decode the bytes using UTF-8
    except IOError as e:
        logger.error(f"Failed to write XML data to {rss_path}: {e}")
        return

    try:
        tree = ET.parse(rss_path)
        tree.write(rss_path, encoding="utf-8", xml_declaration=True)
    except ET.ParseError as e:
        logger.error(f"Failed to parse XML data from {rss_path}: {e}")
        return

    logger.info(f"{count_events(rss_path)} events added.")
    logger.info(f"RSS feed '{rss_name}' in {rss_path} generated successfully!")


def generate_rss_feed(rss_name, rss_title, rss_category):
    """
    Generate an RSS feed for a given category.

    Parameters:
        - rss_name (str): The name of the RSS feed file.
        - rss_title (str): The title of the RSS feed.
        - rss_category (str): The category of the RSS feed.

    Returns:
        None
    """

    logger.info(
        f"Start scraping the RSS category {rss_title} in to the file: {rss_name}."
    )
    locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")

    rss, channel = create_rss_element(rss_title)
    date_list = create_date_list()

    for date in date_list:
        date_str = date.strftime("%Y-%m-%d")
        url = f"https://www.stuttgart.de/service/veranstaltungen.php?form=eventSearch-1.form&sp:dateFrom[]={date_str}&sp:dateTo[]={date_str}&sp:categories[77306][]={rss_category}&action=submit"

        event_entries = fetch_event_entries(url)

        for event_entry in event_entries:
            process_event_entry(event_entry, url, channel)

    write_rss_to_file(rss, rss_name)


def setup_logging():
    """
    Set up logging for the RSS generator.

    Returns:
        logging.Logger: The logger object for logging events.
    """

    # Set up logging
    log_file = "rss_generator.log"
    log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), log_file)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(log_file_path), logging.StreamHandler()],
    )
    logger = logging.getLogger(__name__)

    return logger


def main():
    destination_folder = "/home/pi/rss_feeds"

    logger.info("Starting scraping script. ##############")
    rss_name = "buehne_veranstaltungen.rss"
    rss_title = "Bühne - Stuttgart"
    rss_category = 79078
    generate_rss_feed(rss_name, rss_title, rss_category)

    rss_name = "philo_veranstaltungen.rss"
    rss_title = "Literatur, Philosophie und Geschichte - Stuttgart"
    rss_category = 77317
    generate_rss_feed(rss_name, rss_title, rss_category)

    rss_name = "musik_veranstaltungen.rss"
    rss_title = "Musik - Stuttgart"
    rss_category = 79091
    generate_rss_feed(rss_name, rss_title, rss_category)

    logger.info("RSS feed generation completed.")

    nextcloud_user_id = "danielvolz"
    rss_tld = "https://rss.danielvolz.org"
    nextcloud_container_name = "nextcloud-aio-nextcloud"

    move_rss_log_files(destination_folder)
    update_nextcloud_news(nextcloud_user_id, rss_tld, nextcloud_container_name)

    logger.info("Stopping scraping script. ##############")


if __name__ == "__main__":
    logger = setup_logging()
    main()

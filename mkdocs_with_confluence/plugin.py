import time
import os
import hashlib
import sys
import re
import requests
import mimetypes
import mistune
import contextlib
from mkdocs.config import config_options
from mkdocs.plugins import BasePlugin
from md2cf.confluence_renderer import ConfluenceRenderer
from os import environ
from pathlib import Path
from mkdocs.plugins import get_plugin_logger

log = get_plugin_logger(__name__)

CONTENT_URL_FORMAT = "{base_url}/wiki/rest/api/content"
CONVERT_URL_FORMAT = "{base_url}/wiki/rest/api/contentbody/convert/{to}"
LABEL_URL_FORMAT = "{base_url}/wiki/rest/api/content/{id}/label"

PARENT_TEMPLATE = """
{pagetree:root=@self|startDepth=3}
"""
MERMAID_TEMPLATE = """
{mermaid-cloud:filename=FILE|revision=1}
"""
MERMAID_FORMAT = "000MERMAID_CODE000{file}000"
HASH_LABEL_PREFIX = "cicd_hash_"


@contextlib.contextmanager
def nostdout():
    save_stdout = sys.stdout
    sys.stdout = DummyFile()
    yield
    sys.stdout = save_stdout


class DummyFile(object):
    def write(self, x):
        pass


class BearerAuth(requests.auth.AuthBase):
    def __init__(self, token):
        self.token = token

    def __call__(self, r):
        r.headers["authorization"] = "Bearer " + self.token
        return r


class MkdocsWithConfluence(BasePlugin):
    _id = 0
    config_scheme = (
        ("host_url", config_options.Type(str, default=None)),
        ("space", config_options.Type(str, default=None)),
        ("parent_page_name", config_options.Type(str, default=None)),
        (
            "username",
            config_options.Type(str, default=environ.get("JIRA_USERNAME", None)),
        ),
        (
            "password",
            config_options.Type(str, default=environ.get("JIRA_PASSWORD", None)),
        ),
        ("enabled_if_env", config_options.Type(str, default=None)),
        ("disable_cleanup", config_options.Type(bool, default=False)),
        ("verbose", config_options.Type(bool, default=False)),
        ("debug", config_options.Type(bool, default=False)),
        ("dryrun", config_options.Type(bool, default=False)),
        ("sleep_time", config_options.Type(float, default=5.0)),
        ("timeout", config_options.Type(float, default=30.0)),
    )

    def __init__(self):
        self.enabled = True
        self.confluence_renderer = ConfluenceRenderer(use_xhtml=True)
        self.confluence_mistune = mistune.Markdown(renderer=self.confluence_renderer)
        self.flen = 1
        self.session = requests.Session()
        self.page_attachments = {}

    def on_nav(self, nav, config, files):
        navigation_items = nav.__repr__()

        for n in navigation_items.split("\n"):
            if "Page" in n:
                try:
                    self.page_title = self.__get_page_title(n)
                    if self.page_title is None:
                        raise AttributeError
                except AttributeError:
                    self.page_local_path = self.__get_page_url(n)
                    log.warning(
                        f"Page from path {self.page_local_path} has no"
                        " entity in the mkdocs.yml nav section. It will be uploaded"
                        " to the Confluence, but you may not see it!"
                    )
                    self.page_local_name = self.__get_page_name(n)
                    self.page_title = self.page_local_name

            if "Section" in n:
                try:
                    self.section_title = self.__get_section_title(n)
                    if self.section_title is None:
                        raise AttributeError
                except AttributeError:
                    self.section_local_path = self.__get_page_url(n)
                    log.warning(
                        f"Section from path {self.section_local_path} has no"
                        f" entity in the mkdocs.yml nav section. It will be uploaded"
                        f" to the Confluence, but you may not see it!"
                    )
                    self.section_local_name = self.__get_section_title(n)
                    self.section_title = self.section_local_name

    def on_files(self, files, config):
        pages = files.documentation_pages()
        try:
            self.flen = len(pages)
            log.debug(f"Number of Files in directory tree: {self.flen}")
        except 0:
            log.warning(
                "You have no documentation pages"
                "in the directory tree, please add at least one!"
            )

    def on_post_template(self, output_content, template_name, config):
        log.debug("Start exporting markdown pages...")

    def on_config(self, config):
        if "enabled_if_env" in self.config:
            env_name = self.config["enabled_if_env"]
            if env_name:
                self.enabled = os.environ.get(env_name) == "1"
                if not self.enabled:
                    log.warning(
                        "Exporting Mkdocs pages to Confluence turned OFF: "
                        f"(set environment variable {env_name} to 1 to enable)"
                    )
                    return
                else:
                    log.info(
                        "Exporting Mkdocs pages to Confluence "
                        f"turned ON by var {env_name}==1!"
                    )
                    self.enabled = True
                    self.session.auth = (
                        self.config["username"],
                        self.config["password"],
                    )
            else:
                log.warning(
                    "Exporting Mkdocs pages to Confluence turned OFF: "
                    f"(set environment variable {env_name} to 1 to enable)"
                )
                return
        else:
            log.info("Exporting Mkdocs pages to Confluence turned ON by default!")
            self.enabled = True

    @property
    def dryrun(self):
        if "_dryrun" not in dir(self):
            if self.config["dryrun"]:
                log.warning("Dryrun mode turned ON")
                self._dryrun = True
            else:
                self._dryrun = False
        return self._dryrun

    def is_enabled_page(self, page):
        return str(page.meta.get("mkdocs_with_confluence_skip")).lower() != "true"

    def on_page_markdown(self, markdown, page, config, files):
        MkdocsWithConfluence._id += 1

        if self.enabled and self.is_enabled_page(page):
            log.info(f"Page export progress: {MkdocsWithConfluence._id} / {self.flen}")

            if not all(self.config_scheme):
                log.debug("ERR: you have empty values in your config. Aborting")
                return markdown

            try:
                log.debug("Get section first parent title...: ")

                try:
                    parent = self.__get_section_title(page.ancestors[0].__repr__())
                except IndexError as e:
                    log.debug(
                        f"WRN({e}): No first parent! Assuming "
                        f"{self.config['parent_page_name']}..."
                    )

                    parent = None

                if not parent:
                    parent = self.config["parent_page_name"]

                if self.config["parent_page_name"] is not None:
                    main_parent = self.config["parent_page_name"]
                else:
                    main_parent = self.config["space"]

                log.debug("Get section second parent title...: ")

                try:
                    parent1 = self.__get_section_title(page.ancestors[1].__repr__())
                except IndexError as e:
                    log.debug(
                        f"ERR({e}) No second parent! Assuming "
                        f"second parent is main parent: {main_parent}..."
                    )

                    parent1 = None

                if not parent1:
                    parent1 = main_parent

                    log.debug(
                        f"Only one parent found. Assuming as a "
                        f"first node after main parent config {main_parent}"
                    )

                log.debug(
                    f"Parent0= {parent}, Parent1={parent1}, Main parent={main_parent}"
                )

                site_dir = config.get("site_dir")

                attachments = []

                ###############################################
                log.debug("Processing images in markdown")
                ###############################################
                try:
                    for match in re.finditer(r'img src="file://(.*)" s', markdown):
                        log.debug(f"Found image: {match.group(1)}")

                        attachment_name = match.group(1)
                        attachment_path = attachment_name

                        attachments.append((attachment_name, attachment_path))

                    for match in re.finditer(
                        r"!\[[\w\. -]*\]\((?!http|file)([^\s,]*).*\)", markdown
                    ):
                        file_path = match.group(1).lstrip("./\\")

                        attachment_name = file_path
                        attachment_path = file_path

                        darwio_image = re.search(r"(.*)\.drawio#(\d+)", file_path)

                        if darwio_image:
                            attachment_path = f"{darwio_image.group(1)}.drawio-{darwio_image.group(2)}.png"

                        attachments.append((attachment_name, attachment_path))

                        log.debug(f"FOUND IMAGE: {file_path}")

                    new_markdown = re.sub(
                        r'<img src="file:///tmp/',
                        '<p><ac:image ac:height="350"><ri:attachment ri:filename="',
                        markdown,
                    )
                    new_markdown = re.sub(
                        r'" style="page-break-inside: avoid;">',
                        '"/></ac:image></p>',
                        new_markdown,
                    )
                except AttributeError as e:
                    log.debug(f"WARN(({e}): No images found in markdown. Proceed..")

                confluence_body_changes = []

                ###############################################
                log.debug("Processing mermaid code blocks")
                ###############################################
                try:
                    mermaid_re = r"```mermaid\n([^`]+)\n```"

                    mermaid_counter = 1

                    for match in re.finditer(mermaid_re, new_markdown):
                        mermaid_code = match.group(1)

                        title_id = self.__get_text_md5(page.title)
                        attachment_name = f"mermaid-{title_id}-{mermaid_counter}.txt"
                        attachment_path = attachment_name
                        attachment_file = f"{site_dir}/{attachment_name}"

                        with open(attachment_file, "w") as f:
                            f.write(mermaid_code)

                            attachments.append((attachment_name, attachment_path))

                            swap_id = MERMAID_FORMAT.format(file=attachment_name)

                            confluence_body_changes.append(
                                (
                                    swap_id,
                                    self.convert_page(
                                        MERMAID_TEMPLATE.replace(
                                            "FILE", attachment_name
                                        )
                                    ),
                                )
                            )

                            new_markdown = re.sub(mermaid_re, swap_id, new_markdown)

                            log.debug(f"Found mermaid code #{mermaid_counter}")

                            mermaid_counter += 1
                except Exception as e:
                    log.debug(f"WARN(({e}): Error processing mermaid. Proceed..")

                if not self.config["disable_cleanup"]:
                    ###############################################
                    log.debug("Cleaning Markdown")
                    ###############################################

                    new_markdown = new_markdown.strip()
                    new_markdown = re.sub(r"^# .+", "", new_markdown)
                    new_markdown = new_markdown.strip()

                ###############################################
                log.debug("Converting Markdown to Confluence")
                ###############################################
                confluence_body = self.confluence_mistune(new_markdown)

                ###############################################
                log.debug("Modify Confluence body")
                ###############################################
                for k, v in confluence_body_changes:
                    confluence_body = confluence_body.replace(k, v)

                ###############################################
                log.debug("Sending page to confluence:")
                ###############################################
                log.debug(f"host: {self.config['host_url']}")
                log.debug(f"space: {self.config['space']}")
                log.debug(f"title: {page.title}")
                log.debug(f"parent: {parent}")
                log.debug(f"body: {confluence_body}")

                page_id, _ = self.find_page_id(page.title)
                if page_id is not None:
                    ###############################################
                    log.debug("Updating previous page")
                    ###############################################

                    parent_name = self.find_parent_name_of_page(page.title)

                    if parent_name != parent and page.title != parent:
                        log.warning(
                            f"ERR: Parents does not match: '{parent}' =/= '{parent_name}'. Skipping..."
                        )
                        return markdown

                    self.update_page(page.title, confluence_body)
                else:
                    ###############################################
                    log.debug("Creating mew page")
                    ###############################################
                    main_parent_id, _ = self.find_page_id(main_parent)

                    def find_parent_id():
                        return self.find_page_id(parent)

                    def find_second_parent_id():
                        return self.find_page_id(parent1)

                    parent_id, _ = self.wait_until(find_parent_id)
                    second_parent_id, _ = self.wait_until(find_second_parent_id)

                    if not parent_id:
                        ###############################################
                        log.debug("Creating parent page(s)")
                        ###############################################

                        if not second_parent_id:
                            main_parent_id, _ = self.find_page_id(main_parent)
                            if not main_parent_id:
                                log.warning("Main parent unknown. Aborting!")
                                return markdown

                            log.debug(
                                f"Trying to Add page '{parent1}' to "
                                f"main parent({main_parent}) ID: {main_parent_id}"
                            )

                            body = PARENT_TEMPLATE.replace("TEMPLATE", parent1)

                            self.add_page(parent1, main_parent_id, body, format="wiki")

                            second_parent_id = self.wait_until(find_second_parent_id)

                        log.debug(
                            f"Trying to Add page '{parent}' "
                            f"to parent1({parent1}) ID: {second_parent_id}"
                        )

                        body = PARENT_TEMPLATE.replace("TEMPLATE", parent)

                        self.add_page(parent, second_parent_id, body, format="wiki")

                        parent_id = self.wait_until(find_parent_id)

                    self.add_page(page.title, parent_id, confluence_body)

                    log.info(
                        f"Trying to Add page '{page.title}' to parent0({parent}) ID: {parent_id}"
                    )

                if attachments:
                    self.page_attachments[page.title] = attachments

                self.wait()
            except Exception as e:
                log.warning(
                    f"Error with on_page_markdown for page '{page.title}': {str(e)}"
                )

                return markdown

        return markdown

    def on_post_build(self, config):
        site_dir = config.get("site_dir")

        for title, attachments in self.page_attachments.items():
            log.debug(f"Uploading attachments to confluence for {title}:")
            log.debug(f"Files: {attachments}")

            for attachment_name, attachment_path in attachments:
                log.debug(f"Looking for {attachment_name} in {site_dir}")

                for p in Path(site_dir).rglob(f"*{attachment_path}"):
                    self.add_or_update_attachment(title, attachment_name, p)

                    self.wait()

    def on_page_content(self, html, page, config, files):
        return html

    def __get_page_url(self, section):
        return re.search("url='(.*)'\\)", section).group(1)[:-1] + ".md"

    def __get_page_name(self, section):
        return os.path.basename(re.search("url='(.*)'\\)", section).group(1)[:-1])

    def __get_section_name(self, section):
        log.debug(f"Section name: {section}")

        return os.path.basename(re.search("url='(.*)'\\/", section).group(1)[:-1])

    def __get_section_title(self, section):
        log.debug(f"Section title: {section}")

        try:
            r = re.search("Section\\(title='(.*)'\\)", section)
            return r.group(1)
        except AttributeError:
            name = self.__get_section_name(section)
            log.warning(
                f"Section '{name}' doesn't exist in the mkdocs.yml nav section!"
            )
            return name

    def __get_page_title(self, section):
        try:
            r = re.search("\\s*Page\\(title='(.*)',", section)
            return r.group(1)
        except AttributeError:
            name = self.__get_page_url(section)
            log.warning(f"Page '{name}' doesn't exist in the mkdocs.yml nav section!")
            return name

    def __get_text_md5(self, text):
        if text:
            return hashlib.md5(text.encode("utf-8")).hexdigest()

    # Adapted from https://stackoverflow.com/a/3431838
    def __get_file_sha1(self, file_path):
        hash_sha1 = hashlib.sha1()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha1.update(chunk)
        return hash_sha1.hexdigest()

    def add_or_update_attachment(self, page_name, attachment_name, attachment_path):
        log.debug(
            f"Add or Update attachment[{attachment_name}] to page[{page_name}] using file[{attachment_path}]"
        )

        page_id, _ = self.find_page_id(page_name)
        if page_id:
            file_hash = self.__get_file_sha1(attachment_path)
            attachment_message = f"MKDocsWithConfluence [v{file_hash}]"
            existing_attachment = self.get_attachment(page_id, attachment_name)
            if existing_attachment:
                file_hash_regex = re.compile(r"\[v([a-f0-9]{40})]$")
                existing_match = file_hash_regex.search(
                    existing_attachment["version"]["message"]
                )
                if existing_match is not None and existing_match.group(1) == file_hash:
                    log.debug("Existing attachment skipping")

                    return True
                else:
                    return self.update_attachment(
                        page_id,
                        attachment_name,
                        attachment_path,
                        existing_attachment,
                        attachment_message,
                    )
            else:
                return self.create_attachment(
                    page_id, attachment_name, attachment_path, attachment_message
                )
        else:
            log.debug("ERR: page does not exists")

            return False

    def get_attachment(self, page_id, attachment_name):
        log.debug(f"Get attachment[{attachment_name}] for page[{page_id}]")

        name = os.path.basename(attachment_name)

        url = (
            CONTENT_URL_FORMAT.format(base_url=self.config["host_url"])
            + "/"
            + page_id
            + "/child/attachment"
        )
        headers = {"X-Atlassian-Token": "no-check"}

        r = self.session.get(
            url, headers=headers, params={"filename": name, "expand": "version"}
        )
        r.raise_for_status()
        with nostdout():
            response_json = r.json()
        if response_json["size"]:
            return response_json["results"][0]

    def update_attachment(
        self, page_id, attachment_name, attachment_path, existing_attachment, message
    ):
        log.debug(
            f"Update attachment[{attachment_name}] to page[{page_id}] using file[{attachment_path}]"
        )

        url = (
            CONTENT_URL_FORMAT.format(base_url=self.config["host_url"])
            + "/"
            + page_id
            + "/child/attachment/"
            + existing_attachment["id"]
            + "/data"
        )
        headers = {"X-Atlassian-Token": "no-check"}

        filename = os.path.basename(attachment_name)

        content_type, encoding = mimetypes.guess_type(attachment_path)
        if content_type is None:
            content_type = "multipart/form-data"
        files = {
            "file": (filename, open(Path(attachment_path), "rb"), content_type),
            "comment": message,
        }

        if not self.dryrun:
            r = self.session.post(url, headers=headers, files=files)
            r.raise_for_status()

            if r.status_code == 200:
                log.debug("OK!")
            else:
                log.debug("ERR!")

                return False

        return True

    def create_attachment(self, page_id, attachment_name, attachment_path, message):
        log.debug(
            f"Create attachment[{attachment_name}] to page[{page_id}] using Ffile[{attachment_path}]"
        )

        url = (
            CONTENT_URL_FORMAT.format(base_url=self.config["host_url"])
            + "/"
            + page_id
            + "/child/attachment"
        )
        headers = {"X-Atlassian-Token": "no-check"}

        filename = os.path.basename(attachment_name)

        # determine content-type
        content_type, encoding = mimetypes.guess_type(attachment_path)
        if content_type is None:
            content_type = "multipart/form-data"
        files = {
            "file": (filename, open(attachment_path, "rb"), content_type),
            "comment": message,
        }
        if not self.dryrun:
            r = self.session.post(url, headers=headers, files=files)
            r.raise_for_status()

            if r.status_code == 200:
                log.debug("OK!")
            else:
                log.debug("ERR!")

                return False

        return True

    def find_page_id(self, page_name):
        log.debug(f"Find page_id for page[{page_name}]")

        name_confl = page_name.replace(" ", "+")
        url = (
            CONTENT_URL_FORMAT.format(base_url=self.config["host_url"])
            + "?title="
            + name_confl
            + "&spaceKey="
            + self.config["space"]
            + "&expand=history,metadata.labels"
        )

        r = self.session.get(url)
        r.raise_for_status()

        with nostdout():
            response_json = r.json()

        if response_json["results"]:
            log.debug(f"ID: {response_json['results'][0]['id']}")

            page_result = response_json["results"][0]

            id = page_result["id"]
            hash = [
                x.get("name")
                for x in page_result["metadata"]["labels"]["results"]
                if x.get("prefix") == "global"
                and x.get("name").startswith(HASH_LABEL_PREFIX)
            ]

            return (id, hash[0].replace(HASH_LABEL_PREFIX, "") if hash else None)
        else:
            log.debug("ERR: page does not exist")

            return (None, None)

    def add_page(self, page_name, parent_page_id, page_content, format="storage"):
        log.debug(f"Add page[{page_name}] to parent page[{parent_page_id}]")

        new_md5 = self.__get_text_md5(page_content.strip())

        url = CONTENT_URL_FORMAT.format(base_url=self.config["host_url"]) + "/"

        headers = {"Content-Type": "application/json"}
        space = self.config["space"]
        hash_label = f"{HASH_LABEL_PREFIX}{new_md5}"
        data = {
            "type": "page",
            "title": page_name,
            "space": {"key": space},
            "ancestors": [{"id": parent_page_id}],
            "body": {
                "storage": {
                    "value": page_content,
                    "representation": format,
                }
            },
            "metadata": {"labels": [{"prefix": "global", "name": hash_label}]},
        }

        if not self.dryrun:
            r = self.session.post(url, json=data, headers=headers)
            r.raise_for_status()

            if r.status_code == 200:
                log.debug("OK!")

                self.wait()
            else:
                log.debug("ERR!")

                return False

        return True

    def update_page(self, page_name, page_content, format="storage"):
        log.debug(f"Update page[{page_name}]")

        page_id, current_md5 = self.find_page_id(page_name)

        if not page_id:
            log.debug(f"ERR: page[{page_name}] not found")
            return False

        new_md5 = self.__get_text_md5(page_content.strip())

        if page_id:
            if current_md5 == new_md5:
                log.debug("SKIP!")

                return True

            page_version = self.find_page_version(page_name)
            page_version = page_version + 1
            url = (
                CONTENT_URL_FORMAT.format(base_url=self.config["host_url"])
                + "/"
                + page_id
            )

            headers = {"Content-Type": "application/json"}
            space = self.config["space"]
            hash_label = f"{HASH_LABEL_PREFIX}{new_md5}"
            data = {
                "id": page_id,
                "title": page_name,
                "type": "page",
                "space": {"key": space},
                "body": {
                    "storage": {
                        "value": page_content,
                        "representation": format,
                    }
                },
                "version": {"number": page_version},
                "metadata": {"labels": [{"prefix": "global", "name": hash_label}]},
            }

            if not self.dryrun:
                r = self.session.put(url, json=data, headers=headers)
                r.raise_for_status()

                if r.status_code == 200:
                    log.debug("OK!")

                    self.wait()
                else:
                    log.debug("ERR!")

                    return False

            return True

        else:
            log.debug("Page does not exist yet!")

            return False

    def find_page_version(self, page_name):
        log.debug(f"Find version for page[{page_name}]")

        name_confl = page_name.replace(" ", "+")
        url = (
            CONTENT_URL_FORMAT.format(base_url=self.config["host_url"])
            + "?title="
            + name_confl
            + "&spaceKey="
            + self.config["space"]
            + "&expand=version"
        )

        r = self.session.get(url)
        r.raise_for_status()

        with nostdout():
            response_json = r.json()

        if response_json["results"] is not None:
            version = response_json["results"][0]["version"]["number"]

            log.debug(f"Founfd version[{version}] for page[{page_name}]")

            return version
        else:
            log.debug("Page does not exists")

            return None

    def find_parent_name_of_page(self, page_name):
        log.debug(f" * Find Parent of page with name={page_name}")

        idp, _ = self.find_page_id(page_name)
        url = (
            CONTENT_URL_FORMAT.format(base_url=self.config["host_url"])
            + "/"
            + idp
            + "?expand=ancestors"
        )

        r = self.session.get(url)
        r.raise_for_status()

        with nostdout():
            response_json = r.json()

        if response_json:
            return response_json["ancestors"][-1]["title"]
        else:
            log.debug("Page does not have parent")

            return None

    def convert_page(self, body, from_type="wiki", to_type="storage"):
        log.debug(f"Converting page from {from_type} to {to_type}")

        try:
            url = CONVERT_URL_FORMAT.format(
                base_url=self.config["host_url"], to=to_type
            )

            headers = {"Accept": "application/json", "Content-Type": "application/json"}

            data = {"value": body, "representation": from_type}

            r = self.session.post(url, json=data, headers=headers)
            r.raise_for_status()
            if r.status_code == 200:
                log.debug("OK!")

                response_json = r.json()
                return response_json["value"]
            else:
                log.debug("ERR!")

            return None

        except Exception as e:
            log.debug(f"WARN(({e}): Error converting page")

            return None

    def wait_until(self, condition, interval=None, timeout=None):
        if not timeout:
            timeout = self.config["timeout"]

        start = time.time()

        result = condition()

        while not result and time.time() - start < timeout:
            self.wait(interval)

            result = condition()

        return result

    def wait(self, interval=None):
        if not interval:
            interval = self.config["sleep_time"]

        time.sleep(interval)

#!/usr/bin/env python3

"""
wordpress_to_book.py

Convert a WordPress WXR/XML export into:
  - individual Markdown files
  - a combined Markdown book
  - EPUB via Pandoc
  - PDF via Pandoc

Example:

    python wordpress_to_book.py wordpress-export.xml \
        --title "My Blog" \
        --author "Jethro Carr" \
        --output ./blog-book \
        --epub \
        --pdf

Requirements:

    pip install beautifulsoup4 requests html2text

And install Pandoc separately:

    https://pandoc.org/installing.html

For PDF output, Pandoc also needs a PDF engine. For example,
install BasicTeX / MacTeX on macOS, TeX Live on Linux, etc.
"""

from __future__ import annotations

import argparse
import html
import mimetypes
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import html2text
import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# WordPress XML namespaces
# ---------------------------------------------------------------------------

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "wp": "http://wordpress.org/export/1.2/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

IMAGE_MIME_EXTENSIONS = {
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/vnd.microsoft.icon": ".ico",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
}

IMAGE_EXTENSIONS = set(IMAGE_MIME_EXTENSIONS.values())


@dataclass
class Post:
    title: str
    slug: str
    date: datetime
    content_html: str
    categories: list[str]
    tags: list[str]
    link: str


def slugify(value: str) -> str:
    value = html.unescape(value).strip().lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[-\s]+", "-", value)
    return value.strip("-") or "post"


def safe_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    filename = unquote(Path(parsed.path).name)

    if not filename:
        filename = "image"

    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)

    return filename


def image_extension(content_type: str) -> str | None:
    mime_type = content_type.partition(";")[0].strip().lower()

    if not mime_type.startswith("image/"):
        return None

    extension = IMAGE_MIME_EXTENSIONS.get(mime_type)

    if extension:
        return extension

    extension = mimetypes.guess_extension(mime_type, strict=False)

    if extension == ".jpe":
        return ".jpg"

    return extension


def image_stem_from_url(url: str) -> str:
    filename = safe_filename_from_url(url)
    stem = Path(filename).stem
    return stem or "image"


def parse_wordpress_export(xml_path: Path) -> list[Post]:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    channel = root.find("channel")

    if channel is None:
        raise RuntimeError("Could not find <channel> in WordPress export.")

    posts: list[Post] = []

    for item in channel.findall("item"):
        post_type = item.findtext("wp:post_type", namespaces=NS)
        status = item.findtext("wp:status", namespaces=NS)

        # Only include published blog posts.
        if post_type != "post":
            continue

        if status != "publish":
            continue

        title = item.findtext("title") or "Untitled"

        slug = (
            item.findtext("wp:post_name", namespaces=NS)
            or slugify(title)
        )

        content_html = (
            item.findtext("content:encoded", namespaces=NS)
            or ""
        )

        link = item.findtext("link") or ""

        date_string = item.findtext("wp:post_date", namespaces=NS)

        if not date_string or date_string.startswith("0000-00-00"):
            continue

        try:
            post_date = datetime.strptime(
                date_string,
                "%Y-%m-%d %H:%M:%S",
            )
        except ValueError:
            print(
                f"Warning: couldn't parse date for '{title}': {date_string}",
                file=sys.stderr,
            )
            continue

        categories = []
        tags = []

        for category in item.findall("category"):
            domain = category.attrib.get("domain")
            value = (category.text or "").strip()

            if not value:
                continue

            if domain == "category":
                categories.append(value)
            elif domain == "post_tag":
                tags.append(value)

        posts.append(
            Post(
                title=title.strip(),
                slug=slugify(slug),
                date=post_date,
                content_html=content_html,
                categories=categories,
                tags=tags,
                link=link,
            )
        )

    posts.sort(key=lambda p: p.date)

    return posts


class ImageDownloader:
    def __init__(self, image_dir: Path):
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 WordPressBookArchiver/1.0"
        )

        self.cache: dict[str, Path] = {}

    def download(self, url: str) -> Path | None:
        if url in self.cache:
            return self.cache[url]

        stem = image_stem_from_url(url)

        for extension in sorted(IMAGE_EXTENSIONS):
            existing_path = self.image_dir / f"{stem}{extension}"

            if existing_path.exists():
                self.cache[url] = existing_path
                print(f"Already downloaded: {url}")
                return existing_path

        try:
            response = self.session.get(
                url,
                timeout=30,
                stream=True,
            )

            response.raise_for_status()

        except requests.RequestException as exc:
            print(
                f"Warning: couldn't download {url}: {exc}",
                file=sys.stderr,
            )
            return None

        try:
            content_type = response.headers.get("Content-Type", "")
            extension = image_extension(content_type)

            if extension is None:
                print(
                    f"Warning: skipping non-image response from {url} "
                    f"(Content-Type: {content_type or 'missing'})",
                    file=sys.stderr,
                )
                return None

            path = self.image_dir / f"{stem}{extension}"

            if path.exists():
                self.cache[url] = path
                print(f"Already downloaded: {url}")
                return path

            with open(path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        f.write(chunk)

        except OSError as exc:
            print(
                f"Warning: couldn't save {url}: {exc}",
                file=sys.stderr,
            )
            return None
        finally:
            response.close()

        self.cache[url] = path

        print(f"Downloaded: {url}")

        return path


def clean_and_localise_html(
    content_html: str,
    downloader: ImageDownloader,
    markdown_dir: Path,
    post_url: str = "",
) -> str:
    soup = BeautifulSoup(content_html, "html.parser")

    # Remove common unwanted elements.
    for element in soup.select(
        "script, style, iframe, form, "
        ".sharedaddy, .jp-relatedposts, "
        ".sd-sharing-enabled"
    ):
        element.decompose()

    # Localise images.
    for img in soup.find_all("img"):
        src = img.get("src")

        if not src:
            continue

        if src.startswith("//"):
            src = urljoin(post_url or "https://", src)
        else:
            src = urljoin(post_url, src)

        if not src.startswith(("http://", "https://")):
            continue

        local_path = downloader.download(src)

        if local_path:
            try:
                relative = local_path.relative_to(markdown_dir.parent)
                img["src"] = relative.as_posix()
            except ValueError:
                img["src"] = local_path.as_posix()

        # Strip WordPress responsive image URLs because they would
        # otherwise continue pointing at the live site.
        img.attrs.pop("srcset", None)
        img.attrs.pop("sizes", None)

    # Remove empty WordPress-specific attributes/classes.
    for tag in soup.find_all(True):
        tag.attrs.pop("class", None)
        tag.attrs.pop("id", None)

    return str(soup)


def html_to_markdown(content_html: str) -> str:
    converter = html2text.HTML2Text()

    converter.body_width = 0
    converter.ignore_links = False
    converter.ignore_images = False
    converter.ignore_emphasis = False
    converter.skip_internal_links = False
    converter.inline_links = True

    result = converter.handle(content_html)

    # Avoid huge runs of blank lines.
    result = re.sub(r"\n{4,}", "\n\n\n", result)

    return result.strip()


def yaml_escape(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def make_post_markdown(post: Post, body: str) -> str:
    lines = [
        f"# {post.title}",
        "",
        f"*{post.date.strftime('%-d %B %Y')}*",
        "",
    ]

    if post.categories:
        lines.append(
            "**Categories:** " + ", ".join(post.categories)
        )
        lines.append("")

    if post.tags:
        lines.append(
            "**Tags:** " + ", ".join(post.tags)
        )
        lines.append("")

    lines.append(body)
    lines.append("")

    if post.link:
        lines.extend(
            [
                "",
                f"*Original URL: {post.link}*",
                "",
            ]
        )

    return "\n".join(lines)


def write_posts(
    posts: list[Post],
    output_dir: Path,
) -> tuple[list[Path], Path]:
    posts_dir = output_dir / "posts"
    images_dir = output_dir / "images"

    posts_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    downloader = ImageDownloader(images_dir)

    written_files: list[Path] = []

    combined_path = output_dir / "blog.md"

    current_year = None

    with open(combined_path, "w", encoding="utf-8") as combined:

        for index, post in enumerate(posts, start=1):

            cleaned_html = clean_and_localise_html(
                post.content_html,
                downloader,
                posts_dir,
                post.link,
            )

            markdown_body = html_to_markdown(cleaned_html)

            post_md = make_post_markdown(
                post,
                markdown_body,
            )

            filename = (
                f"{index:05d}-"
                f"{post.date:%Y-%m-%d}-"
                f"{post.slug}.md"
            )

            post_path = posts_dir / filename

            post_path.write_text(
                post_md,
                encoding="utf-8",
            )

            written_files.append(post_path)

            # Insert year heading into combined book.
            if post.date.year != current_year:
                current_year = post.date.year

                combined.write(
                    f"\n# {current_year}\n\n"
                )

            # Posts become second-level chapters below the year.
            combined.write(
                f"## {post.title}\n\n"
            )

            combined.write(
                f"*{post.date.strftime('%-d %B %Y')}*\n\n"
            )

            if post.categories:
                combined.write(
                    "**Categories:** "
                    + ", ".join(post.categories)
                    + "\n\n"
                )

            if post.tags:
                combined.write(
                    "**Tags:** "
                    + ", ".join(post.tags)
                    + "\n\n"
                )

            combined.write(markdown_body)
            combined.write("\n\n")

            if post.link:
                combined.write(
                    f"*Original URL: {post.link}*\n\n"
                )

            combined.write("\\newpage\n\n")

            print(
                f"[{index}/{len(posts)}] {post.date.date()} "
                f"{post.title}"
            )

    return written_files, combined_path


def make_metadata(
    output_dir: Path,
    title: str,
    author: str | None,
    subtitle: str | None,
) -> Path:
    metadata_path = output_dir / "metadata.yaml"

    lines = [
        "---",
        f"title: {yaml_escape(title)}",
    ]

    if subtitle:
        lines.append(
            f"subtitle: {yaml_escape(subtitle)}"
        )

    if author:
        lines.append(
            f"author: {yaml_escape(author)}"
        )

    lines.extend(
        [
            "lang: en-NZ",
            "toc: true",
            "toc-depth: 2",
            "---",
        ]
    )

    metadata_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    return metadata_path


def require_pandoc():
    if not shutil.which("pandoc"):
        raise RuntimeError(
            "Pandoc was not found in PATH.\n"
            "Install it from https://pandoc.org/installing.html"
        )


def build_epub(
    combined_md: Path,
    metadata: Path,
    output_dir: Path,
    filename: str,
):
    require_pandoc()

    epub_path = output_dir / filename

    command = [
        "pandoc",
        metadata.name,
        combined_md.name,
        "--toc",
        "--toc-depth=2",
        "--resource-path=.",
        "-o",
        epub_path.name,
    ]

    print("\nBuilding EPUB...")

    subprocess.run(
        command,
        cwd=output_dir,
        check=True,
    )

    print(f"Created: {epub_path}")


def build_pdf(
    combined_md: Path,
    metadata: Path,
    output_dir: Path,
    filename: str,
):
    require_pandoc()

    pdf_path = output_dir / filename

    command = [
        "pandoc",
        metadata.name,
        combined_md.name,
        "--toc",
        "--toc-depth=2",
        "--resource-path=.",
        "-V",
        "geometry:margin=25mm",
        "-V",
        "fontsize=11pt",
        "-o",
        pdf_path.name,
    ]

    print("\nBuilding PDF...")

    subprocess.run(
        command,
        cwd=output_dir,
        check=True,
    )

    print(f"Created: {pdf_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Convert a WordPress XML export into EPUB/PDF."
    )

    parser.add_argument(
        "xml",
        type=Path,
        help="WordPress WXR/XML export",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("wordpress-book"),
        help="Output directory",
    )

    parser.add_argument(
        "--title",
        default="WordPress Blog Archive",
        help="Book title",
    )

    parser.add_argument(
        "--subtitle",
        default=None,
        help="Optional subtitle",
    )

    parser.add_argument(
        "--author",
        default=None,
        help="Book author",
    )

    parser.add_argument(
        "--epub",
        action="store_true",
        help="Generate EPUB",
    )

    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Generate PDF",
    )

    args = parser.parse_args()

    if not args.xml.exists():
        parser.error(
            f"File does not exist: {args.xml}"
        )

    # Default to EPUB if neither was specified.
    if not args.epub and not args.pdf:
        args.epub = True

    args.output.mkdir(
        parents=True,
        exist_ok=True,
    )

    print(f"Reading {args.xml}...")

    posts = parse_wordpress_export(args.xml)

    print(f"Found {len(posts)} published posts.")

    if not posts:
        raise RuntimeError(
            "No published WordPress posts were found."
        )

    _, combined_md = write_posts(
        posts,
        args.output,
    )

    metadata = make_metadata(
        args.output,
        args.title,
        args.author,
        args.subtitle,
    )

    safe_book_name = slugify(args.title)

    if args.epub:
        build_epub(
            combined_md,
            metadata,
            args.output,
            f"{safe_book_name}.epub",
        )

    if args.pdf:
        build_pdf(
            combined_md,
            metadata,
            args.output,
            f"{safe_book_name}.pdf",
        )

    print("\nFinished.")
    print(f"Output directory: {args.output.resolve()}")


if __name__ == "__main__":
    main()

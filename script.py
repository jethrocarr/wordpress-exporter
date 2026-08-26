#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Jethro Carr

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

    pip install beautifulsoup4 requests html2text pillow

And install Pandoc separately:

    https://pandoc.org/installing.html

For PDF output, Pandoc also needs a PDF engine. For example,
install BasicTeX / MacTeX on macOS, TeX Live on Linux, etc.
"""

from __future__ import annotations

import argparse
import html
import io
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
from PIL import Image, ImageDraw, ImageFont, ImageOps


# ---------------------------------------------------------------------------
# WordPress XML namespaces
# ---------------------------------------------------------------------------

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "excerpt": "http://wordpress.org/export/1.2/excerpt/",
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

# XeLaTeX cannot include these formats directly. Pandoc sometimes attempts its
# own conversion, but that is backend/version dependent and can leave the
# original file in the generated TeX after a conversion failure.
LATEX_INCOMPATIBLE_IMAGE_EXTENSIONS = {".avif", ".webp"}


@dataclass
class Post:
    title: str
    slug: str
    date: datetime
    content_html: str
    categories: list[str]
    tags: list[str]
    link: str


@dataclass
class Attachment:
    post_id: str
    title: str
    url: str
    caption: str
    date: datetime | None


@dataclass
class BlogExport:
    title: str
    description: str
    link: str
    author: str
    channel_image: str | None
    attachments: list[Attachment]
    posts: list[Post]


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


def strip_wordpress_caption_shortcodes(content_html: str) -> str:
    # WordPress expands these when rendering a site, but a WXR export retains
    # the literal shortcode wrapper around otherwise valid image HTML.
    content_html = re.sub(
        r"\[caption\b[^\]]*\]",
        "",
        content_html,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"\[/caption\]",
        "",
        content_html,
        flags=re.IGNORECASE,
    )


def expand_wordpress_galleries(
    content_html: str,
    attachments_by_id: dict[str, Attachment],
) -> str:
    def replace_gallery(match: re.Match) -> str:
        attributes = match.group("attributes")
        ids_match = re.search(
            r"\bids\s*=\s*(?:\"([^\"]*)\"|“([^”]*)”|'([^']*)'|([^\s\]]+))",
            attributes,
            flags=re.IGNORECASE,
        )

        if ids_match is None:
            print(
                f"Warning: skipping gallery without attachment IDs: {match.group(0)}",
                file=sys.stderr,
            )
            return ""

        ids_value = next(
            (value for value in ids_match.groups() if value is not None),
            "",
        )
        parts = ['<div class="wordpress-gallery">']

        for attachment_id in ids_value.split(","):
            attachment_id = attachment_id.strip()

            if not attachment_id:
                continue

            attachment = attachments_by_id.get(attachment_id)

            if attachment is None:
                print(
                    f"Warning: gallery attachment {attachment_id} was not "
                    "found in the WXR export.",
                    file=sys.stderr,
                )
                continue

            caption = BeautifulSoup(
                attachment.caption,
                "html.parser",
            ).get_text(" ", strip=True)
            parts.append('<figure class="wordpress-gallery-item">')
            parts.append(
                f'<img src="{html.escape(attachment.url, quote=True)}" '
                f'alt="{html.escape(attachment.title, quote=True)}" />'
            )

            if caption:
                parts.append(
                    f"<figcaption>{html.escape(caption)}</figcaption>"
                )

            parts.append("</figure>")

        parts.append("</div>")
        return "\n".join(parts)

    return re.sub(
        r"\[gallery\b(?P<attributes>[^\]]*)\]",
        replace_gallery,
        content_html,
        flags=re.IGNORECASE,
    )


def parse_wordpress_export(xml_path: Path) -> BlogExport:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    channel = root.find("channel")

    if channel is None:
        raise RuntimeError("Could not find <channel> in WordPress export.")

    posts: list[Post] = []
    attachments: list[Attachment] = []

    for item in channel.findall("item"):
        post_type = item.findtext("wp:post_type", namespaces=NS)
        status = item.findtext("wp:status", namespaces=NS)

        if post_type == "attachment":
            attachment_url = item.findtext(
                "wp:attachment_url",
                namespaces=NS,
            )

            if attachment_url:
                attachment_date = None
                date_string = item.findtext(
                    "wp:post_date",
                    namespaces=NS,
                )

                if date_string and not date_string.startswith("0000-00-00"):
                    try:
                        attachment_date = datetime.strptime(
                            date_string,
                            "%Y-%m-%d %H:%M:%S",
                        )
                    except ValueError:
                        pass

                attachments.append(
                    Attachment(
                        post_id=(
                            item.findtext("wp:post_id", namespaces=NS) or ""
                        ).strip(),
                        title=(item.findtext("title") or "").strip(),
                        url=attachment_url.strip(),
                        caption=(
                            item.findtext("excerpt:encoded", namespaces=NS) or ""
                        ).strip(),
                        date=attachment_date,
                    )
                )

            continue

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

    author = ""
    author_element = channel.find("wp:author", NS)

    if author_element is not None:
        author = (
            author_element.findtext("wp:author_display_name", namespaces=NS)
            or ""
        ).strip()

    channel_image = channel.findtext("image/url")

    return BlogExport(
        title=(channel.findtext("title") or "WordPress Blog Archive").strip(),
        description=(channel.findtext("description") or "").strip(),
        link=(channel.findtext("link") or "").strip(),
        author=author,
        channel_image=channel_image.strip() if channel_image else None,
        attachments=attachments,
        posts=posts,
    )


class ImageDownloader:
    def __init__(self, image_dir: Path):
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 WordPressBookArchiver/1.0"
        )

        self.cache: dict[str, Path] = {}

    def make_latex_compatible(self, path: Path) -> Path | None:
        if path.suffix.lower() not in LATEX_INCOMPATIBLE_IMAGE_EXTENSIONS:
            return path

        converted_path = path.with_suffix(".png")

        if converted_path.exists():
            return converted_path

        try:
            with Image.open(path) as image:
                # Animated images cannot be represented in a PDF; use their
                # first frame. Preserve transparency where the source has it.
                image.seek(0)
                image = ImageOps.exif_transpose(image)
                has_transparency = (
                    image.mode in {"RGBA", "LA"}
                    or "transparency" in image.info
                )
                image = image.convert("RGBA" if has_transparency else "RGB")
                image.save(converted_path, format="PNG", optimize=True)
        except (OSError, ValueError) as exc:
            print(
                f"Warning: couldn't convert image {path} to PNG: {exc}",
                file=sys.stderr,
            )
            return None

        print(f"Converted for PDF: {path.name} -> {converted_path.name}")
        return converted_path

    def download(self, url: str) -> Path | None:
        if url in self.cache:
            return self.cache[url]

        stem = image_stem_from_url(url)

        for extension in sorted(IMAGE_EXTENSIONS):
            existing_path = self.image_dir / f"{stem}{extension}"

            if existing_path.exists():
                compatible_path = self.make_latex_compatible(existing_path)

                if compatible_path is None:
                    return None

                self.cache[url] = compatible_path
                print(f"Already downloaded: {url}")
                return compatible_path

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

        compatible_path = self.make_latex_compatible(path)

        if compatible_path is None:
            return None

        self.cache[url] = compatible_path

        print(f"Downloaded: {url}")

        return compatible_path


def clean_and_localise_html(
    content_html: str,
    downloader: ImageDownloader,
    markdown_dir: Path,
    post_url: str = "",
    attachments_by_id: dict[str, Attachment] | None = None,
) -> str:
    content_html = expand_wordpress_galleries(
        content_html,
        attachments_by_id or {},
    )
    content_html = strip_wordpress_caption_shortcodes(content_html)
    soup = BeautifulSoup(content_html, "html.parser")

    # Remove common unwanted elements.
    for element in soup.select(
        "script, style, iframe, form, "
        ".sharedaddy, .jp-relatedposts, "
        ".sd-sharing-enabled"
    ):
        element.decompose()

    # Blogger imports commonly represent an image caption as a two-row table.
    # html2text renders the table border as "---" directly below the image,
    # which Markdown readers can mistake for a Setext heading underline.
    for table in soup.select("table.tr-caption-container"):
        replacement = soup.new_tag("div")
        image = table.find("img")
        caption = table.select_one(".tr-caption")

        if image is not None:
            visual = image.parent if image.parent.name == "a" else image
            replacement.append(visual.extract())

        if caption is not None:
            caption_text = caption.get_text(" ", strip=True)

            if caption_text:
                caption_paragraph = soup.new_tag("p")
                caption_paragraph.string = caption_text
                replacement.append(caption_paragraph)

        table.replace_with(replacement)

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
        else:
            # Do not leave a rejected or unreachable remote image for Pandoc
            # to download itself. Its response may be HTML despite appearing
            # in an <img> element, which PDF engines cannot render.
            alt_text = img.get("alt")

            if alt_text:
                img.replace_with(alt_text)
            else:
                img.decompose()

            continue

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


def select_automatic_cover(blog: BlogExport) -> str | None:
    cover_pattern = re.compile(r"(?:cover|blog[ _-]?header|header)", re.I)
    candidates = [
        attachment
        for attachment in blog.attachments
        if cover_pattern.search(
            f"{attachment.title} {safe_filename_from_url(attachment.url)}"
        )
    ]

    if candidates:
        def candidate_rank(attachment: Attachment) -> tuple[int, datetime]:
            stem = Path(safe_filename_from_url(attachment.url)).stem
            normalized = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")

            if re.fullmatch(r"blog-?header\d*", normalized):
                relevance = 3
            elif "cover" in normalized:
                relevance = 2
            else:
                relevance = 1

            return relevance, attachment.date or datetime.min

        candidates.sort(
            key=candidate_rank,
            reverse=True,
        )
        return candidates[0].url

    return blog.channel_image


def load_cover_source(source: str) -> Image.Image:
    if source.startswith(("http://", "https://")):
        try:
            response = requests.get(source, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Couldn't download cover image {source}: {exc}"
            ) from exc

        content_type = response.headers.get("Content-Type", "")

        if image_extension(content_type) is None:
            raise RuntimeError(
                f"Cover URL did not return an image: {source} "
                f"(Content-Type: {content_type or 'missing'})"
            )

        image_data = io.BytesIO(response.content)
    else:
        path = Path(source).expanduser()

        if not path.is_file():
            raise RuntimeError(f"Cover image does not exist: {path}")

        image_data = path

    try:
        with Image.open(image_data) as image:
            return ImageOps.exif_transpose(image).convert("RGB")
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Couldn't read cover image {source}: {exc}") from exc


def cover_font(size: int, bold: bool = False):
    names = (
        ["DejaVuSerif-Bold.ttf", "DejaVuSans-Bold.ttf"]
        if bold
        else ["DejaVuSerif.ttf", "DejaVuSans.ttf"]
    )
    directories = [
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/System/Library/Fonts/Supplemental"),
        Path("/Library/Fonts"),
    ]

    for directory in directories:
        for name in names:
            path = directory / name

            if path.exists():
                return ImageFont.truetype(str(path), size=size)

    for name in names:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            pass

    return ImageFont.load_default()


def wrap_cover_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int,
) -> str:
    lines: list[str] = []

    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        line = ""

        for word in words:
            candidate = f"{line} {word}".strip()
            width = draw.textbbox((0, 0), candidate, font=font)[2]

            if line and width > max_width:
                lines.append(line)
                line = word
            else:
                line = candidate

        if line:
            lines.append(line)

    return "\n".join(lines)


def render_cover(
    size: tuple[int, int],
    source_image: Image.Image | None,
    title: str,
    subtitle: str | None,
    author: str | None,
    date_range: str,
) -> Image.Image:
    width, height = size
    margin = int(width * 0.09)
    canvas = Image.new("RGB", size, (24, 32, 45))
    draw = ImageDraw.Draw(canvas)
    title_font = cover_font(max(48, width // 14), bold=True)
    subtitle_font = cover_font(max(28, width // 32))
    detail_font = cover_font(max(26, width // 38))

    title_text = wrap_cover_text(
        draw,
        title,
        title_font,
        width - (margin * 2),
    )
    title_box = draw.multiline_textbbox(
        (0, 0),
        title_text,
        font=title_font,
        spacing=18,
    )
    title_height = title_box[3] - title_box[1]
    title_y = margin
    draw.multiline_text(
        (margin, title_y),
        title_text,
        font=title_font,
        fill=(248, 247, 242),
        spacing=18,
    )

    content_top = title_y + title_height + int(height * 0.055)
    footer_height = int(height * 0.24)

    if source_image is not None:
        image_height = max(1, height - content_top - footer_height)
        fitted = ImageOps.contain(
            source_image,
            (width, image_height),
            method=Image.Resampling.LANCZOS,
        )
        image_x = (width - fitted.width) // 2
        image_y = content_top + ((image_height - fitted.height) // 2)
        canvas.paste(fitted, (image_x, image_y))

    footer_y = height - footer_height + int(height * 0.045)

    if subtitle:
        subtitle_text = wrap_cover_text(
            draw,
            subtitle,
            subtitle_font,
            width - (margin * 2),
        )
        draw.multiline_text(
            (margin, footer_y),
            subtitle_text,
            font=subtitle_font,
            fill=(213, 220, 229),
            spacing=10,
        )

    detail = " · ".join(part for part in (author, date_range) if part)
    detail_box = draw.textbbox((0, 0), detail, font=detail_font)
    draw.text(
        (margin, height - margin - (detail_box[3] - detail_box[1])),
        detail,
        font=detail_font,
        fill=(166, 181, 198),
    )

    return canvas


def make_cover_assets(
    output_dir: Path,
    blog: BlogExport,
    explicit_source: str | None,
    title: str,
    subtitle: str | None,
    author: str | None,
) -> tuple[Path, Path, Path]:
    source = explicit_source or select_automatic_cover(blog)
    source_image = None

    if source:
        try:
            source_image = load_cover_source(source)
            source_kind = "specified" if explicit_source else "WXR"
            print(f"Using {source_kind} cover image: {source}")
        except RuntimeError as exc:
            if explicit_source:
                raise

            print(f"Warning: {exc}", file=sys.stderr)
            print("Falling back to a typography-only cover.")
    else:
        print("No cover image found; generating a typography-only cover.")

    date_range = f"Blog archive · {blog.posts[0].date.year}–{blog.posts[-1].date.year}"
    epub_cover = output_dir / "cover.jpg"
    pdf_cover = output_dir / "cover-pdf.jpg"
    thumbnail = output_dir / "cover-thumbnail.jpg"

    render_cover(
        (1600, 2560),
        source_image,
        title,
        subtitle,
        author,
        date_range,
    ).save(epub_cover, quality=92, optimize=True)
    render_cover(
        (1600, 2263),
        source_image,
        title,
        subtitle,
        author,
        date_range,
    ).save(pdf_cover, quality=92, optimize=True)

    with Image.open(epub_cover) as cover:
        cover.resize((400, 640), Image.Resampling.LANCZOS).save(
            thumbnail,
            quality=88,
            optimize=True,
        )

    return epub_cover, pdf_cover, thumbnail


def make_pdf_cover_header(output_dir: Path, pdf_cover: Path) -> Path:
    header_path = output_dir / "pdf-cover.tex"
    header_path.write_text(
        "\\usepackage{graphicx}\n"
        "\\renewcommand{\\maketitle}{%\n"
        "  \\begin{titlepage}%\n"
        "  \\newgeometry{margin=0pt}%\n"
        "  \\thispagestyle{empty}%\n"
        f"  \\noindent\\includegraphics[width=\\paperwidth,height=\\paperheight]"
        f"{{{pdf_cover.name}}}%\n"
        "  \\restoregeometry%\n"
        "  \\end{titlepage}%\n"
        "}\n",
        encoding="utf-8",
    )
    return header_path


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
    attachments: list[Attachment] | None = None,
) -> tuple[list[Path], Path]:
    posts_dir = output_dir / "posts"
    images_dir = output_dir / "images"

    posts_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    downloader = ImageDownloader(images_dir)
    attachments_by_id = {
        attachment.post_id: attachment
        for attachment in (attachments or [])
        if attachment.post_id
    }

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
                attachments_by_id,
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

            # Keep generated LaTeX explicit so Pandoc can safely disable
            # implicit raw TeX in article text (for example Windows paths).
            combined.write("```{=latex}\n\\newpage\n```\n\n")

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
    cover: Path,
    output_dir: Path,
    filename: str,
):
    require_pandoc()

    epub_path = output_dir / filename

    command = [
        "pandoc",
        metadata.name,
        combined_md.name,
        "--from=markdown-raw_tex",
        "--toc",
        "--toc-depth=2",
        "--resource-path=.",
        f"--epub-cover-image={cover.name}",
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
    cover_header: Path,
    output_dir: Path,
    filename: str,
):
    require_pandoc()

    pdf_path = output_dir / filename

    command = [
        "pandoc",
        metadata.name,
        combined_md.name,
        "--from=markdown-raw_tex",
        "--toc",
        "--toc-depth=2",
        "--resource-path=.",
        "--pdf-engine=xelatex",
        f"--include-in-header={cover_header.name}",
        "-V",
        "geometry:margin=25mm",
        "-V",
        "fontsize=11pt",
        "-V",
        "mainfont=DejaVu Serif",
        "-V",
        "sansfont=DejaVu Sans",
        "-V",
        "monofont=DejaVu Sans Mono",
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
        default=None,
        help="Book title (defaults to the WXR site title)",
    )

    parser.add_argument(
        "--subtitle",
        default=None,
        help="Subtitle (defaults to the WXR site description)",
    )

    parser.add_argument(
        "--author",
        default=None,
        help="Book author (defaults to the first WXR author)",
    )

    parser.add_argument(
        "--cover-image",
        default=None,
        metavar="PATH_OR_URL",
        help=(
            "Cover source image. If omitted, use a header/cover attachment, "
            "then the WXR channel icon, then typography only"
        ),
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

    blog = parse_wordpress_export(args.xml)
    posts = blog.posts

    print(f"Found {len(posts)} published posts.")

    if not posts:
        raise RuntimeError(
            "No published WordPress posts were found."
        )

    title = args.title or blog.title
    subtitle = args.subtitle if args.subtitle is not None else blog.description
    author = args.author if args.author is not None else blog.author

    print(f"Book title: {title}")

    epub_cover, pdf_cover, thumbnail = make_cover_assets(
        args.output,
        blog,
        args.cover_image,
        title,
        subtitle,
        author,
    )
    print(f"Created cover: {epub_cover}")
    print(f"Created thumbnail: {thumbnail}")

    _, combined_md = write_posts(
        posts,
        args.output,
        blog.attachments,
    )

    metadata = make_metadata(
        args.output,
        title,
        author,
        subtitle,
    )

    safe_book_name = slugify(title)

    if args.epub:
        build_epub(
            combined_md,
            metadata,
            epub_cover,
            args.output,
            f"{safe_book_name}.epub",
        )

    if args.pdf:
        cover_header = make_pdf_cover_header(args.output, pdf_cover)
        build_pdf(
            combined_md,
            metadata,
            cover_header,
            args.output,
            f"{safe_book_name}.pdf",
        )

    print("\nFinished.")
    print(f"Output directory: {args.output.resolve()}")


if __name__ == "__main__":
    main()

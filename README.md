# WordPress to EPUB/PDF Book Converter

# AI Warning

This is a largely throwaway piece of work written largely with Codex. But it solved
my particular problem, so maybe it will solve yours too.

# About

Turn a standard WordPress WXR export into a portable blog archive. The
converter creates:

- an EPUB and/or PDF book
- one Markdown file per published post
- a combined Markdown archive
- locally downloaded copies of post images
- generated cover artwork and book metadata

The recommended way to run the converter is with Docker, which includes
Python, Pandoc, XeLaTeX, and all required dependencies.

## Quick start

### 1. Export your WordPress content

In the WordPress administration panel, go to **Tools → Export**, select
**All content**, and download the XML file into this project directory.

### 2. Build the Docker image

```sh
docker build -t wexport:latest .
```

### 3. Start the container

```sh
docker run --rm -it -v "$PWD:/data" wexport:latest bash
```

The volume mount maps the current directory to `/data` in the container, so
the generated books and downloaded images remain available on your computer.

### 4. Convert the export

Run the following command inside the container:

```sh
wordpress-export MyBlog.WordPress.xml \
  --output my-blog-book \
  --epub \
  --pdf
```

Replace `MyBlog.WordPress.xml` with the name of your downloaded export. If you
omit both `--epub` and `--pdf`, the converter creates an EPUB by default.

## Customise the book

The title, subtitle, and author are read from the WordPress export by default.
You can override them on the command line:

```sh
wordpress-export MyBlog.WordPress.xml \
  --title "My Blog Archive" \
  --subtitle "Posts from 2007–2025" \
  --author "Example Author" \
  --output my-blog-book \
  --epub \
  --pdf
```

### Command options

| Option | Description |
| --- | --- |
| `XML_FILE` | WordPress WXR/XML export to convert (required) |
| `--output DIRECTORY` | Output directory; defaults to `wordpress-book` |
| `--title TITLE` | Book title; defaults to the WordPress site title |
| `--subtitle SUBTITLE` | Book subtitle; defaults to the site description |
| `--author AUTHOR` | Author name; defaults to the first author in the export |
| `--cover-image PATH_OR_URL` | Local image or HTTP(S) URL to use for the cover |
| `--epub` | Generate an EPUB |
| `--pdf` | Generate a PDF |

You can also display the built-in command help:

```sh
wordpress-export --help
```

## Cover artwork

To provide your own cover artwork, use a local file or an HTTP(S) URL:

```sh
wordpress-export MyBlog.WordPress.xml \
  --cover-image ./my-cover-photo.jpg \
  --output my-blog-book \
  --epub \
  --pdf
```

When no cover is supplied, the converter looks for the newest attachment with
`cover`, `header`, or `blogheader` in its name. It then falls back to the
WordPress channel icon and, finally, a typography-only design.

The selected artwork is fitted into a portrait cover containing the book title,
subtitle, author, and archive date range. Three versions are generated:

- `cover.jpg` — EPUB cover and library artwork
- `cover-pdf.jpg` — cover sized for the PDF title page
- `cover-thumbnail.jpg` — standalone 400 × 640 thumbnail

## Output files

A typical output directory looks like this:

```text
my-blog-book/
├── posts/                  # Individual Markdown posts
├── images/                 # Downloaded post images
├── blog.md                 # Combined Markdown archive
├── metadata.yaml           # Pandoc book metadata
├── cover.jpg               # EPUB cover
├── cover-pdf.jpg           # PDF cover
├── cover-thumbnail.jpg     # Library thumbnail
├── my-blog-archive.epub    # Generated when --epub is used
└── my-blog-archive.pdf     # Generated when --pdf is used
```

Output book filenames are derived from the book title.

## Notes

- Only published WordPress posts are included.
- Posts are ordered chronologically and grouped by year in the combined book.
- Images referenced by posts are downloaded into the output directory.
- Original post URLs, categories, and tags are retained when available.

## License

This project is licensed under the [MIT License](LICENSE).
